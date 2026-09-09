#!/usr/bin/env python3
"""Derive a game_profile collection's ``class_in`` list from the real class graph.

    python derive_class_taxonomy.py --game "Super Tux" \
        --base-class BadGuy --collection enemy [--write]

**Why this exists.** A collection should be matched by what an object *is*, not by
which keys it happens to carry. SuperTux's ``enemy`` was the one rule in any game
still doing the latter — ``has_any_key: [dir, frozen, state]`` — which is the
original single-game heuristic that survived the "make the tool generic" refactor.
It has two costs:

* it is wrong at the edges. Any object that gains a field named ``m_dir`` joins the
  enemy collection, so ``Player::m_dir`` could not be reported as a member at all
  (see ``report_state.classifier_keys``);
* it silently depends on ``BadGuy::report_state`` continuing to write those three
  keys by hand, which is exactly what step 2a is meant to stop needing.

**Why libclang and not a regex.** Tried the regex first and it was wrong in both
directions: it missed ``dispenser``, ``flame``, ``goldbomb``, ``haywire`` and
``mrbomb`` (declarations it could not follow) and wrongly added ``crusher``, which
lives in ``src/badguy/`` but derives from ``MovingSprite`` and is not an enemy. The
instrumenter already resolves the true ancestor chain through the cursor graph
(``discover._class_chain``), so this reuses it rather than re-deriving it badly.

The snapshot carries a class's ``class_name()`` literal, not its C++ name, so the
mapping between the two is read from the same parse.

Always verify the result against recorded traces before trusting it —
``--verify-traces`` replays both rules over ``logs_archive/`` and reports any object
that would change collection.
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import config                                                   # noqa: E402

sys.path.insert(0, str(config.GAMERV_SRC_DIR))
from runners.source_instrument import discover as dc            # noqa: E402
from runners.source_instrument import spec as sp                # noqa: E402

import clang.cindex as ci                                       # noqa: E402


def class_name_literals(files, src_root, spec, game) -> dict:
    """``{C++ class -> class_name() literal}`` for every class in these TUs.

    The literal is the ``class`` value the snapshot actually carries, which is what
    a ``class_in`` rule is matched against — the C++ name never appears there.
    """
    db = dc.open_compile_db(game.build_dir, spec)
    out: dict = {}

    def walk(cur, tu_files):
        if (cur.kind == ci.CursorKind.CXX_METHOD
                and cur.spelling == "class_name"
                and cur.is_definition()):
            parent = cur.semantic_parent
            if parent is not None and parent.spelling:
                for tok in cur.get_tokens():
                    if tok.kind == ci.TokenKind.LITERAL and tok.spelling.startswith('"'):
                        out.setdefault(parent.spelling, tok.spelling.strip('"'))
                        break
        for ch in cur.get_children():
            loc = ch.location
            if loc.file is not None and loc.file.name not in tu_files:
                continue
            walk(ch, tu_files)

    for f in files:
        args = dc.compile_args(f, src_root, game.build_dir, game.repo_dir, spec, db)
        parsed = dc.parse_tu(f, src_root, spec, args=args)
        walk(parsed.tu.cursor, parsed.own)
    return out


def verify_traces(names: set, keys: set, root: Path) -> int:
    """Replay both rules over every archived trace; report objects that move."""
    lost, gained = Counter(), Counter()
    n = 0
    for ev in sorted(glob.glob(str(root / "logs_archive" / "row*" / "*" / "events.jsonl"))):
        try:
            lines = open(ev).read().splitlines()
        except OSError:
            continue
        n += 1
        for line in lines:
            if not line.strip():
                continue
            for o in json.loads(line).get("objects", []):
                if not isinstance(o, dict):
                    continue
                cls = o.get("class", "?")
                was, now = bool(keys & set(o)), cls in names
                if was and not now:
                    lost[cls] += 1
                elif now and not was:
                    gained[cls] += 1
    print(f"  verified against {n} archived trace(s)")
    if lost:
        print(f"  LOST   (classified before, not now): {sorted(lost)}")
    if gained:
        print(f"  GAINED (classified now, not before): {sorted(gained)}")
    if not lost and not gained:
        print("  identical classification on every object in every trace")
    return len(lost) + len(gained)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", required=True)
    ap.add_argument("--base-class", required=True,
                    help="the C++ base every member of the collection derives from")
    ap.add_argument("--collection", required=True,
                    help="the game_profile collection to rewrite (e.g. enemy)")
    ap.add_argument("--globs", nargs="*",
                    help="TUs to parse (default: the collection's own directory)")
    ap.add_argument("--write", action="store_true",
                    help="rewrite game_profile.json (default: report only)")
    ap.add_argument("--verify-traces", action="store_true", default=True)
    args = ap.parse_args(argv)

    game = config.get_game(args.game)
    spec = sp.InstrumentSpec.load(game=game.name)
    src_root = game.instrument_src
    patterns = args.globs or [f"{args.base_class.lower()}/*.cpp"]
    files = sorted({Path(p) for g in patterns
                    for p in glob.glob(str(src_root / g))})
    if not files:
        print(f"No sources matched {patterns} under {src_root}", file=sys.stderr)
        return 2
    print(f"parsing {len(files)} translation unit(s) for the class graph...")

    os.environ.setdefault("CLANG_RESOURCE_DIR", str(config.CLANG_RESOURCE_DIR))
    discovery = dc.discover(files, src_root, spec, build_dir=game.build_dir,
                            game_root=game.repo_dir)
    literals = class_name_literals(files, src_root, spec, game)

    # class_bases is self-first, so "derives from X" includes X itself; the base is
    # abstract and never a leaf `class` value, so it is dropped.
    names = sorted({literals[cls] for cls, chain in discovery.class_bases.items()
                    if args.base_class in chain and cls != args.base_class
                    and cls in literals})
    print(f"{len(names)} class(es) derive from {args.base_class} and name themselves:")
    print("  " + ", ".join(names))
    missing = sorted(cls for cls, chain in discovery.class_bases.items()
                     if args.base_class in chain and cls != args.base_class
                     and cls not in literals)
    if missing:
        print(f"  (no class_name() literal, so unreachable from a snapshot: {missing})")

    profile_path = config.GAMERV_DIR / "config" / "games" / game.name / "game_profile.json"
    profile = json.loads(profile_path.read_text())
    coll = (profile.get("collections") or {}).get(args.collection)
    if coll is None:
        print(f"no collection '{args.collection}' in {profile_path}", file=sys.stderr)
        return 2
    before = coll.get("match", {})
    print(f"\n{args.collection}: {json.dumps(before)}\n"
          f"{' ' * len(args.collection)}  -> {{\"class_in\": [{len(names)} classes]}}")

    problems = 0
    if args.verify_traces and before.get("has_any_key"):
        problems = verify_traces(set(names), set(before["has_any_key"]),
                                 config.CONTROLLER_DIR)

    if not args.write:
        print("\n(report only; pass --write to apply)")
        return 0
    if problems:
        print("\nRefusing to write: the classification would change. Investigate "
              "the differences above first.", file=sys.stderr)
        return 1
    coll["match"] = {"class_in": names}
    profile_path.write_text(json.dumps(profile, indent=2) + "\n")
    print(f"\nwrote {profile_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
