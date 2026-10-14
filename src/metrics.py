"""Step 8 mining metrics: how big the mined candidate set was, and where the
ground-truth phi sits inside it.

gamerv's ``compare_bug_condition`` persists the reusable half of its mining to
``<run_dir>/fault_localization.json`` (``runners.fault_localization._persist_mining``):
per violated formula, ``shown`` is the **ranked** list of candidate bug conditions,
each a ``[[[key, want], ...], score key, size]`` triple. That file is the
machine-readable twin of the ``bug_conditions.log`` the same run writes, so the
metrics here are read off it rather than scraped from stdout.

**Only the top tier counts.** ``shown`` holds *every* candidate, at every score: a
candidate's score is its Ochiai suspiciousness (the log's ``ochiai 0.707 — holds in
2/2 violation(s) and 2/4 good state(s)``, flagged ``[perfect]`` when it holds on every
violation and no good state), and gamerv ranks by that score descending, then by
fewest literals. The score key in each triple is a dense rank of the score within
this one ranking, so it orders and ties exactly as the score does. Counting the whole
list would answer a question nobody asks — the point of the metric is how many
candidates a user has to work through before reaching the fix, and a user works down
from the best score. So the candidate set here is the **leading tier of the ranked
list: the candidates sharing the best score and the fewest literals**, with the
redundant threshold spellings inside it folded away (:func:`_top_tier`).

The tier also ends at the first **longer candidate** at the same score, though under
the Ochiai ranking that never happens in practice: gamerv keeps a pair only when it
beats every single literal, and a phi-shaped conjunction or disjunction is one atom.
What *is* held out of the tier is a **tighter or differently-spelled split** of a
bound the tier already carries: ``Forall(e.h < 22)``, ``Forall(e.h < 58)`` and
``~(Exists(e.h > 116))`` are three spellings of one cap on ``max(e.h)``, and the
loosest implies the other two. Those stay in ``bug_conditions.log``, under their own
divider. Dropping implied
candidates cannot change a verdict — the test is ``Exists B: phi -> B``, and the
retained candidate is the weaker one, so it accepts at least as many phis — but it
does move the count and a ground truth's rank, which is the point. It can also move
``gt_match`` from ``exact`` to ``implication`` when phi was spelled at a tighter split
than the retained candidate: that tighter spelling is not a candidate the tool would
show.

Three numbers per case:

  candidates  how many top-ranked candidate bug conditions the miner produced.
              Independent of the candidate ordering: the tier is a *set*, and the
              sort only permutes it (see gamerv's runners.candidate_sort).
  gt_rank     the 1-based position of the ground-truth phi in that top tier,
              blank when phi is not among those candidates.
  union_only  phi is established only by several candidates *together* — the case
              the single-candidate rule below rejects.

and two numbers for the size of the state the miner searched (the run's **space**),
counted by gamerv over the pooled good/violation states it persisted
(``raw_atoms.state_variables``):

  variables          every ``base.field`` the profile's bases and collections carry
                     in those states — what the game logs.
  varying_variables  the ones whose value changes across them — the depth-1
                     vocabulary the miner builds its atoms over. Constant fields
                     carry no signal and never become a candidate.

Both are blank for a mining cache written before gamerv recorded them.

Then the two numbers the ranking was learned from — the labelled examples:

  good_states  the pooled trigger states where the spec held (P & Q): gamerv's
               ``pos_raw``.
  bad_states   the pooled violation states (P & ~Q): ``neg_raw``.

Few examples mean many atoms separate them equally well, so the top tier grows and
the ground truth can sit deep in it; the two counts let a results section show that.
Read off the same cache as the space, so they are known whenever the miner pooled
states.

Then the rank with the order taken out. Every candidate in the top tier ties on score
and size, so any order of it is as justified as another, and a single rank says as
much about the order as about the scoring:

  pool              every candidate the miner ranked (the tier, the spellings folded
                    into it, and the lower-scoring rest); the tier's share of it is
                    how far the scoring narrowed the search. Its size moves with the
                    miner's grammar knobs, so it is the secondary number.
  gt_contained      k, how many tier candidates establish phi (gamerv's
                    ``contained_ranks``). Blank when the run predates the field or
                    the scan was cut short (the count is then a lower bound).
  gt_rank_expected  where the first of them lands under a uniformly random order
                    of the tier, ``(n + 1) / (k + 1)`` — the baseline every sort is
                    measured against.
  gt_rank_worst     where it lands under the worst order, ``n - k + 1``.

The best case is always rank 1 (phi ties the top score, so some order puts a
containing candidate first), so it has no column. Like ``gt_rank``, the three are
kept only on a SUCCESS row.

Then how closely the candidate at ``gt_rank`` matches phi — ``gt_match`` says only
whether it is spelled the same — as one of :data:`STRENGTHS` (``gt_strength``) and
what separates the two when they are not provably equivalent
(``gt_strength_reason``); see :func:`match_strength`. SUCCESS-only as well.

Finally, one rank per candidate sort, ``gt_rank_<sort>`` (``none`` / ``type`` /
``popularity``): the ground truth's position in the *same* top tier read in that
sort's order. The run's own sort always fills its column (equal to ``gt_rank``); a run
started with several sorts (``--candidate-sort all``) re-ranks the cached mining under
each of the others (``pipeline._rerank``), so the columns compare the orderings over
one identical candidate set. A column is blank where the row was not ranked under
that sort, or its ground truth was not found.

**Where the rank comes from.** gamerv decides phi against the top-ranked candidates
one at a time — ``Exists B in the top tier: phi -> B``
(``runners.fault_localization.check_phi_contained``) — and persists that verdict to
``<run_dir>/phi_verdict.json``: the rank of the first candidate that establishes phi,
the candidate itself, and whether the union of candidates would have covered a phi no
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

**Only a SUCCESS row is found.** A verdict record is not enough: gamerv runs
containment — and persists it — for an OUTRANKED phi too, so a phi scoring below the
top tier still gets a "rank" whenever some weaker top-ranked candidate contains it;
and a record is never cleared, so one left by an earlier compare of the same run dir
outlives a later compare that rejected phi on its score alone (above the top, or
holding on no violation). Only a phi that ties the top score is a top-scored
condition, which is exactly what the row's SUCCESS verdict says, so :func:`read`
takes that verdict and keeps the rank only under it (:data:`FOUND_STATUS`). With no
record at all — phi over variables no top-ranked candidate constrains — the ground
truth is not found either.

``gt_match`` says which tier ``gt_rank`` refers to, so tiers are never silently mixed
in a results table:

  exact        the candidate at that rank *is* phi (textually, canonicalised).
  implication  that single candidate is implied by phi (``phi -> B`` holds) without
               being spelled the same — a mined threshold is placed from the data,
               so it rarely equals a hand-written constant.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MINING_CACHE = "fault_localization.json"
#: gamerv's persisted phi verdict (rank / candidate / union_only) — see the module
#: docstring and runners.fault_localization._persist_phi_check.
PHI_CACHE = "phi_verdict.json"

#: The one row verdict under which the ground truth counts as found, and so carries a
#: rank — see the module docstring's "Only a SUCCESS row is found".
FOUND_STATUS = "SUCCESS"

#: Comparison operators, longest first so "<=" is matched before "<".
_OPS = ("<=", ">=", "==", "!=", "<", ">")
_NEGATED = {"<": ">=", ">=": "<", ">": "<=", "<=": ">", "==": "!=", "!=": "=="}

_FLOAT_SUFFIX = re.compile(r"(?<=[0-9.])[fF](?![0-9A-Za-z_.])")


#: The CSV header for :meth:`Mining.cells`, so the columns and the cells that fill
#: them are declared in one place.
#: The candidate sorts a rank can be recorded under, in the order their columns are
#: written. Kept in step with ``config.CANDIDATE_SORTS`` / gamerv's
#: ``runners.candidate_sort.SORTS``; this module stays import-free of both.
SORTS = ("none", "type", "popularity")
RANK_COLUMNS = tuple(f"gt_rank_{s}" for s in SORTS)

#: The order-free rank columns (see the module docstring), after the per-sort ones so
#: a run written before them keeps every column it had in place.
TIE_COLUMNS = ("pool", "gt_contained", "gt_rank_expected", "gt_rank_worst")
#: How closely the candidate at ``gt_rank`` matches phi (see :func:`match_strength`).
STRENGTH_COLUMNS = ("gt_strength", "gt_strength_reason")

COLUMNS = ("candidates", "gt_rank", "gt_match", "union_only",
           "variables", "varying_variables", "good_states", "bad_states",
           *RANK_COLUMNS, *TIE_COLUMNS, *STRENGTH_COLUMNS)

#: The match-strength classes, strictest first (:func:`match_strength`).
STRENGTHS = ("exact", "equivalent", "same-on-data", "weaker-on-data", "undecided")


def expected_rank(n: int, k: int) -> float:
    """Expected position of the first of ``k`` marked items when ``n`` are put in a
    uniformly random order: ``(n + 1) / (k + 1)``."""
    return (n + 1) / (k + 1)


def worst_rank(n: int, k: int) -> int:
    """Position of the first of ``k`` marked items when all ``n - k`` others come
    first."""
    return n - k + 1


def hit_probability(n: int, k: int, top: int) -> float:
    """Chance that a uniformly random order of ``n`` items puts at least one of ``k``
    marked ones within the first ``top``: ``1 - C(n-k, top) / C(n, top)``. Summed
    over rows it is how many a random order is expected to find within ``top``."""
    if k <= 0:
        return 0.0
    if top >= n - k + 1:
        return 1.0
    p_miss = 1.0
    for i in range(top):          # the product form of C(n-k, top) / C(n, top)
        p_miss *= (n - k - i) / (n - i)
    return 1.0 - p_miss


def _fmt_rank(x: float) -> str:
    """A rank that may be fractional, to two decimals without trailing zeros."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


