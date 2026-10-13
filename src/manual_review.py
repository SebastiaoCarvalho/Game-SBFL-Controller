"""Human-in-the-loop answers for gamerv's manual predicate values.

gamerv's visual and audio predicates are answered by hand. Its ``validate``
worker dumps each one's evidence (frames/audio) under
``<run_dir>/manual_assets/`` (plus, for a visual request, a ``clip.mp4`` of the
same window — see ``_ask_one``), lists each undecided predicate in
``<run_dir>/manual_requests.json``, and exits ``NEEDS_MANUAL`` (3).

This module is the controller's side of that handshake: it shows the evidence to
the operator at the terminal, reads a True/False for each request, and writes the
answers to ``<run_dir>/manual_answers.json`` — the file gamerv re-reads on the
next validate pass. It is the headless-controller analogue of the gamerv GUI's
``ManualPredicatesDialog``; ``gamerv.validate`` calls ``collect`` and re-invokes
validate until the verdict is written.

A prompt is the one step of the pipeline that cannot run unattended, so
``collect`` has a third way out for an overnight batch: with ``skip`` set
(``run_experiment.py --skip-manual``) it raises :class:`NeedsManualError` as soon
as a request no hint covers turns up, and the row is reported ``NEEDS_MANUAL``
instead of stopping the run on a question. Nothing is written for such a row —
the operator re-runs it interactively later and the answers land in the hint then.

The JSON layout (``manual_requests.json`` / ``manual_answers.json``) and the
per-request ``key`` are gamerv's stable contract — see ``validation/manual.py``.
Asset paths in a request are stored relative to the run dir.
"""

import json
import subprocess
import sys
from pathlib import Path

import hints


#: How many pending calls a skipped row's note names before summarising the rest.
_NOTE_CALLS = 3


class NoTTYError(RuntimeError):
    """Manual input is needed but there is no terminal to prompt the operator on
    (e.g. a headless/CI controller run). Run the experiment interactively, or
    pass --skip-manual to have such rows skipped (NEEDS_MANUAL) instead of
    failing."""


class NeedsManualError(RuntimeError):
    """Manual input is needed, no hint covers it, and the run asked for such rows
    to be skipped (``run_experiment.py --skip-manual``).

    The pipeline turns this into a ``NEEDS_MANUAL`` result for the row and carries
    on with the next one, so an unattended batch never blocks on a prompt. The
    message names the run dir and the calls still to judge, so the row can be
    re-run interactively afterwards."""


def manifest_path(run_dir: Path, worker_output: str) -> Path:
    """Locate the ``manual_requests.json`` gamerv wrote for this pass.

    Prefer the path on the worker's ``NEEDS_MANUAL:<path>`` line; fall back to
    the conventional location under the run dir.
    """
    for line in worker_output.splitlines():
        if line.startswith("NEEDS_MANUAL:"):
            candidate = Path(line.split(":", 1)[1].strip())
            if candidate.exists():
                return candidate
    return run_dir / "manual_requests.json"


def collect(run_dir: Path, worker_output: str,
            hint_path: Path | None = None,
            defaults: dict[str, bool] | None = None,
            skip: bool = False) -> int:
    """Resolve every pending manual predicate and persist the answers.

    Returns the number of answers gathered (0 when the manifest lists none).

    Each request is answered in two ways. A request whose ``call`` has a per-call
    default (``defaults``, from ``hints.default_map`` — a bare-call entry in the
    tape hint or the branch ``_defaults.hint``) is auto-answered with no prompt,
    index-free, so it works across traces and re-recordings. Any request no
    default covers is asked at the terminal, in trace order (by ``event_index``,
    not gamerv's manifest order) — and raises ``NoTTYError`` only if
    such *unresolved* prompts remain and stdin is not a terminal (fully-defaulted
    traces need no TTY).

    When ``hint_path`` is given, the operator's *hand* answers are appended to that
    hint file so a later ``--manual-auto`` run can replay them; default-resolved
    answers are not baked in — the default stays the single source of truth.

    ``skip`` is the unattended-batch mode: rather than ask (or raise ``NoTTYError``),
    a request no default covers raises :class:`NeedsManualError` and no answers are
    written at all — not even the defaulted ones, since a half-answered run dir
    would only mislead the next pass. The row is then reported ``NEEDS_MANUAL`` and
    the batch moves on. A trace the hints *do* cover is unaffected: it is answered
    and validated exactly as under ``--manual-auto``.
    """
    requests = _read_requests(manifest_path(run_dir, worker_output))
    if not requests:
        return 0
    defaults = defaults or {}

    resolved = {r["key"]: defaults[r["call"]]
                for r in requests if r.get("call") in defaults}
    to_prompt = sorted((r for r in requests if r["key"] not in resolved),
                       key=_event_order)

    # Skip before the TTY check: --skip-manual is meant to work both ways, on a
    # detached batch (where the prompt is impossible) and on an interactive one
    # left running overnight (where it is possible but nobody is there to answer).
    if to_prompt and skip:
        note = _pending_note(run_dir, to_prompt)
        print(f"\n  [manual] {note}")
        print("  [manual] --skip-manual: leaving this row unanswered "
              "(reported NEEDS_MANUAL); re-run it interactively to classify.")
        raise NeedsManualError(note)
    if to_prompt and not sys.stdin.isatty():
        raise NoTTYError(
            f"{len(to_prompt)} predicate value(s) need manual input, but the "
            f"controller has no TTY to prompt on. Add a per-call default to the "
            f"hint (bare-call key, or hints/<branch>/{hints.DEFAULTS_FILE}), run "
            f"the experiment interactively, or pass --skip-manual to skip such "
            f"rows."
        )
    if resolved:
        print(f"\n  [manual] auto-answered {len(resolved)} predicate value(s) "
              f"from per-call defaults.")

    hand: dict[str, bool] = {}
    if to_prompt:
        print(f"\n  [manual] {len(to_prompt)} predicate value(s) need your "
              f"judgement:")
        hand = {r["key"]: _ask_one(run_dir, r) for r in to_prompt}

    answers = {**resolved, **hand}
    _save_answers(run_dir, answers)
    if hint_path is not None and hand:
        hints.save(hint_path, hand)
    return len(answers)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #

