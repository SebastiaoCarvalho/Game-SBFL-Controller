#!/usr/bin/env python
"""Plots for the evaluation step: what the rows classified as, how long they took,
how big the searched state was, how big the mined candidate set was, and how deep in
it the ground truth sat.

Step 8 records, per row, how many **top-ranked** candidate bug conditions the miner
produced and where the ground-truth phi ranked among them (``metrics.Mining``);
``run_experiment`` writes both columns into the run's ``mining.csv``. Both quantities
are over the top-ranked tier of gamerv's Ochiai ranking only — the candidates a user
actually reads before reaching the fix; the lower-scoring candidates are kept in
``bug_conditions.log`` and deliberately excluded here, since a count that mixed score
tiers would answer no question about the method (see metrics.py). The same record
carries the size of the state the miner searched (``variables`` /
``varying_variables``), and ``full.csv`` carries every row's status, bug category and
per-phase wall clock. This module turns them into the figures a results section wants:

  success_by_fault_type.png
                        the **results per bug category**: one horizontal bar per
                        ``Fault Type``, split into success / fail / not mined /
                        undecided / not evaluated, with the success rate at the
                        bar's end. Grouped
                        under their parent (non-temporal, temporal) so the two blocks
                        compare at a glance.
  success_by_parent.png the same split for the two parent categories alone — the cut
                        that says whether the method is weaker on temporal faults.
  success_by_game.png   the same split per game, with the run's pooled success rate in
                        the subtitle (a pooled bar would be five times as long as the
                        rest and squeeze them).
  time.png              **time** of the method's own phases: small multiples, one
                        panel for validate + localize together and one for each, a box
                        per game. Build and record are left out — they cost what the
                        game and its recorded data cost, not what the method does.
                        Each panel has its own axis; a shared one would flatten the
                        faster phase onto its baseline.
  space.png             **space**: per game, the number of variables in the states the
                        miner searched beside the ones that actually vary there (the
                        vocabulary it builds atoms over) — what the game logs vs. what
                        the search works with.
  time_vs_space.png     the localize phase's wall clock against the varying-variable
                        count, one panel per game with the other games' rows drawn in
                        gray behind it: does the analysis cost grow with the state?
  candidates.png        a **boxplot of the candidate-set size**, one box per game,
                        with every row drawn as a point beside its box so the shape of
                        the distribution (and the handful of very large searches) is
                        visible rather than smoothed into five summary numbers. It
                        covers every row that mined, which the next figure does not.
  ranks_by_sort.png     the same comparison for a run ranked under **several candidate
                        sorts** (``--candidate-sort all``): per game, one box of
                        candidate counts — the sort only permutes the tier, so the set
                        is the same under every sort — then one box of ground-truth
                        ranks per sort, over the same rows. Drawn instead of the next
                        figure, which would show only the run's own sort.
  candidates_vs_rank.png
                        the **comparison** that says whether a large candidate set is
                        actually a problem: per game, the candidate count and the
                        ground truth's rank side by side, over the same rows. A run
                        that mines 100 candidates but ranks the ground truth 3rd is a
                        useful result; the same 100 with the truth at 97 is not, and
                        only the paired view separates the two. Both quantities are
                        positions in the same ranked list (its size, and an index into
                        it), so they share one axis — log-scaled when the two spans are
                        too far apart to read linearly.
  states_vs_rank.png    the ground truth's **rank against the labelled states** the
                        ranking was learned from (good + bad per row), one box per
                        bin, with Spearman's rho over the unbinned counts: does the
                        truth rank deeper when a row has few examples? Over the rows
                        whose ground truth was found; the rank is the run's own
                        sort's.
  states_vs_rank_by_game.png
                        the same figure **per game**: small multiples, the pooled
                        ``all`` panel first, then one per game, on the same bins and
                        one shared rank axis, each panel with its own rho — does the
                        relation hold inside every game, or is it one game's rows?

Only rows that actually mined are plotted in the mining figures: a blank
``candidates`` cell means the row never reached the point of persisting a mining
record (see metrics.read), and counting it as zero would claim the miner came back
empty-handed. The rank figure further keeps only the rows whose ground truth *was*
found among the top-scored candidates (a blank ``gt_rank`` has no position to draw) —
the SUCCESS rows, since only a phi tying the top score is among them. The
space figures likewise skip a row with no variable count (it never pooled states, or
its run predates the columns). How many rows were dropped either way is printed in
each figure's subtitle, so a box is never read as covering more rows than it does.

Written after the CSVs, from the CSVs — so the same code re-plots an old run (a figure
whose columns the old run lacks is skipped with a note; a run older than the
good/bad state columns gets them from ``scripts/backfill_states.py``):

    python src/plots.py results/run_20260901_223129
    python src/plots.py results/run_20260901_223129 --figure ranks --out /tmp/r.png

Matplotlib lives in the ``gamerv`` conda env, not in the env the controller runs
under. When it cannot be imported here, ``render`` re-executes this file inside
that env (the same wrapper gamerv's own ops go through) rather than failing the
run — the plots are an extra artefact, never a reason for a finished batch to error.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import statistics
import subprocess
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import config
import metrics
import report

MINING_CSV = "mining.csv"
CANDIDATES_PNG = "candidates.png"
RANKS_PNG = "candidates_vs_rank.png"
FAULT_TYPES_PNG = "success_by_fault_type.png"
PARENTS_PNG = "success_by_parent.png"
GAMES_PNG = "success_by_game.png"
STATES_PNG = "states_vs_rank.png"
STATES_GAMES_PNG = "states_vs_rank_by_game.png"
TIME_PNG = "time.png"
SPACE_PNG = "space.png"
TIME_SPACE_PNG = "time_vs_space.png"
SORTS_PNG = "ranks_by_sort.png"

#: Set on the re-executed child so a missing matplotlib there fails loudly
#: instead of spawning another child.
_CHILD_ENV = "CONTROLLER_PLOTS_CHILD"

# --- Ink (see the data-viz palette: light surface, recessive chrome) --------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
#: Categorical slots 1 and 2 of the reference palette, unchanged (blue, orange) —
#: the pair is documented as passing every hard gate in light mode, so it needs no
#: re-validation. Slot 1 alone carries the single-series figure: there the games
#: label the x-axis, they are not separate series, so a categorical palette would
#: only add noise. Slot 2 joins it for the paired figure, where the two boxes *are*
#: two series and the legend names them.
ACCENT = "#2a78d6"
ACCENT_FILL = "#cde2fb"
ACCENT2 = "#eb6834"
ACCENT2_FILL = "#fbd9cc"
#: The two neutral outcomes of a success bar (undecided, not evaluated) and the
#: context points of the time-vs-space panels. Neutral on purpose: success and fail
#: are the result, these are what the method could not (or was not asked to) decide.
#: Stacked after aqua (a success bar's "not mined") they validate as an adjacent run
#: — blue, orange, aqua, gray, light gray: worst normal-vision Delta E 15.7, worst
#: CVD 8.0 (aqua vs gray, deutan, a hair under the target of 8 — legal here, the
#: segments are told apart by their 2px surface gaps and the legend too). The
#: lighter gray sits at 1.96:1 on the surface and aqua at 2.74:1, so identity rides
#: on the legend and the text report, never on the fill alone.
NEUTRAL = "#7f7e78"
NEUTRAL_LIGHT = "#b8b7ae"
#: Slots 3 and 4 (aqua, yellow), for the by-sort figure's third and fourth series;
#: aqua also fills a success bar's "not mined" segment (yellow cannot: beside the
#: fail segment's orange it is Delta E 13.7, under the normal-vision gate of 15).
#: Blue, orange, aqua, yellow is the reference palette's own order and validates as
#: an adjacent run (worst CVD Delta E 9.1, worst normal-vision 22.9). Both sit below
#: 3:1 on the surface, so every box there is also named in text under it.
ACCENT3 = "#1baf7a"
ACCENT3_FILL = "#c4ecdc"
ACCENT4 = "#eda100"
ACCENT4_FILL = "#fae3b0"

#: One point per row is jittered so ties stay countable; seeded, so re-plotting the
#: same run gives the same figure.
_JITTER_SEED = 0xC0FFEE

#: Label for the aggregate group a multi-game run gets, so the pooled distribution is
#: readable next to the per-game ones.
ALL = "all"


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MinedRow:
    """One row of ``mining.csv`` that reached mining."""
    game: str
    candidates: int
    gt_rank: int | None      # None when the ground truth was not found


def read_mining(run_dir: Path) -> tuple[list[MinedRow], int]:
    """``(rows that mined, dropped)`` from a run's ``mining.csv``.

    ``dropped`` counts the rows whose ``candidates`` cell is blank or unreadable —
    rows that never mined — which are excluded from every box.
    """
    path = Path(run_dir) / MINING_CSV
    rows: list[MinedRow] = []
    dropped = 0

    def _int(cell: str | None) -> int | None:
        try:
            return int((cell or "").strip())
        except ValueError:
            return None

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            n = _int(row.get("candidates"))
            if n is None:
                dropped += 1
                continue
            # Only a SUCCESS row has a rank — see report.row_from_dict for the
            # pre-2026-10-13 runs that also ranked an OUTRANKED phi.
            found = (row.get("status") or "").strip() == metrics.FOUND_STATUS
            rows.append(MinedRow((row.get("game") or "?").strip(), n,
                                 _int(row.get("gt_rank")) if found else None))
    return rows, dropped


@dataclass
class _Run:
    """One run's inputs, each read only when a figure asks for it — so a figure
    whose file an old run lacks is skipped without taking the others down."""
    dir: Path

    @cached_property
    def mining(self) -> tuple[list[MinedRow], int]:
        return read_mining(self.dir)

    @cached_property
    def rows(self) -> list[report.Row]:
        return report.read_rows(self.dir)

    @cached_property
    def sort_note(self) -> str:
        return _sort_note(self.dir)


def _by_median_candidates(groups: dict[str, list[MinedRow]]) -> list[str]:
    """Games ordered by median candidate count, largest search first."""
    return sorted(groups, key=lambda g: -statistics.median(
        [r.candidates for r in groups[g]]))


def _grouped(rows: list[MinedRow]) -> dict[str, list[MinedRow]]:
    groups: dict[str, list[MinedRow]] = {}
    for r in rows:
        groups.setdefault(r.game, []).append(r)
    return groups


#: The pipeline phases the method itself runs. Build (compile the mutant) and record
#: (replay its tapes) cost what the game and its recorded data cost, not what the
#: method does, so the time figure leaves them out.
METHOD_PHASES = ("validate", "localize")


def _seconds(row: report.Row, phases: str | tuple[str, ...]) -> float | None:
    """A row's wall clock summed over ``phases``, or None unless every one of them
    ran on it (a ``-`` cell): a case that stopped before localize would otherwise
    report its validation alone as the method's whole cost."""
    cells = dict(row.phases)
    total = 0.0
    for phase in (phases,) if isinstance(phases, str) else phases:
        try:
            total += float(cells.get(phase, "-"))
        except ValueError:
            return None
    return total


