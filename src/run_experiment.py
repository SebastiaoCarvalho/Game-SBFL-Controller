#!/usr/bin/env python
"""Controller entrypoint: run the pipeline over selected dataset rows.

    python run_experiment.py                 # all rows
    python run_experiment.py --row 4         # only dataset row 4
    python run_experiment.py --rows 4-9      # rows 4..9 inclusive
    python run_experiment.py --game "Super Tux"   # only that game's rows
    python run_experiment.py --dataset other.csv
    python run_experiment.py --row 4 --manual-auto   # replay cached manual answers
    python run_experiment.py --row 20 --refinement-mode symbolic --threshold-strategy weakest
    python run_experiment.py --row 20 --refinement-mode data-direct   # corroborate-only
    python run_experiment.py --row 4 --no-instrument  # build the source as committed
    python run_experiment.py --row 4 --clean     # drop row 4's cached build + logs

Backs up gamerv's properties.json for the duration and restores it at the end,
writes results to results/results_<timestamp>.csv, and prints a summary table.
"""

import argparse
import csv
import shutil
import sys
from datetime import datetime

import buildcache
import config
import gamerv
import logcache
from dataset import read_cases
from pipeline import PHASES, run_case
from game_repo import DirtyTreeError


def _select(cases, row, rows, game):
    if game is not None:
        key = game.strip().casefold()
        cases = [c for c in cases if c.game.strip().casefold() == key]
    if row is not None:
        return [c for c in cases if c.row == row]
    if rows is not None:
        lo, hi = rows
        return [c for c in cases if lo <= c.row <= hi]
    return cases


def _parse_rows(spec: str) -> tuple[int, int]:
    lo, _, hi = spec.partition("-")
    if not hi:
        return int(lo), int(lo)
    return int(lo), int(hi)


