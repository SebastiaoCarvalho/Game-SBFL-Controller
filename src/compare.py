#!/usr/bin/env python
"""Compare two runs' results: which rows improved, which regressed, what moved.

A long batch is re-run after a fix, and the question is always the same one —
*did it help?* Two ``results/run_<timestamp>/`` folders answer it only if someone
diffs 120 rows by hand, so this does it:

    python src/compare.py results/run_20260910_023507 results/run_20260911_105646

The first folder is the **before** run (A), the second the **after** run (B), and
every reading is "A -> B". The headline is the run's own criterion — a row is
SUCCESS when a single mined ``R`` establishes ``phi`` (see the README's *Result
statuses*) — so rows are split into the three groups a fix is judged by:

    gained   anything -> SUCCESS        the fix reached these rows
    lost     SUCCESS -> anything        the fix cost these rows
    other    a change between two non-SUCCESS statuses (FAIL -> INCONCLUSIVE, ...)

with one line per changed row, in the shape the question is asked in::

    row 43  Super Tux    collision_wall_...   INCONCLUSIVE -> SUCCESS
    row 56  Witch Blast  player_hurt_...      SUCCESS -> REFINEMENT_NOT_FOUND

Then the same change, aggregated: the classification counts side by side with
their deltas, the gained/lost split per game and per bug category, and the
MINING numbers — how often the ground truth was found, and how big the candidate
set it had to be found in was.

**GROUND-TRUTH RANK** is the last section and the other half of "did it improve":
a fix can leave every verdict where it stood and still have moved phi from rank
40 to rank 2, or the reverse, which no status column shows. Over the cases that
mined with a ground truth in **both** runs (the only rows whose two rank
distributions are comparable) it reports

    in the top k      how often phi was among the first k mined refinements, at
                      the cut-offs ``--top`` names (1, 3, 5, 10 by default) — the
                      row a results table quotes, since k is how far a user reads
    ranked at all     and its two complements: found but unranked (phi's own
                      sound core region establishes it, so there is no position),
                      and not found
    median rank       the middle position, over each side's own ranked rows
    mean reciprocal rank   1/rank averaged over *every* compared case, 0 where phi
                      was not ranked — the one number that moves both when a fix
                      finds phi on a new row and when it ranks phi higher
    median rank share  the rank as a fraction of that run's candidate set, because
                      the sets move too: rank 11 of 155 is deeper into the list
                      than rank 12 of 236, and only this number says so

then the same cuts per game and per parent category, and the per-case **rank
moves** — biggest first, by how far phi travelled relative to where it was, with
the ranks gained and lost at the top.

**What is compared.** A case is identified by its *game and branch* — the mutant
it ran, not the dataset row number, which shifts whenever a row is inserted
upstream. Only the cases **both** runs hold are compared; the rest are listed
under NOT COMPARED rather than counted as a change on either side, so a batch
that ran 120 rows can be compared against one that ran 24. The row number is
display only (B's, or A's when B has none), and a case whose number moved between
the runs is flagged.

**On "improved".** SUCCESS is the experiment's definition and the verdict line
uses nothing else. The ``closer`` / ``further`` annotation on the *other* changes
is a reading aid over :data:`PROGRESS`, a heuristic ordering of how much a status
settled (ERROR decided nothing, FAIL decided phi and rejected it,
REFINEMENT_NOT_FOUND says phi was right and the search missed it) — useful for
skimming, not a second criterion.

    python src/compare.py A B --out cmp.txt     # also write it to a file
    python src/compare.py A B --csv cmp.csv     # the per-case table, machine-readable
    python src/compare.py A B --game "Super Tux"   # only that game's cases
    python src/compare.py A B --all             # every mining/rank move, not the top ones
    python src/compare.py A B --top 1,5,10,25   # count phi at these rank cut-offs
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import config
import dataset
import report
from report import Row

#: How far along a status is towards the run's goal, worst first. A *heuristic*
#: reading order for the changes that neither gained nor lost a SUCCESS — never a
#: second success criterion (see the module docstring):
#:   ERROR           the experiment itself broke: nothing was learned
#:   NEEDS_MANUAL    the row has not been run yet
#:   SKIPPED         no ground truth to decide
#:   INCONCLUSIVE    nothing to decide phi against (no trace / no violation / no R)
#:   VARIABLE_NOT_FOUND  phi names a quantity the game never exports
#:   FAIL            phi was decided — and rejected
#:   REFINEMENT_NOT_FOUND  phi is right, a single mined R just did not establish it
#:   SUCCESS         the goal
PROGRESS = ("ERROR", "NEEDS_MANUAL", "SKIPPED", "INCONCLUSIVE",
            "VARIABLE_NOT_FOUND", "FAIL", "REFINEMENT_NOT_FOUND", "SUCCESS")

GOAL = "SUCCESS"

#: Mining moves are the long tail of a comparison; only the biggest are printed
#: unless ``--all`` is given.
MOVES_SHOWN = 20

#: The cut-offs the ground-truth rank is counted at ("phi was in the top k mined
#: refinements"), overridable with ``--top``. These are the numbers a results
#: table quotes: how often a user reaching for the fix finds it without reading
#: far.
TOP_K = (1, 3, 5, 10)


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------
def _key(row: Row) -> tuple[str, str]:
    """The case's identity: its game and the mutant branch it ran.

    Not the dataset row number — that is a position in ``dataset.csv`` and moves
    when a row is inserted above it, which would silently compare two different
    mutants. The branch is the mutant.
    """
    return (row.game.strip().casefold(), row.branch.strip().casefold())


def _keyed(rows: list[Row]) -> dict[tuple[str, str, int], Row]:
    """``{key: row}``, with a per-key occurrence counter appended so a run that
    somehow holds the same game/branch twice still pairs them positionally instead
    of one of them being dropped."""
    out: dict[tuple[str, str, int], Row] = {}
    seen: Counter[tuple[str, str]] = Counter()
    for r in rows:
        base = _key(r)
        out[(*base, seen[base])] = r
        seen[base] += 1
    return out


@dataclass(frozen=True)
class Pair:
    """One case as both runs saw it."""
    a: Row
    b: Row

    @property
    def case(self) -> int | None:
        """The dataset row number to print it under (B's, else A's)."""
        return self.b.case or self.a.case

    @property
    def renumbered(self) -> bool:
        """Same mutant, different dataset row number — the dataset moved under the
        comparison. Harmless, but worth saying once."""
        return (self.a.case is not None and self.b.case is not None
                and self.a.case != self.b.case)

    @property
    def game(self) -> str:
        return self.b.game or self.a.game or "?"

    @property
    def branch(self) -> str:
        return self.b.branch or self.a.branch or "?"

    @property
    def fault_type(self) -> str:
        return self.b.fault_type or self.a.fault_type

    @property
    def category(self) -> str:
        return self.fault_type or dataset.UNCATEGORISED

    @property
    def fault_class(self) -> str:
        return dataset.fault_class(self.fault_type)

    @property
    def changed(self) -> bool:
        return self.a.status != self.b.status

    @property
    def kind(self) -> str:
        """``gained`` / ``lost`` / ``other`` / ``same`` — the run's own criterion."""
        if self.a.status == self.b.status:
            return "same"
        if self.b.status == GOAL:
            return "gained"
        if self.a.status == GOAL:
            return "lost"
        return "other"

    @property
    def direction(self) -> int:
        """+1 / -1 / 0 on :data:`PROGRESS`, or 0 when either status is unknown to
        it (a new verdict word is reported, never ranked)."""
        if self.a.status not in PROGRESS or self.b.status not in PROGRESS:
            return 0
        step = PROGRESS.index(self.b.status) - PROGRESS.index(self.a.status)
        return (step > 0) - (step < 0)


