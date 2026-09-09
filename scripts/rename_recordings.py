#!/usr/bin/env python
"""Rename .rec files (and their paired .hint files) so that same-recording
variants are easy to spot.

A SuperTux `.rec` file starts with a small header of initial conditions

    STX_DEMO 3
    level /levels/world1/welcome_antarctica.stl
    bonus none
    pocket fireflower
    coins 0
    tuxdolls 0
    invincible 0
    frames 787
    <input frames...>

followed by the recorded input frames (the "body"). Two recordings that share
an identical body are the SAME recording replayed under DIFFERENT initial
conditions (different coins/tuxdolls/pocket/etc.); the header differs but the
input sequence is byte-for-byte the same.

Recordings were originally numbered sequentially per test case (rec1.rec,
rec2.rec, ...), which hides these relationships. This script regroups them:
within each test-case folder the files are grouped by body, and renamed to

    recN.rec       # recording group N, the only variant
    recN_M.rec     # recording group N, condition-variant M (>1 variant)

So `rec2_1.rec` and `rec2_2.rec` are the same recording under two conditions,
while `rec1.rec` is a distinct recording with no sibling variants.

HINTS: each recording has a matching hint file (expected assertions) in a
parallel `hints/<case>/recN.hint` tree. The grouping is decided from the .rec
bodies, then the SAME renaming is applied to the hints so the two trees stay in
sync. Hints are paired to recordings by their natural sort order within a case;
if the counts don't line up (e.g. leftover odd hint names), that case's hints
are reported and skipped rather than guessed at.

    python rename_recordings.py                 # dry-run over the default dirs
    python rename_recordings.py --apply          # actually rename recs + hints
    python rename_recordings.py --dir PATH       # a different supertux2 dir
    python rename_recordings.py --hints-dir PATH # a different hints tree
    python rename_recordings.py --no-hints       # only touch the .rec files
    python rename_recordings.py --case NAME       # limit to one test-case folder

Renames go through temporary names first, so reusing numbers (e.g. rec2 -> rec1)
never clobbers a file that has not been moved yet. Dry-run is the default; pass
--apply to touch the filesystem.
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

DEFAULT_DIR = Path.home() / ".local/share/supertux2"
# The hints tree lives in this repo, alongside scripts/.
DEFAULT_HINTS_DIR = Path(__file__).resolve().parent.parent / "hints"

# Files named after a hint rather than after a recording: the trace signature its
# per-state answers are anchored to, and the backup kept when they get re-anchored.
HINT_SIDECAR_SUFFIXES = (".trace", ".bak")

# Header key that marks the last line before the recorded body.
BODY_MARKER = "frames"


def natural_key(name: str):
    """Sort rec10 after rec2, not before it."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def body_hash(path: Path) -> str:
    """Hash the input body (everything after the `frames N` header line).

    Splitting on the `frames` marker rather than a fixed line count keeps this
    robust if the header ever gains or loses a field.
    """
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.split(" ", 1)[0] == BODY_MARKER:
            body = lines[i + 1:]
            break
    else:
        # No frames marker found: treat the whole file as its own identity so
        # it is never merged with anything else.
        body = lines
    return hashlib.md5("\n".join(body).encode()).hexdigest()


def plan_case(case_dir: Path):
    """Return [(rec_path, target_stem)] for one test-case folder, grouped order.

    Recordings are grouped by body. Groups are numbered by the natural order of
    their earliest filename, so numbering is stable and predictable. The target
    stem is `recN` for a lone recording or `recN_M` for a condition-variant.
    """
    recs = sorted(case_dir.glob("*.rec"), key=lambda p: natural_key(p.name))
    if not recs:
        return []

    # Group files by body hash, preserving first-seen order.
    groups: dict[str, list[Path]] = {}
    for rec in recs:
        groups.setdefault(body_hash(rec), []).append(rec)

    # Order groups by the earliest file each contains.
    ordered = sorted(groups.values(), key=lambda files: natural_key(files[0].name))

    plan = []
    for gi, files in enumerate(ordered, start=1):
        if len(files) == 1:
            plan.append((files[0], f"rec{gi}"))
        else:
            for vi, f in enumerate(files, start=1):
                plan.append((f, f"rec{gi}_{vi}"))
    return plan


def apply_renames(renames, dry_run: bool):
    """Perform (old_path, new_name) renames via a temp-name pass.

    The temp pass avoids clobbering when a target name is still occupied by a
    not-yet-moved file (e.g. rec2 -> rec1). Works for any extension.
    """
    pending = [(old, new) for old, new in renames if old.name != new]
    if dry_run or not pending:
        return
    staged = []
    for old, new in pending:
        tmp = old.with_name(old.name + ".renametmp")
        old.rename(tmp)
        staged.append((tmp, old.with_name(new)))
    for tmp, final in staged:
        tmp.rename(final)


