"""Step 8 mining metrics: how big the mined candidate set was, and where the
ground-truth phi sits inside it.

gamerv's ``compare_refinement`` persists the reusable half of its mining to
``<run_dir>/fault_localization.json`` (``runners.fault_localization._persist_mining``):
per violated formula, ``shown`` is the **ranked** list of sound candidate
refinements, each a ``[[[key, want], ...], support, size]`` triple. That file is
the machine-readable twin of the ``refinement_separators.log`` the same run
writes, so the metrics here are read off it rather than scraped from stdout.

**Only the top tier counts.** ``shown`` holds *every* sound candidate, at every
score: a candidate's score is its good-state support (the log's ``holds in k/n good
states``, flagged ``[perfect separator]`` at ``k == n``), and gamerv ranks by that
support descending, then by fewest literals. Counting the whole list would answer a
question nobody asks — the point of the metric is how many refinements a user has to
work through before reaching the fix, and a user works down from the best score. So
the candidate set here is the **leading tier of the ranked list: the candidates
sharing the best support and the fewest literals**, with the redundant threshold
spellings inside it folded away (:func:`_top_tier`), and a ground truth that ranks
below that tier is *not found* (see the caveat under "Where the rank comes from").

Two things are excluded from the tier because they are implied by something already
in it, not because they are uninteresting:

  * a **longer conjunction** at the same support. ``_search_conjunctions`` builds a
    pair only when neither literal separates alone, so a size-2 candidate sitting
    beside a sound single says "these two together exclude the violations" where the
    single already did it by itself.
  * a **tighter or differently-spelled split** of a bound the tier already carries:
    ``Forall(e.h < 22)``, ``Forall(e.h < 58)`` and ``~(Exists(e.h > 116))`` are three
    spellings of one cap on ``max(e.h)``, and the loosest implies the other two.

Both are kept in ``refinement_separators.log``, under their own dividers. Dropping
implied candidates cannot change a verdict — the test is ``Exists R: phi -> R``, and
the retained candidate is the weaker one, so it accepts at least as many phis — but it
does move the count and a ground truth's rank, which is the point. It can also move
``gt_match`` from ``exact`` to ``implication`` when phi was spelled at a tighter split
than the retained candidate: that tighter spelling is not a refinement the tool would
show.

Four numbers per case:

  candidates  how many top-scored sound candidate refinements the miner produced.
              Independent of the candidate ordering: the tier is a *set*, and the
              sort only permutes it (see gamerv's runners.candidate_sort).
  gt_rank     the 1-based position of the ground-truth phi in that top tier,
              blank when phi is not among those candidates.
  union_only  phi is established only by several candidates *together* — the case
              the single-candidate rule below rejects.
  below_top   phi *is* established by a single mined candidate, but one scoring
              below the top tier — so this row's gamerv status can read SUCCESS
              while the mining metrics count it as a miss. The flag is what keeps
              that difference visible instead of silent.

**Where the rank comes from.** gamerv decides phi against the mined refinements one
at a time — ``Exists R in R_all: phi -> R``
(``runners.fault_localization.check_phi_refinement``) — and persists that verdict to
``<run_dir>/phi_refinement.json``: the rank of the first candidate that establishes
phi, the match tier, and whether the union of candidates would have covered a phi no
single one does (``union_only``). That semantic verdict is authoritative on *whether*
a single candidate establishes phi, so it is reused here rather than re-derived.

The rank is a **position in the order gamerv's run reported**, which its candidate
sort decides (gamerv's ``runners.candidate_sort``; the controller's
``--candidate-sort``) — that is the point of the knob: the same candidate *set*, read
in a different order, puts the ground truth at a different rank. The mining cache read
here holds the miner's own canonical order instead, so the two must never be crossed:
nothing below indexes the tier by ``rank``. The tier is used only for its **size**
(a set, identical under every sort), and the exact/implication distinction comes off
the verdict's own ``r_text``.

The rank is kept only when it falls inside the top tier. gamerv now decides phi
against that tier itself (``_top_tier`` there), so a fresh verdict's rank is always
inside it — but a verdict **persisted before that change** ranks phi over the whole
list, and one ranking it below the tier is recorded as ``below_top`` and reported as
not found. The row can then read SUCCESS in the status column (that older run did find
a single R establishing phi) and a miss in the mining table (that R is not one of the
candidates the tool would show). The flag is the join between the two readings, and
what lets an archived run be re-read without its numbers silently mixing tiers.

It is missing only when the containment view never ran — a ``data-direct`` (or
``auto`` with a raw phi) run, where the verdict comes from corroborate instead. Then
the rank falls back to **textual** matching over the same ranked list, on a canonical
form (see `_canonical`): whitespace, C float suffixes (``0.f``), quote style, numeric
spelling (``369`` / ``369.0``), conjunct order, and negation (``~(a == b)`` vs
``a != b``) are all normalised away. That fallback ranks over the **miner's** order —
it is the only order this side has — so in those modes a candidate sort does not move
the number. The symbolic mode the experiment runs under always persists a verdict, so
it always reports the sorted rank.

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
COLUMNS = ("candidates", "gt_rank", "gt_match", "union_only", "below_top")


@dataclass
class Mining:
    """Step 8's mining metrics for one case (all-``None`` when no record was found)."""
    candidates: int | None = None   # size of the top-scored tier of the ranked set
    gt_rank: int | None = None      # 1-based position of phi in it, None if not there
    gt_match: str = ""              # see the module docstring's tier list ("" = none)
    union_only: bool = False        # only the union of candidates establishes phi
    below_top: bool = False         # a single candidate establishes phi, but a
                                    # lower-scoring one than the top tier

    @property
    def found(self) -> bool:
        """Was the ground truth found — by a single top-scored candidate, or by phi's
        own region? (The tier says which; ``union_only`` and ``below_top`` are
        deliberately not it: both are ways of *missing*.)"""
        return bool(self.gt_match)

    def cells(self) -> list[str]:
        """The :data:`COLUMNS` cells, blank where the metric does not apply."""
        return ["" if self.candidates is None else str(self.candidates),
                "" if self.gt_rank is None else str(self.gt_rank),
                self.gt_match,
                "yes" if self.union_only else "",
                "yes" if self.below_top else ""]


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
#: An outer quantifier wrapping a whole atom — ``Forall(enemy.x < 5)``. The
#: comparison lives *inside* the parentheses, so the quantifier is stripped before
#: the comparison is read.
_QUANT_WRAP_RE = re.compile(r"^(Forall|Exists)\(\s*(.*?)\s*\)$", re.S)
#: The comparison itself: an arithmetic term, an ordered operator, a number.
#: Deliberately base-agnostic — gamerv builds its own atom regexes from the active
#: game profile, which this side has no access to. Anchoring the number at the end
#: is what keeps it off the equality atoms (a string or container literal) that carry
#: no ordering.
_BOUND_RE = re.compile(r"^(.+?)\s*(<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)$", re.S)
#: Which per-state aggregate a quantified threshold constrains (gamerv's
#: ``raw_atoms._BOUND_AGGREGATE``): ``Forall(t < T)`` / ``Exists(t > T)`` bound the
#: per-state max of ``t``, ``Forall(t > T)`` / ``Exists(t < T)`` the min.
_BOUND_AGGREGATE = {("Forall", "<"): "max", ("Exists", ">"): "max",
                    ("Forall", ">"): "min", ("Exists", "<"): "min"}
