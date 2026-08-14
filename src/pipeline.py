"""The per-row pipeline: nine steps from a dataset row to a SUCCESS/FAIL verdict.

``run_case`` maps to the workflow:

  1. read row            (caller provides the TestCase)
  2. checkout, instrument (2a), build (game_repo.py, instrument.py)
  3. set the ONLY safety property     (gamerv.py)
  4. clear previous logs              (gamerv.py)
  5. record traces via replay         (record_traces — launches the game per tape)
  6. validate all logs                (gamerv.py)
  7. select the last invalid trace    (fault localization skipped — see run_case)
  8. check phi is a valid refinement  (gamerv.compare_refinement)
  9. emit the test result

The row's ``Game Name`` selects a :class:`config.Game` bundle — its repo, build,
replay tapes/timing, and gamerv catalog. The pipeline is identical for every game;
only that bundle (and gamerv's active-game selection, via the ``GAME`` env) varies.

Step 2 builds but does not launch — because the replay is triggered by the game's
``--replay`` startup hook, the game is (re)launched per recording inside step 5,
which is where it is "opened".

Step 2a — source instrumentation — sits *inside* ``game_repo.build``, not beside
it in ``run_case``, so that it wraps every compile from a single place. Before
each compile gamerv's instrumenter writes the local-variable reporting calls into
the checked-out source; the edit is reverted as soon as the compiler is done, so
only the binary carries it. Its cost lands in the "build" phase timing. See
instrument.py.
"""

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import config
import game_repo as repo
import gamerv
import hints
import logcache
import recordings
from config import Game
from dataset import TestCase
from game_repo import DirtyTreeError

# Ordered phase labels for per-case timing. The single elapsed_s total is split
# across these; not every phase runs for every row (e.g. an early INCONCLUSIVE
# return leaves later phases unrecorded). See run_case.
PHASES = ("build", "record", "validate", "refine")


@contextmanager
def _phase(timings: dict[str, float], name: str):
    """Accumulate wall-clock seconds for ``name`` into ``timings`` (re-entrant)."""
    t = time.time()
    try:
        yield
    finally:
        timings[name] = timings.get(name, 0.0) + (time.time() - t)


@dataclass
class CaseResult:
    game: str
    branch: str
    specification: str
    phi: str
    formula: str
    status: str               # SUCCESS | FAIL | NOT_FOUND | INCONCLUSIVE | ERROR
    r_text: str | None = None
    notes: str = ""
    elapsed_s: float = 0.0     # wall-clock seconds for the whole pipeline
    timings: dict[str, float] = field(default_factory=dict)  # per-phase seconds

    def phase_cells(self) -> dict[str, str]:
        """Per-phase seconds as display strings, '-' for phases that didn't run."""
        return {p: (f"{self.timings[p]:.1f}" if p in self.timings else "-")
                for p in PHASES}


# --------------------------------------------------------------------------
# Step 5 — trace logging via replay.
# --------------------------------------------------------------------------
def record_traces(game: Game, branch: str) -> int:
    """Record one trace per .rec tape by replaying it under gamerv logging.

    Only the tapes in this test case's ``<game.rec_dir>/<branch>/`` folder are
    replayed, so distinct test cases log distinct traces. For each tape: (1.1)
    launch the game with the ``--replay`` startup hook and start gamerv logging,
    attaching to the window; (1.2) the game auto-fires the replay once logging has
    cleared the event channel; (1.3) once the replay finishes, stop the log; (1.4)
    leave the trace unvalidated (validation is step 6). Returns the number recorded.

    Each produced run dir is tagged with its manual-hint id (``hints.write_sidecar``)
    so step 6 can cache/replay the operator's image/audio answers per recording.
    The id is namespaced by game so two games can't collide on a shared branch/tape
    name.
    """
    recs = recordings.list_recordings(game, branch)
    if not recs:
        print(f"    no .rec tapes found in {recordings.recordings_dir(game, branch)}")
        return 0

    recorded = 0
    for rec in recs:
        frames = recordings.frame_count(rec)
        dur = recordings.replay_duration(game, rec)
        print(f"    tape {rec.name}  ({frames} frames, ~{dur:.1f}s replay)")
        run_dir = _record_one(game, rec, dur)
        if run_dir is not None:
            recorded += 1
            hints.write_sidecar(run_dir, f"{game.name}/{branch}/{rec.stem}")
    return recorded


