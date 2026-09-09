#!/usr/bin/env python3
"""Lift a game's hand-written report_state bodies into `report_state.extra_keys`.

    python lift_report_state.py --game "Witch Blast" [--write]

Step 2a generates every per-class report_state override now, so a hand-written one
is a second writer on the same snapshot object. Deleting it must not change the
published keys, and the reliable way to guarantee that is to transcribe each
`w.write("key", expr);` **verbatim** into the spec rather than retyping ~70 of them
by hand across Witch Blast and FreeMiner.

What it handles, because the real bodies do:
  * a write guarded by an enclosing `if (...)`, which becomes the entry's `guard`;
  * a write inside a `for` over a fixed-size table, unrolled to one entry per index
    (Witch Blast's BaseCreatureEntity writes one boolean per status effect under
    `specialStateToLabel[i]`, a compile-time array of 13 names) — a static
    declaration cannot express a runtime-computed key, so the loop is expanded.

It deliberately does NOT delete anything: run it, read the diff of the generated
JSON, then delete the bodies. A key it could not lift is reported and is a reason
to stop, not to proceed.
"""

import argparse
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import config                                                   # noqa: E402

WRITE = re.compile(r'\bw\s*\.\s*write\s*\(\s*(?P<key>"[^"]*"|[^,]+?)\s*,\s*', re.S)


def _balanced(text: str, start: int) -> str:
    """The argument text from ``start`` up to the call's closing paren."""
    depth, i = 1, start
    while i < len(text) and depth:
        if text[i] in "([":
            depth += 1
        elif text[i] in ")]":
            depth -= 1
        i += 1
    return text[start:i - 1]


def lift_body(body: str, unroll: dict) -> tuple:
    """``([entry, ...], [unlifted, ...])`` for one report_state body."""
    entries, bad = [], []
    for m in WRITE.finditer(body):
        expr = _balanced(body, m.end()).strip()
        key_tok = m.group("key").strip()
        line_start = body.rfind("\n", 0, m.start()) + 1
        prefix = body[line_start:m.start()].strip()

        # An `if (...)` on the same line, or on the line above with no brace.
        guard = ""
        gm = re.match(r"if\s*\((?P<c>.+)\)\s*$", prefix)
        if gm:
            guard = gm.group("c").strip()
        else:
            prev = body.rfind("\n", 0, line_start - 1)
            above = body[prev + 1:line_start - 1].strip() if prev >= 0 else ""
            gm = re.match(r"if\s*\((?P<c>.+)\)\s*$", above)
            if gm:
                guard = gm.group("c").strip()

        if key_tok.startswith('"'):
            entries.append(OrderedDict(
                [("key", key_tok.strip('"')), ("expr", expr)]
                + ([("guard", guard)] if guard else [])))
            continue

        # A runtime key: only liftable if the caller says how to unroll it.
        rule = next((r for tok, r in unroll.items() if tok in key_tok), None)
        if rule is None:
            bad.append((key_tok, expr))
            continue
        # Substitute the loop variable as an IDENTIFIER. A plain str.replace on a
        # one-letter index rewrites every letter of that name in the expression:
        # `specialState[i].active` became `spec11alState[11].act11ve`.
        var = re.compile(rf"\b{re.escape(rule['index'])}\b")
        for i, name in enumerate(rule["labels"]):
            entries.append(OrderedDict(key=name, expr=var.sub(str(i), expr)))
    return entries, bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", required=True)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    game = config.get_game(args.game)
    spec_path = (config.GAMERV_DIR / "config" / "games" / game.name /
                 "source_instrument.json")
    spec = json.loads(spec_path.read_text(), object_pairs_hook=OrderedDict)
    method = (spec.get("report_state") or {}).get("method", "report_state")

    # Runtime-keyed writes this game needs unrolled, by the token in the key expr.
    unroll = {}
    if game.name == "Witch Blast":
        hdr = (game.instrument_src / "BaseCreatureEntity.h").read_text()
        block = re.search(r"specialStateToLabel\[NB_SPECIAL_STATES\]\s*=\s*\{(.*?)\};",
                          hdr, re.S)
        labels = re.findall(r'"([^"]+)"', block.group(1)) if block else []
        unroll["specialStateToLabel"] = {"labels": labels, "index": "i"}
        print(f"unrolling specialStateToLabel into {len(labels)} keys")

    out, problems = OrderedDict(), []
    for cpp in sorted(game.instrument_src.rglob("*.cpp")):
        text = cpp.read_text(encoding="utf-8", errors="replace")
        # The return type may sit on its own line (SuperTux) or share one
        # (FreeMiner), and the signature may be trailed by `const`, so anchor on
        # the qualified name and scan forward to the body's opening brace.
        for m in re.finditer(rf"\b(\w+)::{method}\s*\(", text):
            # A DEFINITION, not the chain call every override starts with
            # (`BaseCreatureEntity::reportState(w);`). Both match the name, and
            # taking a call as a definition made the scan grab the *enclosing*
            # body and attribute its keys to the base class. A definition's
            # parameter list is followed only by an optional `const` and then the
            # opening brace; a call is followed by `;`.
            close = m.end() + len(_balanced(text, m.end()))
            brace = text.find("{", close)
            if brace < 0 or not re.fullmatch(r"\s*(const\s*)?",
                                             text[close + 1:brace]):
                continue
            cls = m.group(1)
            body = _balanced(text, brace + 1)
            entries, bad = lift_body(body, unroll)
            if entries:
                out[cls] = entries
            problems += [(cls, k, e) for k, e in bad]
            print(f"  {cls:26s} {len(entries):3d} key(s)"
                  + (f"  [{len(bad)} UNLIFTED]" if bad else ""))

    if problems:
        print("\nCould not lift:")
        for cls, k, e in problems:
            print(f"  {cls}: key={k} expr={e}")
        print("A runtime-computed key cannot be declared statically. Add an unroll "
              "rule for it, or keep that body.")
        return 1

    total = sum(len(v) for v in out.values())
    print(f"\n{total} key(s) over {len(out)} class(es)")
    if not args.write:
        print(json.dumps(out, indent=2)[:1500])
        print("\n(report only; pass --write to apply)")
        return 0

    rs = spec.setdefault("report_state", OrderedDict())
    existing = rs.get("extra_keys") or OrderedDict()
    for cls, entries in out.items():
        have = {e["key"] for e in existing.get(cls, [])}
        existing.setdefault(cls, [])
        existing[cls] += [e for e in entries if e["key"] not in have]
    rs["extra_keys"] = existing
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"wrote {spec_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
