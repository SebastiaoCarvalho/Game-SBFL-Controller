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

The per-state keys are only valid against the trace they were collected on, so
each hint carries a ``<recN>.hint.trace`` signature of that trace's events and
``reconcile`` re-anchors the answers whenever the trace shifts — see the *trace
drift* section below.

Operations:
  * **reconcile** — before anything is seeded, diff the run's event sequence
    against the one the hint was collected on and re-anchor (or drop) its
    per-state answers, so a trace that gained or lost an event doesn't replay
    every later answer one state off.
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

import difflib
import json
import shutil
from pathlib import Path

import config

# Name of the per-run sidecar that points a run dir at its hint file. Lives
# inside the run dir so it survives gamerv's log archiving (clear_logs moves the
# whole run dir, sidecar included).
SIDECAR = ".hint_id"

# Branch-wide per-call defaults file, beside the tape hints in hints/<branch>/.
# Same {call: bool} shape (bare-call keys); applies to every tape of the branch.
DEFAULTS_FILE = "_defaults.hint"

# Appended to a hint's name for its trace signature (recN.hint.trace) and for the
# copy kept when reconcile rewrites it (recN.hint.bak). Both sit beside the hint.
TRACE_SUFFIX = ".trace"
BACKUP_SUFFIX = ".bak"


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


# --------------------------------------------------------------------------- #
# trace drift
# --------------------------------------------------------------------------- #
#
# An exact hint key is ``<state_index>::<call>``, and the state index is just the
# position of the event in the tape's ``events.jsonl``. That index is only
# meaningful against the trace it was collected on: as soon as the trace gains or
# loses an event -- a mutant tape that fires one extra ``hurt``, a re-recording, a
# rebuilt binary that behaves slightly differently -- every answer after the
# insertion point slides onto the *wrong* event. Nothing used to notice: seeding
# fed those shifted answers straight to gamerv's ``lookup``, which only requests
# the keys it has no answer for, so the operator was asked about the genuinely new
# states and never about the ones now silently mislabelled.
#
# So each hint file carries a sibling ``<name>.hint.trace`` recording the event-type
# sequence the answers were collected against. ``reconcile`` compares it to the
# trace in the run dir before anything is seeded and, when they differ, diffs the
# two sequences and re-anchors each answer onto its event's new index. Answers
# whose event no longer exists are dropped, so they get asked again instead of
# landing on a neighbour.


def trace_path(hint_path: Path) -> Path:
    """The sibling file recording the event sequence ``hint_path`` was collected on."""
    return hint_path.with_name(hint_path.name + TRACE_SUFFIX)


def trace_signature(run_dir: Path) -> list[str]:
    """The run's event types, in the order gamerv indexes them (``[]`` if absent).

    Mirrors ``validation.event_log_reader``: one entry per usable ``events.jsonl``
    record, sorted by timestamp, so position *i* here is state index *i* in the
    trace gamerv builds.
    """
    records: list[tuple[float, str]] = []
    try:
        with open(run_dir / "events.jsonl") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") is None or record.get("timestamp") is None:
                    continue
                try:
                    records.append((float(record["timestamp"]), str(record["type"])))
                except (TypeError, ValueError):
                    continue
    except OSError:
        return []
    records.sort(key=lambda r: r[0])
    return [event_type for _, event_type in records]


def load_signature(hint_path: Path) -> list[str] | None:
    """The stored event sequence for ``hint_path``, or ``None`` if it has none.

    ``None`` means "unknown", not "empty": hints written before this file existed
    have no signature, and their answers can't be checked for drift.
    """
    try:
        data = json.loads(trace_path(hint_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    events = data.get("events") if isinstance(data, dict) else data
    if not isinstance(events, list):
        return None
    return [str(e) for e in events]


def save_signature(hint_path: Path, signature: list[str]) -> None:
    """Record the event sequence the hint's per-state answers now refer to."""
    path = trace_path(hint_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"events": list(signature)}, indent=2))


