#!/usr/bin/env python3
"""Fill the phi-verdict columns of a run written before them.

    python scripts/backfill_verdict_columns.py results/run_20261013_173748_bug_sorts [--dry-run]

Two column groups, both read off gamerv's phi verdict, which an older run's archive
holds in an older shape:

* metrics.TIE_COLUMNS — ``pool`` (every candidate the miner ranked), ``gt_contained``
  (k, how many top-tier candidates establish phi) and the two ranks derived from them,
  ``gt_rank_expected`` and ``gt_rank_worst``.
* metrics.STRENGTH_COLUMNS — how closely the candidate at ``gt_rank`` matches phi
  (``gt_strength``, ``gt_strength_reason``; metrics.match_strength).

**The run's own candidates, never re-mined.** Both come from gamerv's ``recheck_phi``
worker op, which reads the mining cache the run left in its archived run dir and
decides phi against that tier without mining or writing anything
(``fault_localization.recheck_phi_contained``). Re-running ``compare_bug_condition``
instead would re-mine whenever the mining signature moved — and it moves with the
term-rules file, which step 7a regenerates per branch — so it would rank another
branch's candidates under this run's name. phi's core is taken from the archived
verdict (``phi_used``) rather than re-derived. The match strength evaluates phi and B
on the pooled states, which reads the row's key table: ``FL_KEY_TABLE`` is set to the
branch's slot in the build cache, as the pipeline sets it, and gamerv's self-checks
(B reproduces the miner's counts; no state satisfies phi but not B) turn any
evaluation the table does not support into ``undecided`` rather than a class.

**Every row is checked.** The archive is mapped to the row the way
``backfill_states.py`` maps it (formula + the row's cells reproduced). A SUCCESS row is
then filled only when the re-check reproduces the run's own verdict: contained, the
same first rank (``gt_rank``), the same candidate, the same tier size, and a scan that
was not cut short. A row that fails any of them is left blank and named, never filled
from a guess. ``pool`` needs only the mapping, so every mapped row gets it; the rest
is SUCCESS-only, like ``gt_rank``.

The run's ``settings.json`` gains a ``backfills`` entry naming the columns, the
commits that computed them and the rows left blank. Re-running is safe: the columns
are recomputed and the entry replaced.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import backfill_states  # noqa: E402  (this directory is on sys.path as the script's own)
import config           # noqa: E402
import gamerv           # noqa: E402
import metrics          # noqa: E402
from run_experiment import _commit  # noqa: E402

SCRIPT = "scripts/backfill_verdict_columns.py"
#: This script's earlier name, whose settings entry a re-run replaces.
_FORMER = ("scripts/backfill_tie_ranks.py",)
COLUMNS = (*metrics.TIE_COLUMNS, *metrics.STRENGTH_COLUMNS)
#: The column the new ones are written after, as metrics.COLUMNS orders them.
AFTER = metrics.RANK_COLUMNS[-1]
#: The env the per-row variables are set in, restored afterwards.
_ENV = (config.GAMERV_GAME_ENV, config.GAMERV_KEY_TABLE_ENV)


def _with_columns(fields: list[str]) -> list[str]:
    fields = [f for f in fields if f not in COLUMNS]
    at = fields.index(AFTER) + 1 if AFTER in fields else len(fields)
    return fields[:at] + list(COLUMNS) + fields[at:]


def _key_table(row: dict) -> Path | None:
    """The branch's key table as the build cache keeps it (the slot step 8 read)."""
    path = (config.CONTROLLER_DIR / "build_cache" / row["game"] / row["row_branch"]
            / f"{row['game']}.keys.json")
    return path if path.is_file() else None


