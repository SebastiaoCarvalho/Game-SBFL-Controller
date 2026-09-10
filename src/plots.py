#!/usr/bin/env python
"""Plots for the evaluation step: how big the mined candidate set was, and how deep
in it the ground truth sat.

Step 8 records, per row, how many **top-scored** sound candidate refinements the miner
produced and where the ground-truth phi ranked among them (``metrics.Mining``);
``run_experiment`` writes both columns into the run's ``mining.csv``. Both quantities
are over the top-scored tier of gamerv's ranked list only — the refinements a user
actually reads before reaching the fix; the lower-scoring sound separators are kept in
``refinement_separators.log`` and deliberately excluded here, since a count that mixed
score tiers would answer no question about the method (see metrics.py). This module
turns them into the two figures a results section wants:

  candidates.png        a **boxplot of the candidate-set size**, one box per game,
                        with every row drawn as a point beside its box so the shape of
                        the distribution (and the handful of very large searches) is
                        visible rather than smoothed into five summary numbers.
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

Only rows that actually mined are plotted: a blank ``candidates`` cell means the row
never reached the point of persisting a mining record (see metrics.read), and counting
it as zero would claim the miner came back empty-handed. The rank figure further keeps
only the rows whose ground truth *was* found among the top-scored candidates (a blank
``gt_rank`` has no position to draw — including a ``below_top`` row, whose ground truth
sits outside the plotted set). How many rows were dropped either way is printed in each figure's subtitle, so
a box is never read as covering more rows than it does.

Written after the CSVs, from the CSVs — so the same code re-plots an old run:

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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import config

MINING_CSV = "mining.csv"
CANDIDATES_PNG = "candidates.png"
RANKS_PNG = "candidates_vs_rank.png"

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


def read_mining(run_dir: Path) -> tuple[list[MinedRow], int, bool]:
    """``(rows that mined, dropped, top_scored)`` from a run's ``mining.csv``.

    ``dropped`` counts the rows whose ``candidates`` cell is blank or unreadable —
    rows that never mined — which are excluded from every box.

    ``top_scored`` says whether the counts are over the top-scored tier: the
    ``below_top`` column is what a run written since that change carries, so a CSV
    without it holds the older whole-list counts and is labelled as such rather than
    captioned with a scope it does not have.
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
        reader = csv.DictReader(f)
        top_scored = "below_top" in (reader.fieldnames or [])
        for row in reader:
            n = _int(row.get("candidates"))
            if n is None:
                dropped += 1
                continue
            rows.append(MinedRow((row.get("game") or "?").strip(), n,
                                 _int(row.get("gt_rank"))))
    return rows, dropped, top_scored


def _by_median_candidates(groups: dict[str, list[MinedRow]]) -> list[str]:
    """Games ordered by median candidate count, largest search first."""
    import statistics
    return sorted(groups, key=lambda g: -statistics.median(
        [r.candidates for r in groups[g]]))


def _grouped(rows: list[MinedRow]) -> dict[str, list[MinedRow]]:
    groups: dict[str, list[MinedRow]] = {}
    for r in rows:
        groups.setdefault(r.game, []).append(r)
    return groups


