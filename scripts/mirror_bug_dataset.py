#!/usr/bin/env python
"""Write the bug-condition mirror of the dataset: every Target Phi negated.

The dataset's ``Target Phi`` is the condition under which the property HOLDS (the
ground truth for ``--objective refinement``). ``--objective bug-condition`` scores
the condition under which the bug OCCURS instead, so it needs the opposite ground
truth, the negation of each phi, which this script writes:

    python scripts/mirror_bug_dataset.py            # dataset.csv -> dataset_bug_condition.csv beside it
    python scripts/mirror_bug_dataset.py --check    # exit 1 if the mirror is missing or stale
    python scripts/mirror_bug_dataset.py --src other.csv --out other_bug.csv

Only the ``Target Phi`` cell changes; every other byte of each line — the padded
column layout, the quoting, blank and not-yet-filled rows — is copied as is, so
the two files diff line-for-line. A blank phi stays blank (the row is SKIPPED
under either objective).

The negation is pushed inside, so the bug condition reads like the mutation condition
it transcribes (``m_wading`` -> ``player.wading == true``, not
``~(player.wading == false)``): De Morgan over ``&``/``|``, ``Exists`` <-> ``Forall``,
and each comparison's operator flipped (``<`` <-> ``>=``, ``<=`` <-> ``>``, ``==`` <->
``!=``; a ``== true``/``== false`` flips its literal instead). A phi that already is
one negated operand, ``~(A)`` / ``~Exists(...)``, gives back ``A`` / ``Exists(...)``
verbatim. Two leaves keep a ``~``, because the grammar has no pushed form for them:
a quantifier over a conjunction (``~Exists(a & b)`` would be ``Forall(~a | ~b)``, and
a quantifier body is a conjunction only) and a leaf with no comparison to flip.

The rewrite is exact on every state that reports the quantities an atom reads. On a
state that does not, an atom and its flipped twin both read False, where ``~(atom)``
read True: the pushed form holds only where the variables exist, as the mutation
condition does.

Re-run it after editing dataset.csv; ``--check`` says when that is needed.
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import config  # noqa: E402
from dataset import _PHI_KEYS, read_cases  # noqa: E402


def _closing(text: str, i: int) -> int:
    """The index of the ``)`` matching the ``(`` at ``text[i]``, or -1. Parens
    inside a quoted string literal (``bonus == "(none)"``) are not counted."""
    depth, quote = 0, None
    for j in range(i, len(text)):
        c = text[j]
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _single_primary(text: str) -> str | None:
    """``text`` as one self-contained operand, or None: a parenthesised group
    spanning all of it (returned without the parens), or a call ``Name(...)``
    spanning all of it (returned as is)."""
    text = text.strip()
    if text.startswith("("):
        return text[1:-1].strip() if _closing(text, 0) == len(text) - 1 else None
    head = 0
    while head < len(text) and (text[head].isalnum() or text[head] == "_"):
        head += 1
    if head and head < len(text) and text[head] == "(" \
            and _closing(text, head) == len(text) - 1:
        return text
    return None


_FLIP = {"<": ">=", "<=": ">", ">": "<=", ">=": "<", "==": "!=", "!=": "=="}
_BOOL = {"true": "false", "false": "true"}
_DUAL = {"Exists": "Forall", "Forall": "Exists"}


def _top_level(text: str):
    """``(index, char)`` of each character of ``text`` outside every bracket pair
    (``()``, ``[]``, ``{}``) and every quoted string literal."""
    depth, quote = 0, None
    for i, c in enumerate(text):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0:
            yield i, c


def _split(text: str, op: str) -> list[str]:
    """``text`` split at each top-level connective ``op`` (``&`` or ``|``)."""
    cuts = [i for i, c in _top_level(text) if c == op]
    return [text[a + 1:b].strip() for a, b in zip([-1, *cuts], [*cuts, len(text)])]


def _comparison(text: str) -> tuple[str, str, str] | None:
    """``(lhs, op, rhs)`` of an atom split at its top-level comparison operator, or
    None when it has none."""
    for i, c in _top_level(text):
        op = text[i:i + 2] if text[i:i + 2] in _FLIP else c
        if op in _FLIP:
            return text[:i].strip(), op, text[i + len(op):].strip()
    return None


def _group(text: str) -> str:
    """``text`` parenthesised when it is a top-level ``&``/``|``, so it can be one
    operand of the other connective."""
    return f"({text})" if any(c in "&|" for _, c in _top_level(text)) else text


def _negate(phi: str) -> str:
    phi = phi.strip()
    if phi.startswith("(") and _single_primary(phi) is not None:
        return _negate(_single_primary(phi))
    for op, dual in (("|", "&"), ("&", "|")):
        parts = _split(phi, op)
        if len(parts) > 1:
            return f" {dual} ".join(_group(_negate(p)) for p in parts)
    if phi.startswith("~"):                      # double negation
        operand = phi[1:].strip()
        return (_single_primary(operand) if operand.startswith("(") else None) \
            or operand
    name = phi.split("(", 1)[0]
    if name in _DUAL and _single_primary(phi) == phi:
        body = phi[len(name) + 1:-1].strip()
        if not any(c in "&|" for _, c in _top_level(body)):
            return f"{_DUAL[name]}({_negate(body)})"
    else:
        cmp = _comparison(phi)
        if cmp is not None:
            lhs, op, rhs = cmp
            if op == "==" and rhs in _BOOL:
                return f"{lhs} == {_BOOL[rhs]}"
            return f"{lhs} {_FLIP[op]} {rhs}"
    return f"~({phi})"


def negate(phi: str) -> str:
    """The negation of ``phi`` with the ``~`` pushed inside (see the module
    docstring). Blank stays blank."""
    return _negate(phi) if phi.strip() else ""


def _field_spans(line: str) -> list[tuple[int, int]]:
    """``(start, end)`` of each field of one CSV line, read the way
    ``dataset.read_cases`` reads it (``csv`` with ``skipinitialspace``): a quote is
    special only at the start of a field, after any leading spaces."""
    spans, i, n = [], 0, len(line)
    while True:
        start = i
        while i < n and line[i] == " ":
            i += 1
        if i < n and line[i] == '"':
            i += 1
            while i < n:
                if line[i] == '"':
                    if i + 1 < n and line[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        while i < n and line[i] != ",":
            i += 1
        spans.append((start, i))
        if i >= n:
            return spans
        i += 1                                   # past the comma


def _render(raw: str, value: str) -> str:
    """``value`` written into the cell ``raw`` came from, keeping its leading
    space, its quoting and — when the new text fits — its padded width."""
    lead = raw[:len(raw) - len(raw.lstrip(" "))]
    body = raw.strip(" ")
    quoted = body.startswith('"') or any(c in value for c in ',"\n')
    cell = ('"' + value.replace('"', '""') + '"') if quoted and value else value
    return (lead + cell).ljust(len(raw))


def mirror(src_text: str) -> str:
    """The mirror file's text for a dataset file's text."""
    lines = src_text.splitlines(keepends=True)
    if not lines:
        return src_text
    header = next(csv.reader([lines[0]], skipinitialspace=True))
    try:
        col = next(k for k, h in enumerate(header) if h.strip().lower() in _PHI_KEYS)
    except StopIteration:
        raise SystemExit(f"no Target Phi column in the header: {header}")
    out = [lines[0]]
    for line in lines[1:]:
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        spans = _field_spans(body)
        if col >= len(spans):
            out.append(line)                     # a short / blank line: untouched
            continue
        start, end = spans[col]
        cell = next(csv.reader([body[start:end]], skipinitialspace=True), [""])
        phi = cell[0] if cell else ""
        out.append(body[:start] + _render(body[start:end], negate(phi))
                   + body[end:] + eol)
    return "".join(out)


def _verify(src: Path, out: Path) -> list[str]:
    """Read both files back the way the controller does: same rows, every other
    column identical, and each phi exactly negated."""
    a, b = read_cases(src), read_cases(out)
    if len(a) != len(b):
        return [f"row count differs: {len(a)} vs {len(b)}"]
    problems = []
    for x, y in zip(a, b):
        same = (x.row, x.game, x.branch, x.specification, x.fault_type,
                x.correct_drift) == (y.row, y.game, y.branch, y.specification,
                                     y.fault_type, y.correct_drift)
        if not same:
            problems.append(f"row {x.row}: a column other than Target Phi changed")
        elif y.phi != negate(x.phi):
            problems.append(f"row {x.row}: phi {y.phi!r} is not the negation of "
                            f"{x.phi!r}")
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", type=Path, default=config.DATASET,
                    help=f"the dataset to mirror (default: {config.DATASET})")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"where to write it (default: {config.BUG_DATASET_NAME} "
                         "beside --src)")
    ap.add_argument("--check", action="store_true",
                    help="write nothing; exit 1 if the mirror is missing or stale")
    args = ap.parse_args(argv)
    out = args.out or args.src.with_name(config.BUG_DATASET_NAME)

    with open(args.src, newline="", encoding="utf-8") as f:
        text = mirror(f.read())
    if args.check:
        current = out.read_text(encoding="utf-8") if out.is_file() else None
        if current != text:
            print(f"{out} is {'missing' if current is None else 'stale'} — "
                  f"re-run without --check")
            return 1
        print(f"{out} is up to date with {args.src}")
        return 0

    with open(out, "w", newline="", encoding="utf-8") as f:
        f.write(text)
    problems = _verify(args.src, out)
    for p in problems:
        print(f"  ! {p}", file=sys.stderr)
    changed = sum(1 for c in read_cases(out) if c.phi)
    print(f"wrote {out}: {changed} phi negated "
          f"({len(read_cases(out)) - changed} blank)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
