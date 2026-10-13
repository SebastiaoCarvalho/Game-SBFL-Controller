#!/usr/bin/env python
"""Textual results report for one run: what the rows classified as, cut by game and
by the benchmark's bug categories.

The run's stdout summary is the aggregate view a results section wants; this is
that same view *plus* the per-case detail, written to ``<run_dir>/report.txt``
beside the CSVs:

  classifications   how many rows landed on each status, with its share of the run.
  by game           the same counts per game — one line each, to compare them.
  per-game          then the *whole* analysis again for each game on its own rows:
                    its classifications, its two category cuts and its mining
                    numbers, with no other game in the denominators. This is the
                    division a per-game results table is written from; it is
                    skipped for a single-game run, where it would only repeat the
                    report.
  by parent category / by bug category
                    the same counts per ``Fault Type`` (twelve categories) and per
                    parent — "Non-temporal fault" for a fault of a single state,
                    "Temporal fault" for one of a state *sequence* (see
                    ``dataset.FAULT_TYPES``). This is the cut that says whether the
                    method is weaker on temporal faults than on non-temporal ones,
                    which no per-row table shows.
  mining            how often the ground truth was found among the *top-scored*
                    mined candidates, how big that candidate set was, and where the
                    ground truth ranked inside it — overall, per game and per
                    category. Lower-scoring candidates are in gamerv's log but not
                    in these numbers (see metrics.py).
  by case           one line per dataset case (one row of the dataset): its verdict,
                    its mining numbers and the note the pipeline left, followed by
                    the per-case timing table. This is the per-case detail the run's
                    stdout deliberately no longer prints — the terminal keeps the
                    aggregate view, this file keeps everything.

Written after the CSVs, **from** the CSVs (``full.csv``), so the report can never
disagree with the archival record and the same code re-renders an old run:

    python src/report.py results/run_20260901_223129
    python src/report.py results/run_20260901_223129 --out /tmp/report.txt

``run_experiment`` writes it and prints only the aggregate tables
(:func:`terminal_lines`: per game, per parent category, per bug category) under the
run summary — the same aggregation, so the two cannot drift, and the per-case lines
live in this file alone.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import dataset
import metrics

FULL_CSV = "full.csv"
REPORT_TXT = "report.txt"
#: The knobs a run was produced under (run_experiment._settings). Absent for runs
#: written before it existed.
SETTINGS_JSON = "settings.json"

#: Every status a row can end on, in the order a table reads best: decided rows
#: first, then the ones the method could not decide, then the run's own failures.
STATUSES = ("SUCCESS", "FAIL", "CONDITION_NOT_FOUND", "VARIABLE_NOT_FOUND",
            "INCONCLUSIVE", "SKIPPED", "NEEDS_MANUAL", "ERROR")

#: Short column heads for the per-group tables (a full status word is 19 chars).
ABBREV = {"SUCCESS": "SUCC", "FAIL": "FAIL", "CONDITION_NOT_FOUND": "C_NF",
          "VARIABLE_NOT_FOUND": "V_NF", "INCONCLUSIVE": "INCO", "SKIPPED": "SKIP",
          "NEEDS_MANUAL": "MANU", "ERROR": "ERR"}

#: One line per status, so a reader who has not read the README can still read the
#: table. Kept in sync with the README's "Result statuses" section.
MEANING = {
    "SUCCESS": "phi ties the top Ochiai score and a single top-ranked bug "
               "condition contains it (phi -> B)",
    "FAIL": "phi was decided and rejected (outranked, vacuous, or no candidate "
            "contains it)",
    "CONDITION_NOT_FOUND": "phi scores as high as the best candidate (or higher) "
                           "but no single top-ranked B contains it — the search "
                           "missed it",
    "VARIABLE_NOT_FOUND": "phi reads a quantity the game never exports, so it was "
                          "never evaluated",
    "INCONCLUSIVE": "nothing to decide phi against (no trace / no violation / no "
                    "candidate)",
    "SKIPPED": "the row carries no ground-truth phi (mined only)",
    "NEEDS_MANUAL": "left for a human: a manual predicate no hint covers, and the "
                    "run was --skip-manual (re-run the row interactively)",
    "ERROR": "the experiment itself broke on the row",
}


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Row:
    """One results row, as the report reads it out of ``full.csv``."""
    game: str
    branch: str
    status: str
    phi: str
    fault_type: str
    candidates: int | None
    gt_rank: int | None
    gt_match: str
    union_only: bool
    #: The dataset row this case came from (1-based), None for a run written before
    #: the column existed — the per-case table then falls back to branch order.
    case: int | None = None
    notes: str = ""              # the note the pipeline left on the case
    elapsed_s: float | None = None       # wall-clock seconds for the whole case
    #: ``((phase, seconds-as-text), ...)`` in pipeline order, '-' for a phase that
    #: did not run. Kept as a tuple so :class:`Row` stays hashable/frozen.
    phases: tuple[tuple[str, str], ...] = ()
    #: The size of the state the miner searched (metrics.py): every ``base.field``
    #: the mined states carry, and the ones that vary across them. None for a row
    #: that never mined, or a run written before the columns existed.
    variables: int | None = None
    varying: int | None = None
    #: The labelled examples the ranking was learned from (metrics.py): pooled
    #: trigger states where the spec held, and violation states. None as above.
    good_states: int | None = None
    bad_states: int | None = None
    #: ``((sort, rank), ...)`` over :data:`metrics.SORTS`, None where the row was not
    #: ranked under that sort or its ground truth was not found (metrics.py). A
    #: tuple, like ``phases``, so the row stays hashable.
    ranks: tuple[tuple[str, int | None], ...] = ()

    def rank_under(self, sort: str) -> int | None:
        return dict(self.ranks).get(sort)

    @property
    def fault_class(self) -> str:
        return dataset.fault_class(self.fault_type)

    @property
    def category(self) -> str:
        """The bug category to report the row under (never blank)."""
        return self.fault_type or dataset.UNCATEGORISED

    @property
    def mined(self) -> bool:
        """Did the row mine *and* carry a ground truth to look for? Only these rows
        can be found or missed, so they are the denominator of every mining rate."""
        return self.candidates is not None and bool(self.phi.strip())

    @property
    def found(self) -> bool:
        """Was the ground truth established (see metrics.py's match tiers)?"""
        return bool(self.gt_match)


def _int(cell: str | None) -> int | None:
    try:
        return int((cell or "").strip())
    except ValueError:
        return None


def _float(cell: str | None) -> float | None:
    try:
        return float((cell or "").strip())
    except ValueError:
        return None


#: full.csv's per-phase timing columns are named ``<phase>_s``; ``elapsed_s`` is the
#: whole case and is read separately.
_PHASE_SUFFIX = "_s"
_TOTAL_COLUMN = "elapsed_s"


def _phase_cells(d: dict) -> tuple[tuple[str, str], ...]:
    """The ``<phase>_s`` columns of one CSV row, in the file's own column order (which
    is the pipeline's phase order), so the report never hard-codes a phase list."""
    out = []
    for k, v in d.items():
        if k and k.endswith(_PHASE_SUFFIX) and k != _TOTAL_COLUMN:
            name = k[: -len(_PHASE_SUFFIX)]
            out.append((name, (v or "").strip() or "-"))
    return tuple(out)


def row_from_dict(d: dict) -> Row:
    def cell(key: str) -> str:
        return (d.get(key) or "").strip()

    return Row(game=cell("game"), branch=cell("row_branch") or cell("github_branch"),
               status=cell("status"),
               phi=cell("phi") or cell("ground_truth"),
               fault_type=cell("fault_type"),
               candidates=_int(d.get("candidates")), gt_rank=_int(d.get("gt_rank")),
               gt_match=cell("gt_match"),
               union_only=cell("union_only").lower() in ("yes", "true", "1"),
               case=_int(d.get("row")), notes=cell("notes"),
               elapsed_s=_float(d.get(_TOTAL_COLUMN)), phases=_phase_cells(d),
               variables=_int(d.get("variables")),
               varying=_int(d.get("varying_variables")),
               good_states=_int(d.get("good_states")),
               bad_states=_int(d.get("bad_states")),
               ranks=tuple((s, _int(d.get(c)))
                           for s, c in zip(metrics.SORTS, metrics.RANK_COLUMNS)))


def row_from_result(res) -> Row:
    """A :class:`Row` straight from a pipeline ``CaseResult``, so the run's stdout
    summary and its ``report.txt`` aggregate the identical numbers with no CSV
    round-trip between them."""
    return Row(game=res.game, branch=res.branch, status=res.status, phi=res.phi,
               fault_type=res.fault_type, candidates=res.mining.candidates,
               gt_rank=res.mining.gt_rank, gt_match=res.mining.gt_match,
               union_only=res.mining.union_only,
               case=getattr(res, "row", None) or None, notes=res.notes,
               elapsed_s=res.elapsed_s,
               phases=tuple(res.phase_cells().items()),
               variables=res.mining.variables, varying=res.mining.varying,
               good_states=res.mining.good_states, bad_states=res.mining.bad_states,
               ranks=tuple((s, res.mining.ranks.get(s)) for s in metrics.SORTS))


def rows_from_results(results) -> list[Row]:
    return [row_from_result(r) for r in results]


def read_rows(run_dir: Path | str) -> list[Row]:
    """The rows of a run's ``full.csv``."""
    with open(Path(run_dir) / FULL_CSV, newline="", encoding="utf-8") as f:
        return [row_from_dict(d) for d in csv.DictReader(f)]


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------
def _group(rows: list[Row], key, order=None) -> list[tuple[str, list[Row]]]:
    """``[(label, rows)]`` grouped by ``key``, ordered by ``order`` (a sort key on the
    label) or alphabetically. Empty groups are never invented: only labels the run
    actually produced appear, so a single-game run does not print five empty rows."""
    groups: dict[str, list[Row]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    labels = sorted(groups, key=order) if order else sorted(groups)
    return [(label, groups[label]) for label in labels]


def by_game(rows: list[Row]) -> list[tuple[str, list[Row]]]:
    return _group(rows, lambda r: r.game or "?")


def by_parent(rows: list[Row]) -> list[tuple[str, list[Row]]]:
    #: Parents in taxonomy order (non-temporal, temporal), uncategorised last.
    rank = {dataset.NON_TEMPORAL: 0, dataset.TEMPORAL: 1}
    return _group(rows, lambda r: r.fault_class,
                  order=lambda label: (rank.get(label, 2), label))


def by_category(rows: list[Row]) -> list[tuple[str, list[Row]]]:
    #: Categories in dataset order, so the table reads like the dataset itself.
    return _group(rows, lambda r: r.category,
                  order=lambda label: (dataset.fault_order(label), label))


def counts(rows: list[Row]) -> dict[str, int]:
    """Rows per status, including any status not in :data:`STATUSES` (a new verdict
    word from gamerv is reported rather than silently dropped from the totals)."""
    out: dict[str, int] = {}
    for r in rows:
        out[r.status or "?"] = out.get(r.status or "?", 0) + 1
    return out


def statuses_present(rows: list[Row]) -> list[str]:
    """The statuses to give a column, known ones first in their canonical order."""
    seen = counts(rows)
    known = [s for s in STATUSES if s in seen]
    return known + sorted(s for s in seen if s not in STATUSES)


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------
def _pct(n: int, total: int) -> str:
    return f"{100.0 * n / total:.1f}%" if total else "-"


def _classification_table(rows: list[Row]) -> list[str]:
    """The whole run's status counts, with a share bar."""
    seen = counts(rows)
    total = len(rows)
    width = max((len(s) for s in seen), default=6)
    out = [f"  {'status':<{width}}  {'rows':>5}  {'share':>7}"]
    for status in statuses_present(rows):
        n = seen[status]
        bar = "#" * round(30.0 * n / total) if total else ""
        out.append(f"  {status:<{width}}  {n:>5}  {_pct(n, total):>7}  {bar}")
    out.append(f"  {'-' * (width + 16)}")
    out.append(f"  {'total':<{width}}  {total:>5}  {_pct(total, total):>7}")
    return out


def _status_table(groups: list[tuple[str, list[Row]]], head: str,
                  rows: list[Row]) -> list[str]:
    """One line per group: row count, a column per status present in the run, and the
    success rate — the number the group comparison is actually about."""
    statuses = statuses_present(rows)
    label_w = max([len(head)] + [len(label) for label, _ in groups])
    cols = [ABBREV.get(s, s[:4]) for s in statuses]
    out = [f"  {head:<{label_w}}  {'rows':>5}  "
           + "  ".join(f"{c:>5}" for c in cols) + f"  {'success':>8}"]
    for label, group in groups:
        seen = counts(group)
        cells = "  ".join(f"{seen.get(s, 0):>5}" for s in statuses)
        rate = _pct(seen.get("SUCCESS", 0), len(group))
        out.append(f"  {label:<{label_w}}  {len(group):>5}  {cells}  {rate:>8}")
    return out


def _mining_table(groups: list[tuple[str, list[Row]]], head: str) -> list[str]:
    """Per group: how many rows mined, how often the ground truth was found among the
    top-scored candidates, and the median candidate count / ground-truth rank of
    those rows."""
    label_w = max([len(head)] + [len(label) for label, _ in groups])
    out = [f"  {head:<{label_w}}  {'mined':>5}  {'found':>5}  {'found%':>7}  "
           f"{'union':>5}  {'med.cand':>8}  {'med.rank':>8}"]
    for label, group in groups:
        mined = [r for r in group if r.mined]
        found = [r for r in mined if r.found]
        ranks = [r.gt_rank for r in mined if r.gt_rank]
        cands = [r.candidates for r in mined]
        union = sum(1 for r in mined if r.union_only)
        med_c = f"{statistics.median(cands):g}" if cands else "-"
        med_r = f"{statistics.median(ranks):g}" if ranks else "-"
        out.append(f"  {label:<{label_w}}  {len(mined):>5}  {len(found):>5}  "
                   f"{_pct(len(found), len(mined)):>7}  {union:>5}  "
                   f"{med_c:>8}  {med_r:>8}")
    return out


def _mining_summary(rows: list[Row]) -> list[str]:
    """The run's mining numbers in prose: the found rate, the two ways a ground truth
    can be established, and the spread of both distributions the boxplot draws."""
    mined = [r for r in rows if r.mined]
    if not mined:
        return ["  no row both mined and carried a ground-truth phi."]
    found = [r for r in mined if r.found]
    ranked = [r for r in mined if r.gt_rank]
    exact = [r for r in found if r.gt_match == "exact"]
    union = [r for r in mined if r.union_only]
    cands = [r.candidates for r in mined]
    ranks = [r.gt_rank for r in ranked]
    out = [
        f"  rows that mined with a ground truth: {len(mined)}",
        f"  ground truth found:                  {len(found)}  "
        f"({_pct(len(found), len(mined))})",
        f"    at a known rank among the candidates: {len(ranked)}  "
        f"(spelled exactly as phi: {len(exact)})",
    ]
    out.append(
        f"  rejected by the single-candidate rule: {len(union)}  "
        f"(only the union of several candidates covers phi)")
    if cands:
        out.append(f"  candidates per row:  median {statistics.median(cands):g}   "
                   f"min {min(cands)}   max {max(cands)}")
    if ranks:
        out.append(f"  ground-truth rank:   median {statistics.median(ranks):g}   "
                   f"min {min(ranks)}   max {max(ranks)}   "
                   f"(over the {len(ranks)} ranked row(s))")
    # The state's size does not depend on a ground truth, so it is taken over every
    # row that pooled states (the space figure's rows), not only the mined ones.
    sized = [r for r in rows if r.variables is not None and r.varying is not None]
    if sized:
        logged = [r.variables for r in sized]
        varying = [r.varying for r in sized]
        out.append(f"  variables per row:   median {statistics.median(logged):g} "
                   f"logged, {statistics.median(varying):g} varying   "
                   f"max {max(logged)} / {max(varying)}   "
                   f"(over the {len(sized)} row(s) that pooled states)")
    return out


def ranked_sorts(rows: list[Row]) -> list[str]:
    """The candidate sorts this run recorded a rank under, in column order."""
    return [s for s in metrics.SORTS if any(r.rank_under(s) is not None for r in rows)]


def sort_ranked(rows: list[Row], sorts: list[str]) -> list[Row]:
    """The rows ranked under every one of ``sorts`` — the only rows on which two
    orderings can be compared, since each is a position in the same tier."""
    return [r for r in rows if r.candidates is not None
            and all(r.rank_under(s) is not None for s in sorts)]


def sort_left_out(rows: list[Row], sorts: list[str]) -> tuple[int, int]:
    """Mined rows (with a ground truth) that :func:`sort_ranked` leaves out, as
    ``(not found, ranked under only some sorts)``. The first is the ordinary miss; the
    second should be zero — the order cannot decide *whether* a single candidate
    establishes phi — and a non-zero count is worth a look."""
    mined = [r for r in rows if r.candidates is not None and r.phi.strip()]
    some = [r for r in mined if any(r.rank_under(s) is not None for s in sorts)]
    partial = sum(1 for r in some if any(r.rank_under(s) is None for s in sorts))
    return len(mined) - len(some), partial


def sort_left_out_note(rows: list[Row], sorts: list[str]) -> str:
    """:func:`sort_left_out` as a caption clause (empty when nothing was left out)."""
    missed, partial = sort_left_out(rows, sorts)
    parts = ([f"{missed} mined row(s) left out (ground truth not found)"] if missed
             else [])
    parts += ([f"{partial} ranked under only some of the sorts"] if partial else [])
    return ", ".join(parts)


def sort_rank_lines(rows: list[Row]) -> list[str]:
    """Per game (after the pooled ``all``): the median candidate count and the median
    ground-truth rank under each sort the run ranked under, then how often each sort
    put the ground truth highest. Empty unless at least two sorts were ranked.

    Every sort orders the same top-scored tier, so a lower median is the whole
    comparison; the "ranked best" line says whether that median is a few big wins or
    a consistent edge.
    """
    sorts = ranked_sorts(rows)
    if len(sorts) < 2:
        return []
    both = sort_ranked(rows, sorts)
    games = by_game(both)
    groups = ([("all", both)] if len(games) > 1 else []) + games
    label_w = max([len("game")] + [len(g) for g, _ in groups])
    col_w = max(8, *(len(s) + 1 for s in sorts))
    out = [f"  {'game':<{label_w}}  {'rows':>5}  {'med.cand':>8}  "
           + "  ".join(f"{s:>{col_w}}" for s in sorts)]
    for label, group in groups:
        meds = [f"{statistics.median(r.rank_under(s) for r in group):g}"
                for s in sorts]
        cand = f"{statistics.median(r.candidates for r in group):g}"
        out.append(f"  {label:<{label_w}}  {len(group):>5}  {cand:>8}  "
                   + "  ".join(f"{m:>{col_w}}" for m in meds))
    best = {s: 0 for s in sorts}
    tied = 0
    for r in both:
        top = min(r.rank_under(s) for s in sorts)
        winners = [s for s in sorts if r.rank_under(s) == top]
        if len(winners) == 1:
            best[winners[0]] += 1
        else:
            tied += 1
    out.append("")
    out.append("  ranked best: " + "   ".join(f"{s} {n}" for s, n in best.items())
               + f"   tied {tied}   (of {len(both)} row(s) ranked under every sort)")
    note = sort_left_out_note(rows, sorts)
    if note:
        out.append(f"  {note}")
    return out


def _wrap(text: str, indent: str, width: int = 78) -> list[str]:
    """``text`` folded to ``width``, every line prefixed with ``indent`` (empty for
    an empty note, so a case with nothing to say costs no line)."""
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return []
    return [indent + line
            for line in textwrap.wrap(collapsed, max(20, width - len(indent)))]


def _case_order(rows: list[Row]) -> list[Row]:
    """The cases in dataset order — by their dataset row number, falling back to
    game/branch for a run whose CSV predates the ``row`` column."""
    return sorted(rows, key=lambda r: (r.case is None, r.case or 0,
                                       r.game, r.branch))


def _case_table(rows: list[Row]) -> list[str]:
    """One line per case: its dataset row, game, branch, verdict, step 8's mining
    numbers, and the wall clock — with the pipeline's note folded underneath it.

    This is the detail the run's stdout summary no longer prints. It lives here, at
    the bottom of the report, so the terminal can stay at the aggregate view without
    the per-case verdicts being lost anywhere but ``full.csv``.
    """
    ordered = _case_order(rows)
    gw = max([4] + [len(r.game or "?") for r in ordered])
    bw = max([6] + [len(r.branch or "?") for r in ordered])
    sw = max([6] + [len(r.status or "?") for r in ordered])
    mw = max([5] + [len(r.gt_match) for r in ordered])
    out = [f"  {'row':>3}  {'game':<{gw}}  {'branch':<{bw}}  {'status':<{sw}}  "
           f"{'cand':>5}  {'rank':>5}  {'match':<{mw}}  {'time':>8}"]
    for r in ordered:
        out.append(
            f"  {'-' if r.case is None else r.case:>3}  "
            f"{r.game or '?':<{gw}}  {r.branch or '?':<{bw}}  "
            f"{r.status or '?':<{sw}}  "
            f"{'-' if r.candidates is None else r.candidates:>5}  "
            f"{'-' if r.gt_rank is None else r.gt_rank:>5}  "
            f"{r.gt_match or '-':<{mw}}  "
            f"{'-' if r.elapsed_s is None else f'{r.elapsed_s:.1f}s':>8}")
        out += _wrap(r.notes, " " * 7)
    return out


def phase_names(rows: list[Row]) -> list[str]:
    """Every phase named by any case, in first-seen (pipeline) order."""
    names: list[str] = []
    for r in rows:
        for name, _ in r.phases:
            if name not in names:
                names.append(name)
    return names


def _timing_table(rows: list[Row]) -> list[str]:
    """Per case: total wall clock and the seconds each pipeline phase took, closed by
    the run's totals. Empty when the run recorded no phase timings at all (an older
    CSV), so the section is then simply not written."""
    phases = phase_names(rows)
    if not phases:
        return []
    ordered = _case_order(rows)
    bw = max([6] + [len(r.branch or "?") for r in ordered])
    pw = max([8] + [len(p) for p in phases])
    out = [f"  {'row':>3}  {'branch':<{bw}}  {'total':>9}  "
           + "  ".join(f"{p:>{pw}}" for p in phases)]
    totals = {p: 0.0 for p in phases}
    grand = 0.0
    for r in ordered:
        cells = dict(r.phases)
        for p in phases:
            totals[p] += _float(cells.get(p)) or 0.0
        grand += r.elapsed_s or 0.0
        out.append(f"  {'-' if r.case is None else r.case:>3}  "
                   f"{r.branch or '?':<{bw}}  "
                   f"{'-' if r.elapsed_s is None else f'{r.elapsed_s:.1f}s':>9}  "
                   + "  ".join(f"{cells.get(p, '-'):>{pw}}" for p in phases))
    out.append(f"  {'-' * (bw + 16 + (pw + 2) * len(phases))}")
    out.append(f"  {'all':>3}  {f'{len(ordered)} case(s)':<{bw}}  "
               f"{f'{grand:.1f}s':>9}  "
               + "  ".join(f"{totals[p]:>{pw}.1f}" for p in phases))
    out.append(f"       {'':<{bw}}  {f'({grand / 60:.1f} min)':>9}")
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _section(title: str, body: list[str]) -> list[str]:
    return ["", title, "-" * len(title)] + body


def _indent(lines: list[str], n: int = 2) -> list[str]:
    """Push a table one level in, so it reads as part of the block above it."""
    return [" " * n + line if line.strip() else line for line in lines]


def _game_sections(rows: list[Row]) -> list[str]:
    """One section per game: that game's classifications, its two category cuts and
    its mining numbers, over its own rows alone.

    The aggregate tables give each game a single line, which compares the games but
    says nothing about *where* one of them failed. This repeats the analysis inside
    each game, so a per-game results table (and the paragraph that reads it) has the
    categories and the mining rates for that game with no other game's rows in the
    denominators.

    Empty for a single-game run: every table would be the report's own.
    """
    groups = by_game(rows)
    if len(groups) < 2:
        return []
    out: list[str] = []
    for game, group in groups:
        succ = counts(group).get("SUCCESS", 0)
        out += _section(f"GAME — {game}",
                        [f"  {len(group)} rows   SUCCESS {succ}  "
                         f"({_pct(succ, len(group))})"])
        out += ["", "  classifications:"]
        out += _indent(_classification_table(group))
        out += ["", "  by parent category:"]
        out += _indent(_status_table(by_parent(group), "parent category", group))
        out += ["", "  by bug category:"]
        out += _indent(_status_table(by_category(group), "bug category", group))
        out += ["", "  mining:"]
        out += _indent(_mining_summary(group))
        out += ["", "    by parent category:"]
        out += _indent(_mining_table(by_parent(group), "parent category"), 4)
        out += ["", "    by bug category:"]
        out += _indent(_mining_table(by_category(group), "bug category"), 4)
    return out


def _settings_line(settings: dict | None) -> list[str]:
    """The run's knobs as one header line, or nothing when they were not recorded.

    Two runs over the same rows under different knobs produce reports that are
    otherwise identical, and the candidate sort exists to be compared across runs —
    so the report has to say which one produced these numbers. A run written before
    ``settings.json`` existed simply has no such line (rather than a made-up one).
    """
    if not settings:
        return []
    def fmt(v) -> str:
        return ",".join(map(str, v)) if isinstance(v, list) else str(v)

    # rank_sorts only says something when it names more than the row's own sort.
    shown = {k: settings.get(k) for k in ("objective", "candidate_sort", "rank_sorts")}
    if len(shown.get("rank_sorts") or ()) < 2:
        shown["rank_sorts"] = None
    parts = [f"{k}={fmt(v)}" for k, v in shown.items() if v]
    return [f"settings: {'   '.join(parts)}"] if parts else []


def render(rows: list[Row], title: str = "", settings: dict | None = None) -> str:
    """The whole report as text."""
    games = sorted({r.game for r in rows if r.game})
    head = f"GameRV experiment report{f' — {title}' if title else ''}"
    lines = ["=" * 78, head, "=" * 78,
             f"rows: {len(rows)}   games: {len(games)}"
             f"{'  (' + ', '.join(games) + ')' if games else ''}",
             f"generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             *_settings_line(settings)]

    lines += _section("CLASSIFICATIONS", _classification_table(rows))
    lines += ["", "  status meanings:"]
    lines += [f"    {ABBREV.get(s, s):<4} {s:<21} {MEANING[s]}"
              for s in statuses_present(rows) if s in MEANING]

    if len(games) > 1:
        lines += _section("BY GAME", _status_table(by_game(rows), "game", rows))
    lines += _section("BY PARENT CATEGORY",
                      _status_table(by_parent(rows), "parent category", rows))
    lines += _section("BY BUG CATEGORY",
                      _status_table(by_category(rows), "bug category", rows))
    lines += _section("MINING", _mining_summary(rows))
    if len(games) > 1:
        lines += ["", "  by game:"]
        lines += _mining_table(by_game(rows), "game")
    lines += ["", "  by parent category:"]
    lines += _mining_table(by_parent(rows), "parent category")
    lines += ["", "  by bug category:"]
    lines += _mining_table(by_category(rows), "bug category")
    lines += ["",
              "  mined  = rows that reached mining and carry a ground-truth phi",
              "  found  = a single top-ranked bug condition contains phi",
              "  union  = only the union of several candidates covers phi, so the",
              "           ground truth counts as not found (see metrics.py)",
              "  cand   = the top-ranked tier of the ranked candidate set (the",
              "           bug conditions a user reads from the top); a phi established",
              "           only below that tier counts as not found too"]

    by_sort = sort_rank_lines(rows)
    if by_sort:
        lines += _section(
            "RANK BY CANDIDATE SORT",
            ["  the same top-scored candidates, read in each sort's order; medians",
             "  over the rows whose ground truth was ranked under every sort.", ""]
            + by_sort)

    # Then the same analysis per game, so each game's own numbers can be read
    # (and quoted) without the rest of the run diluting them.
    per_game = _game_sections(rows)
    if per_game:
        lines += _section(
            "PER-GAME BREAKDOWN",
            ["  the cuts above, repeated over one game's rows at a time."])
        lines += per_game

    # The per-case detail last: the aggregate cuts above are what a results section
    # reads, this is what a single row's verdict has to be looked up in.
    lines += _section("BY CASE", _case_table(rows))
    timings = _timing_table(rows)
    if timings:
        lines += _section("BY CASE — TIMINGS", timings)
    lines += [""]
    return "\n".join(lines)


def terminal_lines(rows: list[Row]) -> list[str]:
    """The compact group analysis for the run's stdout summary: one line per game
    (when the run covers more than one), then the two parent categories, then one
    line per bug category — each with its success rate and how often the ground
    truth was found among the mined candidates.

    Deliberately group lines only: the per-case verdicts go to ``report.txt``
    (:func:`_case_table`), so a 120-row run's terminal output stays readable."""
    def line(label: str, group: list[Row], width: int) -> str:
        seen = counts(group)
        mined = [r for r in group if r.mined]
        found = [r for r in mined if r.found]
        return (f"    {label:<{width}}  {len(group):>3} rows  "
                f"SUCCESS {seen.get('SUCCESS', 0):>3} "
                f"({_pct(seen.get('SUCCESS', 0), len(group)):>6})"
                f"  found {len(found):>3}/{len(mined)}")

    games, parents, cats = by_game(rows), by_parent(rows), by_category(rows)
    width = max([len(l) for l, _ in games + parents + cats] + [10])
    out = []
    if len(games) > 1:
        out += ["  by game:"] + [line(l, g, width) for l, g in games]
    out += ["  by parent category:"] + [line(l, g, width) for l, g in parents]
    out += ["  by bug category:"] + [line(l, g, width) for l, g in cats]
    return out


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
def read_settings(run_dir: Path | str) -> dict | None:
    """The knobs a run recorded (``settings.json``), or ``None`` for a run written
    before it existed. Read here rather than passed in, so regenerating a past run's
    report (``python src/report.py <run_dir>``) still carries them."""
    try:
        return json.loads((Path(run_dir) / SETTINGS_JSON).read_text())
    except (OSError, ValueError):
        return None


def write(run_dir: Path | str, out: Path | str | None = None) -> Path | None:
    """Write ``report.txt`` for ``run_dir``; return its path, or None on failure.

    Never raises: a run that produced results is not spoiled by an unreadable CSV or
    an unwritable path — the failure is printed and the batch keeps its CSVs.
    """
    run_dir = Path(run_dir)
    target = Path(out) if out else run_dir / REPORT_TXT
    try:
        rows = read_rows(run_dir)
        target.write_text(render(rows, run_dir.name, read_settings(run_dir)),
                          encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - an extra artefact, never a run failure
        print(f"    report skipped ({type(exc).__name__}: {exc})")
        return None
    return target


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", help="a results/run_<timestamp>/ folder")
    ap.add_argument("--out", help=f"output file (default <run_dir>/{REPORT_TXT})")
    ap.add_argument("--print", dest="to_stdout", action="store_true",
                    help="also print the report")
    args = ap.parse_args(argv)
    path = write(args.run_dir, args.out)
    if path is None:
        return 1
    if args.to_stdout:
        print(path.read_text(encoding="utf-8"))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