def _recheck(run_dir: Path, row: dict, sort: str) -> tuple[dict | None, str]:
    """``(verdict, "")`` for a SUCCESS row whose re-check reproduces the run's
    verdict, else ``(None, why)``."""
    formula, phi = row["formula"], row["phi"]
    archived = metrics._phi_verdict(run_dir, formula, phi)
    if archived is None or not archived.get("contained"):
        return None, "no archived verdict that contains phi"
    if archived.get("sort", "none") != sort:
        return None, f"archived verdict is under sort {archived.get('sort')!r}"
    # The game picks the profile gamerv parses atoms under, the key table how a member
    # nested through a pointer reads — both as during the run.
    os.environ[config.GAMERV_GAME_ENV] = row["game"]
    keys = _key_table(row)
    if keys is None:
        os.environ.pop(config.GAMERV_KEY_TABLE_ENV, None)
    else:
        os.environ[config.GAMERV_KEY_TABLE_ENV] = str(keys)
    proc = gamerv._worker("recheck_phi", str(run_dir), formula, phi,
                          archived.get("phi_used") or "", sort)
    got = gamerv._last_json(proc.stdout)
    if proc.returncode != 0 or got is None or got.get("error"):
        detail = (got or {}).get("error") or (proc.stderr or proc.stdout).strip()[-200:]
        return None, f"recheck failed: {detail}"
    want_rank = int(row["gt_rank"])
    checks = {
        "contained": got.get("contained") is True,
        "rank": got.get("rank") == archived.get("rank") == want_rank,
        "candidate": got.get("r_text") == archived.get("r_text"),
        "tier size": got.get("candidates") == int(row["candidates"]),
        "complete scan": not got.get("truncated") and got.get("contained_ranks") is not None,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        return None, (f"re-check does not reproduce the run ({', '.join(failed)}): "
                      f"rank {got.get('rank')} vs {want_rank}, "
                      f"tier {got.get('candidates')} vs {row['candidates']}")
    return got, ""


def backfill(run_dir: Path, dry_run: bool = False) -> int:
    """Fill one run; return how many rows were left blank where a value was due."""
    full_path, mining_path = run_dir / "full.csv", run_dir / "mining.csv"
    with open(full_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        full_fields, full = list(reader.fieldnames or []), list(reader)
    with open(mining_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        mining_fields, mining = list(reader.fieldnames or []), list(reader)
    if len(mining) != len(full):
        raise SystemExit(f"{run_dir}: mining.csv has {len(mining)} rows, "
                         f"full.csv {len(full)}")
    settings_path = run_dir / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    sort = settings.get("candidate_sort") or "none"

    blank: dict[str, str] = {}
    ks: list[int] = []
    strengths: dict[str, int] = {}
    pooled = 0
    saved_env = {name: os.environ.get(name) for name in _ENV}
    try:
        sources = backfill_states._sources(full, backfill_states._stamp(run_dir))
        for row, mrow, src in zip(full, mining, sources):
            if (mrow.get("github_branch"), mrow.get("game")) != (row.get("row_branch"),
                                                                 row.get("game")):
                raise SystemExit(f"{run_dir}: mining.csv and full.csv rows are not "
                                 f"in the same order (row {row['row']})")
            cells = dict.fromkeys(COLUMNS, "")
            hit = backfill_states.run_dir_for(src, row)
            status = (row.get("status") or "").strip()
            if hit is None:
                if (row.get("candidates") or "").strip():
                    blank[row["row"]] = "no archive reproduces the row"
            else:
                rdir, m = hit
                pooled += m.pool is not None
                if status == metrics.FOUND_STATUS:
                    got, why = _recheck(rdir, row, sort)
                    k = (metrics.contained_count(got, int(row["candidates"]))
                         if got else None)
                    if got is None or k is None:
                        blank[row["row"]] = why or (f"unusable contained_ranks "
                                                    f"{got.get('contained_ranks')}")
                    else:
                        m.contained = k
                        m.strength, m.strength_reason = metrics.match_strength(
                            got, row["gt_match"])
                        ks.append(k)
                        strengths[m.strength or "?"] = strengths.get(
                            m.strength or "?", 0) + 1
                cells.update({c: v for c, v in zip(metrics.COLUMNS, m.cells())
                              if c in COLUMNS})
            for target in (row, mrow):
                target.update(cells)
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    found = sum(1 for r in full if (r.get("status") or "").strip() == metrics.FOUND_STATUS)
    print(f"{run_dir.name}: pool for {pooled}/{len(full)} row(s); "
          f"k and strength for {len(ks)}/{found} SUCCESS row(s)"
          + (f" (k = 1: {ks.count(1)}, max {max(ks)})" if ks else ""))
    if strengths:
        print("  strength: " + ", ".join(f"{c} {strengths[c]}"
                                         for c in (*metrics.STRENGTHS, "?")
                                         if c in strengths))
    for r, why in blank.items():
        print(f"  row {r}: left blank — {why}")
    if not dry_run:
        backfill_states._rewrite(full_path, _with_columns(full_fields), full)
        backfill_states._rewrite(mining_path, _with_columns(mining_fields), mining)
        entries = [b for b in settings.get("backfills", [])
                   if b.get("script") not in (SCRIPT, *_FORMER)]
        entries.append({
            "script": SCRIPT, "columns": list(COLUMNS),
            "commits": {"controller": _commit(config.CONTROLLER_DIR),
                        "gamerv": _commit(config.GAMERV_DIR)},
            "key_table": "per row, the branch's build_cache slot as it is now",
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "left_blank": blank,
        })
        settings["backfills"] = entries
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return len(blank)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", nargs="+", help="a results/run_<timestamp>/ folder")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be filled without rewriting anything")
    args = ap.parse_args(argv)
    missed = sum(backfill(Path(d), args.dry_run) for d in args.run_dir)
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