@dataclass(frozen=True)
class Comparison:
    """Two runs, paired on the cases they share."""
    a_dir: Path
    b_dir: Path
    a_rows: list[Row]
    b_rows: list[Row]
    pairs: list[Pair]
    a_only: list[Row]
    b_only: list[Row]
    a_settings: dict | None
    b_settings: dict | None

    def of_kind(self, kind: str) -> list[Pair]:
        return [p for p in self.pairs if p.kind == kind]

    @property
    def changes(self) -> list[Pair]:
        return [p for p in self.pairs if p.changed]


def _ordered(pairs: list[Pair]) -> list[Pair]:
    """Cases in dataset order, falling back to game/branch for a run whose CSV
    predates the ``row`` column (report._case_order does the same)."""
    return sorted(pairs, key=lambda p: (p.case is None, p.case or 0,
                                        p.game, p.branch))


def compare(a_dir: Path | str, b_dir: Path | str, game: str | None = None) -> Comparison:
    """Pair up two runs' ``full.csv`` rows. ``game`` keeps only that game's cases
    (case-insensitive), applied before pairing so the one-sided lists are scoped
    the same way."""
    a_dir, b_dir = Path(a_dir), Path(b_dir)
    a_rows, b_rows = report.read_rows(a_dir), report.read_rows(b_dir)
    if game:
        key = game.strip().casefold()
        a_rows = [r for r in a_rows if r.game.strip().casefold() == key]
        b_rows = [r for r in b_rows if r.game.strip().casefold() == key]
    a_keyed, b_keyed = _keyed(a_rows), _keyed(b_rows)
    pairs = _ordered([Pair(a_keyed[k], b_keyed[k]) for k in a_keyed if k in b_keyed])
    return Comparison(
        a_dir=a_dir, b_dir=b_dir, a_rows=a_rows, b_rows=b_rows, pairs=pairs,
        a_only=[a_keyed[k] for k in a_keyed if k not in b_keyed],
        b_only=[b_keyed[k] for k in b_keyed if k not in a_keyed],
        a_settings=report.read_settings(a_dir), b_settings=report.read_settings(b_dir))


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------
def _group(pairs: list[Pair], key, order=None) -> list[tuple[str, list[Pair]]]:
    groups: dict[str, list[Pair]] = {}
    for p in pairs:
        groups.setdefault(key(p), []).append(p)
    labels = sorted(groups, key=order) if order else sorted(groups)
    return [(label, groups[label]) for label in labels]


