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


# Exit code gamerv's `validate` worker uses when MANUAL_PREDICATES is on and it
# needs the operator to supply visual/audio predicate values (see manual_review
# and gamerv's gui/worker.py / runners/validate.py).
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

def run_dirs() -> list[Path]:
    """Run directories under logs/ that actually hold a recorded trace."""
    logs = config.GAMERV_LOGS_DIR
    if not logs.exists():
        return []
    return sorted(d for d in logs.iterdir()
                  if d.is_dir() and (d / "events.jsonl").exists())


def validate(run_dir: Path, auto: bool = False,
             skip_manual: bool = False) -> subprocess.CompletedProcess:
    """Validate a logged run, answering manual predicate prompts if gamerv asks.

    With MANUAL_PREDICATES active, gamerv can't compute the visual/audio
    predicates itself: its worker dumps the evidence and exits NEEDS_MANUAL. We
    then collect the operator's True/False for each, save them, and re-invoke —
    looping until the verdict is written (exit 0). Each pass only asks about
    predicates still unanswered, so it converges (usually in one round). When
    manual mode is off this is a single pass, exactly as before.

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
        raise GameRVError(f"validate {run_dir} failed:\n{proc.stderr}")


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
                   strategy: str | None = None,
                   candidate_sort: str | None = None) -> str:
    """Localize the fault in ``run_dir``, mining a refinement R.

    When ``terms`` is given, mining is focused on exactly those raw game-state
    variables (the box-only vocabulary); otherwise gamerv discovers candidates
    generically from every logged variable. Terms are handed to the worker as a
    single JSON list so values with spaces/parens survive argv intact. ``strategy``
    selects how R's thresholds are placed (midpoints / weakest / gap-mean) and
    ``candidate_sort`` how the mined candidates are ordered within one score tier
    (none / type / popularity).

    Once either knob is given, terms occupies a fixed argv slot ("null" if absent)
    so the ones after it stay positionally addressable — the same fixed-slot
    contract :func:`compare_refinement` uses.
    """
    args = ["fault_localize", str(run_dir)]
    if strategy is not None or candidate_sort is not None:
        args += [json.dumps(list(terms) if terms else None),
                 strategy or config.THRESHOLD_STRATEGY,
                 candidate_sort or config.CANDIDATE_SORT]
    elif terms:
        args.append(json.dumps(list(terms)))
    proc = _worker(*args)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.returncode != 0:
        raise GameRVError(f"fault_localize {run_dir} failed:\n{proc.stderr}")
    return proc.stdout or ""


# --- Step 8: phi -> R containment ----------------------------------------

@dataclass
class Containment:
    status: str             # SUCCESS | FAIL | REFINEMENT_NOT_FOUND
                            # | VARIABLE_NOT_FOUND | INCONCLUSIVE | SKIPPED | ERROR
    r_text: str | None      # the mined refinement R, if printed
    stdout: str
    verdict: str | None = None   # gamerv's one-line "=> ..." conclusion, for notes


def _split_depth0(s: str, sep: str) -> list[str]:
    """Split ``s`` on ``sep`` at paren depth 0 only."""
    parts, buf, depth, i = [], [], 0, 0
    while i < len(s):
        c = s[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and s.startswith(sep, i):
            parts.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _unwrap(s: str) -> str:
    """Drop one pair of parens that wraps the whole expression."""
    s = s.strip()
    if not (s.startswith("(") and s.endswith(")")):
        return s
    depth = 0
    for i, c in enumerate(s):
        depth += (c == "(") - (c == ")")
        if depth == 0 and i < len(s) - 1:
            return s                # the opening paren closes early: not a wrapper
    return s[1:-1].strip()


_REFINED_MARKER = "Refined invariant:"

#: gamerv's corroborate refuses to score a phi that reads a quantity no pooled
#: state carries ("Could not evaluate phi: phi reads <k>, which no logged state
#: reports — the game does not export the quantity ..."). That is a fault in the
#: game's exported vocabulary, not in the experiment, so it gets its own status
#: (VARIABLE_NOT_FOUND) rather than being lumped in with ERROR.
_UNEXPORTED_MARKER = "which no logged state reports"


def mined_r(out: str) -> str | None:
    """The mined R from fault_localize's ``Refined invariant:`` line, if it printed one.

    The line reads ``Refined invariant:  G((P) & (R) -> (Q))``. Splitting at paren
    depth 0 inside the ``G(...)`` isolates the antecedent ``(P) & (R)``, whose last
    top-level conjunct is R — P's own ``&``s sit at depth 1, inside P's parens.
    """
    for line in reversed(out.splitlines()):
        s = line.strip()
        if not s.startswith(_REFINED_MARKER):
            continue
        body = s[len(_REFINED_MARKER):].strip()
        if not (body.startswith("G(") and body.endswith(")")):
            return None
        antecedent = _split_depth0(body[2:-1], "->")[0]
        return _unwrap(_split_depth0(antecedent, "&")[-1]) or None
    return None


def mine_only(run_dir: Path, terms: list[str] | None = None,
              strategy: str | None = None,
              candidate_sort: str | None = None) -> Containment:
    """Mine R with no phi to judge it against — the SKIPPED path for a dataset row
    whose ``Target Phi`` cell is empty.

    There is no ground truth to compare, so there is no verdict to reach: the row
    is reported ``SKIPPED`` rather than run through compare_refinement (which,
    handed an empty phi, could only fail to evaluate it and come back ERROR).
    Mining still runs, via fault_localize, because it is the half of step 8 that
    does not depend on phi: it produces the same R_all a compare would have mined
    and persists it (``fault_localization.json``), so the row still contributes its
    mined R and candidate count — the numbers that let a ground truth be written
    for it later. A mining failure keeps the row SKIPPED (the row was never going
    to yield a verdict); the reason is carried in the note.
    """
    try:
        out = fault_localize(run_dir, terms, strategy, candidate_sort)
    except GameRVError as exc:
        return Containment("SKIPPED", None, str(exc),
                           "no ground-truth phi (mining R failed)")
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


def parse_containment(out: str, mode: str = "auto") -> Containment:
    """Classify a compare_refinement stdout into a verdict on phi as a refinement.

    gamerv's run_compare_refinement always runs the *data-direct* corroborate test
    when phi is raw-evaluable: it evaluates phi on the pooled good (P & Q) and
    violation (P & ~Q) states, independent of R's thresholds ("phi IS a sound
    refinement" / "is NOT sound" / "VACUOUS"). For a non-raw phi it instead decides
    symbolic containment — ``Exists R in R_all: phi -> R``, phi against each mined
    refinement **on its own** ("IS CONTAINED in a single mined refinement" / "is NOT
    contained in ANY single mined refinement") — and in ``symbolic`` mode it *also*
    appends that containment view (a "(symbolic view …)" block) alongside the
    corroborate verdict. So in symbolic mode the output carries BOTH conclusions.

    The containment view is per-candidate on purpose: ``phi -> (R1 | … | Rn)`` against
    their union passes as soon as several candidates *jointly* cover phi, which is not
    a refinement any single mined R establishes. When that is what happened, gamerv
    says so and the row is reported as the search having missed phi — the note carries
    it, and ``metrics.Mining.union_only`` counts it.

    Classification therefore depends on ``mode``:

    * ``symbolic`` — SUCCESS is decided by the per-candidate containment ONLY. When no
      single mined refinement establishes phi (phi not contained, or nothing was mined
      over phi's variable) yet the data-direct corroborate test *does* show phi sound,
      the mined R simply failed to *find* phi as a refinement even though the data says
      it is one -> ``REFINEMENT_NOT_FOUND`` (distinct from both SUCCESS and FAIL). FAIL
      is reserved for a phi that is genuinely unsound (keeps a violation) or not
      contained with no corroboration.
    * ``auto`` / ``data-direct`` — the data-direct corroborate verdict is
      authoritative (gamerv's historical behaviour), with symbolic containment as the
      fallback for a non-raw phi.

    Ahead of both paths sits ``VARIABLE_NOT_FOUND``: gamerv could not evaluate phi at
    all because it reads a quantity no logged state reports. Nothing was decided about
    phi — the game simply does not export the variable its ground truth is written
    over — so the row is neither a verdict nor an experiment failure (``ERROR``).

    Pure string logic, separated out so it can be unit-tested without the worker.
    """
    r_text = None
    verdict = None          # first conclusion (the data-direct one when phi is raw)
    sym_verdict = None      # the phi -> R_all conclusion under the "(symbolic view …)"
    in_symbolic = False
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("R") and "=" in s and r_text is None:
            r_text = s.split("=", 1)[1].strip()
        if s.startswith("(symbolic view"):
            in_symbolic = True
        if s.startswith("=>") or s.startswith("Could not") or s.startswith("Cannot compare"):
            concl = s.lstrip("=> ").strip()
            if verdict is None:
                verdict = concl
            if in_symbolic and sym_verdict is None:
                sym_verdict = concl

    # Signals (mutually distinct substrings, so membership tests don't collide).
    contained     = "IS CONTAINED" in out           # some single R establishes phi
    not_contained = "is NOT contained" in out       # no single R does (witness shown)
    # phi is covered only by the *union* of several candidates — gamerv reports it on a
    # miss, and it is the reason a row the old union test called found is now a miss.
    union_only    = "established only by several mined refinements together" in out
    no_r_all      = ("not comparable by containment" in out
                     or "no mined refinement constrains" in out)
    data_sound    = "IS a sound refinement" in out  # corroborate: sound + informative
    data_unsound  = "is NOT sound" in out or "VACUOUS" in out

    # Decided before either mode: phi was never scored, because the game exports
    # none of the quantities it reads. Reported as its own outcome (a gap in the
    # game's instrumentation) rather than as an ERROR (a broken experiment run).
    if _UNEXPORTED_MARKER in out:
        return Containment("VARIABLE_NOT_FOUND", r_text, out,
                           _unexported_note(out) or verdict)

    if mode == "symbolic":
        # SUCCESS comes from the per-candidate containment only; everything else
        # cross-references the data-direct verdict to separate REFINEMENT_NOT_FOUND (R
        # missed a real refinement) from FAIL (phi genuinely not sound).
        if contained:
            status = "SUCCESS"
            # A phi *stronger* than the mined R it is contained in is sound whatever the
            # data says about its informativeness, so a VACUOUS corroborate verdict (phi
            # excludes every good state too) can survive into a SUCCESS. Say so in the
            # note rather than letting such a row read as a clean find.
            note = (f"[vacuous on the data] {sym_verdict or verdict}"
                    if "VACUOUS" in out else (sym_verdict or verdict))
        elif (not_contained or no_r_all) and data_sound:
            status = "REFINEMENT_NOT_FOUND"
            note = ("phi corroborated by the data, but no single mined refinement "
                    "establishes it — the search did not find it"
                    + (" (only the union of several candidates covers it)"
                       if union_only else ""))
        elif not_contained or data_unsound:
            status, note = "FAIL", (sym_verdict or verdict)
        elif ("No sound refinement R was mined" in out or "Cannot compare phi" in out
              or "No violated formula" in out or no_r_all):
            status, note = "INCONCLUSIVE", verdict
        else:
            status, note = "ERROR", verdict
        return Containment(status, r_text, out, note)

    # auto / data-direct: data-direct corroborate is authoritative, symbolic is the
    # non-raw fallback (historical behaviour).
    if data_sound or contained:
        status = "SUCCESS"                  # phi is a sound (valid) refinement
    elif data_unsound or not_contained:
        status = "FAIL"                     # phi does not exclude every violation
    elif ("No sound refinement R was mined" in out or "Cannot compare phi" in out
          or "No violated formula" in out):
        status = "INCONCLUSIVE"             # nothing to decide phi against
    else:  # "Could not evaluate phi" / "Could not decide containment" / unexpected
        status = "ERROR"
    return Containment(status, r_text, out, verdict)


def compare_refinement(run_dir: Path, phi: str,
                       terms: list[str] | None = None,
                       mode: str | None = None,
                       strategy: str | None = None,
                       candidate_sort: str | None = None) -> Containment:
    """Run gamerv's compare_refinement and classify the verdict on phi.

    ``terms`` focuses R-mining on those raw variables (box-only), matching the
    vocabulary used in fault localization, so the verdict is decided against the
    same R. Empty/None mines R generically. ``mode`` selects gamerv's decision
    path (auto / data-direct / symbolic), ``strategy`` how R's thresholds are
    placed (midpoints / weakest / gap-mean), and ``candidate_sort`` how the mined
    candidates are ordered within one score tier (none / type / popularity — only
    the order they are reported and checked in, never which R is mined); each
    defaults to the config value.

    terms/mode/strategy/sort are passed as fixed argv positions (terms as JSON,
    "null" when absent) so each stays addressable even when no terms are set.

    An empty ``phi`` (a dataset row with no ground truth) is not an error: it is
    routed to :func:`mine_only`, which mines R and returns ``SKIPPED``.
    """
    if not phi.strip():
        # No ground truth to compare: gamerv would only report that it could not
        # evaluate an empty phi, which reads as an ERROR. Mine R instead.
        return mine_only(run_dir, terms, strategy, candidate_sort)
    mode = mode or config.REFINEMENT_MODE
    strategy = strategy or config.THRESHOLD_STRATEGY
    candidate_sort = candidate_sort or config.CANDIDATE_SORT
    args = ["compare_refinement", str(run_dir), phi,
            json.dumps(list(terms) if terms else None), mode, strategy,
            candidate_sort]
    proc = _worker(*args)
    out = proc.stdout or ""
    if proc.stdout:
        print(out, end="")
    if proc.returncode != 0:
        return Containment("ERROR", None, out + "\n" + (proc.stderr or ""))
    return parse_containment(out, mode)