def _record_one(game: Game, rec, replay_dur: float) -> Path | None:
    """Launch the game on one tape, log the replay, then stop.

    Returns the run dir gamerv just wrote (so the caller can tag it with its hint
    id), or ``None`` if the tape was skipped/failed and produced no run dir. The
    new run dir is found by diffing the logs dir before launch vs. after the log
    is stopped (gamerv writes the dir at SIGINT, inside ``stop_log``).
    """
    before = {d.name for d in gamerv.run_dirs()}
    t_launch = time.time()
    proc = repo.launch(game, game.replay_args(str(rec)))
    log = None
    ok = False
    try:
        # 1.1 start logging + attach to the window.
        log = gamerv.start_log()
        if not gamerv.wait_log_started(log):
            print("    [warn] gamerv log did not start (window/portal?) — skipping tape")
            return None

        # The replay's events start ~replay_delay seconds after launch (SuperTux
        # waits exactly that long via --replay-delay; SuperTuxKart has no such flag,
        # so it's the estimated track-load + countdown latency). If attaching took
        # longer than that, the earliest replay events were cleared away.
        elapsed = time.time() - t_launch
        if elapsed > game.replay_delay:
            print(f"    [warn] logging attached after {elapsed:.1f}s "
                  f"(> replay_delay={game.replay_delay}s); early events may be "
                  f"missed — increase the game's replay_delay.")

        # 1.2/1.3 wait for the replay to play out, then stop. Poll so we also
        # stop promptly if the game exits early (replay ended / window closed /
        # crash) rather than logging dead air until the timer elapses.
        finish_at = t_launch + game.replay_delay + replay_dur + game.replay_margin
        while time.time() < finish_at:
            if proc.poll() is not None:
                print(f"    [{game.name}] game exited — stopping log")
                break
            time.sleep(0.5)
        ok = True
    finally:
        if log is not None:
            gamerv.stop_log(log)   # 1.3 stop logging (1.4: not validated here)
        repo.shutdown(proc, grace=game.shutdown_grace)

    if not ok:
        return None
    new = sorted({d.name for d in gamerv.run_dirs()} - before)
    return config.GAMERV_LOGS_DIR / new[-1] if new else None


# --------------------------------------------------------------------------
# Step 5 — log cache: skip recording when the same binary replays the same tapes.
# --------------------------------------------------------------------------
def _record_or_restore(game: Game, case: TestCase) -> int:
    """Step 5 for a normal row: restore cached logs on a hit, else record + store.

    The raw logs depend only on the (already-built) binary and the row's tapes, so
    a content-hash hit lets us skip recording entirely and re-validate the cached
    traces. A miss deletes any stale slot, records fresh, and snapshots the pristine
    run dirs (before step 6 validates them). See logcache.py.
    """
    key = logcache.compute_key([logcache.file_sha(game.binary)], game, case.branch)
    if logcache.has(game, case.branch, key):
        print("    [cache] hit — restoring logs, skipping recording")
        return logcache.restore(game, case.branch)
    print("    [cache] miss — recording")
    logcache.invalidate(game, case.branch)
    n = record_traces(game, case.branch)
    if n:
        logcache.store(game, case.branch, key, gamerv.run_dirs())
    return n