def by_game(pairs: list[Pair]) -> list[tuple[str, list[Pair]]]:
    return _group(pairs, lambda p: p.game)


def by_parent(pairs: list[Pair]) -> list[tuple[str, list[Pair]]]:
    rank = {dataset.NON_TEMPORAL: 0, dataset.TEMPORAL: 1}
    return _group(pairs, lambda p: p.fault_class,
                  order=lambda label: (rank.get(label, 2), label))


def by_category(pairs: list[Pair]) -> list[tuple[str, list[Pair]]]:
    return _group(pairs, lambda p: p.category,
                  order=lambda label: (dataset.fault_order(label), label))


def _statuses_present(pairs: list[Pair]) -> list[str]:
    """Every status either side used, known ones first in report.py's order."""
    seen = {p.a.status or "?" for p in pairs} | {p.b.status or "?" for p in pairs}
    return ([s for s in report.STATUSES if s in seen]
            + sorted(s for s in seen if s not in report.STATUSES))


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------
def _pct(n: int, total: int) -> str:
    return f"{100.0 * n / total:.1f}%" if total else "-"


def _delta(n: int | float, fmt: str = "+d") -> str:
    """A signed delta, or a plain ``0`` (an unsigned zero reads as "no change",
    which is what it is)."""
    return format(n, fmt) if n else "0"


def _num(value: int | None) -> str:
    return "-" if value is None else str(value)


def _rank_delta(p: "Pair") -> str:
    """The CSV's rank movement: a signed step, ``ranked`` / ``unranked`` where phi
    was found or lost, blank where neither run ranked it."""
    cell = _rank_delta_cell(p)
    return "" if cell == "-" else cell


def _cell(value: int | None) -> str:
    """A metric for the CSV: blank where the pipeline had no number, exactly as
    ``full.csv`` writes it (a '-' would read as a value)."""
    return "" if value is None else str(value)


def _section(title: str, body: list[str]) -> list[str]:
    return ["", title, "-" * len(title)] + body


def _median(values: list[int]) -> str:
    return f"{statistics.median(values):g}" if values else "-"


def _relative(a: int | None, b: int | None) -> float:
    """How big the move from ``a`` to ``b`` is, as a share of the larger of the
    two (0 when either side has no number, so a metric that only one run produced
    is ordered by the other one's move)."""
    if a is None or b is None:
        return 0.0
    return abs(b - a) / max(abs(a), abs(b), 1)


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------
def _change_lines(pairs: list[Pair], annotate: bool = False) -> list[str]:
    """One line per changed case, in the shape the question is asked in:
    ``row 43  Super Tux  branch  INCONCLUSIVE -> SUCCESS``."""
    if not pairs:
        return ["    (none)"]
    gw = max(len(p.game) for p in pairs)
    bw = max(len(p.branch) for p in pairs)
    sw = max(len(p.a.status or "?") for p in pairs)
    out = []
    for p in _ordered(pairs):
        note = ""
        if annotate and p.direction:
            note = "   (closer)" if p.direction > 0 else "   (further)"
        out.append(f"    row {'-' if p.case is None else p.case:>3}  "
                   f"{p.game:<{gw}}  {p.branch:<{bw}}  "
                   f"{p.a.status or '?':<{sw}} -> {p.b.status or '?'}{note}")
    return out


def _classification_table(pairs: list[Pair]) -> list[str]:
    """Rows per status in each run, with the delta — the run-level shape of the
    change, over the compared cases only."""
    a_seen = Counter(p.a.status or "?" for p in pairs)
    b_seen = Counter(p.b.status or "?" for p in pairs)
    total = len(pairs)
    width = max([len("status")] + [len(s) for s in _statuses_present(pairs)])
    out = [f"  {'status':<{width}}  {'A':>5}  {'B':>5}  {'delta':>6}  "
           f"{'A share':>8}  {'B share':>8}"]
    for status in _statuses_present(pairs):
        a_n, b_n = a_seen.get(status, 0), b_seen.get(status, 0)
        out.append(f"  {status:<{width}}  {a_n:>5}  {b_n:>5}  "
                   f"{_delta(b_n - a_n):>6}  "
                   f"{_pct(a_n, total):>8}  {_pct(b_n, total):>8}")
    out.append(f"  {'-' * (width + 40)}")
    out.append(f"  {'total':<{width}}  {total:>5}  {total:>5}")
    return out