_NEGATED_OP = {"<": ">=", "<=": ">", ">": "<=", ">=": "<"}
_FLIP_QUANT = {"Forall": "Exists", "Exists": "Forall"}


def _threshold_bound(key: str, want: bool):
    """The half-line a threshold literal constrains, or ``None`` for anything else.

    Returns ``((quantity, aggregate, side), (value, strict))`` — the offline twin of
    gamerv's ``raw_atoms.threshold_bound``, which is the authority on the mapping and
    carries its full reasoning. Reimplemented rather than imported: this module reads a
    persisted JSON cache with no gamerv process, no game profile and no venv, and
    gamerv's own atom regexes are built from the active profile's bases.
    """
    text = key.strip()
    quant = None
    wrap = _QUANT_WRAP_RE.match(text)
    if wrap is not None:
        quant, text = wrap.group(1), wrap.group(2)
    m = _BOUND_RE.match(text)
    if m is None:
        return None
    term, op, num = m.group(1), m.group(2), m.group(3)
    if _QUANT_WRAP_RE.match(term):               # a nested quantifier is not a term
        return None
    if not want:                                 # read the negated literal positively
        op = _NEGATED_OP[op]
        if quant is not None:
            quant = _FLIP_QUANT[quant]
    if quant is None:
        aggregate = "value"
    else:
        aggregate = _BOUND_AGGREGATE.get((quant, op[0]))
        if aggregate is None:
            return None
    side = "upper" if op[0] == "<" else "lower"
    return (term, aggregate, side), (float(num), len(op) == 1)


def _bound_contains(side: str, outer: tuple, inner: tuple) -> bool:
    """Does half-line ``outer`` contain ``inner``? (gamerv's ``raw_atoms.bound_contains``)"""
    (vo, so), (vi, si) = outer, inner
    if vo != vi:
        return vo > vi if side == "upper" else vo < vi
    return si or not so


