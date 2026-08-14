"""Cached operator answers ("hints") for the manual image/audio predicate prompts.

When ``MANUAL_PREDICATES`` is on, gamerv can't compute the visual/audio
predicates and the controller prompts the operator at the terminal for each one
(see ``manual_review``). Answering them by hand on every run is the cost this
module removes: each answer is cached so a later run can replay it instead of
re-asking.

The cache is **per recording** — one ``.hint`` file per ``.rec`` tape — because
gamerv's answer key (``"<state_index>::<call>"``) resets per trace, so the same
key can name different predicates in two tapes; aggregating them would be
ambiguous. A run dir carries a ``.hint_id`` sidecar naming its hint file (written
at record time, when the controller knows which tape produced the run); the file
itself is the same ``{key: bool}`` shape as ``manual_answers.json``.

**Per-call defaults** sidestep the per-trace index entirely. A hint entry whose
key is a *bare call string* (no ``::``) — e.g. ``"ObjectPresent(\"Tux\")": true``
— answers that predicate at EVERY state it is evaluated in the trace, regardless
of the state index. It is index-free, so one such entry survives re-recording and
can be shared across traces. Two places carry them: bare-call keys inside a tape's
own ``.hint``, and a branch-wide ``hints/<branch>/_defaults.hint`` that applies to
every tape of the branch (write once instead of duplicating per recording). They
are only useful when the answer is *uniform* across the trace's states; a
predicate whose truth varies state-to-state still needs the per-state keys.
``manual_review.collect`` resolves each of gamerv's requests against these
defaults and only prompts for calls no default covers.

Operations:
  * **seed** — before validating with ``--manual-auto``, merge the stored *exact*
    (``<idx>::<call>``) hint answers into the run dir's ``manual_answers.json`` so
    gamerv's ``lookup`` finds them and only requests predicates not yet recorded.
  * **default_map** — the merged per-call defaults (branch file + tape bare-call
    keys) that ``collect`` uses to auto-answer requests without prompting.
  * **save** — whenever the operator answers a prompt, append the answers to the
    hint file so the cache grows (the controller records on every interactive run).

This module imports only ``config`` (no import cycle): ``manual_review`` and
``gamerv`` import it, not the other way round.
"""

import json
from pathlib import Path

import config

# Name of the per-run sidecar that points a run dir at its hint file. Lives
# inside the run dir so it survives gamerv's log archiving (clear_logs moves the
# whole run dir, sidecar included).
SIDECAR = ".hint_id"

# Branch-wide per-call defaults file, beside the tape hints in hints/<branch>/.
# Same {call: bool} shape (bare-call keys); applies to every tape of the branch.
DEFAULTS_FILE = "_defaults.hint"


def _is_exact(key: str) -> bool:
    """True for a per-state key (``<idx>::<call>``), False for a bare-call default."""
    return "::" in key


def write_sidecar(run_dir: Path, rel_id: str) -> None:
    """Tag ``run_dir`` with the relative hint id of the tape that produced it.

    ``rel_id`` is a ``<branch>/<recX>`` style path; ``path_for`` turns it into the
    ``.hint`` file.
    """
    (run_dir / SIDECAR).write_text(rel_id)


def path_for(run_dir: Path) -> Path | None:
    """The hint file for ``run_dir``, or ``None`` if it has no sidecar.

    A missing sidecar (e.g. a recording that failed before the run dir was
    tagged) means no hints for that run — it falls back to prompting.
    """
    try:
        rel_id = (run_dir / SIDECAR).read_text().strip()
    except OSError:
        return None
    if not rel_id:
        return None
    return config.HINTS_DIR / (rel_id + ".hint")


def load(path: Path) -> dict[str, bool]:
    """Read a ``{key: bool}`` hint file (empty dict if absent/unreadable)."""
    try:
        data = json.loads(path.read_text())
        return {str(k): bool(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def save(path: Path, answers: dict[str, bool]) -> None:
    """Merge ``answers`` into the hint file, creating the ``hints/<branch>/`` dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    merge_into(path, answers)


def seed(run_dir: Path, hint_path: Path) -> None:
    """Pre-fill ``run_dir``'s ``manual_answers.json`` from the stored hint, so the
    next validate pass finds the answers and only asks about anything not cached.

    Only the *exact* per-state keys are seeded; bare-call defaults are applied
    later by ``collect`` (they aren't real gamerv keys, so seeding them would just
    add dead entries to ``manual_answers.json``)."""
    exact = {k: v for k, v in load(hint_path).items() if _is_exact(k)}
    merge_into(run_dir / "manual_answers.json", exact)


def default_map(hint_path: Path) -> dict[str, bool]:
    """Per-call default answers for a tape, keyed by the bare call string.

    Merges two layers, most specific last: the branch-wide ``_defaults.hint``
    beside the tape hints, then any bare-call (no ``::``) entries in the tape's own
    ``.hint`` (so a tape can override the branch default for one call). A default
    answers EVERY state at which that call is evaluated, independent of the
    per-trace state index — so it is reusable across traces without re-collecting.
    Returns ``{}`` when neither file contributes a default."""
    result: dict[str, bool] = {}
    for source in (hint_path.parent / DEFAULTS_FILE, hint_path):
        for k, v in load(source).items():
            if not _is_exact(k):
                result[k] = v
    return result


def merge_into(json_path: Path, answers: dict[str, bool]) -> None:
    """Merge ``{key: bool}`` into a JSON file, keeping any entries already there.

    The shared writer behind both the hint files and ``manual_answers.json`` (the
    file gamerv re-reads each validate pass)."""
    if not answers:
        return
    try:
        existing = json.loads(json_path.read_text())
    except (OSError, json.JSONDecodeError):
        existing = {}
    existing.update({str(k): bool(v) for k, v in answers.items()})
    json_path.write_text(json.dumps(existing, indent=2))
