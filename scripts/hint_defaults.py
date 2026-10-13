#!/usr/bin/env python
"""Derive per-call hint defaults from the answers already collected.

The manual-answer cache (`hints/<branch>/<recX>.hint`) keys every answer by
gamerv's per-trace `"<state_index>::<call>"`. Those indices don't line up between
traces, so copying a `.hint` from one recording to another is unreliable. When a
predicate's answer is the SAME at every state it's evaluated, that fragility is
avoidable: a bare-call default (`{"VisualPredicate(\"Tux jumps\")": true}`) answers the
call at every state, index-free, and can live in `hints/<branch>/_defaults.hint`
to cover every tape of the branch at once (see `src/hints.py`).

This script reads the exact per-state keys you have ALREADY collected and, per
call, checks whether the answer is uniform across every state of every tape:

    python scripts/hint_defaults.py --branch hurt_animation_disappear
    python scripts/hint_defaults.py --all
    python scripts/hint_defaults.py --branch fall_no_sound --write

Without `--write` it only reports. A call that is uniform (all True or all False)
is a safe default; a MIXED call is left alone (it genuinely varies state-to-state
and still needs the collected per-state keys). `--write` merges the uniform calls
into `hints/<branch>/_defaults.hint`, keeping any entries already there.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import config  # noqa: E402
import hints   # noqa: E402


def _call_of(key: str) -> str:
    """The call part of a `<idx>::<call>` key."""
    return key.split("::", 1)[1]


def collect_values(branch_dir: Path) -> dict[str, set[bool]]:
    """Map each call to the set of answer values seen across the branch's tapes.

    Reads only the exact per-state keys of the tape hints; the `_defaults.hint`
    file itself and any bare-call entries are skipped (they're the output side).
    """
    values: dict[str, set[bool]] = defaultdict(set)
    for hint_file in sorted(branch_dir.glob("*.hint")):
        if hint_file.name == hints.DEFAULTS_FILE:
            continue
        for key, val in hints.load(hint_file).items():
            if "::" in key:                       # exact per-state key only
                values[_call_of(key)].add(bool(val))
    return values


def report(branch: str, values: dict[str, set[bool]]) -> dict[str, bool]:
    """Print the per-call verdict for a branch; return the uniform defaults."""
    if not values:
        print(f"  {branch}: no collected answers")
        return {}
    print(f"  {branch}:")
    defaults: dict[str, bool] = {}
    for call in sorted(values):
        seen = values[call]
        if len(seen) == 1:
            val = next(iter(seen))
            defaults[call] = val
            print(f"    [default] {call} = {str(val).lower()}")
        else:
            print(f"    [MIXED  ] {call}  (both true and false seen — needs per-state)")
    return defaults


def write_defaults(branch_dir: Path, defaults: dict[str, bool]) -> None:
    """Merge uniform defaults into hints/<branch>/_defaults.hint."""
    if not defaults:
        print("    nothing uniform to write")
        return
    out = branch_dir / hints.DEFAULTS_FILE
    hints.save(out, defaults)   # merge_into keeps any existing entries
    print(f"    wrote {len(defaults)} default(s) -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--branch", help="branch (hints/<game>/<branch>/) to process")
    g.add_argument("--all", action="store_true",
                   help="every branch under the game's hints/<game>/ tree")
    ap.add_argument("--game", default=None,
                    help="game whose hints to process (default: the only "
                         "registered game); hints live under hints/<game>/")
    ap.add_argument("--write", action="store_true",
                    help="write uniform calls to hints/<game>/<branch>/_defaults.hint")
    args = ap.parse_args()

    if args.game is not None:
        game = config.get_game(args.game)
    elif len(config.GAMES) == 1:
        game = next(iter(config.GAMES.values()))
    else:
        ap.error(f"multiple games registered {sorted(config.GAMES)}; pass --game")
    hints_root = config.HINTS_DIR / game.name

    if args.all:
        branch_dirs = sorted(d for d in hints_root.iterdir() if d.is_dir()) \
            if hints_root.is_dir() else []
    else:
        branch_dirs = [hints_root / args.branch]
        if not branch_dirs[0].is_dir():
            ap.error(f"no such branch dir: {branch_dirs[0]}")

    for branch_dir in branch_dirs:
        defaults = report(branch_dir.name, collect_values(branch_dir))
        if args.write:
            write_defaults(branch_dir, defaults)


if __name__ == "__main__":
    main()
