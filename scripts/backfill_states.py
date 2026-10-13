#!/usr/bin/env python3
"""Fill the ``good_states`` / ``bad_states`` columns of a run written before them.

    python scripts/backfill_states.py results/run_20261004_234117_bug_sorts [--dry-run]

A run's CSVs carry the counts since 2026-10-05 (metrics.py); an older run's are
rebuilt here from the mining cache each row left behind, so its figures can draw
them (``python src/plots.py <run>`` afterwards). Both counts are list lengths in
``fault_localization.json`` (``pos_raw`` / ``neg_raw``), so unlike the space columns
this needs no game profile and runs in the controller's own env.

**Where a row's cache is.** Step 4 of the *next* row in the batch archives the
logs, so row ``k``'s cache sits in ``logs_archive/row<next>_<stamp>/`` — the latest
such archive stamped no later than the run's results dir, whose stamp is the run's
end. The batch's last row was archived by whatever cleared the logs after it: the
first archive stamped after the run, or nothing yet, leaving it in gamerv's live
``logs/``.

**Every mapping is checked.** An archive is accepted only when its cache holds the
row's own formula (exactly — not :func:`metrics._entry`'s single-formula fallback)
and :func:`metrics.read` on it reproduces the row's ``candidates``, ``gt_rank``,
``variables`` and ``varying_variables`` cells. A row that fails is left blank and
named, never filled from a guess.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import config   # noqa: E402
import metrics  # noqa: E402

COLUMNS = ("good_states", "bad_states")
#: The columns the new ones are written after, as metrics.COLUMNS orders them.
AFTER = "varying_variables"
#: The cells a candidate archive must reproduce to be accepted as the row's.
CHECKED = ("candidates", "gt_rank", "variables", "varying_variables")


def _stamp(run_dir: Path) -> str:
    """``YYYYmmdd_HHMMSS`` out of ``run_<stamp>[_suffix]``."""
    parts = run_dir.name.split("_")
    return f"{parts[1]}_{parts[2]}"


def _archives() -> list[tuple[str, int | None, Path]]:
    """``(stamp, row, dir)`` for every archive, oldest first."""
    out = []
    for d in config.GAMERV_LOGS_ARCHIVE_DIR.iterdir():
        parts = d.name.split("_")
        if not d.is_dir() or len(parts) < 2:
            continue
        row = None
        if parts[0].startswith("row"):
            row, parts = int(parts[0][3:]), parts[1:]
        out.append((f"{parts[0]}_{parts[1]}", row, d))
    return sorted(out, key=lambda a: a[0])


def _sources(rows: list[dict], end: str) -> list[Path | None]:
    """The logs dir each row's mining cache should sit in, in row order."""
    archives = _archives()
    out: list[Path | None] = []
    for i in range(len(rows)):
        if i + 1 < len(rows):
            nxt = int(rows[i + 1]["row"])
            hits = [d for s, r, d in archives if r == nxt and s <= end]
            out.append(hits[-1] if hits else None)
        else:
            later = [d for s, _r, d in archives if s > end]
            out.append(later[0] if later else config.GAMERV_LOGS_DIR)
    return out


def _counts(src: Path | None, row: dict) -> tuple[int, int] | None:
    """``(good, bad)`` from the run dir under ``src`` whose cache is this row's."""
    if src is None:
        return None
    for path in sorted(src.glob(f"*/{metrics.MINING_CACHE}")):
        try:
            entry = (json.loads(path.read_text()).get("formulas") or {}).get(
                row["formula"])
        except (OSError, ValueError):
            continue
        if entry is None:
            continue
        status = (row.get("status") or "").strip()
        m = metrics.read(path.parent, row["formula"], row["phi"], status=status)
        got = dict(zip(metrics.COLUMNS, m.cells()))
        # A run written before metrics.read took the verdict ranked a non-SUCCESS
        # row too; read that cell as the blank it now is (report.row_from_dict).
        want = {c: ("" if c == "gt_rank" and status != metrics.FOUND_STATUS
                    else (row.get(c) or "").strip()) for c in CHECKED}
        if all(got[c] == want[c] for c in CHECKED):
            if m.good_states is not None and m.bad_states is not None:
                return m.good_states, m.bad_states
    return None


def _with_columns(fields: list[str]) -> list[str]:
    if all(c in fields for c in COLUMNS):
        return fields
    at = fields.index(AFTER) + 1 if AFTER in fields else len(fields)
    return fields[:at] + [c for c in COLUMNS if c not in fields] + fields[at:]


def _rewrite(path: Path, fields: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def backfill(run_dir: Path, dry_run: bool = False) -> int:
    """Fill one run; return how many rows could not be mapped."""
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

    missed = []
    for row, mrow, src in zip(full, mining, _sources(full, _stamp(run_dir))):
        if (mrow.get("github_branch"), mrow.get("game")) != (row.get("row_branch"),
                                                             row.get("game")):
            raise SystemExit(f"{run_dir}: mining.csv and full.csv rows are not "
                             f"in the same order (row {row['row']})")
        counts = _counts(src, row)
        if counts is None:
            missed.append(row["row"])
            continue
        for target in (row, mrow):
            target["good_states"], target["bad_states"] = map(str, counts)

    print(f"{run_dir.name}: {len(full) - len(missed)}/{len(full)} row(s) mapped"
          + (f"; left blank: rows {', '.join(missed)}" if missed else ""))
    if not dry_run:
        _rewrite(full_path, _with_columns(full_fields), full)
        _rewrite(mining_path, _with_columns(mining_fields), mining)
    return len(missed)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", nargs="+", help="a results/run_<timestamp>/ folder")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the mapping without rewriting the CSVs")
    args = ap.parse_args(argv)
    missed = sum(backfill(Path(d), args.dry_run) for d in args.run_dir)
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