def _group_table(groups: list[tuple[str, list[Pair]]], head: str) -> list[str]:
    """Per group: the SUCCESS count on each side and its delta, then how that delta
    was made up — gained, lost, and the changes that were neither."""
    label_w = max([len(head)] + [len(label) for label, _ in groups])
    out = [f"  {head:<{label_w}}  {'rows':>5}  {'A succ':>6}  {'B succ':>6}  "
           f"{'delta':>6}  {'B rate':>7}  {'gain':>5}  {'lost':>5}  {'other':>5}"]
    for label, group in groups:
        a_n = sum(1 for p in group if p.a.status == GOAL)
        b_n = sum(1 for p in group if p.b.status == GOAL)
        kinds = Counter(p.kind for p in group)
        out.append(f"  {label:<{label_w}}  {len(group):>5}  {a_n:>6}  {b_n:>6}  "
                   f"{_delta(b_n - a_n):>6}  {_pct(b_n, len(group)):>7}  "
                   f"{kinds.get('gained', 0):>5}  {kinds.get('lost', 0):>5}  "
                   f"{kinds.get('other', 0):>5}")
    return out


def _mining_summary(pairs: list[Pair]) -> list[str]:
    """The mining numbers side by side: how often the ground truth was found among
    the top-scored candidates, how big that set was, and where phi sat in it.

    The medians are taken over each side's own mined rows (a row that mined in only
    one run cannot contribute to both), and the paired lines below them over the
    rows that mined in *both* — the only rows on which a median delta means
    anything.
    """
    a_mined = [p for p in pairs if p.a.mined]
    b_mined = [p for p in pairs if p.b.mined]
    if not a_mined and not b_mined:
        return ["  no compared case both mined and carried a ground-truth phi."]
    a_found = sum(1 for p in a_mined if p.a.found)
    b_found = sum(1 for p in b_mined if p.b.found)
    out = [
        f"  rows that mined with a ground truth:  {len(a_mined):>5} -> {len(b_mined)}",
        f"  ground truth found:                   {a_found:>5} -> {b_found}  "
        f"({_pct(a_found, len(a_mined))} -> {_pct(b_found, len(b_mined))}, "
        f"{_delta(b_found - a_found)})",
        f"  rejected by the single-refinement rule:"
        f"{sum(1 for p in a_mined if p.a.union_only):>6} -> "
        f"{sum(1 for p in b_mined if p.b.union_only)}  (union only)",
        f"  median candidates (own mined rows):   "
        f"{_median([p.a.candidates for p in a_mined]):>5} -> "
        f"{_median([p.b.candidates for p in b_mined])}",
    ]
    both = [p for p in pairs if p.a.mined and p.b.mined]
    if both:
        da = [p.b.candidates - p.a.candidates for p in both]
        smaller = sum(1 for d in da if d < 0)
        bigger = sum(1 for d in da if d > 0)
        out += [
            f"  over the {len(both)} row(s) that mined in both runs:",
            f"    candidate set:  smaller on {smaller}, bigger on {bigger}, "
            f"unchanged on {len(both) - smaller - bigger}  "
            f"(median change {_delta(statistics.median(da), '+g')})",
        ]
    out.append("  where the ground truth sat inside those sets is the "
               "GROUND-TRUTH RANK section.")
    return out


def _case_table_lines(shown: list[Pair], columns: list[tuple[str, list[str]]],
                      total: int, limit: int | None) -> list[str]:
    """``row / game / branch`` followed by ``columns`` — ``[(head, cells)]``, one
    cell per shown case, right-aligned — and the truncation note. The two move
    tables below differ only in their columns, so the layout is written once."""
    gw = max(len(p.game) for p in shown)
    bw = max(len(p.branch) for p in shown)
    # Every column is right-aligned on its widest cell except the last, which is
    # left-aligned and unpadded: it is the widest one (a status transition), and
    # right-aligning it would ragged the left edge of the column that reads most.
    widths = [max([len(head)] + [len(c) for c in cells])
              for head, cells in columns[:-1]] + [0]
    def row(prefix: str, cells: list[str]) -> str:
        return (prefix + "  ".join(f"{c:>{w}}" for c, w in zip(cells, widths))).rstrip()
    out = [row(f"  {'row':>3}  {'game':<{gw}}  {'branch':<{bw}}  ",
               [head for head, _ in columns])]
    for i, p in enumerate(shown):
        out.append(row(f"  {'-' if p.case is None else p.case:>3}  "
                       f"{p.game:<{gw}}  {p.branch:<{bw}}  ",
                       [cells[i] for _, cells in columns]))
    if limit is not None and total > limit:
        out.append(f"  ... and {total - limit} more (--all to list them)")
    return out


def _status_cell(p: Pair) -> str:
    return "same" if not p.changed else f"{p.a.status} -> {p.b.status}"


def _moves_table(pairs: list[Pair], limit: int | None) -> list[str]:
    """The candidate-set moves, biggest first: how many top-scored refinements each
    case mined in each run.

    A fix can leave every verdict where it was and still have cut a candidate set
    from 6873 to 56 — that is the size of the list a user reads, and no status
    column shows it. Ordered by *relative* change, so a 100x cut outranks a set
    that grew by 40.
    """
    moved = [p for p in pairs if p.a.candidates != p.b.candidates]
    if not moved:
        return ["  no case's candidate count moved."]
    ordered = sorted(moved, key=lambda p: (_relative(p.a.candidates, p.b.candidates),
                                           abs((p.b.candidates or 0)
                                               - (p.a.candidates or 0))),
                     reverse=True)
    shown = ordered if limit is None else ordered[:limit]
    return _case_table_lines(
        shown,
        [("candidates", [f"{_num(p.a.candidates)} -> {_num(p.b.candidates)}"
                         for p in shown]),
         ("rank", [f"{_num(p.a.gt_rank)} -> {_num(p.b.gt_rank)}" for p in shown]),
         ("status", [_status_cell(p) for p in shown])],
        len(ordered), limit)


