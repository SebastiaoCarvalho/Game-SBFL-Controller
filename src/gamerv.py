"""GameRV side of the pipeline.

Thin wrappers around the gamerv ``worker.py`` ops (each its own subprocess, the
way gamerv intends — clean native-library state per op) plus a few direct
filesystem helpers (properties config, logs dir). All worker ops run with
``cwd=GAMERV_DIR`` using the same bare ``python`` ``run.sh`` uses.
"""

import json
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import config
import hints
import manual_review


class GameRVError(RuntimeError):
    pass


# Exit code gamerv's `validate` worker uses when it needs the operator to supply
# visual/audio predicate values (see manual_review and gamerv's gui/worker.py /
# runners/validate.py).
NEEDS_MANUAL_EXIT = 3


# --- Step 5: logging (drive worker.py `log` as a long-running subprocess) -----
# run_log() starts capture, clears the event channel, prints
# "Recording started — press Ctrl+C to stop.", then blocks until SIGINT, at
# which point it pulls events and writes the run dir. We start it, wait for that
# ready line (so the replay only fires after the channel is cleared), then SIGINT.

_LOG_READY_MARKER = "Recording started"


@dataclass
class LogSession:
    proc: subprocess.Popen
    ready: threading.Event
    reader: threading.Thread


def _drain_log_output(proc: subprocess.Popen, ready: threading.Event) -> None:
    """Echo the log subprocess's lines and flag when capture has started.

    Runs on its own thread for the whole session: the worker goes silent after
    the ready line, so a foreground readline would block; draining continuously
    also prevents the pipe from filling if a native lib chatters mid-capture.
    """
    stream = proc.stdout
    if stream is None:
        return
    for line in iter(stream.readline, ""):
        print("    [log] " + line.rstrip(), flush=True)
        if _LOG_READY_MARKER in line:
            ready.set()
    stream.close()