@dataclass
class Mining:
    """Step 8's mining metrics for one case (all-``None`` when no record was found)."""
    candidates: int | None = None   # size of the top-ranked tier of the ranked set
    gt_rank: int | None = None      # 1-based position of phi in it, None if not there
    gt_match: str = ""              # see the module docstring's tier list ("" = none)
    union_only: bool = False        # only the union of candidates establishes phi
    variables: int | None = None    # base.field variables in the mined states
    varying: int | None = None      # ... of which change across them
    good_states: int | None = None  # pooled P & Q states (gamerv's pos_raw)
    bad_states: int | None = None   # pooled violation states (neg_raw)
    #: ``{sort: rank}`` for every sort the row was ranked under (the run's own sort
    #: included); a sort mapped to None was ranked and the ground truth not found.
    ranks: dict = field(default_factory=dict)
    pool: int | None = None         # every ranked candidate: tier + folded + lower
    contained: int | None = None    # k: tier candidates establishing phi (found only)
    strength: str = ""              # one of STRENGTHS (found only; "" = not recorded)
    strength_reason: str = ""       # what separates B from phi, "+"-joined

    @property
    def found(self) -> bool:
        """Was the ground truth found — phi top-scored and contained in a single
        top-scored candidate? (The tier says how; ``union_only`` is deliberately not
        it: it is a way of *missing*.)"""
        return bool(self.gt_match)

    @property
    def rank_expected(self) -> float | None:
        if self.candidates is None or not self.contained:
            return None
        return expected_rank(self.candidates, self.contained)

    @property
    def rank_worst(self) -> int | None:
        if self.candidates is None or not self.contained:
            return None
        return worst_rank(self.candidates, self.contained)

    def cells(self) -> list[str]:
        """The :data:`COLUMNS` cells, blank where the metric does not apply."""
        expected = self.rank_expected
        return ["" if self.candidates is None else str(self.candidates),
                "" if self.gt_rank is None else str(self.gt_rank),
                self.gt_match,
                "yes" if self.union_only else "",
                "" if self.variables is None else str(self.variables),
                "" if self.varying is None else str(self.varying),
                "" if self.good_states is None else str(self.good_states),
                "" if self.bad_states is None else str(self.bad_states),
                *("" if self.ranks.get(s) is None else str(self.ranks[s])
                  for s in SORTS),
                "" if self.pool is None else str(self.pool),
                "" if self.contained is None else str(self.contained),
                "" if expected is None else _fmt_rank(expected),
                "" if self.rank_worst is None else str(self.rank_worst),
                self.strength, self.strength_reason]


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
    """A whole condition in canonical form: conjuncts canonicalised and sorted,
    so conjunct order never decides a match."""
    conjuncts = sorted(_canonical_atom(p) for p in _split_top(expr, "&") if p.strip())
    return " & ".join(conjuncts)


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
    every other and reporting it alone loses no candidate — only repetitions of one
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
    **score and size**, with the redundant threshold spellings inside it folded away
    (:func:`_fold_threshold_bounds`).

    Everything below is strictly less suspicious (a lower score) or strictly less
    general (same score, more literals), and a user reading the list from the top
    would never have to reach it, so it is not part of the candidate set these metrics
    report (see the module docstring).

    Taken as the leading *run* rather than by filtering the whole list on the best
    score: the list is score-descending then size-ascending, and a prefix is what
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
    """gamerv's persisted existential verdict for ``phi``, or None when containment
    did not run (or ran for a *different* phi — the file is keyed by formula, so a
    re-compare of the same run with another ground truth would otherwise be read as
    this row's)."""
    entry = _entry(run_dir / PHI_CACHE, formula)
    if entry is None or entry.get("error"):
        return None
    if _canonical(entry.get("phi") or "") != _canonical(phi):
        return None
    return entry


