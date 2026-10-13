#!/usr/bin/env python
"""Controller entrypoint: run the pipeline over selected dataset rows.

    python run_experiment.py                 # all rows
    python run_experiment.py --row 4         # only dataset row 4
    python run_experiment.py --rows 4-9      # rows 4..9 inclusive
    python run_experiment.py --game "Super Tux"   # only that game's rows
    python run_experiment.py --dataset other.csv
    python run_experiment.py --row 4 --manual-auto   # replay cached manual answers
    python run_experiment.py --rows 1-60 --skip-manual  # unattended: skip rows needing a human
    python run_experiment.py --candidate-sort type   # reorder each score tier: flags before thresholds
    python run_experiment.py --candidate-sort all    # mine once, rank under every sort (one folder)
    python run_experiment.py --row 4 --no-instrument  # build the source as committed
    python run_experiment.py --row 4 --clean     # drop row 4's cached build + logs

Backs up gamerv's properties.json for the duration and restores it at the end,
writes one results folder per run (results/run_<timestamp>/, see _write_results)
and prints an aggregate summary — the per-case verdicts, notes and timings go to
that folder's report.txt ("BY CASE"), not to stdout.
"""

import argparse
import csv
import json
import statistics
import shutil
import sys
from datetime import datetime
from pathlib import Path

import buildcache
import config
import gamerv
import logcache
import metrics
import plots
import report
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


def _parse_sorts(spec: str) -> list[str]:
    """``--candidate-sort``: one sort, a comma-separated list of them, or ``all``.

    More than one ranks every row under each of them in **one pass**: the first sort
    is the row's own (its verdict, notes and ``gt_rank``), and step 8b re-reads the
    candidates it mined in each further sort's order (``pipeline._rerank``). The sort
    only permutes one score tier, so the candidate set is identical under all of them
    and re-mining per sort — as three separate batches would — buys nothing but three
    times the wall clock. The order is the one given (``all`` means every sort in
    config order, ``none`` first) and duplicates are dropped.
    """
    names = list(dict.fromkeys(s.strip() for s in spec.split(",") if s.strip()))
    if names == ["all"]:
        return list(config.CANDIDATE_SORTS)
    bad = [n for n in names if n not in config.CANDIDATE_SORTS]
    if bad or not names:
        raise argparse.ArgumentTypeError(
            f"invalid candidate sort {', '.join(bad) if bad else spec!r}: choose "
            f"from {', '.join(config.CANDIDATE_SORTS)}, a comma-separated list of "
            f"them, or 'all'")
    return names


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
    ap.add_argument("--dataset", default=None,
                    help=f"override the dataset path (default: {config.DATASET})")
    ap.add_argument("--manual-auto", action="store_true",
                    help="reuse stored hint answers for the manual image/audio "
                         "predicates; prompt only for ones not yet recorded")
    ap.add_argument("--skip-manual", action="store_true",
                    help="never prompt for a manual image/audio predicate value: "
                         "a row the stored hints do not fully cover is reported "
                         "NEEDS_MANUAL and the batch moves on, so a long run can "
                         "be left unattended (overnight) and finished by re-running "
                         "just those rows interactively. Implies --manual-auto.")
    ap.add_argument("--candidate-sort", type=_parse_sorts,
                    default=[config.CANDIDATE_SORT],
                    metavar="{" + ",".join((*config.CANDIDATE_SORTS, "all")) + "}",
                    help="how the mined candidate bug conditions are ordered "
                         "*within* one score tier (they all implicate the fault "
                         "equally strongly, so the order decides the ground truth's "
                         "rank): 'none' = "
                         "the miner's order, 'type' = boolean flags, then parity, "
                         "then categorical equalities, then numeric thresholds, "
                         "'popularity' = most-used in the game's source first "
                         "(needs step 7a's per-variable counts). Never changes "
                         "which candidates are mined. Pass several "
                         "comma-separated (or 'all') to rank each row under every "
                         "one of them in a single pass: mined once under the first, "
                         "re-ranked under the rest (step 8b), written to one "
                         "results/run_<timestamp>_sorts/ folder with a "
                         "gt_rank_<sort> column each and the ranks_by_sort.png "
                         f"figure. Default: {config.CANDIDATE_SORT}.")
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

    # Skipping is only sensible on top of the hint cache: without the replay every
    # row with a manual predicate would be skipped, hints or no hints.
    if args.skip_manual:
        args.manual_auto = True

    dataset_path = Path(args.dataset) if args.dataset else config.DATASET
    if not dataset_path.is_file():
        print(f"No dataset at {dataset_path}: set FL_DATASET or FL_DATA_DIR "
              f"(see data/README.md), or pass --dataset.", file=sys.stderr)
        return 1
    print(f"Dataset: {dataset_path}")

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

    sorts = args.candidate_sort
    multi = len(sorts) > 1
    print(f"Bug-condition ranking (Ochiai): candidate sort={sorts[0]}"
          + (f" (also ranked under: {', '.join(sorts[1:])})" if multi else ""))
    print(f"Source instrumentation (step 2a): "
          f"{'on' if args.instrument else 'off'}")
    if args.skip_manual:
        print("Manual predicates: never prompting (--skip-manual) — rows the "
              "hints do not cover are reported NEEDS_MANUAL")
    if multi:
        print(f"Candidate sorts: each row is mined once under {sorts[0]} and "
              f"re-ranked under {', '.join(sorts[1:])} (step 8b, no re-mining) — "
              f"one results folder.")

    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    gamerv.backup_properties()
    try:
        try:
            results = _run_pass(cases, args, sorts[0], rank_sorts=tuple(sorts))
        except DirtyTreeError as exc:
            # Experiment-wide precondition, not a per-row failure: every later row
            # would abort on the same dirty tree.
            print(f"\nABORTING: {exc}", file=sys.stderr)
            return 2
        out = _write_results(results, _settings(args, sorts),
                             suffix="_bug" + ("_sorts" if multi else ""))
        _summary(results, out)
    finally:
        gamerv.restore_properties()
    # Exit non-zero if any row errored, so CI/scripts can notice.
    return 1 if any(r.status == "ERROR" for r in results) else 0