def _fold_threshold_bounds(tier: list) -> list:
    """Drop the tier candidates a retained one already implies — gamerv's
    ``fault_localization._fold_threshold_bounds``, on the persisted list.

    Within one ``(quantity, aggregate, side)`` group the candidates are half-lines
    over one aggregate, hence totally ordered by inclusion, so the loosest implies
    every other and reporting it alone loses no refinement — only repetitions of one
    (``Forall(e.h < 22)``, ``Forall(e.h < 58)``, ``~(Exists(e.h > 116))`` are three
    spellings of one cap on ``max(e.h)``). The two polarities fold separately and a
    negated representative may absorb a positive one but never the reverse: a positive
    literal reads False where its quantity is missing or non-numeric, its negation
    True. See gamerv's twin for the full argument.
    """
    groups: dict[tuple, list] = {}
    for i, item in enumerate(tier):
        combo = item[0]
        if len(combo) != 1:
            continue
        key, want = combo[0][0], bool(combo[0][1])
        parsed = _threshold_bound(key, want)
        if parsed is None:
            continue
        family, bound = parsed
        groups.setdefault(family, []).append((i, want, bound))
    if not groups:
        return tier

    drop: set[int] = set()
    for (_term, _aggregate, side), members in groups.items():
        widest: dict[bool, tuple] = {}
        for i, want, bound in members:
            held = widest.get(want)
            if held is None or _bound_contains(side, bound, held[1]):
                widest[want] = (i, bound)
        drop.update(i for i, want, _b in members if widest[want][0] != i)
        lax, strict = widest.get(False), widest.get(True)
        if lax and strict and _bound_contains(side, lax[1], strict[1]):
            drop.add(strict[0])
    return [item for i, item in enumerate(tier) if i not in drop]


def _top_tier(shown: list) -> list:
    """The reported candidate set: gamerv's ranked list cut to its leading run of equal
    **support and size**, with the redundant threshold spellings inside it folded away
    (:func:`_fold_threshold_bounds`).

    Everything below is a strictly worse separator (lower support) or a strictly less
    general one (same support, more literals) that a user reading the list from the top
    would never have to reach, so it is not part of the candidate set these metrics
    report (see the module docstring).

    Taken as the leading *run* rather than by filtering the whole list on the best
    score: the list is support-descending then size-ascending, and a prefix is what
    "how many before the fix" means even if a future ranking key reorders ties.
    """
    if not shown:
        return []
    best, size = shown[0][1], shown[0][2]
    n = 0
    for item in shown:
        if item[1] != best or item[2] != size:
            break
        n += 1
    return _fold_threshold_bounds(shown[:n])


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

    The candidate count is the size of the mining cache's **top-scored tier**, the
    ground truth's rank and tier come from gamerv's phi verdict beside it — falling
    back to textual matching over that same tier when no verdict was persisted (see
    the module docstring).

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
    top = _top_tier(shown)            # the reported candidate set — see _top_tier
    m = Mining(candidates=len(top))
    if not phi.strip():
        return m                      # no ground truth on this row to look for

    verdict = _phi_verdict(run_dir, formula, phi)
    if verdict is not None:
        m.union_only = bool(verdict.get("union_only"))
        rank, match = verdict.get("rank"), (verdict.get("match") or "")
        if match == "implication" and rank:
            rank = int(rank)
            if rank > len(top):
                # gamerv's rank indexes the whole ranked list: phi is established, but
                # by a candidate scoring below the top tier. Not found here, and
                # flagged so the miss and the row's SUCCESS status can be reconciled.
                m.below_top = True
                return m
            # Name the tier the way a results table reads it: a candidate that is
            # *spelled* like phi is an exact match, one that merely implies it is not.
            m.gt_rank = rank
            m.gt_match = ("exact" if _same_formula(verdict.get("r_text"), phi)
                          else "implication")
        elif match:
            m.gt_match = match        # "region": established, but by no candidate
        return m

    # No persisted verdict (data-direct / auto with a raw phi): fall back to textual
    # matching, at its own two tiers — over the top tier only, for the same reason.
    want, want_term = _canonical(phi), _term_key(phi)
    threshold_rank = None
    for i, item in enumerate(top, start=1):
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


def _same_formula(r_text: str | None, phi: str) -> bool:
    """Is the refinement that established phi phi itself (canonically)?

    Read off the verdict's own ``r_text`` rather than by indexing the tier at
    ``rank``: the rank is a position in the order *gamerv's run* reported (which the
    candidate sort decides), while the mining cache holds the miner's canonical
    order, so indexing one with the other would name a different candidate. ``r_text``
    is the refinement the verdict is actually about, whatever the order was.
    """
    return bool(r_text) and _canonical(r_text) == _canonical(phi)
