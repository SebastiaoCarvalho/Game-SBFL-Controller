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


def validate(run_dir: Path, auto: bool = False) -> subprocess.CompletedProcess:
    """Validate a logged run, answering manual predicate prompts if gamerv asks.

    With MANUAL_PREDICATES active, gamerv can't compute the visual/audio
    predicates itself: its worker dumps the evidence and exits NEEDS_MANUAL. We
    then collect the operator's True/False for each, save them, and re-invoke —
    looping until the verdict is written (exit 0). Each pass only asks about
    predicates still unanswered, so it converges (usually in one round). When
    manual mode is off this is a single pass, exactly as before.

    ``auto`` replays cached operator answers: the run's hint file (if any) is
    merged into manual_answers.json before the first pass, so gamerv finds those
    answers and only requests predicates not yet recorded (or exits 0 outright
    when the hint covers them all). It also loads the run's per-call defaults
    (``hints.default_map`` — bare-call entries in the tape hint or the branch
    ``_defaults.hint``), which ``collect`` uses to auto-answer any request without
    prompting, index-free so they survive re-recording and reuse across traces.
    Every answer the operator does give is saved back to the hint, regardless of
    ``auto``, so a normal run builds the cache.
    """
    hint_path = hints.path_for(run_dir)
    defaults: dict[str, bool] = {}
    if auto and hint_path is not None:
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
            if manual_review.collect(run_dir, proc.stdout or "", hint_path, defaults) == 0:
                # Signalled NEEDS_MANUAL but listed nothing to answer: re-running
                # would loop forever, so treat it as a failure.
                raise GameRVError(
                    f"validate {run_dir} reported NEEDS_MANUAL but listed no "
                    f"predicates to answer:\n{proc.stdout}"
                )
            continue
        raise GameRVError(f"validate {run_dir} failed:\n{proc.stderr}")


def validate_all(auto: bool = False) -> list[Path]:
    """Validate every recorded run. Returns the list of run dirs validated."""
    dirs = run_dirs()
    for d in dirs:
        print(f"  [gamerv] validating {d.name} ...")
        validate(d, auto=auto)
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
                   strategy: str | None = None) -> str:
    """Localize the fault in ``run_dir``, mining a refinement R.

    When ``terms`` is given, mining is focused on exactly those raw game-state
    variables (the box-only vocabulary); otherwise gamerv discovers candidates
    generically from every logged variable. Terms are handed to the worker as a
    single JSON list so values with spaces/parens survive argv intact. ``strategy``
    selects how R's thresholds are placed (midpoints / weakest / gap-mean); when
    given, terms occupies a fixed argv slot ("null" if absent) so strategy follows.
    """
    args = ["fault_localize", str(run_dir)]
    if strategy is not None:
        args += [json.dumps(list(terms) if terms else None), strategy]
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
    status: str             # SUCCESS | FAIL | NOT_FOUND | INCONCLUSIVE | ERROR
    r_text: str | None      # the mined refinement R, if printed
    stdout: str
    verdict: str | None = None   # gamerv's one-line "=> ..." conclusion, for notes


def parse_containment(out: str, mode: str = "auto") -> Containment:
    """Classify a compare_refinement stdout into a verdict on phi as a refinement.

    gamerv's run_compare_refinement always runs the *data-direct* corroborate test
    when phi is raw-evaluable: it evaluates phi on the pooled good (P & Q) and
    violation (P & ~Q) states, independent of R's thresholds ("phi IS a sound
    refinement" / "is NOT sound" / "VACUOUS"). For a non-raw phi it instead decides
    symbolic phi -> R_all containment ("IS CONTAINED" / "is NOT contained"), and in
    ``symbolic`` mode it *also* appends that containment view (a "(symbolic view …)"
    block) alongside the corroborate verdict. So in symbolic mode the output carries
    BOTH conclusions.

    Classification therefore depends on ``mode``:

    * ``symbolic`` — SUCCESS is decided by phi -> R_all containment ONLY. When R_all
      does not establish phi (phi not contained, or no R_all was mined over phi's
      variable) yet the data-direct corroborate test *does* show phi sound, the mined
      R simply failed to *find* phi as a refinement even though the data says it is
      one -> ``NOT_FOUND`` (distinct from both SUCCESS and FAIL). FAIL is reserved for
      a phi that is genuinely unsound (keeps a violation) or not contained with no
      corroboration.
    * ``auto`` / ``data-direct`` — the data-direct corroborate verdict is
      authoritative (gamerv's historical behaviour), with symbolic containment as the
      fallback for a non-raw phi.

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
    contained     = "IS CONTAINED" in out          # phi -> R_all holds
    not_contained = "is NOT contained" in out       # phi -> R_all fails (witness)
    no_r_all      = ("not comparable by containment" in out
                     or "no mined refinement constrains" in out)
    data_sound    = "IS a sound refinement" in out  # corroborate: sound + informative
    data_unsound  = "is NOT sound" in out or "VACUOUS" in out

    if mode == "symbolic":
        # SUCCESS comes from R_all containment only; everything else cross-references
        # the data-direct verdict to separate NOT_FOUND (R missed a real refinement)
        # from FAIL (phi genuinely not sound).
        if contained:
            status, note = "SUCCESS", (sym_verdict or verdict)
        elif (not_contained or no_r_all) and data_sound:
            status = "NOT_FOUND"
            note = ("phi not established by R_all (symbolic), but corroborated by the "
                    "data — mined R did not find it")
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
                       strategy: str | None = None) -> Containment:
    """Run gamerv's compare_refinement and classify the verdict on phi.

    ``terms`` focuses R-mining on those raw variables (box-only), matching the
    vocabulary used in fault localization, so the verdict is decided against the
    same R. Empty/None mines R generically. ``mode`` selects gamerv's decision
    path (auto / data-direct / symbolic) and ``strategy`` how R's thresholds are
    placed (midpoints / weakest / gap-mean); each defaults to the config value.

    terms/mode/strategy are passed as fixed argv positions (terms as JSON, "null"
    when absent) so mode and strategy stay addressable even when no terms are set.
    """
    mode = mode or config.REFINEMENT_MODE
    strategy = strategy or config.THRESHOLD_STRATEGY
    args = ["compare_refinement", str(run_dir), phi,
            json.dumps(list(terms) if terms else None), mode, strategy]
    proc = _worker(*args)
    out = proc.stdout or ""
    if proc.stdout:
        print(out, end="")
    if proc.returncode != 0:
        return Containment("ERROR", None, out + "\n" + (proc.stderr or ""))
    return parse_containment(out, mode)