def _pending_note(run_dir: Path, pending: list[dict]) -> str:
    """One line naming what is still to be judged, for the skipped row's note.

    Names the run dir (which recording of the row needs attention) and the first
    few calls with their event index — enough to find them in the row's hint
    without re-running validate. Kept short: it ends up in a results CSV cell.
    """
    shown = ", ".join(f"{r.get('call')} @ event {r.get('event_index')}"
                      for r in pending[:_NOTE_CALLS])
    more = (f", +{len(pending) - _NOTE_CALLS} more"
            if len(pending) > _NOTE_CALLS else "")
    return (f"{len(pending)} predicate value(s) in {run_dir.name} need manual "
            f"judgement and no hint covers them ({shown}{more})")


def _event_order(request: dict) -> tuple[int, float, str]:
    """Sort key that walks the operator through the trace in event order.

    gamerv lists requests in whatever order its validators produced them, which
    can jump around (event 30 before event 20). Requests without a usable
    ``event_index`` keep a stable spot at the end, ordered by call name.
    """
    try:
        return (0, float(request.get("event_index")), request.get("call") or "")
    except (TypeError, ValueError):
        return (1, 0.0, request.get("call") or "")


def _read_requests(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text()).get("requests", [])
    except (OSError, json.JSONDecodeError):
        return []


def _ask_one(run_dir: Path, request: dict) -> bool:
    """Show one request's evidence and read t/f.

    A visual request carries a ``clip.mp4`` of the same window the frames were
    sampled from, and that is the *only* thing popped open for it: the clip is
    the form a human judges well, and spraying eight stills across the desktop
    viewer on top of it is just windows to close. The frames' paths are still
    printed (they are the record of what the automatic judge saw), and they are
    opened when there is no clip — absent on manifests predating the clip, and on
    windows too short to make a playable one.
    """
    print(f"\n    Event #{request.get('event_index')} "
          f"({request.get('event_type')}) — {request.get('call')}")
    if request.get("question"):
        print(f"    {request['question']}")
    clip = request.get("video")
    if clip:
        asset = run_dir / clip
        print(f"      clip:  {asset}")
        _open(asset)
    for rel in request.get("images", []):
        asset = run_dir / rel
        print(f"      frame: {asset}")
        if not clip:
            _open(asset)
    if request.get("audio"):
        asset = run_dir / request["audio"]
        print(f"      audio: {asset}")
        _open(asset)
    return _ask_bool()


def _ask_bool() -> bool:
    while True:
        raw = input("      value [t/f]? ").strip().lower()
        if raw in ("t", "true", "y", "yes", "1"):
            return True
        if raw in ("f", "false", "n", "no", "0"):
            return False
        print("      Please answer 't' (true) or 'f' (false).")


def _open(asset: Path) -> None:
    """Best-effort: pop the clip/frame/audio open in the desktop viewer so the
    operator can actually judge it (the terminal can't render it). Failures
    (no xdg-open, no display) are silent — the printed path is the fallback."""
    try:
        subprocess.Popen(
            ["xdg-open", str(asset)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _save_answers(run_dir: Path, answers: dict) -> None:
    """Merge ``{key: bool}`` into manual_answers.json (the file gamerv re-reads
    on the next validate pass), keeping any answers already recorded."""
    hints.merge_into(run_dir / "manual_answers.json", answers)