# --------------------------------------------------------------------------
# Shared figure furniture
# --------------------------------------------------------------------------
def _new_axes(n_groups: int, width_per_group: float = 1.9, height: float = 4.8):
    """A figure sized to the group count, with the palette's surface and chrome.

    The floor keeps a two-line subtitle inside the canvas when a run covers a single
    game and the boxes alone would need barely any width.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(
        figsize=(max(6.8, width_per_group * n_groups + 2.4), height), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    return fig, ax


def _box_style(color: str, fill: str) -> dict:
    """Thin marks, one accent per series (see the data-viz mark specs)."""
    return dict(widths=0.42, showfliers=False,   # every point is drawn; no doubles
                medianprops=dict(color=color, linewidth=2),
                boxprops=dict(color=color, linewidth=1.2, facecolor=fill, alpha=0.55),
                whiskerprops=dict(color=color, linewidth=1.2),
                capprops=dict(color=color, linewidth=1.2),
                patch_artist=True)


def _scatter(ax, positions: list[float], series: list[list[int]], color: str,
             spread: float = 0.11) -> None:
    """Every row as its own point beside its box, with a surface ring so overlapping
    points stay countable."""
    rng = random.Random(_JITTER_SEED)
    for x, vals in zip(positions, series):
        xs = [x + rng.uniform(-spread, spread) for _ in vals]
        ax.scatter(xs, vals, s=26, color=color, alpha=0.75, zorder=3,
                   linewidths=0.8, edgecolors=SURFACE)


def _log_if_wide(ax, values: list[int], headroom: float) -> bool:
    """Log-scale the y-axis when the values span more than ~50x, and leave headroom
    above the largest one (for the legend / the top median label). Returns whether the
    axis went log, so the label can say so — a log axis a reader mistakes for linear
    understates every large search by an order of magnitude.

    A zero keeps the axis linear: log cannot show it, and "no candidate at all" is a
    value the figure must not silently drop.
    """
    if values and min(values) > 0 and max(values) / min(values) >= 50:
        ax.set_yscale("log")
        ax.set_ylim(top=ax.get_ylim()[1] * headroom)
        return True
    ax.set_ylim(bottom=0, top=ax.get_ylim()[1] * 1.18)
    return False


def _chrome(ax, ylabel: str, title: str, subtitle: str) -> None:
    ax.set_ylabel(ylabel, fontsize=10, color=INK_SECONDARY)
    ax.tick_params(axis="y", labelsize=9, colors=INK_MUTED, length=0)
    ax.tick_params(axis="x", length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
        ax.spines[side].set_linewidth(0.8)
    # The subtitle grows upward from the axes top, so the title's pad has to clear it:
    # a fixed pad sized for two lines is overrun the moment a third is added (the
    # pre-tier scope note), and the title lands on top of the caption.
    lines = subtitle.count("\n") + 1
    ax.set_title(title, loc="left", pad=8 + 13 * lines, fontsize=13, color=INK)
    ax.annotate(subtitle, xy=(0, 1), xycoords="axes fraction",
                xytext=(0, 8), textcoords="offset points", linespacing=1.5,
                fontsize=9.5, color=INK_MUTED, ha="left", va="bottom")


def _save(fig, out_path: Path) -> Path:
    import matplotlib.pyplot as plt

    fig.tight_layout()
    out_path = Path(out_path)
    fig.savefig(out_path, facecolor=SURFACE)
    plt.close(fig)
    return out_path


# --------------------------------------------------------------------------
# Figure 1 — candidate-set size
# --------------------------------------------------------------------------
def draw_candidates(rows: list[MinedRow], out_path: Path, subtitle: str,
                    top_scored: bool = True) -> Path:
    """Boxplot of the top-scored candidate count, one box per game (matplotlib must
    import)."""
    import statistics

    groups = _grouped(rows)
    labels = _by_median_candidates(groups)
    series = [[r.candidates for r in groups[g]] for g in labels]

    fig, ax = _new_axes(len(series))
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
    log = _log_if_wide(ax, [v for vals in series for v in vals], headroom=2.0)
    scope = "Top-scored candidate" if top_scored else "Candidate"
    _chrome(ax, ("top-scored candidate refinements" if top_scored
                 else "candidate refinements mined") + (" (log scale)" if log else ""),
            f"{scope} refinements per row", subtitle)
    return _save(fig, out_path)


# --------------------------------------------------------------------------
# Figure 2 — candidate count vs. where the ground truth ranked
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


def draw_ranks(rows: list[MinedRow], out_path: Path, subtitle: str,
               top_scored: bool = True) -> Path:
    """Paired boxplot: the candidate count and the ground truth's rank, per game.

    Only the rows whose ground truth was found are drawn — a row with no rank has no
    position to compare against its candidate count. Both series share one axis (they
    are the size of, and an index into, the same ranked list); it goes log when the
    spans are more than ~50x apart, since a linear axis then flattens every rank box
    onto the baseline.
    """
    import statistics

    groups = _rank_groups(rows)
    labels = [g for g, _c, _r in groups]
    cands = [c for _g, c, _r in groups]
    ranks = [r for _g, _c, r in groups]
    positions = list(range(1, len(groups) + 1))
    left = [p - 0.22 for p in positions]
    right = [p + 0.22 for p in positions]

    fig, ax = _new_axes(len(groups), width_per_group=1.9, height=5.4)
    style = _box_style(ACCENT, ACCENT_FILL)
    style["widths"] = 0.34
    ax.boxplot(cands, positions=left, **style)
    style2 = _box_style(ACCENT2, ACCENT2_FILL)
    style2["widths"] = 0.34
    ax.boxplot(ranks, positions=right, **style2)
    _scatter(ax, left, cands, ACCENT, spread=0.08)
    _scatter(ax, right, ranks, ACCENT2, spread=0.08)

    # One axis for both series (never two scales): log it when the spread demands.
    # The headroom keeps the largest candidate count clear of the legend.
    log = _log_if_wide(ax, [v for vals in cands + ranks for v in vals], headroom=3.0)
    scope = "top-scored candidates" if top_scored else "candidates"
    ylabel = f"{scope} / ground-truth rank" + (" (log scale)" if log else "")

    # Medians outside the outer edge of each box, so neither label sits on an
    # outline, on the jittered points, or on its neighbour.
    half = 0.34 / 2
    for x, vals, side, ha in ((left, cands, -half, "right"),
                              (right, ranks, half, "left")):
        for xi, v in zip(x, vals):
            med = statistics.median(v)
            ax.annotate(f"{med:g}", xy=(xi + side, med),
                        xytext=(6 if ha == "left" else -6, 0),
                        textcoords="offset points",
                        va="center", ha=ha, fontsize=9, color=INK_SECONDARY, zorder=4)

    pad = (max(2, len(groups)) - len(groups)) / 2 + 0.35
    ax.set_xlim(0.5 - pad, len(groups) + 0.5 + pad)
    ax.set_xticks(positions)
    ax.set_xticklabels([f"{g}\nn={len(c)}" for g, c in zip(labels, cands)],
                       fontsize=10, color=INK_SECONDARY)
    _chrome(ax, ylabel,
            f"{scope.capitalize()} vs. rank of the ground truth", subtitle)

    # Two series, so a legend is always present (identity is never colour-alone).
    import matplotlib.patches as mpatches
    ax.legend(handles=[mpatches.Patch(facecolor=ACCENT_FILL, edgecolor=ACCENT,
                                      label=scope),
                       mpatches.Patch(facecolor=ACCENT2_FILL, edgecolor=ACCENT2,
                                      label="rank of ground truth")],
              loc="upper right", frameon=False, fontsize=9,
              labelcolor=INK_SECONDARY)
    return _save(fig, out_path)


# --------------------------------------------------------------------------
# Subtitles
# --------------------------------------------------------------------------
_BOX_LEGEND = "box = median and IQR · whiskers 1.5×IQR · one point per row"


def _scope_note(top_scored: bool) -> str:
    """The caption line naming what the counts are over. A run written before the
    metric was narrowed to the top-scored tier is captioned as the whole ranked list,
    so an old figure is never read as a new one (see read_mining)."""
    return "" if top_scored else "\ncounts over the whole ranked list (pre-tier run)"


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
def _render_candidates(rows: list[MinedRow], dropped: int, out: Path,
                       top_scored: bool = True) -> Path | None:
    if not rows:
        print("    no mined rows in this run — candidates boxplot skipped")
        return None
    return draw_candidates(rows, out,
                           _candidates_subtitle(rows, dropped) + _scope_note(top_scored),
                           top_scored)


def _render_ranks(rows: list[MinedRow], _dropped: int, out: Path,
                  top_scored: bool = True) -> Path | None:
    ranked = [r for r in rows if r.gt_rank]
    if not ranked:
        print("    no row's ground truth was ranked — candidates-vs-rank boxplot "
              "skipped")
        return None
    return draw_ranks(ranked, out,
                      _ranks_subtitle(ranked, len(rows)) + _scope_note(top_scored),
                      top_scored)


#: ``figure name -> (renderer, default file name)``. ``render`` draws them all; the
#: CLI can draw one by name.
FIGURES = {
    "candidates": (_render_candidates, CANDIDATES_PNG),
    "ranks": (_render_ranks, RANKS_PNG),
}


def _render_here(run_dir: Path, figures: list[str],
                 out: Path | None) -> list[Path]:
    rows, dropped, top_scored = read_mining(run_dir)
    written = []
    for name in figures:
        renderer, filename = FIGURES[name]
        target = Path(out) if out else Path(run_dir) / filename
        path = renderer(rows, dropped, target, top_scored)
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
    notes = [ln for ln in proc.stdout.splitlines()
             if ln.strip() and not ln.strip().startswith("wrote ")]
    if notes:
        print("\n".join("    " + ln.strip() for ln in notes))
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or ["no output"])[-1]
        print(f"    plots skipped ({last.strip()})")
        return []
    targets = ([Path(out)] if out else
               [Path(run_dir) / FIGURES[n][1] for n in figures])
    return [t for t in targets if t.exists()]


def render(run_dir: Path | str, figures: list[str] | None = None,
           out: Path | str | None = None) -> list[Path]:
    """Write the run's plots; return the paths written (possibly empty).

    Never raises: a run that produced results is not spoiled by a missing plotting
    dependency or an unreadable mining.csv — the failure is printed and the batch
    keeps its CSVs.
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
    try:
        return _render_here(run_dir, figures, out)
    except Exception as exc:  # noqa: BLE001 - an extra artefact, never a failure
        print(f"    plots skipped ({type(exc).__name__}: {exc})")
        return []


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
