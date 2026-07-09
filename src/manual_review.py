"""Human-in-the-loop answers for gamerv's manual predicate values.

When ``MANUAL_PREDICATES`` is active (set in ``gamerv/.env``), gamerv can't
compute the visual (LLM) and audio predicates itself. Its ``validate`` worker
instead dumps the exact frames/audio it would have judged under
``<run_dir>/manual_assets/``, lists each undecided predicate in
``<run_dir>/manual_requests.json``, and exits ``NEEDS_MANUAL`` (3).

This module is the controller's side of that handshake: it shows the evidence to
the operator at the terminal, reads a True/False for each request, and writes the
answers to ``<run_dir>/manual_answers.json`` — the file gamerv re-reads on the
next validate pass. It is the headless-controller analogue of the gamerv GUI's
``ManualPredicatesDialog``; ``gamerv.validate`` calls ``collect`` and re-invokes
validate until the verdict is written.

The JSON layout (``manual_requests.json`` / ``manual_answers.json``) and the
per-request ``key`` are gamerv's stable contract — see ``validation/manual.py``.
Asset paths in a request are stored relative to the run dir.
"""

import json
import subprocess
import sys
from pathlib import Path

import hints


class NoTTYError(RuntimeError):
    """Manual input is needed but there is no terminal to prompt the operator on
    (e.g. a headless/CI controller run). Turn MANUAL_PREDICATES off, or run the
    experiment interactively."""


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
            defaults: dict[str, bool] | None = None) -> int:
    """Resolve every pending manual predicate and persist the answers.

    Returns the number of answers gathered (0 when the manifest lists none).

    Each request is answered in two ways. A request whose ``call`` has a per-call
    default (``defaults``, from ``hints.default_map`` — a bare-call entry in the
    tape hint or the branch ``_defaults.hint``) is auto-answered with no prompt,
    index-free, so it works across traces and re-recordings. Any request no
    default covers is asked at the terminal — and raises ``NoTTYError`` only if
    such *unresolved* prompts remain and stdin is not a terminal (fully-defaulted
    traces need no TTY).

    When ``hint_path`` is given, the operator's *hand* answers are appended to that
    hint file so a later ``--manual-auto`` run can replay them; default-resolved
    answers are not baked in — the default stays the single source of truth.
    """
    requests = _read_requests(manifest_path(run_dir, worker_output))
    if not requests:
        return 0
    defaults = defaults or {}

    resolved = {r["key"]: defaults[r["call"]]
                for r in requests if r.get("call") in defaults}
    to_prompt = [r for r in requests if r["key"] not in resolved]

    if to_prompt and not sys.stdin.isatty():
        raise NoTTYError(
            f"{len(to_prompt)} predicate value(s) need manual input, but the "
            f"controller has no TTY to prompt on. Add a per-call default to the "
            f"hint (bare-call key, or hints/<branch>/{hints.DEFAULTS_FILE}), run "
            f"the experiment interactively, or turn MANUAL_PREDICATES off."
        )
    if resolved:
        print(f"\n  [manual] auto-answered {len(resolved)} predicate value(s) "
              f"from per-call defaults.")

    hand: dict[str, bool] = {}
    if to_prompt:
        print(f"\n  [manual] {len(to_prompt)} predicate value(s) need your "
              f"judgement (MANUAL_PREDICATES is on):")
        hand = {r["key"]: _ask_one(run_dir, r) for r in to_prompt}

    answers = {**resolved, **hand}
    _save_answers(run_dir, answers)
    if hint_path is not None and hand:
        hints.save(hint_path, hand)
    return len(answers)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #

def _read_requests(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text()).get("requests", [])
    except (OSError, json.JSONDecodeError):
        return []


def _ask_one(run_dir: Path, request: dict) -> bool:
    """Show one request's evidence (opening the frames/audio) and read t/f."""
    print(f"\n    Event #{request.get('event_index')} "
          f"({request.get('event_type')}) — {request.get('call')}")
    if request.get("question"):
        print(f"    {request['question']}")
    for rel in request.get("images", []):
        asset = run_dir / rel
        print(f"      frame: {asset}")
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
    """Best-effort: pop the frame/audio open in the desktop viewer so the
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
