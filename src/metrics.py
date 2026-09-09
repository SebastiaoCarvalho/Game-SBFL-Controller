"""Step 8 mining metrics: how big the mined candidate set was, and where the
ground-truth phi sits inside it.

gamerv's ``compare_refinement`` persists the reusable half of its mining to
``<run_dir>/fault_localization.json`` (``runners.fault_localization._persist_mining``):
per violated formula, ``shown`` is the **ranked** list of sound candidate
refinements, each a ``[[[key, want], ...], support, size]`` triple. That file is
the machine-readable twin of the ``refinement_separators.log`` the same run
writes, so the metrics here are read off it rather than scraped from stdout.

Three numbers per case:

  candidates  how many sound candidate refinements the miner produced.
  gt_rank     the 1-based position of the ground-truth phi in that ranked list,
              blank when phi is not among the candidates.
  union_only  phi is established only by several candidates *together* — the case
              the single-candidate rule below rejects.

**Where the rank comes from.** gamerv decides phi against the mined refinements one
at a time — ``Exists R in R_all: phi -> R``
(``runners.fault_localization.check_phi_refinement``) — and persists that verdict to
``<run_dir>/phi_refinement.json``: the rank of the first candidate that establishes
phi, the match tier, and whether the union of candidates would have covered a phi no
single one does (``union_only``). That semantic verdict is authoritative here, because
it is the same decision the row's SUCCESS/FAIL status came from: reading it off the
file keeps the rank in a results table and the verdict in the status column from ever
disagreeing.

It is missing only when the containment view never ran — a ``data-direct`` (or
``auto`` with a raw phi) run, where the verdict comes from corroborate instead. Then
the rank falls back to **textual** matching over the same ranked list, on a canonical
form (see `_canonical`): whitespace, C float suffixes (``0.f``), quote style, numeric
spelling (``369`` / ``369.0``), conjunct order, and negation (``~(a == b)`` vs
``a != b``) are all normalised away.

``gt_match`` says which tier ``gt_rank`` refers to, so tiers are never silently mixed
in a results table:

  exact        the candidate at that rank *is* phi (textually, canonicalised).
  implication  that single candidate implies phi (``phi -> R`` holds) without being
               spelled the same — a mined threshold is placed from the data, so it
               rarely equals a hand-written constant.
  region       no mined candidate establishes phi, but phi's own sound core region
               does: a conjunctive-quantifier phi whose joint per-item pattern no
               single mined atom names. There is no rank — the region is derived
               from phi, not mined (see gamerv's check_phi_refinement).
  threshold    textual fallback only: same quantity, same direction, different split
               point (``player.coins < 5`` vs a mined ``player.coins < 369``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MINING_CACHE = "fault_localization.json"
#: gamerv's persisted phi verdict (rank / tier / union_only) — see the module
#: docstring and runners.fault_localization._persist_phi_check.
PHI_CACHE = "phi_refinement.json"

#: Comparison operators, longest first so "<=" is matched before "<".
_OPS = ("<=", ">=", "==", "!=", "<", ">")
_NEGATED = {"<": ">=", ">=": "<", ">": "<=", "<=": ">", "==": "!=", "!=": "=="}
#: Direction classes for the threshold tier: only ordered comparisons over the
#: same term can differ in their split point and still be "the same" refinement.
_DIRECTION = {"<": "lt", "<=": "lt", ">": "gt", ">=": "gt"}

_FLOAT_SUFFIX = re.compile(r"(?<=[0-9.])[fF](?![0-9A-Za-z_.])")


#: The CSV header for :meth:`Mining.cells`, so the columns and the cells that fill
#: them are declared in one place.
COLUMNS = ("candidates", "gt_rank", "gt_match", "union_only")


@dataclass
class Mining:
    """Step 8's mining metrics for one case (all-``None`` when no record was found)."""
    candidates: int | None = None   # size of the ranked candidate set
    gt_rank: int | None = None      # 1-based position of phi, None if not mined
    gt_match: str = ""              # see the module docstring's tier list ("" = none)
    union_only: bool = False        # only the union of candidates establishes phi

    @property
    def found(self) -> bool:
        """Was the ground truth found — by a single candidate, or by phi's own
        region? (The tier says which; ``union_only`` is deliberately not it.)"""
        return bool(self.gt_match)

    def cells(self) -> list[str]:
        """The :data:`COLUMNS` cells, blank where the metric does not apply."""
        return ["" if self.candidates is None else str(self.candidates),
                "" if self.gt_rank is None else str(self.gt_rank),
                self.gt_match,
                "yes" if self.union_only else ""]


# --------------------------------------------------------------------------
# Canonical form
# --------------------------------------------------------------------------
def _split_top(expr: str, sep: str) -> list[str]:
    """Split on ``sep`` at paren depth 0 and outside quotes."""
    parts, buf, depth, quote = [], [], 0, ""
    i = 0
    while i < len(expr):
        c = expr[i]
        if quote:
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and expr.startswith(sep, i):
            parts.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _find_op(expr: str) -> tuple[str, str, str] | None:
    """Split ``expr`` into (lhs, op, rhs) on its top-level comparison, or None."""
    depth, quote = 0, ""
    i = 0
    while i < len(expr):
        c = expr[i]
        if quote:
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0:
            for op in _OPS:
                if expr.startswith(op, i):
                    return expr[:i], op, expr[i + len(op):]
        i += 1
    return None


def _strip_outer_parens(s: str) -> str:
    """Drop parens that wrap the whole expression (``(a & b)`` -> ``a & b``)."""
    s = s.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        for i, c in enumerate(s):
            depth += (c == "(") - (c == ")")
            if depth == 0 and i < len(s) - 1:
                return s          # the parens close early: not a wrapper
        s = s[1:-1].strip()
    return s