def plan_hints(hints_case: Path, rec_plan):
    """Map each .hint to its recording's target stem, or explain why we can't.

    Returns (renames, warning). Hints are paired to recordings by natural sort
    order; the pairing is only trustworthy when the two sets line up 1:1, so a
    count mismatch yields no renames and a warning instead.
    """
    hint_files = sorted(hints_case.glob("*.hint"), key=lambda p: natural_key(p.name))
    if not hint_files:
        return [], None
    if len(hint_files) != len(rec_plan):
        return [], (f"{len(hint_files)} hint(s) but {len(rec_plan)} recording(s) "
                    f"-- names don't line up, skipping hints")
    recs_sorted = sorted(rec_plan, key=lambda ps: natural_key(ps[0].name))
    renames = []
    for hint, (_rec, stem) in zip(hint_files, recs_sorted):
        renames.append((hint, f"{stem}.hint"))
        # A hint's sidecars (.hint.trace, .hint.bak) are addressed by the hint's
        # own name, so they have to move with it -- a .trace left behind would
        # claim the answers of whichever hint inherits the old name.
        for suffix in HINT_SIDECAR_SUFFIXES:
            sidecar = hint.with_name(hint.name + suffix)
            if sidecar.exists():
                renames.append((sidecar, f"{stem}.hint{suffix}"))
    return renames, None


def report(title, renames):
    """Print a rename block; return how many files actually change."""
    changed = [(o, n) for o, n in renames if o.name != n]
    if not renames:
        return 0
    print(f"  {title}")
    for old, new in renames:
        mark = "*" if old.name != new else " "
        print(f"    {mark} {old.name:18} -> {new}")
    return len(changed)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                        help=f"supertux2 data dir (default: {DEFAULT_DIR})")
    parser.add_argument("--hints-dir", type=Path, default=DEFAULT_HINTS_DIR,
                        help=f"parallel hints tree root (default: {DEFAULT_HINTS_DIR})")
    parser.add_argument("--game", default=None,
                        help="game name; hints live under <hints-dir>/<game>/<branch>/ "
                             "(omit for the legacy <hints-dir>/<branch>/ layout)")
    parser.add_argument("--no-hints", action="store_true",
                        help="only rename .rec files, leave hints alone")
    parser.add_argument("--case", help="limit to a single test-case folder by name")
    parser.add_argument("--apply", action="store_true",
                        help="actually rename (default is a dry-run preview)")
    args = parser.parse_args()

    if not args.dir.is_dir():
        parser.error(f"not a directory: {args.dir}")
    do_hints = not args.no_hints
    hints_root = args.hints_dir / args.game if args.game else args.hints_dir
    if do_hints and not hints_root.is_dir():
        parser.error(f"hints dir not found: {hints_root} (use --no-hints to skip)")

    if args.case:
        case_dirs = [args.dir / args.case]
    else:
        case_dirs = sorted((d for d in args.dir.iterdir() if d.is_dir()),
                           key=lambda d: d.name)

    total_changed = 0
    for case_dir in case_dirs:
        if not case_dir.is_dir():
            print(f"skip (missing): {case_dir}")
            continue
        rec_plan = plan_case(case_dir)
        if not rec_plan:
            continue

        n_groups = len({stem.split("_")[0] for _, stem in rec_plan})
        print(f"\n{case_dir.name}  ({len(rec_plan)} recs, {n_groups} recordings)")

        rec_renames = [(p, f"{stem}.rec") for p, stem in rec_plan]
        total_changed += report("recordings:", rec_renames)
        apply_renames(rec_renames, dry_run=not args.apply)

        if do_hints:
            hints_case = hints_root / case_dir.name
            if not hints_case.is_dir():
                print(f"  hints: (no {hints_root.name}/{case_dir.name} folder)")
                continue
            hint_renames, warning = plan_hints(hints_case, rec_plan)
            if warning:
                print(f"  hints: WARNING {warning}")
            else:
                total_changed += report("hints:", hint_renames)
                apply_renames(hint_renames, dry_run=not args.apply)

    action = "renamed" if args.apply else "would rename"
    print(f"\n{action} {total_changed} file(s)"
          + ("" if args.apply else "  (dry-run; pass --apply to do it)"))


if __name__ == "__main__":
    main()