# --------------------------------------------------------------------------
# Ground-truth rank
# --------------------------------------------------------------------------
def _mrr(rows: list[Row]) -> float:
    """Mean reciprocal rank over ``rows``: ``1/rank`` where phi was ranked, averaged
    over *all* of them, so a row whose ground truth was never ranked contributes 0.

    One number for both halves of the question — a fix that finds phi on a row it
    used to miss, and a fix that keeps the same rows but ranks phi higher, both
    raise it. It is dominated by the top of the list (rank 1 -> 1.0, rank 10 ->
    0.1), which is the reading order a user actually pays for.
    """
    return sum(1.0 / r.gt_rank for r in rows if r.gt_rank) / len(rows) if rows else 0.0


def _rank_share(rows: list[Row]) -> float | None:
    """Median of ``rank / candidates``: how deep into its own candidate set phi sat.

    The companion to the raw rank, because the candidate sets move too — rank 11 of
    155 is deeper into the list than rank 12 of 236, and only this number says so.
    """
    shares = [r.gt_rank / r.candidates for r in rows if r.gt_rank and r.candidates]
    return statistics.median(shares) if shares else None


def _ab_table(rows: list[tuple[str, str, str, str] | None], head: str) -> list[str]:
    """``label | A | B | delta``, one line per entry; a ``None`` entry is a rule."""
    body = [r for r in rows if r]
    lw = max([len(head)] + [len(r[0]) for r in body])
    aw = max([1] + [len(r[1]) for r in body])
    bw = max([1] + [len(r[2]) for r in body])
    dw = max([len("delta")] + [len(r[3]) for r in body])
    out = [f"  {head:<{lw}}  {'A':>{aw}}  {'B':>{bw}}  {'delta':>{dw}}"]
    for r in rows:
        out.append(f"  {'-' * (lw + aw + bw + dw + 6)}" if r is None else
                   f"  {r[0]:<{lw}}  {r[1]:>{aw}}  {r[2]:>{bw}}  {r[3]:>{dw}}")
    return out


def _rank_summary(pairs: list[Pair], top_k: tuple[int, ...]) -> list[str]:
    """Where the ground truth ranked in each run, over the cases that mined with a
    ground truth in **both** — the only rows on which the two runs' rank
    distributions are comparable at all.

    Read top-down: the ``top k`` lines are the ones a results table quotes (how
    often a user reaching for the fix finds it in the first k refinements), the
    rule below them summarises the same rows as medians and a reciprocal rank.
    """
    both = [p for p in pairs if p.a.mined and p.b.mined]
    if not both:
        return ["  no compared case mined with a ground truth in both runs — "
                "there is no rank distribution to compare."]
    a_rows, b_rows = [p.a for p in both], [p.b for p in both]

    def counted(rows: list[Row], k: int) -> int:
        return sum(1 for r in rows if r.gt_rank and r.gt_rank <= k)

    def line(label, a, b, fmt="d", dfmt=None):
        return (label, format(a, fmt), format(b, fmt),
                _delta(b - a, dfmt or f"+{fmt}"))

    table: list[tuple | None] = [line(f"in the top {k}", counted(a_rows, k),
                                      counted(b_rows, k)) for k in top_k]
    ranked = (len([r for r in a_rows if r.gt_rank]),
              len([r for r in b_rows if r.gt_rank]))
    unranked = (sum(1 for r in a_rows if r.found and not r.gt_rank),
                sum(1 for r in b_rows if r.found and not r.gt_rank))
    table += [
        line("ranked at all", *ranked),
        line("found without a rank", *unranked),
        line("not found at all", len(a_rows) - ranked[0] - unranked[0],
             len(b_rows) - ranked[1] - unranked[1]),
        None,
        ("median rank", _median([r.gt_rank for r in a_rows if r.gt_rank]),
         _median([r.gt_rank for r in b_rows if r.gt_rank]), _median_delta(a_rows, b_rows)),
        line("mean reciprocal rank", _mrr(a_rows), _mrr(b_rows), ".3f", "+.3f"),
    ]
    a_share, b_share = _rank_share(a_rows), _rank_share(b_rows)
    if a_share is not None and b_share is not None:
        table.append(("median rank share", f"{a_share:.1%}", f"{b_share:.1%}",
                      _delta((b_share - a_share) * 100, "+.1f") + "pt"))

    out = [f"  over the {len(both)} case(s) that mined with a ground truth in "
           f"both runs:", ""]
    out += _ab_table(table, "where phi ranked")

    moved = [p for p in both if p.a.gt_rank and p.b.gt_rank]
    better = sum(1 for p in moved if p.b.gt_rank < p.a.gt_rank)
    worse = sum(1 for p in moved if p.b.gt_rank > p.a.gt_rank)
    gained = sum(1 for p in both if p.b.gt_rank and not p.a.gt_rank)
    lost = sum(1 for p in both if p.a.gt_rank and not p.b.gt_rank)
    step = ""
    if moved:
        steps = [p.b.gt_rank - p.a.gt_rank for p in moved]
        step = f"  (median step {_delta(statistics.median(steps), '+g')})"
    out += ["",
            f"  per case: of the {len(moved)} ranked in both runs, better on "
            f"{better}, worse on {worse}, unchanged on "
            f"{len(moved) - better - worse}{step}",
            f"            {gained} newly ranked, {lost} lost their rank"]
    return out