def _norm_operand(s: str) -> str:
    """One side of a comparison: whitespace, float suffix, quotes and numeric
    spelling normalised, so ``0.f`` / ``0`` / ``0.0`` and ``'x'`` / ``"x"`` unify."""
    s = _strip_outer_parens(_FLOAT_SUFFIX.sub("", s.strip()))
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return f'"{s[1:-1]}"'
    try:
        val = float(s)
    except ValueError:
        return re.sub(r"\s+", " ", s)
    return f"{val:g}"


def _canonical_atom(atom: str) -> str:
    """One literal in canonical form, with any negation pushed into its operator."""
    a = _strip_outer_parens(atom)
    while a.startswith("~"):
        inner = _strip_outer_parens(a[1:])
        found = _find_op(inner)
        if found is None:
            return f"~({_canonical_atom(inner)})"   # nothing to flip: keep the ~
        lhs, op, rhs = found
        a = f"{lhs}{_NEGATED[op]}{rhs}"
    found = _find_op(a)
    if found is None:
        return re.sub(r"\s+", " ", a)
    lhs, op, rhs = found
    return f"{_norm_operand(lhs)} {op} {_norm_operand(rhs)}"


def _canonical(expr: str) -> str:
    """A whole refinement in canonical form: conjuncts canonicalised and sorted,
    so conjunct order never decides a match."""
    conjuncts = sorted(_canonical_atom(p) for p in _split_top(expr, "&") if p.strip())
    return " & ".join(conjuncts)


def _term_key(expr: str) -> tuple[str, str] | None:
    """(term, direction) for a single ordered comparison, else None. Two candidates
    sharing this key differ only in where the threshold was placed."""
    if "&" in expr:
        return None
    atom = _canonical_atom(expr)
    found = _find_op(atom)
    if found is None:
        return None
    lhs, op, _rhs = found
    direction = _DIRECTION.get(op.strip())
    return (lhs.strip(), direction) if direction else None


# --------------------------------------------------------------------------
# Reading gamerv's mining record
# --------------------------------------------------------------------------
def _combo_formula(combo) -> str:
    """Render a persisted combo the way gamerv prints it (``_conj_to_formula``)."""
    return " & ".join(key if want else f"~({key})" for key, want in combo)


def _entry(path: Path, formula: str) -> dict | None:
    """The record for ``formula`` in one of the two JSON files — or the only one
    present, when the property was renamed/reworded between the run and this read."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    formulas = data.get("formulas") or {}
    entry = formulas.get(formula)
    if entry is None and len(formulas) == 1:
        entry = next(iter(formulas.values()))
    return entry


def _phi_verdict(run_dir: Path, formula: str, phi: str) -> dict | None:
    """gamerv's persisted existential verdict for ``phi``, or None when the
    containment view did not run (or ran for a *different* phi — the file is keyed by
    formula, so a re-compare of the same run with another ground truth would otherwise
    be read as this row's)."""
    entry = _entry(run_dir / PHI_CACHE, formula)
    if entry is None or entry.get("error"):
        return None
    if _canonical(entry.get("phi") or "") != _canonical(phi):
        return None
    return entry


def read(run_dir: Path | str | None, formula: str, phi: str) -> Mining:
    """Mining metrics for one case, read from ``run_dir``'s persisted records.

    The candidate count comes from the mining cache, the ground truth's rank and tier
    from gamerv's phi verdict beside it — falling back to textual matching over the
    same ranked list when no verdict was persisted (see the module docstring).

    Returns an empty :class:`Mining` when the run has no mining record (mining never
    reached the point of persisting: an unparseable property, no violation states, no
    raw candidates) — the metrics are then simply blank rather than reported as zero,
    which would claim the miner produced nothing.
    """
    if run_dir is None:
        return Mining()
    run_dir = Path(run_dir)
    entry = _entry(run_dir / MINING_CACHE, formula)
    if entry is None:
        return Mining()
    shown = entry.get("shown")
    if shown is None:
        return Mining()
    m = Mining(candidates=len(shown))
    if not phi.strip():
        return m                      # no ground truth on this row to look for

    verdict = _phi_verdict(run_dir, formula, phi)
    if verdict is not None:
        m.union_only = bool(verdict.get("union_only"))
        rank, match = verdict.get("rank"), (verdict.get("match") or "")
        if match == "implication" and rank:
            # Name the tier the way a results table reads it: a candidate that is
            # *spelled* like phi is an exact match, one that merely implies it is not.
            m.gt_rank = int(rank)
            m.gt_match = ("exact" if _same_formula(shown, int(rank), phi)
                          else "implication")
        elif match:
            m.gt_match = match        # "region": established, but by no candidate
        return m

    # No persisted verdict (data-direct / auto with a raw phi): fall back to textual
    # matching, at its own two tiers.
    want, want_term = _canonical(phi), _term_key(phi)
    threshold_rank = None
    for i, item in enumerate(shown, start=1):
        combo = item[0]
        cand = _combo_formula(combo)
        if _canonical(cand) == want:
            m.gt_rank, m.gt_match = i, "exact"
            return m
        if (threshold_rank is None and want_term is not None
                and _term_key(cand) == want_term):
            threshold_rank = i
    if threshold_rank is not None:
        m.gt_rank, m.gt_match = threshold_rank, "threshold"
    return m


def _same_formula(shown: list, rank: int, phi: str) -> bool:
    """Is the candidate at 1-based ``rank`` phi itself (canonically)? Ranks index the
    same ranked list gamerv scanned, so an out-of-range rank means the two files were
    written by different runs — reported as "not the same formula" rather than
    guessing."""
    if not (1 <= rank <= len(shown)):
        return False
    return _canonical(_combo_formula(shown[rank - 1][0])) == _canonical(phi)