def reconcile(run_dir: Path, hint_path: Path) -> str | None:
    """Re-anchor ``hint_path``'s per-state answers onto ``run_dir``'s trace.

    Call this before ``seed``/``default_map``, on every validate (not just
    ``--manual-auto``): it keeps the file's indices meaning what they say, so both
    the answers replayed into gamerv and the ones appended after a prompt describe
    the same trace.

    Does nothing when the trace still matches the stored signature, or when there
    is no event log to compare against. Otherwise it rewrites the hint in place
    (keeping a ``.bak``) and returns a one-line summary for the caller to print;
    ``None`` means there was nothing to report. Bare-call defaults are never
    touched -- being index-free is the whole point of them.
    """
    signature = trace_signature(run_dir)
    if not signature:
        return None
    stored = load_signature(hint_path)
    if stored == signature:
        return None

    answers = load(hint_path)
    exact = {k: v for k, v in answers.items() if _is_exact(k)}

    if stored is None:
        # A hint from before signatures existed. Its answers can't be diffed
        # against anything, so the most we can do is adopt the current trace as
        # the baseline (drift from here on is caught) and drop the answers this
        # trace provably can't have -- ones pointing past its last event.
        save_signature(hint_path, signature)
        if not exact:
            return None
        stale = {k for k in exact if _index_of(k) >= len(signature)}
        if not stale:
            return (f"{hint_path.name} predates trace tracking: its {len(exact)} "
                    f"per-state answer(s) are replayed unchecked -- delete the "
                    f"file to re-collect if the trace has changed since.")
        _rewrite(hint_path, {k: v for k, v in answers.items() if k not in stale},
                 backup=True)
        return (f"{hint_path.name} predates trace tracking and {len(stale)} "
                f"answer(s) fall outside its {len(signature)}-event trace: dropped "
                f"those (they'll be asked again); the other {len(exact) - len(stale)} "
                f"are replayed unchecked.")

    mapping = _align(stored, signature)
    migrated = {k: v for k, v in answers.items() if not _is_exact(k)}
    moved = 0
    dropped = 0
    for key, value in exact.items():
        index = _index_of(key)
        new_index = mapping.get(index)
        if new_index is None:
            dropped += 1
            continue
        if new_index != index:
            moved += 1
        migrated[f"{new_index}::{key.split('::', 1)[1]}"] = value
    _rewrite(hint_path, migrated, backup=True)
    save_signature(hint_path, signature)
    return (f"{hint_path.name}: trace changed ({len(stored)} -> {len(signature)} "
            f"events), so cached answers no longer line up -- re-anchored {moved} "
            f"to the event's new index, dropped {dropped} whose event is gone "
            f"(those get asked again). Previous file kept as "
            f"{hint_path.name}{BACKUP_SUFFIX}.")


def _index_of(key: str) -> int:
    """The state index of an exact key, or ``-1`` if it doesn't carry one."""
    head = key.split("::", 1)[0]
    try:
        return int(head)
    except ValueError:
        return -1


def _align(old: list[str], new: list[str]) -> dict[int, int]:
    """Map old state indices to new ones by diffing the two event sequences.

    Only the events the diff matches get an entry, so an index whose event was
    inserted/removed/retyped is simply absent -- the caller drops its answer
    rather than guessing a neighbour.
    """
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    mapping: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset] = block.b + offset
    return mapping


def _rewrite(hint_path: Path, answers: dict[str, bool], backup: bool = False) -> None:
    """Replace the hint file's contents (``merge_into`` can only ever add keys)."""
    if backup and hint_path.exists():
        shutil.copyfile(hint_path, hint_path.with_name(hint_path.name + BACKUP_SUFFIX))
    hint_path.parent.mkdir(parents=True, exist_ok=True)
    hint_path.write_text(json.dumps(answers, indent=2))