def _median_delta(a_rows: list[Row], b_rows: list[Row]) -> str:
    """The change in median rank — over each side's own ranked rows, so it matches
    the two medians printed beside it (which are different row sets whenever a fix
    finds or loses a ground truth)."""
    a = [r.gt_rank for r in a_rows if r.gt_rank]
    b = [r.gt_rank for r in b_rows if r.gt_rank]
    if not a or not b:
        return "-"
    return _delta(statistics.median(b) - statistics.median(a), "+g")


def _rank_by_game(pairs: list[Pair], head: str, groups) -> list[str]:
    """Per group: the rank distribution on each side, over that group's cases that
    mined with a ground truth in both runs."""
    rows = [(label, [p for p in group if p.a.mined and p.b.mined])
            for label, group in groups]
    rows = [(label, both) for label, both in rows if both]
    if not rows:
        return []
    label_w = max([len(head)] + [len(label) for label, _ in rows])
    out = [f"  {head:<{label_w}}  {'cases':>5}  {'ranked':>11}  "
           f"{'med.rank':>13}  {'MRR':>15}  {'better':>6}  {'worse':>5}"]
    for label, both in rows:
        a_rows, b_rows = [p.a for p in both], [p.b for p in both]
        a_ranked = [r for r in a_rows if r.gt_rank]
        b_ranked = [r for r in b_rows if r.gt_rank]
        moved = [p for p in both if p.a.gt_rank and p.b.gt_rank]
        ranked = f"{len(a_ranked)} -> {len(b_ranked)}"
        med = (f"{_median([r.gt_rank for r in a_ranked])} -> "
               f"{_median([r.gt_rank for r in b_ranked])}")
        mrr = f"{_mrr(a_rows):.3f} -> {_mrr(b_rows):.3f}"
        out.append(f"  {label:<{label_w}}  {len(both):>5}  {ranked:>11}  "
                   f"{med:>13}  {mrr:>15}  "
                   f"{sum(1 for p in moved if p.b.gt_rank < p.a.gt_rank):>6}  "
                   f"{sum(1 for p in moved if p.b.gt_rank > p.a.gt_rank):>5}")
    return out


def _rank_delta_cell(p: Pair) -> str:
    """What happened to one case's rank: a signed step, or the found/lost event."""
    if p.a.gt_rank and p.b.gt_rank:
        return _delta(p.b.gt_rank - p.a.gt_rank)
    if p.b.gt_rank:
        return "ranked"
    if p.a.gt_rank:
        return "unranked"
    return "-"


def _rank_moves(pairs: list[Pair], limit: int | None) -> list[str]:
    """Every case whose ground-truth rank (or match tier) moved, biggest first.

    Ordered by how far phi travelled *relative* to where it was — 40 -> 2 is the
    move worth reading, 262 -> 270 is noise — with a rank gained or lost first,
    since those change the found count as well.
    """
    moved = [p for p in pairs
             if p.a.gt_rank != p.b.gt_rank or p.a.gt_match != p.b.gt_match]
    if not moved:
        return ["  no case's ground-truth rank or match tier moved."]
    ordered = sorted(
        moved,
        key=lambda p: (bool(p.a.gt_rank) != bool(p.b.gt_rank),
                       _relative(p.a.gt_rank, p.b.gt_rank),
                       abs((p.b.gt_rank or 0) - (p.a.gt_rank or 0))),
        reverse=True)
    shown = ordered if limit is None else ordered[:limit]
    return _case_table_lines(
        shown,
        [("rank", [f"{_num(p.a.gt_rank)} -> {_num(p.b.gt_rank)}" for p in shown]),
         ("delta", [_rank_delta_cell(p) for p in shown]),
         ("of candidates", [f"{_num(p.a.candidates)} -> {_num(p.b.candidates)}"
                            for p in shown]),
         ("match", [f"{p.a.gt_match or '-'} -> {p.b.gt_match or '-'}"
                    for p in shown]),
         ("status", [_status_cell(p) for p in shown])],
        len(ordered), limit)


