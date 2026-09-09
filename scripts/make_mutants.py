#!/usr/bin/env python
"""Create mutants of a SuperTux recording: same actions, different conditions.

A `.rec` file is a header of initial conditions followed by the recorded input
frames (the "body"):

    STX_DEMO 3
    level /levels/world1/welcome_antarctica.stl
    bonus none
    pocket fireflower
    coins 0
    tuxdolls 0
    invincible 0
    frames 787
    <input frames...>

A MUTANT is a new recording with the SAME body (identical Tux actions) but a
DIFFERENT header. Any subset of the mutable initial conditions may change, and a
single mutant may change several at once. `level` is never mutated: the recorded
inputs are tied to that level's geometry, so changing it would break the replay.

Values are drawn from the full valid game ranges (see DOMAINS below), not just
values already seen in your dataset. Every generated mutant is guaranteed to
differ from the original and from every other mutant.

    python make_mutants.py rec1.rec 9          # 9 mutants + the original = 10
    python make_mutants.py rec1.rec 9 --apply   # actually write the files
    python make_mutants.py rec1.rec 5 --seed 42 # reproducible
    python make_mutants.py rec1.rec 5 --fields coins,tuxdolls   # limit fields
    python make_mutants.py rec1.rec 5 --exclude bonus=none      # forbid a value

Use --exclude to forbid specific values for a field, e.g. when the recording
shows Tux surviving a hit, `--exclude bonus=none` keeps every mutant at a bonus
that can actually take that hit. It is repeatable and accepts comma lists:
`--exclude bonus=none --exclude pocket=none` or `--exclude bonus=none,pocket=none`.

Output files are written next to the original as `<stem>_mut1.rec`,
`<stem>_mut2.rec`, ... The original is left untouched. Afterwards you can run
rename_recordings.py to fold them into the recN_M.rec grouping convention.

HINTS: a mutant replays the SAME body as the original, so its manual-predicate
answers (hints/<branch>/<stem>.hint) are usually identical. When the original has
a hint, each mutant gets a COPY of it (`<stem>_mut<i>.hint`) so `--manual-auto`
can reuse it instead of re-collecting. This also makes the hint and rec counts
line up, so a later `rename_recordings.py` folds both trees together (skip with
`--no-hint`). The hint's `.hint.trace` signature is copied along with it, so where
a changed initial condition shifts the mutant's trace the controller diffs the two
event sequences, re-anchors the answers that survived and re-prompts only the
states that genuinely differ. Recommended order:
collect the original's hint once, then `make_mutants.py ... --apply`, then
`rename_recordings.py --apply`.

Dry-run is the default; pass --apply to write files.
"""

import argparse
import random
import shutil
import sys
from pathlib import Path

# The hints tree lives in this repo, alongside scripts/ (same as rename_recordings).
DEFAULT_HINTS_DIR = Path(__file__).resolve().parent.parent / "hints"

BODY_MARKER = "frames"

# Full valid game ranges for each mutable initial condition.
#   numeric fields: (min, max) inclusive
#   categorical fields: list of valid tokens
DOMAINS = {
    "coins": (0, 9999),        # game caps the coin counter at 9999
    "tuxdolls": (0, 99),
    "bonus": ["none", "egg", "fireflower", "iceflower", "airflower", "earthflower"],
    "pocket": ["none", "fireflower", "iceflower", "airflower", "earthflower"],
    "invincible": [0, 1],
}
DEFAULT_FIELDS = list(DOMAINS)  # everything but the level


def parse_rec(path: Path):
    """Split a .rec into (header_pairs, body_lines).

    header_pairs is an ordered list of (key, value_str) covering every line up
    to and including `frames N`. Body is everything after.
    """
    lines = path.read_text().splitlines()
    header, body = [], []
    for i, line in enumerate(lines):
        key = line.split(" ", 1)[0]
        header.append(line)
        if key == BODY_MARKER:
            body = lines[i + 1:]
            break
    else:
        sys.exit(f"error: no '{BODY_MARKER}' line found in {path}; not a valid .rec")
    return header, body


def header_value(header, key):
    for line in header:
        parts = line.split(" ", 1)
        if parts[0] == key:
            return parts[1] if len(parts) > 1 else ""
    return None