def run_case(case: TestCase, manual_auto: bool = False,
             refinement_mode: str | None = None,
             threshold_strategy: str | None = None,
             instrument_source: bool | None = None) -> CaseResult:
    """Execute the pipeline for one dataset row.

    The row's ``game`` (Game Name) is resolved to a :class:`config.Game`; an
    unknown game becomes an ERROR result. gamerv's active game is set to that name
    for the row (the ``GAME`` env var, which overrides gamerv's active_game.txt) so
    every worker op loads the right predicate catalog.

    When ``manual_auto`` is set, step 6 replays cached operator answers for the
    manual image/audio predicates (``hints/``) instead of re-prompting for ones
    already recorded.

    ``refinement_mode`` (auto / data-direct / symbolic) selects how step 8 decides
    phi, and ``threshold_strategy`` (midpoints / weakest / gap-mean) how R's
    thresholds are placed. ``None`` defers to ``config.REFINEMENT_MODE`` /
    ``config.THRESHOLD_STRATEGY``.

    ``instrument_source`` runs step 2a — gamerv's source instrumenter — before
    each compile, so the built binary reports its function-local variables.
    ``None`` defers to ``config.INSTRUMENT``; ``False`` builds the source as
    committed (fault localization then sees only the variables the game already
    reported). Games with no ``instrument_src`` are unaffected either way.

    Raises DirtyTreeError (the one fatal, experiment-wide precondition); every
    other failure is captured as an ERROR result so the batch keeps going.
    """
    t_start = time.time()
    timings: dict[str, float] = {}

    def result(status: str, r_text: str | None = None, notes: str = "") -> CaseResult:
        return CaseResult(case.game, case.branch, case.specification, case.phi,
                          case.formula, status, r_text, notes,
                          elapsed_s=time.time() - t_start, timings=dict(timings))

    # Resolve the game before touching any game state; an unknown Game Name is a
    # per-row ERROR, not a crash.
    try:
        game = config.get_game(case.game)
    except config.UnknownGameError as exc:
        return result("ERROR", notes=str(exc))

    # Select gamerv's active game for the whole row (overrides active_game.txt).
    prev_game_env = os.environ.get(config.GAMERV_GAME_ENV)
    os.environ[config.GAMERV_GAME_ENV] = game.name

    instr = config.INSTRUMENT if instrument_source is None else instrument_source

    try:
        # Preflight: gamerv must have a catalog bundle for this game, or it would
        # silently fall back to its built-in default.
        if game.name not in gamerv.available_games():
            return result("ERROR", notes=(
                f"gamerv has no catalog for game {game.name!r} "
                f"(config/games/{game.name}/); create it before running this row"))

        # Step 2 — checkout + build (launch happens per-tape in step 5). The
        # build instruments the source first (step 2a) unless the row opts out or
        # the game has no instrument_src; the edit is undone before build returns.
        print(f"\n[2] checkout + build: {game.name} / {case.branch}"
              f"{'' if instr else '   (step 2a instrumentation off)'}")
        repo.ensure_clean(game)              # DirtyTreeError -> abort experiment
        with _phase(timings, "build"):
            repo.checkout(game, case.branch)
            repo.build(game, instrument_source=instr)

        # Step 3 — make the spec the only safety property (guarded by check_formula).
        print(f"[3] set property: {case.formula}")
        ok, err = gamerv.check_formula(case.formula)
        if not ok:
            return result("ERROR", notes=f"formula rejected by gamerv: {err}")
        gamerv.set_single_property(name=case.branch, formula=case.formula,
                                   description=f"dataset row {case.row}")

        # Step 4 — clear previous logs (archived under the row number).
        print("[4] clear previous logs")
        gamerv.clear_logs(row=case.row)

        # Step 5 — record traces by replaying each tape under logging, on the
        # mutant alone: its trace carries both the good and the violation states.
        print("[5] record traces (replay tapes under gamerv logging)")
        with _phase(timings, "record"):
            n = _record_or_restore(game, case)
        if n == 0:
            return result("INCONCLUSIVE", notes="no traces recorded (no tapes / log failed)")

        # Step 6 — validate all logs (replaying cached manual answers if --manual-auto).
        print("[6] validate all logs")
        with _phase(timings, "validate"):
            validated = gamerv.validate_all(auto=manual_auto)
        if not validated:
            return result("INCONCLUSIVE", notes="no traces to validate")

        print("    mining generically (all logged variables)")

        # Decision path + threshold placement for step 8 (default to config).
        mode = refinement_mode or config.REFINEMENT_MODE
        strategy = threshold_strategy or config.THRESHOLD_STRATEGY
        print(f"    refinement mode: {mode}   threshold strategy: {strategy}")

        # Step 7 — pick the trace to check phi against (the last invalid run).
        # Fault localization is intentionally skipped: compare_refinement re-mines
        # R itself and, on its data-direct path, decides whether phi is a sound
        # refinement straight from the trace states (no dependence on R's midpoint
        # thresholds), so a separate FL pass would only duplicate that mining.
        print("[7] select last invalid trace")
        run_dir = gamerv.last_invalid_run()
        if run_dir is None:
            return result("INCONCLUSIVE", notes="no invalid trace to localize")

        # Step 8 — is phi a valid refinement? (mode/strategy chosen above)
        print(f"[8] check phi as refinement   (phi = {case.phi})")
        with _phase(timings, "refine"):
            cont = gamerv.compare_refinement(run_dir, case.phi, None,
                                             mode=mode, strategy=strategy)

        # Step 9 — emit result, carrying gamerv's one-line verdict as the note.
        return result(cont.status, r_text=cont.r_text,
                      notes=cont.verdict or "see compare_refinement output")

    except DirtyTreeError:
        raise
    except Exception as exc:  # keep the batch going; record the failure
        return result("ERROR", notes=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            repo.return_to_base(game)
        except Exception:
            pass
        # Restore the previous GAME env so it doesn't leak to a later row/game.
        if prev_game_env is None:
            os.environ.pop(config.GAMERV_GAME_ENV, None)
        else:
            os.environ[config.GAMERV_GAME_ENV] = prev_game_env