def _settings_lines(cmp: Comparison) -> list[str]:
    """The knobs that differed between the runs — the first thing to check when a
    comparison shows a big move, since a run under another candidate sort or
    threshold strategy is not the same experiment.

    A run written before ``settings.json`` existed records none; the line says so
    rather than pretending the knobs matched.
    """
    a, b = cmp.a_settings, cmp.b_settings
    if a is None and b is None:
        return ["  neither run recorded its settings (written before settings.json)."]
    if a is None or b is None:
        missing = "A" if a is None else "B"
        return [f"  run {missing} recorded no settings.json — knobs not comparable."]
    keys = [k for k in ("candidate_sort", "refinement_mode", "threshold_strategy",
                        "instrument_source", "game", "rows", "dataset")
            if a.get(k) != b.get(k)]
    if not keys:
        return ["  identical knobs on both runs "
                f"({a.get('candidate_sort')} / {a.get('refinement_mode')} / "
                f"{a.get('threshold_strategy')})."]
    width = max(len(k) for k in keys)
    return ([f"  {k:<{width}}  {a.get(k)!r} -> {b.get(k)!r}" for k in keys]
            + ["  a knob that differs is a second explanation for every change below."])


def _unmatched_lines(rows: list[Row], label: str) -> list[str]:
    if not rows:
        return []
    out = [f"  only in {label} ({len(rows)}):"]
    for r in sorted(rows, key=lambda r: (r.case is None, r.case or 0, r.branch)):
        out.append(f"    row {'-' if r.case is None else r.case:>3}  "
                   f"{r.game}  {r.branch}  [{r.status}]")
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _verdict(cmp: Comparison) -> list[str]:
    """The headline: the SUCCESS count on each side, how the change is made up, and
    the one-word reading of it."""
    pairs = cmp.pairs
    a_n = sum(1 for p in pairs if p.a.status == GOAL)
    b_n = sum(1 for p in pairs if p.b.status == GOAL)
    kinds = Counter(p.kind for p in pairs)
    net = b_n - a_n
    word = ("IMPROVED" if net > 0 else "REGRESSED" if net < 0
            else "UNCHANGED" if not cmp.changes else "EVEN (same count, "
            "different rows)")
    errors = sum(1 for p in pairs if p.b.status == "ERROR" and p.a.status != "ERROR")
    out = [
        f"  SUCCESS   {a_n} -> {b_n}   ({_delta(net)})   "
        f"{_pct(a_n, len(pairs))} -> {_pct(b_n, len(pairs))} of "
        f"{len(pairs)} compared case(s)",
        f"  gained {kinds.get('gained', 0)}   lost {kinds.get('lost', 0)}   "
        f"other changes {kinds.get('other', 0)}   unchanged {kinds.get('same', 0)}",
        "",
        f"  => {word}",
    ]
    if errors:
        out.append(f"  note: {errors} case(s) newly ERROR — the experiment broke on "
                   f"them, which is a run failure, not a result.")
    return out