def _dir_size(path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _confirm(n: int) -> bool:
    """Gate an unscoped ``--clean``, which is the one that can cost real time.

    A scoped clean is cheap to undo — one branch rebuilds and re-records. Wiping
    the whole dataset's log cache is not: re-recording every row means replaying
    every tape, and the manual validation passes that go with them. So the bare
    form asks, and refuses outright when there is nobody to ask (a script, a
    pipe) unless it passed --yes.
    """
    if not sys.stdin.isatty():
        print(f"Refusing to clean all {n} row(s) non-interactively. Scope it with "
              f"--row/--rows/--game, or pass --yes to mean it.", file=sys.stderr)
        return False
    reply = input(f"Remove the cached build AND logs for all {n} row(s)? [y/N] ")
    return reply.strip().lower() in ("y", "yes")


def _clean(cases) -> int:
    """Delete the selected rows' cached build artifacts and logs, then report.

    Both caches are keyed by game+branch, not by row, so rows sharing a branch
    (the same mutant tested against several properties) name the same two slots —
    deduplicated here so each is reported once.

    Only the two *caches* are touched. Deliberately left alone: ``hints/``, which
    holds operator answers that cost a human to recreate; ``logs_archive/``, which
    is the only copy of past runs' traces rather than a cache; and ``results/``.
    Everything removed here is something the next run regenerates on its own.
    """
    seen, removed, freed = set(), 0, 0
    for case in cases:
        try:
            game = config.get_game(case.game)
        except config.UnknownGameError as exc:
            print(f"  row {case.row}: {exc}")
            continue
        if (game.name, case.branch) in seen:
            continue
        seen.add((game.name, case.branch))
        print(f"  {game.name} / {case.branch}")
        for label, path in (("build", buildcache.slot(game, case.branch)),
                            ("logs ", logcache.slot(game, case.branch))):
            if not path.exists():
                print(f"      {label}  — nothing cached")
                continue
            size = _dir_size(path)
            shutil.rmtree(path, ignore_errors=True)
            if path.exists():
                print(f"      {label}  FAILED to remove {path}")
                continue
            removed += 1
            freed += size
            print(f"      {label}  removed {path}  ({size / 1e6:.1f} MB)")
    print(f"\nChecked {len(seen)} game/branch pair(s): {removed} cache dir(s) "
          f"removed, {freed / 1e6:.1f} MB freed.")
    return removed


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Multi-game / GameRV experiment controller")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--row", type=int, help="run a single dataset row (1-based)")
    g.add_argument("--rows", type=_parse_rows, metavar="A-B",
                   help="run an inclusive row range, e.g. 4-9")
    ap.add_argument("--game", default=None,
                    help="only run rows whose Game Name matches (case-insensitive)")
    ap.add_argument("--dataset", default=None, help="override dataset.csv path")
    ap.add_argument("--manual-auto", action="store_true",
                    help="reuse stored hint answers for the manual image/audio "
                         "predicates; prompt only for ones not yet recorded")
    ap.add_argument("--refinement-mode", choices=config.REFINEMENT_MODES,
                    default=config.REFINEMENT_MODE,
                    help="how step 8 decides phi: 'symbolic' forces phi -> R "
                         "containment against the mined R, 'data-direct' evaluates "
                         "phi on the pooled good/violation states (independent of "
                         "R), 'auto' is data-direct with a symbolic fallback for "
                         f"non-raw phi. Default: {config.REFINEMENT_MODE}.")
    ap.add_argument("--threshold-strategy", choices=config.THRESHOLD_STRATEGIES,
                    default=config.THRESHOLD_STRATEGY,
                    help="how R's numeric split-points are placed: 'weakest' = just "
                         "past the greatest/lowest violation value, 'gap-mean' = "
                         "midpoint of the violation->good gap, 'midpoints' = sampled "
                         "midpoints (search picks). Affects the symbolic path only. "
                         f"Default: {config.THRESHOLD_STRATEGY}.")
    ap.add_argument("--clean", action="store_true",
                    help="delete the selected rows' build-cache and log-cache "
                         "slots, then exit without running anything. Forces the "
                         "next run of those rows to rebuild and re-record. Leaves "
                         "hints/, logs_archive/ and results/ alone. Combine with "
                         "--row/--rows/--game to scope it; unscoped it covers "
                         "every row in the dataset and asks first.")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation an unscoped --clean asks for")
    ap.add_argument("--no-instrument", dest="instrument", action="store_false",
                    default=config.INSTRUMENT,
                    help="skip step 2a: build each branch's source as committed "
                         "instead of running gamerv's source instrumenter over it "
                         "first. Fault localization then sees only the variables "
                         "the game already reports (no function-locals). Games "
                         "without an instrument_src are unaffected either way.")
    args = ap.parse_args(argv)

    cases = _select(read_cases(args.dataset), args.row, args.rows, args.game)
    if not cases:
        print("No matching dataset rows.")
        return 1
    print(f"Selected {len(cases)} case(s): rows {[c.row for c in cases]}")

    if args.clean:
        # Before the properties backup and the results file: this runs nothing,
        # so it should leave no trace of its own beyond the deletions.
        scoped = any(v is not None for v in (args.row, args.rows, args.game))
        if not scoped and not args.yes and not _confirm(len(cases)):
            print("Nothing removed.")
            return 1
        print(f"\nCleaning cached builds and logs for {len(cases)} row(s):")
        _clean(cases)
        return 0

    print(f"Refinement decision: mode={args.refinement_mode}  "
          f"threshold strategy={args.threshold_strategy}")
    print(f"Source instrumentation (step 2a): "
          f"{'on' if args.instrument else 'off'}")

    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    gamerv.backup_properties()

    results = []
    try:
        for case in cases:
            print("\n" + "=" * 70)
            print(f"ROW {case.row}: {case.branch}")
            print("=" * 70)
            try:
                res = run_case(case, manual_auto=args.manual_auto,
                               refinement_mode=args.refinement_mode,
                               threshold_strategy=args.threshold_strategy,
                               instrument_source=args.instrument)
            except DirtyTreeError as exc:
                print(f"\nABORTING: {exc}", file=sys.stderr)
                return 2
            results.append(res)
            phases = "  ".join(f"{p}={v}s" for p, v in res.phase_cells().items())
            print(f"\n--> ROW {case.row} [{res.status}] "
                  f"({res.elapsed_s:.1f}s total | {phases}) {res.notes}")
    finally:
        gamerv.restore_properties()

    out = _write_results(results)
    _summary(results, out)
    # Exit non-zero if any row errored, so CI/scripts can notice.
    return 0 if all(r.status != "ERROR" for r in results) else 1


def _write_results(results) -> "object":
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.RESULTS_DIR / f"results_{stamp}.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["game", "row_branch", "specification", "phi", "formula",
                    "status", "R", "notes", "elapsed_s"]
                   + [f"{p}_s" for p in PHASES])
        for r in results:
            cells = r.phase_cells()
            w.writerow([r.game, r.branch, r.specification, r.phi, r.formula,
                        r.status, r.r_text or "", r.notes, f"{r.elapsed_s:.1f}"]
                       + [cells[p] for p in PHASES])
    return out


def _summary(results, out) -> None:
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    width = max((len(r.branch) for r in results), default=6)
    phw = 9  # per-phase column width
    header = (f"  {'branch':<{width}}  {'status':<12} {'total':>8}  "
              + "".join(f"{p:>{phw}}" for p in PHASES))
    print(header)
    for r in results:
        cells = r.phase_cells()
        phase_cols = "".join(f"{cells[p]:>{phw}}" for p in PHASES)
        print(f"  {r.branch:<{width}}  {r.status:<12} {r.elapsed_s:>7.1f}s "
              f"{phase_cols}  {r.notes}")
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("\n  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    total = sum(r.elapsed_s for r in results)
    phase_totals = {p: sum(r.timings.get(p, 0.0) for r in results) for p in PHASES}
    print(f"  total time: {total:.1f}s ({total / 60:.1f} min)")
    print("  by phase:   "
          + "  ".join(f"{p}={phase_totals[p]:.1f}s" for p in PHASES))
    print(f"\n  results written to {out}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