def draw_value(field, current, rng, excluded):
    """Pick a value for `field` that differs from `current` and is not blocked."""
    domain = DOMAINS[field]
    blocked = excluded.get(field, set())
    if isinstance(domain, tuple):
        lo, hi = domain
        while True:
            v = str(rng.randint(lo, hi))
            if v != str(current) and v not in blocked:
                return v
    else:
        choices = [str(c) for c in domain
                   if str(c) != str(current) and str(c) not in blocked]
        return rng.choice(choices)


def make_mutation(header, fields, rng, excluded):
    """Return a dict {field: new_value} for one mutant (>=1 field changed)."""
    k = rng.randint(1, len(fields))
    chosen = rng.sample(fields, k)
    return {f: draw_value(f, header_value(header, f), rng, excluded) for f in chosen}


def apply_mutation(header, mutation):
    """Return a new header with `mutation` applied to the matching lines."""
    out = []
    for line in header:
        key = line.split(" ", 1)[0]
        out.append(f"{key} {mutation[key]}" if key in mutation else line)
    return out


def condition_signature(header, fields):
    """Tuple of mutable-field values, used to enforce uniqueness."""
    return tuple(header_value(header, f) for f in fields)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recording", type=Path, help="the original .rec file")
    parser.add_argument("count", type=int, help="number of mutants to create")
    parser.add_argument("--fields", help="comma-separated fields to mutate "
                        f"(default: {','.join(DEFAULT_FIELDS)})")
    parser.add_argument("--exclude", action="append", default=[],
                        metavar="FIELD=VALUE",
                        help="forbid a value for a field, e.g. --exclude bonus=none; "
                        "repeatable and accepts comma lists")
    parser.add_argument("--out-dir", type=Path,
                        help="where to write mutants (default: same dir as original)")
    parser.add_argument("--hints-dir", type=Path, default=DEFAULT_HINTS_DIR,
                        help=f"parallel hints tree root (default: {DEFAULT_HINTS_DIR})")
    parser.add_argument("--game", default=None,
                        help="game name; hints live under <hints-dir>/<game>/<branch>/ "
                             "(omit for the legacy <hints-dir>/<branch>/ layout)")
    parser.add_argument("--no-hint", action="store_true",
                        help="do NOT copy the original's hint to each mutant")
    parser.add_argument("--seed", type=int, help="RNG seed for reproducible mutants")
    parser.add_argument("--apply", action="store_true",
                        help="actually write files (default is a dry-run preview)")
    args = parser.parse_args()

    if not args.recording.is_file():
        parser.error(f"no such file: {args.recording}")
    if args.count < 1:
        parser.error("count must be >= 1")

    fields = args.fields.split(",") if args.fields else list(DEFAULT_FIELDS)
    unknown = [f for f in fields if f not in DOMAINS]
    if unknown:
        parser.error(f"unknown field(s): {', '.join(unknown)}; "
                     f"valid: {', '.join(DOMAINS)}")

    # Parse --exclude FIELD=VALUE specs into {field: {blocked values...}}.
    excluded = {}
    for spec in args.exclude:
        for item in filter(None, (s.strip() for s in spec.split(","))):
            if "=" not in item:
                parser.error(f"bad --exclude {item!r}; expected FIELD=VALUE")
            f, v = (s.strip() for s in item.split("=", 1))
            if f not in DOMAINS:
                parser.error(f"unknown field in --exclude: {f}; valid: {', '.join(DOMAINS)}")
            domain = DOMAINS[f]
            if isinstance(domain, tuple):
                lo, hi = domain
                if not (v.lstrip("-").isdigit() and lo <= int(v) <= hi):
                    parser.error(f"--exclude {f}={v}: expected an integer in [{lo}, {hi}]")
            elif v not in {str(c) for c in domain}:
                parser.error(f"--exclude {f}={v}: not a valid {f} value "
                             f"({', '.join(map(str, domain))})")
            excluded.setdefault(f, set()).add(v)

    rng = random.Random(args.seed)
    header, body = parse_rec(args.recording)
    out_dir = args.out_dir or args.recording.parent
    stem = args.recording.stem

    # Number of reachable values for a field (its domain minus blocked values).
    def field_span(f):
        d = DOMAINS[f]
        blocked = excluded.get(f, set())
        if isinstance(d, tuple):
            lo, hi = d
            return (hi - lo + 1) - sum(1 for b in blocked if lo <= int(b) <= hi)
        return len([c for c in d if str(c) not in blocked])

    # A field can only be mutated if it has a reachable value other than the
    # original's — otherwise draw_value would have nothing to return.
    def differing_values(f):
        d = DOMAINS[f]
        blocked = excluded.get(f, set())
        current = str(header_value(header, f))
        if isinstance(d, tuple):
            lo, hi = d
            current_reachable = lo <= int(current) <= hi and current not in blocked
            return field_span(f) - (1 if current_reachable else 0)
        return len({str(c) for c in d if str(c) not in blocked} - {current})

    for f in fields:
        if differing_values(f) < 1:
            parser.error(f"field {f!r} has no value left to mutate to after "
                         f"--exclude (current={header_value(header, f)}); drop it "
                         f"from --fields or relax --exclude")

    # Distinct upper bound: how many unique condition-sets the domains allow.
    max_unique = 1
    for f in fields:
        max_unique *= field_span(f)
    if args.count >= max_unique:
        parser.error(f"cannot make {args.count} distinct mutants from fields "
                     f"{fields}: at most {max_unique - 1} differ from the original")

    # The original's hint, copied to each mutant (same body -> same answers). The
    # branch is the recording's parent folder name (<rec_dir>/<branch>/<stem>.rec);
    # hints are namespaced by game (<hints-dir>/<game>/<branch>/) to match the
    # runtime layout, with a legacy fallback when --game is omitted.
    branch = args.recording.parent.name
    hints_branch_dir = (args.hints_dir / args.game / branch if args.game
                        else args.hints_dir / branch)
    src_hint = hints_branch_dir / f"{stem}.hint"
    src_trace = hints_branch_dir / f"{stem}.hint.trace"
    copy_hint = not args.no_hint and src_hint.is_file()
    if not args.no_hint and not src_hint.is_file():
        print(f"note: no hint {src_hint} to propagate (mutants get no .hint)")

    seen = {condition_signature(header, fields)}  # exclude the original
    print(f"original: {args.recording}  (mutating: {', '.join(fields)})")
    for line in header:
        if line.split(" ", 1)[0] in fields:
            print(f"    {line}")

    hints_copied = 0
    for i in range(1, args.count + 1):
        # Retry until this mutant's conditions are unique.
        while True:
            mutation = make_mutation(header, fields, rng, excluded)
            new_header = apply_mutation(header, mutation)
            sig = condition_signature(new_header, fields)
            if sig not in seen:
                seen.add(sig)
                break

        out_path = out_dir / f"{stem}_mut{i}.rec"
        mutant_hint = hints_branch_dir / f"{stem}_mut{i}.hint"
        changed = ", ".join(f"{k}:{header_value(header, k)}->{v}"
                            for k, v in sorted(mutation.items()))
        mark = "*" if args.apply else " "
        note = f"  + {mutant_hint.name}" if copy_hint else ""
        print(f"  {mark} {out_path.name:20} [{changed}]{note}")
        if args.apply:
            out_path.write_text("\n".join(new_header + body) + "\n")
            if copy_hint:
                mutant_hint.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src_hint, mutant_hint)
                # The .trace signature travels with the answers: it says which
                # trace their state indices refer to, and that is exactly what
                # the controller needs to spot a mutant whose trace shifted.
                if src_trace.is_file():
                    shutil.copyfile(src_trace, Path(f"{mutant_hint}.trace"))
                hints_copied += 1

    action = "wrote" if args.apply else "would write"
    hint_summary = (f" + {hints_copied} hint(s)" if args.apply and hints_copied
                    else f" (+ a hint copy each)" if copy_hint else "")
    print(f"\n{action} {args.count} mutant(s){hint_summary} into {out_dir}"
          + ("" if args.apply else "  (dry-run; pass --apply to write)"))


if __name__ == "__main__":
    main()