def render(cmp: Comparison, limit: int | None = MOVES_SHOWN,
           top_k: tuple[int, ...] = TOP_K) -> str:
    """The whole comparison as text."""
    lines = ["=" * 78, "GameRV run comparison", "=" * 78,
             f"A (before):  {cmp.a_dir}   {len(cmp.a_rows)} row(s)",
             f"B (after):   {cmp.b_dir}   {len(cmp.b_rows)} row(s)",
             f"compared:    {len(cmp.pairs)} case(s) in both   "
             f"(A only {len(cmp.a_only)}, B only {len(cmp.b_only)})",
             f"generated:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    if not cmp.pairs:
        lines += ["", "The two runs share no case (nothing to compare)."]
        lines += _section("NOT COMPARED",
                          _unmatched_lines(cmp.a_only, "A")
                          + _unmatched_lines(cmp.b_only, "B"))
        return "\n".join(lines + [""])

    renumbered = [p for p in cmp.pairs if p.renumbered]
    if renumbered:
        lines.append(f"note:        {len(renumbered)} case(s) changed dataset row "
                     f"number between the runs (matched on game+branch; B's number "
                     f"is shown)")

    lines += _section("SETTINGS", _settings_lines(cmp))
    lines += _section("VERDICT", _verdict(cmp))

    lines += _section("CHANGES", [f"  {len(cmp.changes)} case(s) changed status."])
    lines += ["", "  gained (now SUCCESS):"]
    lines += _change_lines(cmp.of_kind("gained"))
    lines += ["", "  lost (no longer SUCCESS):"]
    lines += _change_lines(cmp.of_kind("lost"))
    lines += ["", "  other changes (neither side SUCCESS):"]
    lines += _change_lines(cmp.of_kind("other"), annotate=True)
    lines += ["",
              "  closer/further rank the two statuses on how much they settled",
              "  (ERROR < NEEDS_MANUAL < SKIPPED < INCONCLUSIVE < VARIABLE_NOT_FOUND",
              "  < FAIL < REFINEMENT_NOT_FOUND < SUCCESS) — a reading aid, not a",
              "  second success criterion."]

    lines += _section("CLASSIFICATIONS", _classification_table(cmp.pairs))
    if len({p.game for p in cmp.pairs}) > 1:
        lines += _section("BY GAME", _group_table(by_game(cmp.pairs), "game"))
    lines += _section("BY PARENT CATEGORY",
                      _group_table(by_parent(cmp.pairs), "parent category"))
    lines += _section("BY BUG CATEGORY",
                      _group_table(by_category(cmp.pairs), "bug category"))

    lines += _section("MINING", _mining_summary(cmp.pairs))
    lines += ["", "  candidate-set moves (biggest first):"]
    lines += _moves_table(cmp.pairs, limit)

    lines += _section("GROUND-TRUTH RANK", _rank_summary(cmp.pairs, top_k))
    # The group cuts and the legend are only written when there is a rank
    # distribution to cut: a comparison of rows that never mined would otherwise
    # print three empty tables and a paragraph explaining them.
    if any(p.a.mined and p.b.mined for p in cmp.pairs):
        for head, groups in ((("game", by_game(cmp.pairs)) if
                              len({p.game for p in cmp.pairs}) > 1 else (None, None)),
                             ("parent category", by_parent(cmp.pairs))):
            table = _rank_by_game(cmp.pairs, head, groups) if head else []
            if table:
                lines += ["", f"  by {head}:"] + table
        lines += ["", "  rank moves (biggest first):"]
        lines += _rank_moves(cmp.pairs, limit)
        lines += ["",
                  "  ranked = phi is at a known position among the top-scored mined",
                  "  candidates; a phi established only by its own sound core region",
                  "  is found but unranked, and counts 0 in the reciprocal rank. The",
                  "  rank is a position in the order --candidate-sort chose, so a run",
                  "  under another sort ranks the same candidate set differently",
                  "  (SETTINGS above says whether that is the case here)."]

    unmatched = _unmatched_lines(cmp.a_only, "A") + _unmatched_lines(cmp.b_only, "B")
    if unmatched:
        lines += _section("NOT COMPARED",
                          ["  cases only one run holds — left out of every number "
                           "above."] + unmatched)
    return "\n".join(lines + [""])


CSV_COLUMNS = ("row", "game", "branch", "change", "status_a", "status_b",
               "candidates_a", "candidates_b", "gt_rank_a", "gt_rank_b",
               "rank_delta", "gt_match_a", "gt_match_b", "fault_type",
               "notes_a", "notes_b")


def write_csv(cmp: Comparison, path: Path | str) -> Path:
    """The per-case comparison as a CSV — one row per compared case, so a spread
    sheet (or another script) can slice it the way this report does not."""
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for p in cmp.pairs:
            w.writerow([p.case or "", p.game, p.branch, p.kind,
                        p.a.status, p.b.status,
                        _cell(p.a.candidates), _cell(p.b.candidates),
                        _cell(p.a.gt_rank), _cell(p.b.gt_rank),
                        _rank_delta(p),
                        p.a.gt_match, p.b.gt_match, p.fault_type,
                        p.a.notes, p.b.notes])
    return path


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
def _run_dir(arg: str) -> Path:
    """A results folder, given as a path or as a bare run name (``run_2026...``,
    resolved under ``results/``)."""
    for path in (Path(arg), config.RESULTS_DIR / arg):
        if (path / report.FULL_CSV).is_file():
            return path
        if path.is_dir():
            raise SystemExit(f"{path} holds no {report.FULL_CSV} "
                             f"(not a finished run folder)")
    raise SystemExit(f"not a results folder: {arg}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="A is the before run, B the after run; every reading is A -> B.")
    ap.add_argument("before", help="the earlier results/run_<timestamp>/ folder (A)")
    ap.add_argument("after", help="the later results/run_<timestamp>/ folder (B)")
    ap.add_argument("--game", help="compare only this game's cases")
    ap.add_argument("--all", action="store_true",
                    help="list every candidate-set and rank move, not just the "
                         f"biggest {MOVES_SHOWN}")
    ap.add_argument("--top", default=",".join(str(k) for k in TOP_K),
                    help="the rank cut-offs to count phi at, comma-separated "
                         f"(default {','.join(str(k) for k in TOP_K)})")
    ap.add_argument("--out", help="also write the comparison to this file")
    ap.add_argument("--csv", help="write the per-case comparison table here")
    ap.add_argument("--quiet", action="store_true",
                    help="do not print the comparison (with --out/--csv)")
    args = ap.parse_args(argv)

    try:
        top_k = tuple(sorted({int(k) for k in args.top.split(",") if k.strip()}))
    except ValueError:
        ap.error(f"--top takes comma-separated integers, not {args.top!r}")
    if not top_k or top_k[0] < 1:
        ap.error("--top takes positive rank cut-offs")

    cmp = compare(_run_dir(args.before), _run_dir(args.after), args.game)
    text = render(cmp, None if args.all else MOVES_SHOWN, top_k)
    if not args.quiet:
        print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    if args.csv:
        print(f"wrote {write_csv(cmp, args.csv)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