# --------------------------------------------------------------------------
# Shared figure furniture
# --------------------------------------------------------------------------
def _pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _new_axes(n_groups: int, width_per_group: float = 1.9, height: float = 4.8):
    """A figure sized to the group count, with the palette's surface and chrome.

    The floor keeps a two-line subtitle inside the canvas when a run covers a single
    game and the boxes alone would need barely any width.
    """
    fig, ax = _pyplot().subplots(
        figsize=(max(6.8, width_per_group * n_groups + 2.4), height), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    return fig, ax


def _new_panels(n: int, width: float, height: float, nrows: int = 1, **kw):
    """``n`` panels on the palette's surface (small multiples), filled left to right
    over ``nrows`` rows; a grid cell past the ``n``-th is hidden."""
    ncols = -(-n // nrows)
    fig, axes = _pyplot().subplots(nrows, ncols, figsize=(width, height), dpi=200,
                                   squeeze=False, **kw)
    fig.patch.set_facecolor(SURFACE)
    flat = [ax for row in axes for ax in row]
    for ax in flat[n:]:
        ax.set_visible(False)
    for ax in flat[:n]:
        ax.set_facecolor(SURFACE)
    return fig, flat[:n]


def _box_style(color: str, fill: str) -> dict:
    """Thin marks, one accent per series (see the data-viz mark specs)."""
    return dict(widths=0.42, showfliers=False,   # every point is drawn; no doubles
                medianprops=dict(color=color, linewidth=2),
                boxprops=dict(color=color, linewidth=1.2, facecolor=fill, alpha=0.55),
                whiskerprops=dict(color=color, linewidth=1.2),
                capprops=dict(color=color, linewidth=1.2),
                patch_artist=True)


def _scatter(ax, positions: list[float], series: list[list[float]], color: str,
             spread: float = 0.11, horizontal: bool = False) -> None:
    """Every row as its own point beside its box, with a surface ring so overlapping
    points stay countable. ``horizontal`` puts the values on x (boxes lying down)."""
    rng = random.Random(_JITTER_SEED)
    for p, vals in zip(positions, series):
        jitter = [p + rng.uniform(-spread, spread) for _ in vals]
        xs, ys = (vals, jitter) if horizontal else (jitter, vals)
        ax.scatter(xs, ys, s=26, color=color, alpha=0.75, zorder=3,
                   linewidths=0.8, edgecolors=SURFACE)


def _log_if_wide(ax, values: list[float], headroom: float, axis: str = "y") -> bool:
    """Log-scale the value axis when the values span more than ~50x, and leave
    headroom above the largest one (for the legend / the top median label). Returns
    whether the axis went log, so the label can say so — a log axis a reader mistakes
    for linear understates every large search by an order of magnitude.

    A zero keeps the axis linear: log cannot show it, and "no candidate at all" is a
    value the figure must not silently drop.
    """
    set_scale, get_lim, set_lim = (
        (ax.set_yscale, ax.get_ylim, ax.set_ylim) if axis == "y"
        else (ax.set_xscale, ax.get_xlim, ax.set_xlim))
    if values and min(values) > 0 and max(values) / min(values) >= 50:
        set_scale("log")
        lo, hi = get_lim()
        set_lim(lo, hi * headroom)
        return True
    set_lim(0, get_lim()[1] * 1.18)
    return False


def _style_axes(ax, value_axis: str = "y") -> None:
    """Recessive chrome: gridlines along the value axis only, no top/right spine."""
    cat_axis = "x" if value_axis == "y" else "y"
    ax.tick_params(axis=value_axis, labelsize=9, colors=INK_MUTED, length=0)
    ax.tick_params(axis=cat_axis, length=0)
    getattr(ax, f"{value_axis}axis").grid(True, color=GRID, linewidth=0.8)
    getattr(ax, f"{cat_axis}axis").grid(False)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
        ax.spines[side].set_linewidth(0.8)


def _chrome(ax, value_label: str, title: str, subtitle: str,
            value_axis: str = "y", lift: float = 0) -> None:
    """Axis label, title and subtitle. ``lift`` (points) raises the subtitle and title
    to leave a band above the axes, for a legend that must not sit on the data."""
    _style_axes(ax, value_axis)
    (ax.set_ylabel if value_axis == "y" else ax.set_xlabel)(
        value_label, fontsize=10, color=INK_SECONDARY)
    # The subtitle grows upward from the axes top, so the title's pad has to clear it:
    # a fixed pad sized for two lines is overrun the moment a third is added (the
    # sort note), and the title lands on top of the caption.
    lines = subtitle.count("\n") + 1
    ax.set_title(title, loc="left", pad=8 + 13 * lines + lift, fontsize=13, color=INK)
    ax.annotate(subtitle, xy=(0, 1), xycoords="axes fraction",
                xytext=(0, 8 + lift), textcoords="offset points", linespacing=1.5,
                fontsize=9.5, color=INK_MUTED, ha="left", va="bottom")


def _figure_heading(fig, title: str, subtitle: str) -> float:
    """Title and subtitle across the top of a multi-panel figure. Returns the height
    fraction left for the panels, to hand ``_save`` as the layout's top edge."""
    h = fig.get_figheight()
    lines = subtitle.count("\n") + 1
    fig.text(0.012, 1 - 0.14 / h, title, ha="left", va="top", fontsize=13, color=INK)
    fig.text(0.012, 1 - 0.46 / h, subtitle, ha="left", va="top", fontsize=9.5,
             color=INK_MUTED, linespacing=1.5)
    return 1 - (0.6 + 0.2 * lines) / h


def _save(fig, out_path: Path, top: float | None = None) -> Path:
    fig.tight_layout(rect=(0, 0, 1, top) if top else None)
    out_path = Path(out_path)
    fig.savefig(out_path, facecolor=SURFACE)
    _pyplot().close(fig)
    return out_path


def _legend(ax, entries: list[tuple[str, str, str]], **kw) -> None:
    """A swatch legend (``(label, edge, fill)`` per entry) in secondary ink."""
    import matplotlib.patches as mpatches
    kw.setdefault("loc", "upper right")
    ax.legend(handles=[mpatches.Patch(facecolor=fill, edgecolor=edge, label=label)
                       for label, edge, fill in entries],
              frameon=False, fontsize=9, labelcolor=INK_SECONDARY, **kw)


def _fmt_seconds(s: float) -> str:
    return f"{s:.1f} s" if s < 10 else f"{s:.0f} s"


# --------------------------------------------------------------------------
# Figure — success per bug category / per parent category
# --------------------------------------------------------------------------
#: The status a success bar calls "not mined": phi scores at the top of the ranking
#: (or above it), but no top-ranked candidate the miner produced contains it. phi
#: *was* decided, so it is not undecided; the data does not reject it, so it is not a
#: FAIL either — the predicate was simply not mined. Its own segment, beside fail:
#: either way the developer reading the ranking does not meet the ground truth.
NOT_MINED = "CONDITION_NOT_FOUND"

#: The statuses a success bar folds into "undecided": the method ran but could not
#: decide phi — it reads a quantity no state reports, or there was nothing to score
#: it against. Everything else that is neither SUCCESS, FAIL nor not mined (SKIPPED,
#: NEEDS_MANUAL, ERROR, an unknown verdict word) was never evaluated.
UNDECIDED = ("VARIABLE_NOT_FOUND", "INCONCLUSIVE")

#: The segments of a success bar, in stacking order: ``(label, fill)``.
OUTCOMES = (("success", ACCENT), ("fail", ACCENT2), ("not mined", ACCENT3),
            ("undecided", NEUTRAL), ("not evaluated", NEUTRAL_LIGHT))


def _outcome(status: str) -> str:
    if status == "SUCCESS":
        return "success"
    if status == "FAIL":
        return "fail"
    if status == NOT_MINED:
        return "not mined"
    return "undecided" if status in UNDECIDED else "not evaluated"


def _outcome_note(rows: list[report.Row]) -> str:
    """The subtitle clause naming which statuses each segment past fail holds — only
    the ones this run produced, in the report's own abbreviations (so "not mined"
    reads back to the C_NF of report.txt)."""
    parts = []
    for bucket in ("not mined", "undecided", "not evaluated"):
        seen = [report.ABBREV.get(s, s) for s in report.statuses_present(rows)
                if _outcome(s) == bucket]
        if seen:
            parts.append(f"{bucket} = {', '.join(seen)}")
    return " · ".join(parts)


def draw_outcomes(blocks: list[tuple[str | None, list[tuple[str, list[report.Row]]]]],
                  out_path: Path, title: str, subtitle: str) -> Path:
    """Horizontal stacked bars, one per group, split by :data:`OUTCOMES`.

    ``blocks`` is ``[(header, [(label, rows), ...])]``: each block's bars are drawn
    under its header (``None`` for no header), with a gap between blocks. A bar's
    length is its row count, so a small category is visibly small; the success rate
    it is about is written at its end.
    """
    n_bars = sum(len(groups) for _h, groups in blocks)
    n_heads = sum(1 for h, _g in blocks if h)
    slots = n_bars + 0.9 * n_heads + 0.5 * (len(blocks) - 1)
    fig, ax = _new_axes(1, height=0.36 * slots + 2.6)
    fig.set_figwidth(9.6)

    y, ticks, labels, widest = 0.0, [], [], 0
    present: set[str] = set()
    for header, groups in blocks:
        if header:
            ax.annotate(header, xy=(0, y - 0.1), xycoords=("axes fraction", "data"),
                        xytext=(-8, 0), textcoords="offset points", ha="right",
                        va="center", fontsize=9.5, fontweight="bold", color=INK_MUTED)
            y += 0.9
        for label, group in groups:
            tally = {name: 0 for name, _fill in OUTCOMES}
            for r in group:
                tally[_outcome(r.status)] += 1
            left = 0
            for name, fill in OUTCOMES:
                if tally[name]:
                    present.add(name)
                    # The surface-coloured edge is the 2px gap between segments.
                    ax.barh(y, tally[name], left=left, height=0.62, color=fill,
                            edgecolor=SURFACE, linewidth=1.5, zorder=2)
                    left += tally[name]
            n, succ = len(group), tally["success"]
            ax.annotate(f"{succ}/{n} · {100.0 * succ / n:.1f}%", xy=(n, y),
                        xytext=(6, 0), textcoords="offset points", va="center",
                        ha="left", fontsize=9, color=INK_SECONDARY)
            ticks.append(y)
            labels.append(label)
            widest = max(widest, n)
            y += 1
        y += 0.5

    ax.set_yticks(ticks)
    ax.set_yticklabels(labels, fontsize=10, color=INK_SECONDARY)
    ax.set_ylim(y - 0.9, -0.6)          # top-down: the first group on top
    ax.set_xlim(0, widest * 1.24)
    from matplotlib.ticker import MaxNLocator
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    # No tick past the longest bar: the room beyond it is for the end labels.
    ax.set_xticks([t for t in ax.get_xticks() if 0 <= t <= widest])
    # The legend rides in a band between the subtitle and the first bar: every bar
    # can run the full width, so there is no free corner inside the axes.
    _chrome(ax, "rows", title, subtitle, value_axis="x", lift=20)
    _legend(ax, [(name, fill, fill) for name, fill in OUTCOMES if name in present],
            loc="lower left", bbox_to_anchor=(0, 1), ncol=len(present),
            handlelength=1.4, borderaxespad=0.3, borderpad=0)
    return _save(fig, out_path)


def _parent_blocks(rows: list[report.Row]):
    """The bug categories in dataset order, one block per parent category."""
    blocks: list[tuple[str, list]] = []
    for label, group in report.by_category(rows):
        parent = group[0].fault_class if group else ""
        if not blocks or blocks[-1][0] != parent:
            blocks.append((parent, []))
        blocks[-1][1].append((label, group))
    return blocks


def _outcomes_subtitle(rows: list[report.Row]) -> str:
    note = _outcome_note(rows)
    return (f"{len(rows)} row(s) · bar length = rows · label = SUCCESS / rows"
            + (f"\n{note}" if note else ""))


def _render_fault_types(run: _Run, out: Path) -> Path | None:
    rows = run.rows
    if not rows:
        print("    no rows in this run — success-by-fault-type chart skipped")
        return None
    return draw_outcomes(_parent_blocks(rows), out,
                         "Results per bug category", _outcomes_subtitle(rows))


def _render_parents(run: _Run, out: Path) -> Path | None:
    rows = run.rows
    if not rows:
        print("    no rows in this run — success-by-parent chart skipped")
        return None
    return draw_outcomes([(None, report.by_parent(rows))], out,
                         "Results per parent category", _outcomes_subtitle(rows))


def _render_games(run: _Run, out: Path) -> Path | None:
    rows = run.rows
    if not rows:
        print("    no rows in this run — success-by-game chart skipped")
        return None
    # No pooled `all` bar: bar length is the row count, so a bar five times longer
    # would squeeze every game's bar into a fifth of the width. The pooled rate
    # rides in the subtitle instead.
    succ = sum(1 for r in rows if _outcome(r.status) == "success")
    return draw_outcomes([(None, report.by_game(rows))], out, "Results per game",
                         f"{succ}/{len(rows)} SUCCESS overall "
                         f"({100.0 * succ / len(rows):.1f}%) · "
                         + _outcomes_subtitle(rows))


# --------------------------------------------------------------------------
# Figure — time per case, per pipeline phase
# --------------------------------------------------------------------------
def _game_groups(rows: list[report.Row]) -> list[tuple[str, list[report.Row]]]:
    """Per game, led by the pooled ``all`` group when the run covers several."""
    games = report.by_game(rows)
    return ([(ALL, rows)] if len(games) > 1 else []) + games


def draw_time(rows: list[report.Row], out_path: Path, subtitle: str) -> Path:
    """The method's time per case — validate and localize together, then each alone —
    one panel each, a horizontal box per game."""
    phases = ([(METHOD_PHASES, " + ".join(METHOD_PHASES))]
              + [(p, p) for p in METHOD_PHASES])
    groups = _game_groups(rows)
    fig, axes = _new_panels(len(phases), width=2.7 * len(phases) + 1.6,
                            height=0.52 * len(groups) + 2.6, sharey=True)
    positions = list(range(len(groups)))
    for ax, (phase, name) in zip(axes, phases):
        series = [[s for r in g if (s := _seconds(r, phase)) is not None]
                  for _label, g in groups]
        drawn = [(p, v) for p, v in zip(positions, series) if v]
        if drawn:
            ax.boxplot([v for _p, v in drawn], positions=[p for p, _v in drawn],
                       orientation="horizontal", **_box_style(ACCENT, ACCENT_FILL))
            _scatter(ax, [p for p, _v in drawn], [v for _p, v in drawn], ACCENT,
                     horizontal=True)
        # Over the rows themselves, not the groups: the pooled `all` group repeats
        # every row, and counting it would double the caption's case count.
        values = [s for r in rows if (s := _seconds(r, phase)) is not None]
        log = _log_if_wide(ax, values, headroom=1.6, axis="x")
        _style_axes(ax, value_axis="x")
        ax.set_xlabel("seconds" + (" (log)" if log else ""), fontsize=9.5,
                      color=INK_SECONDARY)
        ax.set_title(name, loc="left", fontsize=11, color=INK, pad=20)
        med = (f"median {_fmt_seconds(statistics.median(values))} · "
               f"{len(values)} case(s)") if values else "did not run"
        ax.annotate(med, xy=(0, 1), xycoords="axes fraction", xytext=(0, 6),
                    textcoords="offset points", fontsize=9, color=INK_MUTED,
                    ha="left", va="bottom")
    axes[0].set_yticks(positions)
    axes[0].set_yticklabels([f"{g}  n={len(rs)}" for g, rs in groups],
                            fontsize=10, color=INK_SECONDARY)
    axes[0].set_ylim(len(groups) - 0.5, -0.5)          # first group on top
    top = _figure_heading(fig, "Method time per case: validate and localize", subtitle)
    return _save(fig, out_path, top)


_PHASE_NOTE = ("validate = monitor the traces against the spec · "
               "localize = rank the bug conditions and decide phi (steps 7a-8)\n"
               "build and record are left out: they depend on the game and its data, "
               "not on the method\n"
               "a case that stopped before localize is left out of the localize "
               "panels")


def _render_time(run: _Run, out: Path) -> Path | None:
    rows = [r for r in run.rows
            if any(_seconds(r, p) is not None for p in METHOD_PHASES)]
    if not rows:
        print("    no row recorded a validate or localize time — time figure skipped")
        return None
    return draw_time(rows, out,
                     f"{len(rows)} case(s) · each panel has its own scale · "
                     f"{_BOX_LEGEND}\n{_PHASE_NOTE}")


# --------------------------------------------------------------------------
# Figure — the size of the searched state
# --------------------------------------------------------------------------
def _paired_boxes(groups: list[tuple[str, list[float], list[float]]],
                  out_path: Path, series: tuple[str, str], value_label: str,
                  title: str, subtitle: str, headroom: float = 3.0) -> Path:
    """Two boxes per group on one shared axis — slot 1 left, slot 2 right — with the
    medians written outside each box and a legend naming both series.

    ``groups`` is ``[(label, left values, right values)]``, paired per row. One axis
    serves both (never two scales); it goes log when the spans are more than ~50x
    apart, since a linear axis then flattens the smaller series onto the baseline.
    """
    labels = [g for g, _a, _b in groups]
    lefts = [a for _g, a, _b in groups]
    rights = [b for _g, _a, b in groups]
    positions = list(range(1, len(groups) + 1))
    left = [p - 0.22 for p in positions]
    right = [p + 0.22 for p in positions]

    fig, ax = _new_axes(len(groups), width_per_group=1.9, height=5.4)
    style = _box_style(ACCENT, ACCENT_FILL)
    style["widths"] = 0.34
    ax.boxplot(lefts, positions=left, **style)
    style2 = _box_style(ACCENT2, ACCENT2_FILL)
    style2["widths"] = 0.34
    ax.boxplot(rights, positions=right, **style2)
    _scatter(ax, left, lefts, ACCENT, spread=0.08)
    _scatter(ax, right, rights, ACCENT2, spread=0.08)

    # The headroom keeps the largest value clear of the legend.
    log = _log_if_wide(ax, [v for vals in lefts + rights for v in vals], headroom)

    # Medians outside the outer edge of each box, so neither label sits on an
    # outline, on the jittered points, or on its neighbour.
    half = 0.34 / 2
    for x, vals, side, ha in ((left, lefts, -half, "right"),
                              (right, rights, half, "left")):
        for xi, v in zip(x, vals):
            med = statistics.median(v)
            ax.annotate(f"{med:g}", xy=(xi + side, med),
                        xytext=(6 if ha == "left" else -6, 0),
                        textcoords="offset points",
                        va="center", ha=ha, fontsize=9, color=INK_SECONDARY, zorder=4)

    pad = (max(2, len(groups)) - len(groups)) / 2 + 0.35
    ax.set_xlim(0.5 - pad, len(groups) + 0.5 + pad)
    ax.set_xticks(positions)
    ax.set_xticklabels([f"{g}\nn={len(a)}" for g, a in zip(labels, lefts)],
                       fontsize=10, color=INK_SECONDARY)
    _chrome(ax, value_label + (" (log scale)" if log else ""), title, subtitle)
    # Two series, so a legend is always present (identity is never colour-alone).
    _legend(ax, [(series[0], ACCENT, ACCENT_FILL), (series[1], ACCENT2, ACCENT2_FILL)])
    return _save(fig, out_path)


def _space_groups(rows: list[report.Row]) -> list[tuple[str, list[int], list[int]]]:
    """``[(label, logged, varying)]`` per game, largest state first, led by ``all``."""
    games = sorted(report.by_game(rows),
                   key=lambda lg: -statistics.median(r.variables for r in lg[1]))
    groups = ([(ALL, rows)] if len(games) > 1 else []) + games
    return [(g, [r.variables for r in rs], [r.varying for r in rs]) for g, rs in groups]


def _sized(rows: list[report.Row]) -> list[report.Row]:
    return [r for r in rows if r.variables is not None and r.varying is not None]


def _render_space(run: _Run, out: Path) -> Path | None:
    rows = _sized(run.rows)
    if not rows:
        print("    no row carries a variable count (run predates the columns?) — "
              "space figure skipped")
        return None
    missed = len(run.rows) - len(rows)
    tail = f", {missed} excluded (no mining record)" if missed else ""
    return _paired_boxes(
        _space_groups(rows), out, ("logged", "varying (searched)"),
        "variables in the mined states",
        "Variables per row: logged vs. varying",
        f"{len(rows)} row(s){tail} · a variable is one base.field of the mined "
        f"states\nvarying = not constant across them, the vocabulary the miner "
        f"builds atoms over\n{_BOX_LEGEND}",
        # Games run largest state first, so the legend's corner holds the smallest.
        headroom=1.8)


# --------------------------------------------------------------------------
# Figure — analysis time against the size of the state
# --------------------------------------------------------------------------
_LOCALIZE = "localize"


def draw_time_vs_space(rows: list[report.Row], out_path: Path, subtitle: str) -> Path:
    """One panel per game on shared axes: that game's rows in the accent, every other
    row in gray behind them, so each game reads against the whole run."""
    games = report.by_game(rows)
    point = {id(r): (r.varying, _seconds(r, _LOCALIZE)) for r in rows}
    fig, axes = _new_panels(len(games), width=max(6.8, 3.0 * len(games) + 1.0),
                            height=4.3, sharex=True, sharey=True)
    for ax, (game, group) in zip(axes, games):
        mine = {id(r) for r in group}
        others = [point[i] for i in point if i not in mine]
        if others:
            ax.scatter(*zip(*others), s=18, color=NEUTRAL_LIGHT, alpha=0.6,
                       linewidths=0, zorder=2)
        ax.scatter(*zip(*(point[id(r)] for r in group)), s=30, color=ACCENT,
                   alpha=0.85, linewidths=0.8, edgecolors=SURFACE, zorder=3)
        _style_axes(ax, value_axis="y")
        ax.xaxis.grid(True, color=GRID, linewidth=0.8)
        ax.tick_params(axis="x", labelsize=9, colors=INK_MUTED)
        ax.set_title(f"{game}  n={len(group)}", loc="left", fontsize=11, color=INK)
    xs = [x for x, _y in point.values()]
    ys = [y for _x, y in point.values()]
    logx = _log_if_wide(axes[0], xs, headroom=1.4, axis="x")
    logy = _log_if_wide(axes[0], ys, headroom=1.4, axis="y")
    for ax in axes:
        ax.set_xlabel("varying variables" + (" (log)" if logx else ""),
                      fontsize=9.5, color=INK_SECONDARY)
    axes[0].set_ylabel("localize time, seconds" + (" (log)" if logy else ""),
                       fontsize=10, color=INK_SECONDARY)
    top = _figure_heading(fig, "Localize time vs. variables searched", subtitle)
    return _save(fig, out_path, top)


def _render_time_space(run: _Run, out: Path) -> Path | None:
    rows = [r for r in _sized(run.rows) if _seconds(r, _LOCALIZE) is not None]
    if not rows:
        print("    no row carries both a variable count and a localize time — "
              "time-vs-space figure skipped")
        return None
    missed = len(run.rows) - len(rows)
    tail = (f", {missed} excluded (no variable count or no localize phase)"
            if missed else "")
    return draw_time_vs_space(
        rows, out, f"{len(rows)} row(s){tail} · one point per row · "
                   f"gray = the other games' rows\nlocalize = rank the bug "
                   f"conditions and decide phi (steps 7a-8)")


# --------------------------------------------------------------------------
# Figure — candidate count vs. the rank under each candidate sort
# --------------------------------------------------------------------------
#: Each sort keeps its colour whichever sorts a run ranked under — colour follows the
#: sort, not its position — so two runs' figures read the same way.
_SORT_STYLE = {"none": (ACCENT2, ACCENT2_FILL), "type": (ACCENT3, ACCENT3_FILL),
               "popularity": (ACCENT4, ACCENT4_FILL)}
_SORT_SHORT = {"none": "none", "type": "type", "popularity": "pop."}


def draw_ranks_by_sort(rows: list[report.Row], sorts: list[str], out_path: Path,
                       subtitle: str) -> Path:
    """Per game (after the pooled ``all``): one box of top-scored candidate counts,
    then one box of ground-truth ranks per sort, over the same rows.

    The sort only permutes the top tier, so the candidate count is one box for all of
    them, and every rank box indexes into the list that box sizes — one shared axis,
    log-scaled when the spans are far apart. Every box is named, with its median,
    under the axis; the games are labelled below that.
    """
    games = sorted(report.by_game(rows),
                   key=lambda lg: -statistics.median(r.candidates for r in lg[1]))
    groups = ([(ALL, rows)] if len(games) > 1 else []) + games
    series = [("cand.", ACCENT, ACCENT_FILL, lambda r: r.candidates)]
    series += [(_SORT_SHORT.get(s, s), *_SORT_STYLE.get(s, (NEUTRAL, NEUTRAL_LIGHT)),
                lambda r, s=s: r.rank_under(s)) for s in sorts]
    k = len(series)
    step, width = 0.2, 0.16
    offsets = [(i - (k - 1) / 2) * step for i in range(k)]
    centres = list(range(1, len(groups) + 1))

    fig, ax = _new_axes(len(groups), width_per_group=0.62 * k + 0.25, height=6.0)
    ticks, labels, values = [], [], []
    for (short, color, fill, get), off in zip(series, offsets):
        positions = [c + off for c in centres]
        data = [[get(r) for r in group] for _label, group in groups]
        style = _box_style(color, fill)
        style["widths"] = width
        ax.boxplot(data, positions=positions, manage_ticks=False, **style)
        _scatter(ax, positions, data, color, spread=0.045)
        for x, vals in zip(positions, data):
            ticks.append(x)
            labels.append(f"{short}\n{statistics.median(vals):g}")
        values += [v for vals in data for v in vals]

    log = _log_if_wide(ax, values, headroom=3.0)
    ax.set_xlim(0.5, len(groups) + 0.5)
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, fontsize=8, color=INK_SECONDARY, linespacing=1.3)
    games_axis = ax.secondary_xaxis("bottom")
    games_axis.set_xticks(centres)
    games_axis.set_xticklabels([f"{g}  n={len(rs)}" for g, rs in groups],
                               fontsize=10, color=INK_SECONDARY)
    games_axis.tick_params(length=0, pad=30)
    games_axis.spines["bottom"].set_visible(False)
    _chrome(ax, "top-scored candidates / ground-truth rank"
                + (" (log scale)" if log else ""),
            "Top-scored candidates vs. rank of the ground truth, per candidate sort",
            subtitle)
    _legend(ax, [("top-scored candidates", ACCENT, ACCENT_FILL)]
                + [(f"rank · {s}", *_SORT_STYLE.get(s, (NEUTRAL, NEUTRAL_LIGHT)))
                   for s in sorts])
    return _save(fig, out_path)


def _render_ranks_by_sort(run: _Run, out: Path) -> Path | None:
    rows = run.rows
    sorts = report.ranked_sorts(rows)
    if len(sorts) < 2:
        print("    ranked under one candidate sort only — by-sort figure skipped "
              "(run with --candidate-sort all)")
        return None
    both = report.sort_ranked(rows, sorts)
    if not both:
        print("    no row's ground truth was ranked under every sort — by-sort "
              "figure skipped")
        return None
    note = report.sort_left_out_note(rows, sorts)
    tail = f", {note}" if note else ""
    return draw_ranks_by_sort(
        both, sorts, out,
        f"{len(both)} row(s) whose ground truth was ranked under every sort{tail}\n"
        f"every sort orders the same top-scored candidates, so their count is one "
        f"box and each sort's rank another\n"
        f"label = median · {_BOX_LEGEND}")


# --------------------------------------------------------------------------
# Figure — candidate-set size
# --------------------------------------------------------------------------
def draw_candidates(rows: list[MinedRow], out_path: Path, subtitle: str) -> Path:
    """Boxplot of the top-scored candidate count, one box per game (matplotlib must
    import)."""
    groups = _grouped(rows)
    labels = _by_median_candidates(groups)
    series = [[r.candidates for r in groups[g]] for g in labels]

    fig, ax = _new_axes(len(series))
    log = _log_if_wide(ax, _boxes(ax, labels, series), headroom=2.0)
    _chrome(ax, "top-ranked candidate bug conditions" + (" (log scale)" if log else ""),
            "Top-ranked candidate bug conditions per row", subtitle)
    return _save(fig, out_path)


def _boxes(ax, labels: list[str], series: list[list[float]]) -> list[float]:
    """One box per group with every row as a point beside it, the median written
    next to it, and the groups named under the axis with their row count. Returns
    every value drawn, for the caller's scale decision."""
    ax.boxplot(series, **_box_style(ACCENT, ACCENT_FILL))
    positions = list(range(1, len(series) + 1))
    _scatter(ax, [float(p) for p in positions], series, ACCENT)

    # Direct-label each median: the one number a reader takes away.
    for x, vals in zip(positions, series):
        med = statistics.median(vals)
        ax.annotate(f"{med:g}", xy=(x + 0.26, med), va="center", ha="left",
                    fontsize=9, color=INK_SECONDARY, zorder=4)

    # Boxes keep the same visual width whatever the group count: a run covering one
    # game pads the axis out to three slots and centres its single box, rather than
    # stretching it across the whole canvas.
    pad = (max(3, len(series)) - len(series)) / 2
    ax.set_xlim(0.5 - pad, len(series) + 0.5 + pad)
    ax.set_xticks(positions)
    ax.set_xticklabels([f"{g}\nn={len(v)}" for g, v in zip(labels, series)],
                       fontsize=10, color=INK_SECONDARY)
    return [v for vals in series for v in vals]


# --------------------------------------------------------------------------
# Figure — candidate count vs. where the ground truth ranked
# --------------------------------------------------------------------------
def _rank_groups(rows: list[MinedRow]) -> list[tuple[str, list[int], list[int]]]:
    """``[(label, candidate counts, ground-truth ranks)]``, paired per row.

    A multi-game run leads with the pooled ``all`` group: the per-game boxes are what
    differs between games, the pooled one is the number a results section quotes.
    """
    groups = _grouped(rows)
    labels = _by_median_candidates(groups)
    out = []
    if len(labels) > 1:
        out.append((ALL, [r.candidates for r in rows], [r.gt_rank for r in rows]))
    out += [(g, [r.candidates for r in groups[g]], [r.gt_rank for r in groups[g]])
            for g in labels]
    return out


def draw_ranks(rows: list[MinedRow], out_path: Path, subtitle: str) -> Path:
    """Paired boxplot: the candidate count and the ground truth's rank, per game.

    Only the rows whose ground truth was found are drawn — a row with no rank has no
    position to compare against its candidate count. Both series share one axis (they
    are the size of, and an index into, the same ranked list).
    """
    scope = "top-scored candidates"
    return _paired_boxes(_rank_groups(rows), out_path,
                         (scope, "rank of ground truth"),
                         f"{scope} / ground-truth rank",
                         f"{scope.capitalize()} vs. rank of the ground truth",
                         subtitle)


# --------------------------------------------------------------------------
# Figure — ground-truth rank against the labelled states
# --------------------------------------------------------------------------
#: The bins of labelled states per row (good + bad), ``(label, lowest, highest)``.
#: Fixed, so two runs' figures bin alike, and finest at the bottom, where most rows
#: are: a row needs one violation to mine at all, and many have exactly one good
#: state beside it. An empty bin is not drawn.
STATE_BINS = (("1", 1, 1), ("2", 2, 2), ("3–4", 3, 4), ("5–9", 5, 9),
              ("10+", 10, None))


def _examples(row: report.Row) -> int:
    """The labelled states a row's ranking was learned from: good + bad."""
    return row.good_states + row.bad_states


def _in_bin(row: report.Row, lo: int, hi: int | None) -> bool:
    return _examples(row) >= lo and (hi is None or _examples(row) <= hi)


def _spearman(xs: list[int], ys: list[int]) -> float | None:
    """Spearman's rank correlation (ties share their mean rank), or None where it is
    undefined: fewer than three rows, or one side constant."""
    if len(xs) < 3:
        return None
    try:
        return statistics.correlation(xs, ys, method="ranked")
    except statistics.StatisticsError:
        return None


def _fmt_rho(rho: float | None) -> str:
    return "undefined" if rho is None else f"{rho:.2f}".replace("-", "−")


def draw_states_vs_rank(rows: list[report.Row], out_path: Path, subtitle: str) -> Path:
    """The ground truth's rank per bin of labelled states (good + bad), one box per
    bin: does a deep rank go with few examples?

    Spearman's rho over the unbinned counts goes in the title's caption — the bins
    are for the eye, the coefficient is the number to quote.
    """
    labels, series = [], []
    for label, lo, hi in STATE_BINS:
        ranks = [r.gt_rank for r in rows if _in_bin(r, lo, hi)]
        if ranks:
            labels.append(label)
            series.append(ranks)
    fig, ax = _new_axes(len(series))
    log = _log_if_wide(ax, _boxes(ax, labels, series), headroom=2.0)
    rho = _spearman([_examples(r) for r in rows], [r.gt_rank for r in rows])
    ax.set_xlabel("labelled states per row (good + bad)", fontsize=10,
                  color=INK_SECONDARY)
    _chrome(ax, "rank of the ground truth, 1 = top" + (" (log scale)" if log else ""),
            "Ground-truth rank vs. labelled states per row",
            f"Spearman ρ = {_fmt_rho(rho)} over the unbinned counts · {subtitle}")
    return _save(fig, out_path)


def draw_states_vs_rank_by_game(rows: list[report.Row], out_path: Path,
                                subtitle: str) -> Path:
    """:func:`draw_states_vs_rank` in small multiples: the pooled ``all`` panel, then
    one per game, three to a row.

    Every panel has the same bins at the same positions (any bin some row of the run
    falls in; a game with no row there leaves the slot empty) and one shared rank
    axis, so a box reads against the same box in the next panel. Each panel names
    its own row count per bin and its own rho — a game's rows are few, and a rho
    over a handful of them is the number that says so.
    """
    groups = _game_groups(rows)
    bins = [b for b in STATE_BINS if any(_in_bin(r, b[1], b[2]) for r in rows)]
    ncols = min(3, len(groups))
    nrows = -(-len(groups) // ncols)
    fig, axes = _new_panels(len(groups), width=max(6.8, (0.85 * len(bins) + 0.9)
                                                    * ncols + 0.8),
                            height=3.5 * nrows + 2.0, nrows=nrows, sharey=True)
    positions = list(range(1, len(bins) + 1))
    for i, (ax, (game, group)) in enumerate(zip(axes, groups)):
        series = [[r.gt_rank for r in group if _in_bin(r, lo, hi)]
                  for _label, lo, hi in bins]
        drawn = [(p, v) for p, v in zip(positions, series) if v]
        ax.boxplot([v for _p, v in drawn], positions=[p for p, _v in drawn],
                   manage_ticks=False, **_box_style(ACCENT, ACCENT_FILL))
        _scatter(ax, [float(p) for p, _v in drawn], [v for _p, v in drawn], ACCENT)
        for x, vals in drawn:
            med = statistics.median(vals)
            ax.annotate(f"{med:g}", xy=(x + 0.26, med), va="center", ha="left",
                        fontsize=8.5, color=INK_SECONDARY, zorder=4)
        ax.set_xlim(0.5, len(bins) + 0.5)
        ax.set_xticks(positions)
        ax.set_xticklabels([f"{label}\nn={len(v)}" if v else label
                            for (label, _lo, _hi), v in zip(bins, series)],
                           fontsize=9, color=INK_SECONDARY)
        _style_axes(ax)
        ax.set_title(f"{game}  n={len(group)}", loc="left", fontsize=11, color=INK,
                     pad=20)
        rho = _spearman([_examples(r) for r in group], [r.gt_rank for r in group])
        ax.annotate(f"ρ = {_fmt_rho(rho)}", xy=(0, 1), xycoords="axes fraction",
                    xytext=(0, 6), textcoords="offset points", fontsize=9,
                    color=INK_MUTED, ha="left", va="bottom")
        if i + ncols >= len(groups):         # nothing drawn below this panel
            ax.set_xlabel("labelled states (good + bad)", fontsize=9.5,
                          color=INK_SECONDARY)
    # The rank axis is shared, so one scale decision over every row covers them all.
    log = _log_if_wide(axes[0], [r.gt_rank for r in rows], headroom=2.0)
    for ax in axes[::ncols]:
        ax.set_ylabel("rank of the ground truth" + (" (log)" if log else ""),
                      fontsize=10, color=INK_SECONDARY)
    top = _figure_heading(fig, "Ground-truth rank vs. labelled states per row, "
                               "per game", subtitle)
    return _save(fig, out_path, top)


# --------------------------------------------------------------------------
# Subtitles
# --------------------------------------------------------------------------
_BOX_LEGEND = "box = median and IQR · whiskers 1.5×IQR · one point per row"


def _sort_note(run_dir: Path | str) -> str:
    """A caption clause naming the candidate ordering, when it was not the default.

    Ranks are positions within a score tier whose internal order the sort decides,
    so two figures drawn under two sorts are the same measurement of different
    orderings — and must not be mistakable for each other.
    """
    settings = report.read_settings(run_dir) or {}
    sort = settings.get("candidate_sort")
    return f"\ncandidates within a tier ordered by: {sort}" if sort and sort != "none" else ""


def _candidates_subtitle(rows: list[MinedRow], dropped: int) -> str:
    tail = f", {dropped} excluded (never mined)" if dropped else ""
    return f"{len(rows)} mined row(s){tail}\n{_BOX_LEGEND}"


def _ranks_subtitle(ranked: list[MinedRow], mined: int) -> str:
    missed = mined - len(ranked)
    tail = (f", {missed} mined row(s) excluded (ground truth not found)"
            if missed else "")
    return (f"{len(ranked)} row(s) whose ground truth was found{tail}\n"
            f"{_BOX_LEGEND}")


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
def _render_candidates(run: _Run, out: Path) -> Path | None:
    rows, dropped = run.mining
    if not rows:
        print("    no mined rows in this run — candidates boxplot skipped")
        return None
    return draw_candidates(rows, out, _candidates_subtitle(rows, dropped) + run.sort_note)


def _render_ranks(run: _Run, out: Path) -> Path | None:
    try:
        several = len(report.ranked_sorts(run.rows)) >= 2
    except OSError:                  # no full.csv: an old run, one sort at most
        several = False
    if several:
        print("    ranked under several candidate sorts — the candidates-vs-rank "
              "boxplot is drawn per sort in ranks_by_sort.png instead")
        return None
    rows, _dropped = run.mining
    ranked = [r for r in rows if r.gt_rank]
    if not ranked:
        print("    no row's ground truth was ranked — candidates-vs-rank boxplot "
              "skipped")
        return None
    return draw_ranks(ranked, out, _ranks_subtitle(ranked, len(rows)) + run.sort_note)


def _states_rows(run: _Run, figure: str) -> tuple[list[report.Row], str] | None:
    """The rows both states figures draw — found, and carrying their state counts —
    with the caption saying which rows were left out and which sort ranked them;
    None, with a note naming ``figure``, when there is no such row."""
    counted = [r for r in run.rows if r.candidates is not None
               and r.good_states is not None and r.bad_states is not None]
    if not counted:
        print("    no row carries its good/bad state counts (run predates the "
              f"columns? see scripts/backfill_states.py) — {figure} skipped")
        return None
    found = [r for r in counted if r.gt_rank]
    if not found:
        print(f"    no row's ground truth was ranked — {figure} skipped")
        return None
    left_out = []
    if len(counted) > len(found):
        left_out.append(f"{len(counted) - len(found)} excluded (not found)")
    uncounted = sum(1 for r in run.rows if r.candidates is not None) - len(counted)
    if uncounted:
        left_out.append(f"{uncounted} without state counts")
    # A multi-sort run ranks the same rows several ways; this figure draws one.
    sorts = report.ranked_sorts(run.rows)
    sort_note = run.sort_note
    if len(sorts) > 1:
        own = (report.read_settings(run.dir) or {}).get("candidate_sort") or sorts[0]
        sort_note = (f"\nrank = position under the run's own candidate sort ({own}); "
                     f"the other sorts are in {SORTS_PNG}")
    return found, (f"{len(found)} row(s) whose ground truth was found"
                   + "".join(f", {s}" for s in left_out)
                   + "\ngood = a trigger state where the spec held · bad = a "
                     "violation · pooled over the row's traces"
                   + f"\n{_BOX_LEGEND}"
                   + sort_note)


def _render_states(run: _Run, out: Path) -> Path | None:
    drawn = _states_rows(run, "states figure")
    if drawn is None:
        return None
    found, caption = drawn
    return draw_states_vs_rank(found, out, caption)


def _render_states_by_game(run: _Run, out: Path) -> Path | None:
    drawn = _states_rows(run, "states-by-game figure")
    if drawn is None:
        return None
    found, caption = drawn
    return draw_states_vs_rank_by_game(
        found, out, "ρ = Spearman over each panel's unbinned counts, undefined below "
                    "3 rows · " + caption)


#: ``figure name -> (renderer, default file name)``. ``render`` draws them all; the
#: CLI can draw one by name.
FIGURES = {
    "fault-types": (_render_fault_types, FAULT_TYPES_PNG),
    "parents": (_render_parents, PARENTS_PNG),
    "games": (_render_games, GAMES_PNG),
    "time": (_render_time, TIME_PNG),
    "space": (_render_space, SPACE_PNG),
    "time-space": (_render_time_space, TIME_SPACE_PNG),
    "candidates": (_render_candidates, CANDIDATES_PNG),
    "ranks": (_render_ranks, RANKS_PNG),
    "sorts": (_render_ranks_by_sort, SORTS_PNG),
    "states": (_render_states, STATES_PNG),
    "states-games": (_render_states_by_game, STATES_GAMES_PNG),
}


def _render_here(run_dir: Path, figures: list[str],
                 out: Path | None) -> list[Path]:
    """Draw ``figures`` in this process. Each figure fails on its own: an old run
    missing one input (no ``mining.csv``, no variable columns) still gets the rest."""
    run = _Run(Path(run_dir))
    written = []
    for name in figures:
        renderer, filename = FIGURES[name]
        target = Path(out) if out else Path(run_dir) / filename
        try:
            path = renderer(run, target)
        except Exception as exc:  # noqa: BLE001 - an extra artefact, never a failure
            print(f"    {name} figure skipped ({type(exc).__name__}: {exc})")
            continue
        if path is not None:
            written.append(path)
    return written


def _render_in_gamerv_env(run_dir: Path, figures: list[str],
                          out: Path | None) -> list[Path]:
    """Re-run this file inside the gamerv conda env, which has matplotlib."""
    args = [str(Path(__file__).resolve()), str(run_dir)]
    for name in figures:
        args += ["--figure", name]
    if out is not None:
        args += ["--out", str(out)]
    env = dict(os.environ, **{_CHILD_ENV: "1"})
    proc = subprocess.run(config.gamerv_cmd(args), env=env,
                          capture_output=True, text=True)
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    notes = [ln for ln in lines if not ln.startswith("wrote ")]
    if notes:
        print("\n".join("    " + ln for ln in notes))
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or ["no output"])[-1]
        print(f"    plots skipped ({last.strip()})")
        return []
    # What the child says it wrote — not whichever targets exist, which would count
    # a stale file from an earlier render as a figure this one produced.
    return [Path(ln[len("wrote "):]) for ln in lines if ln.startswith("wrote ")]


def render(run_dir: Path | str, figures: list[str] | None = None,
           out: Path | str | None = None) -> list[Path]:
    """Write the run's plots; return the paths written (possibly empty).

    Never raises: a run that produced results is not spoiled by a missing plotting
    dependency or an unreadable CSV — the failure is printed and the batch keeps its
    CSVs.
    """
    run_dir, out = Path(run_dir), (Path(out) if out else None)
    figures = list(figures) if figures else list(FIGURES)
    try:
        import matplotlib  # noqa: F401
    except ModuleNotFoundError:
        if os.environ.get(_CHILD_ENV):
            raise
        try:
            return _render_in_gamerv_env(run_dir, figures, out)
        except Exception as exc:  # noqa: BLE001 - an extra artefact, never a failure
            print(f"    plots skipped ({type(exc).__name__}: {exc})")
            return []
    return _render_here(run_dir, figures, out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", help="a results/run_<timestamp>/ folder")
    ap.add_argument("--figure", action="append", choices=sorted(FIGURES),
                    help="draw only this figure (repeatable; default: all)")
    ap.add_argument("--out", help="output image; only with a single --figure "
                                  "(default <run_dir>/<figure>.png)")
    args = ap.parse_args(argv)
    figures = args.figure or list(FIGURES)
    if args.out and len(figures) > 1:
        ap.error("--out takes a single --figure (each figure needs its own path)")
    # In the child (or when matplotlib is present) this draws directly; from a bare
    # shell it takes the same conda detour run_experiment does.
    paths = (_render_here(Path(args.run_dir), figures,
                          Path(args.out) if args.out else None)
             if os.environ.get(_CHILD_ENV)
             else render(args.run_dir, figures, args.out))
    # A run with nothing to plot is not a failure: the renderers have already said so.
    # Only a real error (which raises) leaves a non-zero exit.
    for path in paths:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