def _run_pass(cases, args, candidate_sort: str,
              rank_sorts: tuple[str, ...] = ()) -> list:
    """Run every selected case once, under one candidate sort (ranked under
    ``rank_sorts`` too, see :func:`_parse_sorts`). Returns the results.

    One call is one results folder. Every per-row failure
    is captured by :func:`pipeline.run_case` as an ERROR result so the batch keeps
    going; ``DirtyTreeError`` is the one exception and propagates, because no later
    row (or pass) can run against a dirty game tree either.
    """
    results = []
    for case in cases:
        print("\n" + "=" * 70)
        print(f"ROW {case.row}: {case.branch}")
        print("=" * 70)
        res = run_case(case, manual_auto=args.manual_auto,
                       instrument_source=args.instrument,
                       skip_manual=args.skip_manual,
                       candidate_sort=candidate_sort,
                       rank_sorts=rank_sorts)
        results.append(res)
        phases = "  ".join(f"{p}={v}s" for p, v in res.phase_cells().items())
        print(f"\n--> ROW {case.row} [{res.status}] "
              f"({res.elapsed_s:.1f}s total | {phases}) {res.notes}")
    return results


def _write_csv(path, header, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _settings(args, sorts: list[str]) -> dict:
    """The knobs this run ran under, for ``settings.json``.

    Only the ones that change what the numbers *mean*: two runs over the same rows
    with different knobs produce two folders that are otherwise indistinguishable.
    ``candidate_sort`` is the rows' own sort (their verdicts and ``gt_rank``);
    ``rank_sorts`` every sort a rank was recorded under, that one first.
    """
    return {
        # Constant, but recorded so a results folder states what it measured.
        "objective": "bug-condition",
        "candidate_sort": sorts[0],
        "rank_sorts": list(sorts),
        "instrument_source": bool(args.instrument),
        "game": args.game,
        "rows": ([args.row] if args.row else list(args.rows) if args.rows else None),
        "dataset": str(args.dataset or config.DATASET),
        "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _write_results(results, settings: dict | None = None,
                   suffix: str = "") -> "object":
    """Write this run's CSVs into their own results/run_<timestamp>/ folder.

    ``suffix`` is appended to the folder name: every run ends in ``_bug``, and a
    run ranked under several candidate sorts in ``_bug_sorts``.

    One folder per run, so a run can grow extra artefacts (more views of the
    same rows, plots) without cluttering results/:

      full.csv     every column the pipeline produced (the dataset row number,
                   phi, formula, notes, mining metrics, per-phase timings) — the
                   archival record.
      summary.csv  the five columns a results table needs: game, branch,
                   property, ground truth, and the bug condition found (the
                   top-ranked candidate the verdict is about). ``result`` falls
                   back to the status word (SKIPPED/ERROR/VARIABLE_NOT_FOUND/...)
                   for rows that produced no candidate, so no cell is silently
                   blank.
      settings.json the knobs this batch ran under (candidate sort(s),
                   instrumentation, row/game selection). Two runs over the same
                   rows under different knobs are otherwise indistinguishable;
                   report.txt and the plots read it back.
      mining.csv   step 8's search metrics per row: how many top-ranked candidate
                   bug conditions were mined, and the ground truth's rank among
                   them (blank when it was not established at all — see
                   metrics.py for the match tiers), plus whether only the *union*
                   of several candidates covered it, the variables in the
                   states the miner searched (logged / varying), and how many
                   good and bad (violation) states it learned from.
      report.txt   the aggregate view: rows per classification, and the same
                   counts cut by game, by bug category and by parent category,
                   with the mining rates per cut — then the same analysis again
                   per game (each game's classifications, category tables and
                   mining numbers over its own rows) — then the per-case
                   division: one line per dataset row with its verdict, mining
                   numbers, note and phase timings (report.py). The stdout
                   summary prints the aggregate tables only, so this file is the
                   place the per-game and per-case detail is read.
      success_by_fault_type.png / success_by_parent.png / success_by_game.png
                   the classifications per bug category, per parent category
                   and per game, as stacked bars (plots.py, from full.csv).
      time.png     the method's time per case (validate, localize, and the two
                   together), a box per game.
      space.png    the variables in the mined states, logged vs. varying.
      time_vs_space.png  localize seconds against the varying-variable count.
      candidates.png  a boxplot of that candidate count, one box per game, drawn
                   from mining.csv (plots.py). Skipped, with a printed note, if
                   no row in the run mined.
      candidates_vs_rank.png  the same candidate counts beside the ground truth's
                   rank, over the rows where it was found — a big candidate set
                   read together with how deep in it the answer sat.
      ranks_by_sort.png  instead of the above when the run ranked under several
                   candidate sorts: the candidate count beside the rank under
                   each sort, over the same rows.
      states_vs_rank.png  the ground truth's rank per bin of labelled states
                   (good + bad), with Spearman's rho: does a deep rank go with
                   few examples?
      states_vs_rank_by_game.png  the same, one panel per game (after the
                   pooled one), each with its own rho.

    Every artefact after the CSVs is derived *from* them, so each can be
    regenerated for a past run (``python src/report.py <run_dir>``,
    ``python src/plots.py <run_dir>``).
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.RESULTS_DIR / f"run_{stamp}{suffix}"
    out.mkdir(parents=True, exist_ok=True)

    _write_csv(
        out / "full.csv",
        ["row", "game", "row_branch", "fault_type", "fault_class",
         "specification", "phi", "formula", "status", "B", "notes",
         *metrics.COLUMNS,
         "elapsed_s"] + [f"{p}_s" for p in PHASES],
        [[r.row or "", r.game, r.branch, r.fault_type, r.fault_class,
          r.specification, r.phi,
          r.formula, r.status, r.r_text or "", r.notes] + r.mining.cells()
         + [f"{r.elapsed_s:.1f}"] + [r.phase_cells()[p] for p in PHASES]
         for r in results],
    )
    _write_csv(
        out / "summary.csv",
        ["game", "github_branch", "property", "ground_truth", "result"],
        [[r.game, r.branch, r.specification, r.phi, r.r_text or r.status]
         for r in results],
    )
    _write_csv(
        out / "mining.csv",
        ["game", "github_branch", "fault_type", "fault_class", "ground_truth",
         "status", *metrics.COLUMNS],
        [[r.game, r.branch, r.fault_type, r.fault_class, r.phi, r.status]
         + r.mining.cells() for r in results],
    )
    if settings is not None:
        # Written before the report/plots so both can read it back and caption
        # themselves with the knobs the numbers came from.
        (out / "settings.json").write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    report.write(out)
    plots.render(out)
    return out


def _summary(results, out) -> None:
    """Print the run's aggregate results — deliberately no per-case line.

    Every per-case number (its verdict, note, mining metrics and phase timings) is
    written to the run's ``report.txt`` ("BY CASE") and ``full.csv``, so stdout
    stays at the level a batch can actually be read at: the classification counts,
    the mining rates, the per-game / per-category divisions, and where the rest was
    written. The row-by-row progress printed *during* the run is untouched — this is
    only the closing summary.
    """
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    games = sorted({r.game for r in results if r.game})
    print(f"  {len(results)} case(s)"
          + (f" over {len(games)} game(s) ({', '.join(games)})" if games else ""))
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    # Rows that actually mined *and* carry a ground truth to look for — a row
    # with an empty Target Phi would otherwise pad the denominator.
    mined = [r for r in results
             if r.mining.candidates is not None and r.phi.strip()]
    # Rows with no Target Phi: mined, but never compared (see pipeline step 8).
    # Counted only — which rows they are is the per-case table's job (they carry
    # the SKIPPED status there), and naming them here is a per-row print again.
    skipped = [r for r in results if not r.phi.strip()]
    if skipped:
        print(f"  skipped:    {len(skipped)} row(s) carry no ground-truth phi "
              f"(mined only, no verdict)")
    # Rows --skip-manual left for a human. Named individually — unlike every other
    # status — because an unattended run is only *finished* once they have been
    # re-run interactively, so this line is the morning's to-do list.
    pending = [r for r in results if r.status == "NEEDS_MANUAL"]
    if pending:
        rows = ", ".join(str(r.row) for r in pending if r.row)
        print(f"  needs manual: {len(pending)} row(s) skipped at step 6 — a manual "
              f"predicate no hint covers (nothing was decided for them)")
        print(f"                row(s) {rows or '?'} — re-run each interactively "
              f"(--manual-auto, no --skip-manual) to classify and cache the answers")
    if mined:
        # "Found" is decided by the match tier, not by the rank (see metrics.py).
        found = [r for r in mined if r.mining.found]
        ranks = [r.mining.gt_rank for r in mined if r.mining.gt_rank]
        tiers: dict[str, int] = {}
        for r in found:
            tiers[r.mining.gt_match] = tiers.get(r.mining.gt_match, 0) + 1
        union_only = sum(1 for r in mined if r.mining.union_only)
        median = f", median rank {statistics.median(ranks):.0f}" if ranks else ""
        detail = " ".join(f"{k}={v}" for k, v in sorted(tiers.items())) or "none"
        # The tier breakdown is part of the line: "established" is a single
        # top-ranked candidate spelled as phi (exact) or implied by it (implication).
        print(f"  mining:     ground truth established in {len(found)}/{len(mined)} "
              f"row(s) ({detail}{median})")
        if union_only:
            print(f"              {union_only} further row(s) are covered only by the "
                  f"union of several candidates — not counted as found")
    # The same aggregation report.txt is written from, so the two cannot disagree.
    rows = report.rows_from_results(results)
    by_sort = report.sort_rank_lines(rows)
    if by_sort:
        print("\n  rank by candidate sort (same candidates, each sort's order):")
        for line in by_sort:
            print(line)
    print()
    for line in report.terminal_lines(rows):
        print(line)
    total = sum(r.elapsed_s for r in results)
    phase_totals = {p: sum(r.timings.get(p, 0.0) for r in results) for p in PHASES}
    print(f"  total time: {total:.1f}s ({total / 60:.1f} min)")
    print("  by phase:   "
          + "  ".join(f"{p}={phase_totals[p]:.1f}s" for p in PHASES))
    print(f"\n  results written to {out}/ "
          f"({', '.join(sorted(p.name for p in out.iterdir()))})")
    print(f"  per-case verdicts, notes and timings: "
          f"{out / report.REPORT_TXT} (BY CASE)")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