def start_log() -> LogSession:
    """Launch `worker.py log` and drain its output on a background thread."""
    proc = subprocess.Popen(
        config.gamerv_cmd([str(config.GAMERV_WORKER), "log"]),
        cwd=str(config.GAMERV_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    ready = threading.Event()
    reader = threading.Thread(target=_drain_log_output, args=(proc, ready), daemon=True)
    reader.start()
    return LogSession(proc, ready, reader)


def wait_log_started(session: LogSession, timeout: float = 120.0) -> bool:
    """Block until the log reports capture started, or the process dies/times out.

    (The first run also waits here while the GNOME portal asks you to pick the
    SuperTux window; later runs reuse the restore token and are near-instant.)
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if session.ready.wait(0.5):
            return True
        if session.proc.poll() is not None:
            return False  # exited before signalling ready
    return False


def stop_log(session: LogSession) -> None:
    """Stop the logger so it finalizes the trace, escalating if it won't exit.

    SIGINT is the graceful stop run_log handles (pull events from the still-live
    game, close capture, write the run dir). If its teardown hangs (e.g. the game
    already died), escalate to SIGTERM then SIGKILL rather than block forever.
    """
    proc = session.proc
    for sig, grace in ((signal.SIGINT, 30.0), (signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
        if proc.poll() is not None:
            break
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            print(f"    [log] did not exit after {sig.name}; escalating ...")
    session.reader.join(timeout=5.0)


def _worker(*args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run ``python src/gui/worker.py <args...>`` in the gamerv repo.

    stdin is detached (DEVNULL): in manual-predicate mode gamerv's ``validate``
    self-prompts on the terminal if it sees a TTY on stdin, but its stdout is
    captured here so that prompt would be invisible and block. With no TTY it
    deterministically dumps a manifest and exits NEEDS_MANUAL instead, which
    ``validate`` below turns into the controller's own prompt.
    """
    return subprocess.run(
        config.gamerv_cmd([str(config.GAMERV_WORKER), *args]),
        cwd=str(config.GAMERV_DIR),
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
        timeout=timeout or config.GAMERV_OP_TIMEOUT,
    )


# --- Step 3: properties config -------------------------------------------

def backup_properties() -> None:
    """Park the user's properties.json so we can restore it after the experiment."""
    if config.GAMERV_PROPERTIES.exists():
        shutil.copy2(config.GAMERV_PROPERTIES, config.GAMERV_PROPERTIES_BACKUP)


def restore_properties() -> None:
    if config.GAMERV_PROPERTIES_BACKUP.exists():
        shutil.copy2(config.GAMERV_PROPERTIES_BACKUP, config.GAMERV_PROPERTIES)


def _last_json(text: str) -> dict | None:
    """The last JSON object printed on its own line (worker ops emit trailing JSON)."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def available_games() -> list[str]:
    """The games gamerv has a catalog bundle for (``config/games/<name>`` folders).

    Used to preflight a dataset row: if its Game Name isn't here, gamerv would
    silently fall back to its built-in default catalog, so the pipeline fails the
    row instead. Returns [] if the worker output can't be parsed.
    """
    proc = _worker("game_config")
    obj = _last_json(proc.stdout)
    games = obj.get("games") if obj else None
    return [str(g) for g in games] if isinstance(games, list) else []


#: The per-game files gamerv's loaders read; each falls back to a built-in Super Tux
#: default when absent, so a missing one is a silent change of game, not an error.
GAME_CONFIG_FILES = ("game_profile.json", "event_types.json")


def missing_game_files(game_name: str) -> list[str]:
    """Which of :data:`GAME_CONFIG_FILES` the game's gamerv bundle lacks."""
    folder = config.GAMERV_DIR / "config" / "games" / game_name
    return [f for f in GAME_CONFIG_FILES if not (folder / f).is_file()]


def check_formula(formula: str) -> tuple[bool, str | None]:
    """Validate predicate names/arity + Spot syntax. Returns (ok, error)."""
    proc = _worker("check_formula", formula)
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "ok" in obj:
            return bool(obj["ok"]), obj.get("error")
    raise GameRVError(
        f"check_formula produced no JSON verdict:\n{proc.stdout}\n{proc.stderr}"
    )


def set_single_property(name: str, formula: str, description: str = "") -> None:
    """Make ``formula`` the ONLY enabled safety property gamerv verifies.

    Writes config/properties.json directly (stable, documented format). Call
    ``backup_properties`` once before the first use so it can be restored.
    """
    payload = {"properties": [{
        "name": name,
        "formula": formula,
        "description": description,
        "enabled": True,
    }]}
    config.GAMERV_PROPERTIES.parent.mkdir(parents=True, exist_ok=True)
    config.GAMERV_PROPERTIES.write_text(json.dumps(payload, indent=2) + "\n")


# --- Step 4: clear logs ---------------------------------------------------

def clear_logs(row: int | None = None) -> None:
    """Clear gamerv/logs/ for a fresh run, archiving its contents instead of
    deleting them.

    Every run subdir (and .last_run) is *moved* into a subdir under
    config.GAMERV_LOGS_ARCHIVE_DIR so a previous run's traces can be restored
    later. The archive subdir is named ``row<row>_<timestamp>`` (or just the
    timestamp when no row is given) so it ties back to the dataset row that
    triggered the clear. logs/ itself is kept (empty)."""
    logs = config.GAMERV_LOGS_DIR
    if not logs.exists():
        logs.mkdir(parents=True)
        return

    children = [c for c in logs.iterdir() if c.is_dir() or c.name == ".last_run"]
    if not children:
        return

    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = f"row{row}_{stamp}" if row is not None else stamp
    archive = config.GAMERV_LOGS_ARCHIVE_DIR / name
    archive.mkdir(parents=True, exist_ok=True)
    for child in children:
        shutil.move(str(child), str(archive / child.name))


# --- Step 6: validate -----------------------------------------------------

def run_dirs(include_empty: bool = False) -> list[Path]:
    """Run directories under logs/ that actually hold a recorded trace.

    A run whose ``events.jsonl`` exists but is EMPTY is not a trace: the capture
    stack ran and wrote its files, but the game emitted no state. It happens for
    real — a game that renders nothing (a browser build that never paints), or one
    whose snapshot throws on every event — and the video/audio come out normal
    either way, so nothing downstream notices. Such a run is excluded by default,
    which keeps it out of the log cache and out of validation instead of letting it
    masquerade as a trace with no violations.

    ``include_empty`` returns them anyway, for callers that need to tell "the game
    logged nothing" apart from "there was no run at all".
    """
    logs = config.GAMERV_LOGS_DIR
    if not logs.exists():
        return []
    def has_trace(d: Path) -> bool:
        f = d / "events.jsonl"
        if not f.exists():
            return False
        try:
            return include_empty or f.stat().st_size > 0
        except OSError:
            return False
    return sorted(d for d in logs.iterdir() if d.is_dir() and has_trace(d))


def empty_run_dirs() -> list[Path]:
    """Run dirs that were written but logged no events at all — see :func:`run_dirs`."""
    kept = {d.name for d in run_dirs()}
    return [d for d in run_dirs(include_empty=True) if d.name not in kept]


def validate(run_dir: Path, auto: bool = False,
             skip_manual: bool = False) -> subprocess.CompletedProcess:
    """Validate a logged run, answering manual predicate prompts if gamerv asks.

    gamerv has no automatic evaluator for the visual/audio predicates: its
    worker dumps the evidence and exits NEEDS_MANUAL. We then collect the
    operator's True/False for each, save them, and re-invoke — looping until the
    verdict is written (exit 0). Each pass only asks about predicates still
    unanswered, so it converges (usually in one round). A property with no
    visual/audio predicate validates in a single pass.

    The hint is first reconciled against this run's trace (``hints.reconcile``):
    its per-state keys are positions in the event log, so a trace that gained or
    lost an event since the answers were collected — a mutant tape, a re-recording
    — would replay every later answer one state off. Reconciling re-anchors them
    and drops the ones whose event is gone, so those are asked again rather than
    silently mislabelled.

    ``auto`` replays cached operator answers: the run's hint file (if any) is
    merged into manual_answers.json before the first pass, so gamerv finds those
    answers and only requests predicates not yet recorded (or exits 0 outright
    when the hint covers them all). It also loads the run's per-call defaults
    (``hints.default_map`` — bare-call entries in the tape hint or the branch
    ``_defaults.hint``), which ``collect`` uses to auto-answer any request without
    prompting, index-free so they survive re-recording and reuse across traces.
    Every answer the operator does give is saved back to the hint, regardless of
    ``auto``, so a normal run builds the cache.

    ``skip_manual`` makes the loop give up instead of prompting when the hints run
    out: ``collect`` raises ``manual_review.NeedsManualError``, which ``run_case``
    turns into a ``NEEDS_MANUAL`` row. That is what lets a batch run unattended —
    every trace the hints cover still validates normally, and only the ones needing
    a human are left for later.
    """
    hint_path = hints.path_for(run_dir)
    defaults: dict[str, bool] = {}
    if hint_path is not None:
        # Always, not just under --manual-auto: the answers appended after a
        # prompt are indexed by *this* trace, so the ones already in the file have
        # to be talking about it too.
        note = hints.reconcile(run_dir, hint_path)
        if note:
            print(f"  [hints] {note}")
        if auto:
            if hint_path.exists():
                hints.seed(run_dir, hint_path)
            defaults = hints.default_map(hint_path)
    while True:
        proc = _worker("validate", str(run_dir), timeout=config.VALIDATE_TIMEOUT)
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.returncode == 0:
            return proc
        if proc.returncode == NEEDS_MANUAL_EXIT:
            if manual_review.collect(run_dir, proc.stdout or "", hint_path,
                                     defaults, skip=skip_manual) == 0:
                # Signalled NEEDS_MANUAL but listed nothing to answer: re-running
                # would loop forever, so treat it as a failure.
                raise GameRVError(
                    f"validate {run_dir} reported NEEDS_MANUAL but listed no "
                    f"predicates to answer:\n{proc.stdout}"
                )
            continue
        # stdout FIRST: the worker prints its own reason there ("Invalid argument
        # for 'validate': frame_timestamps.csv is empty in ..."), while stderr under
        # this env is all TensorFlow/absl startup noise. Raising with stderr alone
        # put that noise in the row's ERROR note and left the actual cause nowhere.
        detail = "\n".join(s.strip() for s in (proc.stdout, proc.stderr) if s and s.strip())
        raise GameRVError(f"validate {run_dir} failed:\n{detail}")


def validate_all(auto: bool = False, skip_manual: bool = False) -> list[Path]:
    """Validate every recorded run. Returns the list of run dirs validated.

    ``skip_manual`` propagates to each pass: the first run dir whose manual
    predicates no hint covers aborts the whole row with
    ``manual_review.NeedsManualError`` (a row's verdict is read off its traces
    together, so validating the rest of them would decide phi against a partial
    picture)."""
    dirs = run_dirs()
    for d in dirs:
        print(f"  [gamerv] validating {d.name} ...")
        validate(d, auto=auto, skip_manual=skip_manual)
    return dirs


# --- Step 7: fault localization ------------------------------------------

def _verdict(run_dir: Path) -> bool | None:
    """True/False if the run has a validation verdict, None if unvalidated."""
    f = run_dir / "validation_result.json"
    if not f.exists():
        return None
    try:
        return bool(json.loads(f.read_text()).get("violated"))
    except (json.JSONDecodeError, OSError):
        return None


def last_invalid_run() -> Path | None:
    """The most recent (latest-timestamp) run whose property was violated."""
    invalid = [d for d in run_dirs() if _verdict(d) is True]
    return max(invalid, key=lambda d: d.name) if invalid else None


def fault_localize(run_dir: Path, terms: list[str] | None = None,
                   candidate_sort: str | None = None) -> str:
    """Localize the fault in ``run_dir``: rank its candidate bug conditions by Ochiai.

    When ``terms`` is given, mining is focused on exactly those raw game-state
    variables (the box-only vocabulary); otherwise gamerv discovers candidates
    generically from every logged variable. Terms are handed to the worker as a
    single JSON list so values with spaces/parens survive argv intact ("null" when
    absent, so the sort after it stays in a fixed argv slot). ``candidate_sort``
    orders the candidates within one score tier (none / type / popularity).
    """
    proc = _worker("fault_localize", str(run_dir),
                   json.dumps(list(terms) if terms else None),
                   candidate_sort or config.CANDIDATE_SORT)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.returncode != 0:
        raise GameRVError(f"fault_localize {run_dir} failed:\n{proc.stderr}")
    return proc.stdout or ""


# --- Step 8: phi against the ranked bug conditions ---------------------------

@dataclass
class Containment:
    status: str             # SUCCESS | FAIL | CONDITION_NOT_FOUND
                            # | VARIABLE_NOT_FOUND | INCONCLUSIVE | SKIPPED | ERROR
    r_text: str | None      # the top-ranked bug condition B the verdict is about
    stdout: str
    verdict: str | None = None   # gamerv's one-line "=> ..." conclusion, for notes


#: fault_localize's line naming the top of the Ochiai ranking:
#: ``Most suspicious condition:  B``.
_MOST_SUSPICIOUS_MARKER = "Most suspicious condition:"
#: The line that opens compare_bug_condition's containment check, so its own "=>"
#: conclusion can be told apart from the score comparison's.
_CONTAINMENT_MARKER = "(containment"

#: gamerv refuses to score a phi that reads a quantity no pooled state carries
#: ("Could not evaluate phi: phi reads <k>, which no logged state reports — the game
#: does not export the quantity ..."). That is a fault in the game's exported
#: vocabulary, not in the experiment, so it gets its own status (VARIABLE_NOT_FOUND)
#: rather than being lumped in with ERROR.
_UNEXPORTED_MARKER = "which no logged state reports"

#: phi's own Ochiai score against the top of the ranking. OUTRANKED (below it) is
#: decided on the data, ahead of containment: the data implicates another condition
#: more strongly. OUTRANKS (above it) means the search never built a condition as good
#: as phi: not found, whatever containment would say. TOP_RANKED (equal) is the one
#: standing under which containment decides.
_OUTRANKED_MARKER = "phi is OUTRANKED"
_OUTRANKS_MARKER = "phi OUTRANKS every mined bug condition"
_TOP_RANKED_MARKER = "phi IS a top-ranked bug condition"


def mined_r(out: str) -> str | None:
    """The top-ranked bug condition B from fault_localize's ``Most suspicious
    condition:`` line, if it printed one."""
    for line in reversed(out.splitlines()):
        s = line.strip()
        if s.startswith(_MOST_SUSPICIOUS_MARKER):
            return s[len(_MOST_SUSPICIOUS_MARKER):].strip() or None
    return None


def mine_only(run_dir: Path, terms: list[str] | None = None,
              candidate_sort: str | None = None) -> Containment:
    """Rank bug conditions with no phi to judge — the SKIPPED path for a dataset row
    whose ``Target Phi`` cell is empty.

    There is no ground truth to compare, so there is no verdict to reach: the row
    is reported ``SKIPPED`` rather than run through compare_bug_condition (which,
    handed an empty phi, could only fail to evaluate it and come back ERROR).
    Mining still runs, via fault_localize, because it is the half of step 8 that
    does not depend on phi: it produces the same ranking a compare would have mined
    and persists it (``fault_localization.json``), so the row still contributes its
    most suspicious condition and candidate count — the numbers that let a ground
    truth be written for it later. A mining failure keeps the row SKIPPED (the row
    was never going to yield a verdict); the reason is carried in the note.
    """
    try:
        out = fault_localize(run_dir, terms, candidate_sort)
    except GameRVError as exc:
        return Containment("SKIPPED", None, str(exc),
                           "no ground-truth phi (mining failed)")
    return Containment("SKIPPED", mined_r(out), out,
                       "no ground-truth phi for this row — nothing to compare")


def _unexported_note(out: str) -> str | None:
    """The missing quantities from gamerv's unexported-variable message, as one line.

    gamerv spells the reason out over four sentences (what it read, why it refuses
    to score it, what to do about it); the full text stays in ``Containment.stdout``,
    while the notes column keeps only the part that identifies the row: which
    quantities phi reads that the game never exports.
    """
    for line in out.splitlines():
        head, sep, tail = line.partition("phi reads ")
        if not sep or _UNEXPORTED_MARKER not in tail:
            continue
        refs = tail.split(", " + _UNEXPORTED_MARKER)[0].strip()
        return (f"phi reads {refs}, which the game does not export "
                f"(no logged state reports it) — phi was not evaluated")
    return None


def parse_containment(out: str) -> Containment:
    """Classify a compare_bug_condition stdout into a verdict on phi.

    gamerv scores phi exactly like every mined candidate — Ochiai over the pooled
    violation (failing) and good (passing) states — and prints where that score
    stands against the top of the ranking: *OUTRANKED* (below it), *top-ranked*
    (equal), *OUTRANKS* (above it) or *VACUOUS* (phi holds on no violation). For a
    top-ranked phi it then checks containment against the top tier, one candidate at
    a time — ``Exists B in the top tier: phi -> B`` ("IS CONTAINED in a single
    top-ranked bug condition" / "is NOT contained in ANY single mined bug
    condition"). A phi reading a non-raw catalogue predicate has no score and goes
    straight to containment.

    * ``VARIABLE_NOT_FOUND`` first: phi was never scored, because it reads a quantity
      no logged state reports. Nothing was decided about phi — the game does not
      export the variable its ground truth is written over — so the row is neither a
      verdict nor an experiment failure (``ERROR``).
    * ``FAIL`` for an OUTRANKED phi, decided on the data whatever a mined candidate
      says about it: containment alone would call it a find whenever *some* top-ranked
      candidate is weak enough to contain it.
    * ``CONDITION_NOT_FOUND`` for a phi that OUTRANKS every mined candidate — the
      search never built it.
    * ``SUCCESS`` when a single top-ranked candidate contains phi.
    * ``CONDITION_NOT_FOUND`` when phi ties the top score but no single top-ranked
      candidate contains it (or none is over phi's variables): the data says phi is a
      best explanation, the ranking just does not show it. The containment test is
      per-candidate on purpose — against the union of several candidates phi would
      pass as soon as they *jointly* cover it, which is not a condition any single
      mined B establishes; when that is what happened gamerv says so, the note
      carries it and ``metrics.Mining.union_only`` counts it.
    * ``FAIL`` for a phi that is not contained (and not top-ranked), or VACUOUS.
    * ``INCONCLUSIVE`` when there was nothing to decide phi against, else ``ERROR``.

    Pure string logic, separated out so it can be unit-tested without the worker.
    """
    r_text = None
    verdict = None          # first conclusion (the score comparison when phi is raw)
    contain_verdict = None  # the phi -> B conclusion under the "(containment …)" line
    in_containment = False
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("B ") and "=" in s and r_text is None:
            r_text = s.split("=", 1)[1].strip()
        if s.startswith(_CONTAINMENT_MARKER):
            in_containment = True
        if s.startswith("=>") or s.startswith("Could not") or s.startswith("Cannot compare"):
            concl = s.lstrip("=> ").strip()
            if verdict is None:
                verdict = concl
            if in_containment and contain_verdict is None:
                contain_verdict = concl

    # Signals (mutually distinct substrings, so membership tests don't collide).
    contained     = "IS CONTAINED" in out           # some single B establishes phi
    not_contained = "is NOT contained" in out       # no single B does (witness shown)
    # phi is covered only by the *union* of several candidates — gamerv reports it on a
    # miss, and it is why a row the union would call found is a miss.
    union_only    = "established only by several mined bug conditions together" in out
    no_candidate  = "not comparable by containment" in out
    outranks      = _OUTRANKS_MARKER in out
    top_ranked    = _TOP_RANKED_MARKER in out
    outranked     = _OUTRANKED_MARKER in out
    vacuous       = "VACUOUS" in out
    nothing_mined = "No bug condition B was mined" in out

    if _UNEXPORTED_MARKER in out:
        return Containment("VARIABLE_NOT_FOUND", r_text, out,
                           _unexported_note(out) or verdict)
    if outranked:
        return Containment("FAIL", r_text, out, verdict)
    if outranks:
        return Containment("CONDITION_NOT_FOUND", r_text, out, (
            "phi scores above every mined bug condition — the search did not "
            "build it"))
    if contained:
        status, note = "SUCCESS", (contain_verdict or verdict)
    elif (not_contained or no_candidate) and top_ranked:
        status = "CONDITION_NOT_FOUND"
        note = ("phi ties the top score, but no single top-ranked bug condition "
                "contains it — the search did not find it"
                + (" (only the union of several candidates covers it)"
                   if union_only else ""))
    elif not_contained or vacuous:
        status, note = "FAIL", (contain_verdict or verdict)
    elif (nothing_mined or "Cannot compare phi" in out
          or "No violated formula" in out or no_candidate):
        status, note = "INCONCLUSIVE", verdict
    else:
        status, note = "ERROR", verdict
    return Containment(status, r_text, out, note)


def compare_bug_condition(run_dir: Path, phi: str,
                          terms: list[str] | None = None,
                          candidate_sort: str | None = None,
                          echo: bool = True) -> Containment:
    """Run gamerv's compare_bug_condition and classify the verdict on phi.

    ``terms`` focuses mining on those raw variables (box-only), matching the
    vocabulary used in fault localization, so the verdict is decided against the
    same ranking. Empty/None mines generically. ``candidate_sort`` orders the mined
    candidates within one score tier (none / type / popularity — only the order they
    are reported and checked in, never which candidates are mined); it defaults to
    the config value. terms and sort are passed as fixed argv positions (terms as
    JSON, "null" when absent).

    An empty ``phi`` (a dataset row with no ground truth) is not an error: it is
    routed to :func:`mine_only`, which ranks the candidates and returns ``SKIPPED``.

    ``echo=False`` keeps gamerv's output (the whole candidate list) off stdout — for
    a re-ranking call, whose only product is the verdict it persists.
    """
    if not phi.strip():
        # No ground truth to compare: gamerv would only report that it could not
        # evaluate an empty phi, which reads as an ERROR. Rank the candidates instead.
        return mine_only(run_dir, terms, candidate_sort)
    proc = _worker("compare_bug_condition", str(run_dir), phi,
                   json.dumps(list(terms) if terms else None),
                   candidate_sort or config.CANDIDATE_SORT)
    out = proc.stdout or ""
    if proc.stdout and echo:
        print(out, end="")
    if proc.returncode != 0:
        return Containment("ERROR", None, out + "\n" + (proc.stderr or ""))
    return parse_containment(out)
