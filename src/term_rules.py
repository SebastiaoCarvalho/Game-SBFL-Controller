"""Step 7a — regenerate the game's compound-term allow-list from its own source.

Fault localization's compound sweep builds arithmetic terms over pairs of reported
quantities. Unconstrained, that is where the candidate explosion lives: in one Super
Tux run 2136 of 2136 perfectly-separating refinements were compound, only 9 of them
over a single variable. The allow-list (``gamerv/config/games/<game>/
fl_term_rules.json``, consumed by ``runners.fl_term_rules``) cuts it to the
combinations the game's source actually forms — if the game computes ``a - b``
somewhere, ``a - b`` is a meaningful quantity; if it never does, it is arithmetic
noise.

The file is **derived, never written by hand**, because a hand-written one goes stale
silently and in the worst possible direction. That is not hypothetical: a single
shared ``config/fl_term_rules.json``, digested from Super Tux in July, was still being
applied to every other game months later. Being keyed on literal ``base.field`` tokens
it matched almost nothing outside Super Tux, and being a *strict* allow-list, matching
nothing meant blocking everything: Super Tux Kart mined 9 compound terms in total, all
of them the ones whose names happen to coincide with Super Tux's, and the games whose
primary is not named ``player`` could mine none at all.

So this module regenerates a game's rules whenever the inputs change, and the
regeneration is keyed by :func:`instrument.fingerprint` — the same content key the
build cache uses, covering the source tree, the instrumenter, and the spec. A row that
changes none of them re-uses the file.

Three bridges feed the digest (see :mod:`runners.source_keys`), and this step supplies
all three:

* the **key table** kept from step 2a (``instrument.keys_path``) — what identifier
  ``engine_power`` reports as. Without it a scan sees none of the instrumented locals,
  which for every game but Super Tux is most of the vocabulary.
* a **recorded run** — the base fields (``x``, ``vx``, …) the game's own state writer
  reports, which no instrumentation record names and which the geometric terms are
  built from.
* the optional hand-written **accessor map**, which only Super Tux has.

It runs *after* the step-2a window has closed, deliberately: the key records carry
pre-edit line numbers, so the tree the digest scans must be the pristine one.

The same scan produces a second consumed output, ``fl_var_popularity.json``: how many
source lines mention each reported quantity, which the ``popularity`` candidate sort
(``--candidate-sort``) ranks mined refinements by. It is written beside the rules and
is part of :func:`up_to_date`'s key, so a game digested before it existed is
re-scanned once rather than left without counts forever.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import config
import instrument
from config import Game

#: Written into the generated file so a later run can tell what produced it, and
#: whether that input has since moved.
STAMP = "_generated_by"


def rules_path(game: Game) -> Path:
    return config.GAMERV_DIR / "config" / "games" / game.name / "fl_term_rules.json"


def var_popularity_path(game: Game) -> Path:
    """The digest's other consumed output: per-variable source-mention counts, which
    the ``popularity`` candidate sort ranks by (gamerv's ``runners.candidate_sort``).
    Same scan, same folder, written by the same run as the rules."""
    return rules_path(game).with_name("fl_var_popularity.json")


def _accessor_map(game: Game) -> Path | None:
    """The game's hand-written accessor map, if it has one. Only Super Tux does; the
    legacy shared file is treated as Super Tux's, which is what it always was."""
    cfg = config.GAMERV_DIR / "config"
    for candidate in (cfg / "games" / game.name / "source_field_map.json",
                      cfg / "source_field_map.json" if game.name == "Super Tux" else None):
        if candidate is not None and candidate.is_file():
            return candidate
    return None


def _stamp(game: Game, run_dir: Path | None) -> dict:
    return {"game": game.name, "fingerprint": instrument.fingerprint(game),
            "keys": instrument.keys_path(game).is_file(),
            "snapshot": bool(run_dir)}


def up_to_date(game: Game, run_dir: Path | None) -> bool:
    """Does the existing file already describe these inputs?

    The snapshot flag is part of the key on purpose: rules generated with no run to
    read base fields from are missing every positional combination, and must not be
    served once a run exists.

    The *other* output of the same scan counts as an input here too: a game digested
    before the per-variable counts existed has a perfectly current rules file and no
    counts at all, and would never be re-scanned — leaving the ``popularity``
    candidate sort with nothing to rank by, for exactly the games that have been run
    the most.
    """
    if not var_popularity_path(game).is_file():
        return False
    try:
        have = json.loads(rules_path(game).read_text()).get(STAMP)
    except (OSError, ValueError):
        return False
    return isinstance(have, dict) and have == _stamp(game, run_dir)


def ensure(game: Game, run_dir: Path | None = None, *, force: bool = False) -> bool:
    """Regenerate ``game``'s term rules when the inputs have moved. Returns whether a
    usable rules file is in place afterwards.

    Never raises: a game with no source to scan, a digest that fails, or a tree this
    machine does not have simply leaves the rules absent, and an absent file means
    allow-all — the unconstrained sweep, which is the behaviour this step improves on
    rather than depends on.
    """
    if not instrument.enabled(game) or game.instrument_src is None:
        return rules_path(game).is_file()

    keys = instrument.keys_path(game)
    if not keys.is_file():
        # Refuse to install an allow-list built from a partial view of the
        # vocabulary. Without the key table the scan cannot resolve a single
        # instrumented local, so every combination over one is absent from the rules
        # — and the rules are a *strict* allow-list, so absent means blocked. Measured
        # on Super Tux: digesting it with the accessor bridge alone permits 26
        # combinations and blocks three of its own dataset rows, whose targets name
        # locals (`handle_input__max_bullets`, `collision_static_constrains__
        # movement_y`). Allow-all is worse for the candidate count and better for
        # correctness, and it is what an absent file means, so leaving it absent is
        # the safe state to fail into.
        print(f"  [term-rules] no key table yet ({keys.name}); compound terms stay "
              f"unconstrained until step 2a has run once for this game")
        return False
    if run_dir is None:
        # The same argument as the key table, for the other half of the vocabulary:
        # with no run to read the game's own base fields off (`x`, `vx`, …), every
        # positional combination would be missing from the rules and therefore
        # blocked. Step 7a always has a run; a caller that does not can pass force.
        print("  [term-rules] no run to read base fields from; compound terms stay "
              "unconstrained")
        return False
    if not force and up_to_date(game, run_dir):
        print(f"  [term-rules] up to date ({rules_path(game).name})")
        return True

    args = ["-m", "runners.source_digest", str(game.instrument_src),
            "--game", game.name, "--rule-granularity", "object",
            "--rules-out", str(rules_path(game)), "--keys-from", str(keys)]
    if run_dir is not None:
        args += ["--snapshot", str(run_dir)]
    amap = _accessor_map(game)
    if amap is not None:
        args += ["--map", str(amap)]

    print(f"  [term-rules] digesting {game.instrument_src} "
          f"(key table, "
          f"{'snapshot, ' if run_dir else ''}"
          f"{'accessor map' if amap else 'no accessor map'})")
    try:
        proc = subprocess.run(config.gamerv_cmd(args),
                              cwd=str(config.GAMERV_SRC_DIR),
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=config.INSTRUMENT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  [term-rules] [warn] digest failed to run ({exc}); "
              f"compound terms stay unconstrained")
        return rules_path(game).is_file()
    if proc.returncode != 0:
        print(f"  [term-rules] [warn] digest exit {proc.returncode}; "
              f"compound terms stay unconstrained\n{proc.stdout[-600:]}")
        return rules_path(game).is_file()

    _restamp(game, run_dir)
    for line in proc.stdout.splitlines():
        if (line.startswith(("Scanned", "Generated", "Bridges",
                             "Per-variable source mentions"))
                or "NOT permitted" in line):
            print(f"  [term-rules] {line.strip()}")
    return rules_path(game).is_file()


def _restamp(game: Game, run_dir: Path | None) -> None:
    """Record the inputs inside the generated file, so :func:`up_to_date` can tell
    whether it still describes them."""
    path = rules_path(game)
    try:
        data = json.loads(path.read_text())
        data[STAMP] = _stamp(game, run_dir)
        path.write_text(json.dumps(data, indent=2) + "\n")
    except (OSError, ValueError) as exc:
        print(f"  [term-rules] [warn] could not stamp {path.name}: {exc}")