def read(run_dir: Path | str | None, formula: str, phi: str, *,
         status: str) -> Mining:
    """Mining metrics for one case, read from ``run_dir``'s persisted records.

    The candidate count is the size of the mining cache's **top-ranked tier**, the
    ground truth's rank and tier come from gamerv's phi verdict beside it — kept only
    when the row's ``status`` is :data:`FOUND_STATUS`; any other verdict, or none
    persisted, means the ground truth was not found (see the module docstring).

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
    # The state size and the example counts are known as soon as the miner pooled
    # states, whether or not it then ranked anything.
    size = entry.get("variables") or {}
    space = Mining(variables=_count(size.get("logged")),
                   varying=_count(size.get("varying")),
                   good_states=_states(entry, "pos_raw"),
                   bad_states=_states(entry, "neg_raw"))
    shown = entry.get("shown")
    if shown is None:
        return space
    top = _top_tier(shown)            # the reported candidate set — see _top_tier
    m = Mining(candidates=len(top), variables=space.variables, varying=space.varying,
               good_states=space.good_states, bad_states=space.bad_states,
               pool=len(shown))
    if not phi.strip():
        return m                      # no ground truth on this row to look for

    verdict = _phi_verdict(run_dir, formula, phi)
    if verdict is None:
        # Containment never ran: no top-ranked candidate is over phi's variables, or
        # phi was rejected on its score alone. Nothing established it: not found.
        return m
    m.union_only = bool(verdict.get("union_only"))
    if status != FOUND_STATUS:
        # The record may still say phi -> B — an OUTRANKED phi inside a weaker
        # top-ranked B, or a stale record from an earlier compare — but phi is not a
        # top-scored condition, so it has no rank among them.
        return m
    rank, match = verdict.get("rank"), (verdict.get("match") or "")
    if match == "implication" and rank:
        rank = int(rank)
        if rank > len(top):
            return m                  # outside the top tier: not a find
        # Name the tier the way a results table reads it: a candidate that is
        # *spelled* like phi is an exact match, one that merely implies it is not.
        m.gt_rank = rank
        m.gt_match = ("exact" if _same_formula(verdict.get("r_text"), phi)
                      else "implication")
        m.contained = contained_count(verdict, len(top))
        m.strength, m.strength_reason = match_strength(verdict, m.gt_match)
    # Any other match is not a find.
    return m


def match_strength(verdict: dict, gt_match: str) -> tuple[str, str]:
    """``(class, reason)`` for the candidate that established phi, off gamerv's
    ``contained_strength`` record of it; ``("", "")`` for a verdict written before
    gamerv recorded one. The classes, strictest first (:data:`STRENGTHS`):

      exact           B is spelled as phi (``gt_match``), and the converse check does
                      not refute it. Spelling alone is not enough: the canonical form
                      reads ``x != v`` as ``~(x == v)``, which differs for a ``subject``
                      field when the event has no subject (both atoms read False).
      equivalent      ``B -> phi`` holds too.
      same-on-data    not provably equivalent, but B and phi hold on exactly the same
                      recorded states — no recording at hand tells them apart.
      weaker-on-data  B holds on some recorded state phi does not.
      undecided       either could not be evaluated on the states, or the evaluation
                      failed its self-checks.

    The reason (``same_variables`` / ``fewer_variables`` / ``unused_alternative``,
    ``+``-joined) says what separates a B that is not provably equivalent from phi.
    """
    records = verdict.get("contained_strength")
    if not isinstance(records, list):
        return "", ""
    rec = next((r for r in records if isinstance(r, dict)
                and r.get("r_text") == verdict.get("r_text")), None)
    if rec is None:
        return "", ""
    equivalent = rec.get("equivalent")
    if gt_match == "exact" and equivalent is not False:
        return "exact", ""
    if equivalent is True:
        return "equivalent", ""
    reason = "+".join(rec.get("reasons") or [])
    if rec.get("same_on_data") is True:
        return "same-on-data", reason
    if rec.get("same_on_data") is False:
        return "weaker-on-data", reason
    return "undecided", reason


def contained_count(verdict: dict, n_top: int) -> int | None:
    """k off a phi verdict: how many top-tier candidates establish phi (gamerv's
    ``contained_ranks``), or None when the verdict cannot say — written before gamerv
    listed them, a scan cut short by its budget (the list is then a lower bound), or a
    list that does not fit the tier or the verdict's own rank."""
    hits = verdict.get("contained_ranks")
    if not isinstance(hits, list) or not hits or verdict.get("truncated"):
        return None
    if any(not isinstance(r, int) or not 1 <= r <= n_top for r in hits):
        return None
    if min(hits) != verdict.get("rank"):
        return None
    return len(set(hits))


def _count(value) -> int | None:
    """A persisted non-negative count, or None when absent or malformed."""
    return value if isinstance(value, int) and value >= 0 else None


def _states(entry: dict, key: str) -> int | None:
    """How many pooled states the cache holds under ``key`` (``pos_raw`` /
    ``neg_raw``), or None when the list is absent."""
    states = entry.get(key)
    return len(states) if isinstance(states, list) else None


def _same_formula(r_text: str | None, phi: str) -> bool:
    """Is the candidate that established phi phi itself (canonically)?

    Read off the verdict's own ``r_text`` rather than by indexing the tier at
    ``rank``: the rank is a position in the order *gamerv's run* reported (which the
    candidate sort decides), while the mining cache holds the miner's canonical
    order, so indexing one with the other would name a different candidate. ``r_text``
    is the candidate the verdict is actually about, whatever the order was.
    """
    return bool(r_text) and _canonical(r_text) == _canonical(phi)
