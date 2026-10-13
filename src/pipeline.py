"""The per-row pipeline: nine steps from a dataset row to a SUCCESS/FAIL verdict.

``run_case`` maps to the workflow:

  1. read row            (caller provides the TestCase)
  2. checkout, instrument (2a), build (game_repo.py, instrument.py)
  7a. term rules — the compound-term allow-list, digested from the game's own
      source (term_rules.py)
  3. set the ONLY safety property     (gamerv.py)
  4. clear previous logs              (gamerv.py)
  5. record traces via replay         (record_traces — launches the game per tape)
  6. validate all logs                (gamerv.py)
  7. select the last invalid trace    (fault localization skipped — see run_case)
  8. rank the bug conditions, decide phi (gamerv.compare_bug_condition)
  9. emit the test result

The row's ``Game Name`` selects a :class:`config.Game` bundle — its repo, build,
replay tapes/timing, and gamerv catalog. The pipeline is identical for every game;
only that bundle (and gamerv's active-game selection, via the ``GAME`` env) varies.

Step 2 builds but does not launch — because the replay is triggered by the game's
``--replay`` startup hook, the game is (re)launched per recording inside step 5,
which is where it is "opened".

Step 2a — source instrumentation — normally sits *inside* ``game_repo.build``, not
beside it in ``run_case``, so that it wraps every compile from a single place.
Before each compile gamerv's instrumenter writes the local-variable reporting
calls into the checked-out source; the edit is reverted as soon as the compiler is
done, so only the binary carries it. Its cost lands in the "build" phase timing.
See instrument.py.

A game with no compiler behind it cannot work that way: the thing that runs IS the
checked-out source, so an edit reverted at the end of ``build`` never reaches the
game. ``Game.source_served_at_runtime`` marks that case, ``build`` leaves the
apply to its caller, and ``_source_instrumented`` here holds it open across the
build AND the replay (step 5), reverting once the traces are recorded.
"""

import os
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import config
import dataset
import game_repo as repo
import gamerv
import hints
import instrument
import term_rules
import logcache
import manual_review
import metrics
import recordings
from config import Game
from dataset import TestCase
from game_repo import DirtyTreeError

# Ordered phase labels for per-case timing. The single elapsed_s total is split
# across these; not every phase runs for every row (e.g. an early INCONCLUSIVE
# return leaves later phases unrecorded). See run_case. "localize" is steps 7a-8
# (rank the bug conditions, decide phi). "rerank" is step 8b, which only a
# multi-sort run has: experiment bookkeeping, kept out of "localize" so the
# method's own time is not inflated by it.
PHASES = ("build", "record", "validate", "localize", "rerank")

#: What a re-ranking call (step 8b) rewrites in the run dir. Restored afterwards, so
#: the row's artefacts — archived with its logs — are the primary sort's.
_RANK_ARTEFACTS = ("phi_verdict.json", "bug_conditions.log")

#: gamerv's note that a sort could not do its job (popularity with no source counts)
#: and left the mined order in place — see fault_localization._announce_sort.
_SORT_DEGRADED = "candidates keep their mined order"


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
    status: str               # SUCCESS | FAIL | CONDITION_NOT_FOUND
                              # | VARIABLE_NOT_FOUND | INCONCLUSIVE
                              # | NEEDS_MANUAL | SKIPPED | ERROR
    r_text: str | None = None
    notes: str = ""
    elapsed_s: float = 0.0     # wall-clock seconds for the whole pipeline
    timings: dict[str, float] = field(default_factory=dict)  # per-phase seconds
    fault_type: str = ""       # the row's Fault Type (bug category)
    #: The 1-based dataset row this case came from, so a per-case results table can
    #: name the case the way the dataset does (0 when the result was built without
    #: one). See report._case_table.
    row: int = 0
    #: Step 8's search metrics: candidate count, the ground truth's rank among them,
    #: the match tier (see metrics.py). Blank on a row that never reached step 8.
    mining: metrics.Mining = field(default_factory=metrics.Mining)

    @property
    def fault_class(self) -> str:
        """The row's parent category — "Non-temporal fault" / "Temporal fault"."""
        return dataset.fault_class(self.fault_type)

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
        print(f"    tape {rec.name}  ({frames} frames, ~{dur:.1f}s replay estimated)")
        run_dir = _record_one(game, rec, dur)
        if run_dir is not None:
            recorded += 1
            hints.write_sidecar(run_dir, f"{game.name}/{branch}/{rec.stem}")
    return recorded


class ReplayIncompleteError(RuntimeError):
    """A tape's replay never reported its end: the trace would be cut short.

    Raised instead of keeping a partial trace. A cut-short replay is not a smaller
    experiment, it is a wrong one: the end of a tape is often where the bug is
    (FreeMiner's fall through the floor was its last event), and a hint answered
    on the full trace loses those answers when it is reconciled against a short
    one. The row is reported ERROR, and nothing is cached.
    """


def _record_one(game: Game, rec, replay_dur: float) -> Path | None:
    """Launch the game on one tape, log the replay until the game says it is over,
    then stop.

    The end comes from the game, never from a timer: each game writes the file
    named by ``config.REPLAY_DONE_ENV`` where its replay freezes (see there), and
    the log runs on for ``game.replay_margin`` after it, on the frozen last frame.
    ``replay_dur`` is only the estimate the timeout is sized from. A replay that
    does not report its end in time, or a game that exits before it does, raises
    :class:`ReplayIncompleteError`.

    Returns the run dir gamerv just wrote (so the caller can tag it with its hint
    id), or ``None`` if the tape was skipped (gamerv could not start logging) and
    produced no run dir. The new run dir is found by diffing the logs dir before
    launch vs. after the log is stopped (gamerv writes the dir at SIGINT, inside
    ``stop_log``).
    """
    before = {d.name for d in gamerv.run_dirs()}
    done_file = (Path(tempfile.gettempdir())
                 / f"gamerv-replay-done-{os.getpid()}-{time.time_ns()}")
    done_file.unlink(missing_ok=True)
    t_launch = time.time()
    proc = repo.launch(game, game.replay_args(str(rec)),
                       env={config.REPLAY_DONE_ENV: str(done_file)})
    log = None
    ok = False
    incomplete = ""
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

        # 1.2/1.3 wait for the game to report the replay over, then let the log
        # run on the frozen last frame for replay_margin. The estimate only sizes
        # the timeout: a replay is allowed to run REPLAY_TIMEOUT_FACTOR times
        # slower than estimated before the tape is given up.
        give_up_at = (t_launch + game.replay_delay
                      + config.REPLAY_TIMEOUT_FACTOR * replay_dur
                      + game.replay_margin + config.REPLAY_TIMEOUT_SLACK)
        why = None
        while why is None:
            if done_file.exists():
                why = done_file.read_text().strip() or "done"
                break
            if proc.poll() is not None:
                incomplete = (f"the game exited (rc {proc.returncode}) before "
                              f"reporting the end of the replay")
                break
            if time.time() > give_up_at:
                incomplete = (f"the replay did not report its end within "
                              f"{give_up_at - t_launch:.0f}s of launch (estimated "
                              f"{game.replay_delay + replay_dur:.0f}s)")
                break
            time.sleep(0.25)
        if why is not None:
            took = time.time() - t_launch
            print(f"    [{game.name}] replay over ({why}) {took:.1f}s after launch "
                  f"(estimated {game.replay_delay + replay_dur:.1f}s)")
            settle_until = time.time() + game.replay_margin
            while time.time() < settle_until and proc.poll() is None:
                time.sleep(0.25)
            ok = True
    finally:
        if log is not None:
            gamerv.stop_log(log)   # 1.3 stop logging (1.4: not validated here)
        repo.shutdown(proc, grace=game.shutdown_grace)
        done_file.unlink(missing_ok=True)

    if incomplete:
        # The partial run dir stays in gamerv's logs for inspection (the next row's
        # step 4 archives it), but this row records nothing and caches nothing.
        partial = sorted({d.name for d in gamerv.run_dirs(include_empty=True)}
                         - before)
        where = f" (partial trace left in {partial[-1]})" if partial else ""
        raise ReplayIncompleteError(
            f"tape {rec.name}: {incomplete} — not recorded, since the trace would "
            f"be cut short{where}")
    if not ok:
        return None
    new = sorted({d.name for d in gamerv.run_dirs()} - before)
    if not new:
        # Distinguish "gamerv wrote nothing" from "gamerv wrote a run dir whose
        # event log is empty" — the second means the capture stack worked and the
        # GAME stayed silent, which is a game/replay fault and needs naming here.
        # Left un-recorded either way; run_dirs() already refuses to count it.
        blank = sorted({d.name for d in gamerv.empty_run_dirs()} - before)
        if blank:
            print(f"    [warn] {blank[-1]} logged 0 events — the game ran and was "
                  f"captured but emitted no state; check the replay actually "
                  f"started and that the game's snapshot is not failing")
        return None
    return config.GAMERV_LOGS_DIR / new[-1]


# --------------------------------------------------------------------------
# Step 2a — the instrumentation's lifetime, for a game that runs from its source.
# --------------------------------------------------------------------------
@contextmanager
def _source_instrumented(game: Game, instrument_source: bool,
                         timings: dict[str, float]):
    """Hold step 2a applied across the build AND the replay, for the games that need it.

    ``game_repo.build`` reverts the instrumentation the moment the compile is done,
    which is the right lifetime for a compiled game: the reporting calls are in the
    binary, so the source can go back to how it was before the tape ever replays.

    A game whose SOURCE is what runs has no such handoff. Asteroids' "build" only
    stamps a launcher; ``predicates/bridge.py`` serves ``game.js`` straight out of
    the repo when the browser asks for it, in step 5. An edit reverted at the end of
    ``build`` is gone by then, and the trace comes back carrying only the reporting
    the branch already had written into it by hand — every generated member and
    local silently absent, which reads downstream as ``VARIABLE_NOT_FOUND`` on every
    row whose phi names one. ``build`` therefore skips the apply for such a game
    (``Game.source_served_at_runtime``) and leaves it to this block, which spans the
    replay and reverts after it.

    A no-op — yielding False — for every other game and whenever step 2a is off, so
    the compiled games keep the single apply inside ``build``. The apply's cost lands
    in the "build" phase timing, as it does there.
    """
    if not (instrument_source and game.source_served_at_runtime
            and instrument.enabled(game)):
        yield False
        return
    print("  [instrument] step 2a held applied across the replay "
          "(this game's source is what runs)")
    # Entered inside the phase so the apply's cost is billed to "build" as it is
    # inside build(); the revert then runs when the caller's block ends, through
    # the stack, which is what forwards an exception from the body to it.
    stack = ExitStack()
    with _phase(timings, "build"):
        stack.enter_context(instrument.applied(game))
    with stack:
        yield True


# --------------------------------------------------------------------------
# Step 5 — log cache: skip recording when the same build replays the same tapes.
# --------------------------------------------------------------------------
def _record_or_restore(game: Game, case: TestCase,
                       instrument_source: bool = True) -> int:
    """Step 5 for a normal row: restore cached logs on a hit, else record + store.

    The raw logs depend only on the (already-built) game and the row's tapes, so
    a content-hash hit lets us skip recording entirely and re-validate the cached
    traces. A miss deletes any stale slot, records fresh, and snapshots the pristine
    run dirs (before step 6 validates them). See logcache.py.

    "The built game" is not the artifact bytes alone (see logcache.py): the HEAD tree
    hash goes in first, because a mutant can live in a file the game loads at run
    time rather than compiles — four of FreeMiner's branches change only
    ``builtin/game/*.lua`` — and produce a byte-identical binary the artifact sha
    cannot distinguish.

    The step-2a fingerprint follows, but only for a game that runs from its source.
    Everywhere else the instrumentation's effect is *in* the artifact bytes already
    (it went through a compiler), so keying on it would re-record over a change to
    the instrumenter that did not change its output. For Asteroids it is the only
    signal there is: the launcher its "build" stamps carries ``HEAD^{tree}``, which
    is blind to a working-tree edit, so an instrumented and an uninstrumented replay
    of the same branch would otherwise share a key.
    """
    build_parts = [repo.head_tree_hash(game), logcache.file_sha(game.binary)]
    if (instrument_source and game.source_served_at_runtime
            and instrument.enabled(game)):
        build_parts.append(instrument.fingerprint(game))
    key = logcache.compute_key(build_parts, game, case.branch)
    if logcache.has(game, case.branch, key):
        print("    [cache] hit — restoring logs, skipping recording")
        return logcache.restore(game, case.branch)
    print("    [cache] miss — recording")
    logcache.invalidate(game, case.branch)
    n = record_traces(game, case.branch)
    if n:
        logcache.store(game, case.branch, key, gamerv.run_dirs())
    return n


def _rerank(run_dir: Path, case: TestCase, sorts: list[str],
            primary: metrics.Mining, primary_status: str) -> dict[str, int | None]:
    """Step 8b — the ground truth's rank under each of ``sorts``, over the candidates
    step 8 already mined.

    Each sort is one more ``compare_bug_condition`` call on the same run dir. Its mining
    cache matches (the sort is not part of the signature), so nothing is re-mined:
    gamerv re-orders the cached tier and decides phi against it, exactly as a run
    started under that sort would — the rank recorded here is the one that run would
    report. What differs between the calls is only the order, so the candidate count
    and the verdict must not move; either moving is printed as a warning (a cache miss
    that re-mined, or the phi scan's budget cutting a different prefix).

    Returns ``{sort: rank}``, None where the ground truth was not found — or where
    the sort could not order anything (popularity without the game's source counts),
    since the mined order it falls back to would be recorded under the wrong name.
    """
    saved = {n: (run_dir / n).read_bytes() for n in _RANK_ARTEFACTS
             if (run_dir / n).is_file()}
    ranks: dict[str, int | None] = {}
    try:
        for s in sorts:
            cont = gamerv.compare_bug_condition(run_dir, case.phi, None,
                                                candidate_sort=s, echo=False)
            m = metrics.read(run_dir, case.formula, case.phi)
            if _SORT_DEGRADED in cont.stdout:
                print(f"    {s}: [warn] the sort had nothing to order by and kept the "
                      f"mined order — no rank recorded")
                ranks[s] = None
                continue
            ranks[s] = m.gt_rank
            where = ("not among them" if m.gt_rank is None
                     else f"ground truth at rank {m.gt_rank}")
            print(f"    {s}: {where}")
            if m.candidates != primary.candidates:
                print(f"    {s}: [warn] {m.candidates} candidate(s), not "
                      f"{primary.candidates} — the call re-mined instead of re-ranking")
            if cont.status != primary_status or m.found != primary.found:
                print(f"    {s}: [warn] verdict {cont.status} "
                      f"({'found' if m.found else 'not found'}), where the row's own "
                      f"sort gave {primary_status} — the order moved more than the rank")
    finally:
        # Put the primary sort's artefacts back (and drop any the calls created).
        for n in _RANK_ARTEFACTS:
            if n in saved:
                (run_dir / n).write_bytes(saved[n])
            else:
                (run_dir / n).unlink(missing_ok=True)
    return ranks


def run_case(case: TestCase, manual_auto: bool = False,
             instrument_source: bool | None = None,
             skip_manual: bool = False,
             candidate_sort: str | None = None,
             rank_sorts: tuple[str, ...] = ()) -> CaseResult:
    """Execute the pipeline for one dataset row.

    The row's ``game`` (Game Name) is resolved to a :class:`config.Game`; an
    unknown game becomes an ERROR result. gamerv's active game is set to that name
    for the row (the ``GAME`` env var, which overrides gamerv's active_game.txt) so
    every worker op loads the right predicate catalog.

    When ``manual_auto`` is set, step 6 replays cached operator answers for the
    manual image/audio predicates (``hints/``) instead of re-prompting for ones
    already recorded. ``skip_manual`` adds the unattended-batch escape: a row whose
    manual predicates the hints do *not* cover returns ``NEEDS_MANUAL`` at step 6
    rather than stopping the batch at a prompt (or erroring out with no TTY), so it
    can be re-run interactively later. Steps 7-9 are not reached for such a row —
    its traces have no verdict, so there is nothing to decide phi against.

    Step 8 ranks the row's candidate bug conditions by Ochiai and decides the row's
    phi — the condition under which the bug occurs — against the top of that ranking.
    ``candidate_sort`` (none / type / popularity) orders the mined candidates
    *within* one score tier — which moves the ground truth's rank without changing
    the candidate set or the top score. ``None`` defers to ``config.CANDIDATE_SORT``.

    Alongside the verdict, step 8's search metrics are attached to the result
    (:class:`metrics.Mining`): the candidate count, the ground truth's rank among the
    ranked candidates and the match tier.

    ``rank_sorts`` names further candidate sorts to rank the ground truth under
    (step 8b, :func:`_rerank`): the candidates step 8 mined are read again in each
    sort's order, without re-mining, so ``mining.ranks`` compares the orderings over
    one identical candidate set. ``candidate_sort`` stays the row's own sort — its
    verdict, notes and ``gt_rank``.

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
    # Filled once step 8 has mined (see below); a row that returns before then
    # carries blank metrics rather than a zero candidate count.
    mining = metrics.Mining()

    def result(status: str, r_text: str | None = None, notes: str = "") -> CaseResult:
        return CaseResult(case.game, case.branch, case.specification, case.phi,
                          case.formula, status, r_text, notes,
                          elapsed_s=time.time() - t_start, timings=dict(timings),
                          fault_type=case.fault_type, mining=mining, row=case.row)

    # Resolve the game before touching any game state; an unknown Game Name is a
    # per-row ERROR, not a crash.
    try:
        game = config.get_game(case.game)
    except config.UnknownGameError as exc:
        return result("ERROR", notes=str(exc))

    # Select gamerv's active game for the whole row (overrides active_game.txt).
    prev_game_env = os.environ.get(config.GAMERV_GAME_ENV)
    os.environ[config.GAMERV_GAME_ENV] = game.name
    prev_keys_env = os.environ.get(config.GAMERV_KEY_TABLE_ENV)
    os.environ[config.GAMERV_KEY_TABLE_ENV] = str(instrument.keys_path(game))

    instr = config.INSTRUMENT if instrument_source is None else instrument_source

    try:
        # Preflight: gamerv must have a catalog bundle for this game, or it would
        # silently fall back to its built-in default. Both files count: a folder
        # without one of them still loads, under Super Tux's taxonomy or events.
        if game.name not in gamerv.available_games():
            return result("ERROR", notes=(
                f"gamerv has no catalog for game {game.name!r} "
                f"(config/games/{game.name}/); create it before running this row"))
        missing = gamerv.missing_game_files(game.name)
        if missing:
            return result("ERROR", notes=(
                f"gamerv's catalog for {game.name!r} lacks {', '.join(missing)} "
                f"(config/games/{game.name}/); without it gamerv would use Super "
                f"Tux's built-in one"))

        # Step 2 — checkout + build (launch happens per-tape in step 5). The
        # build instruments the source first (step 2a) unless the row opts out or
        # the game has no instrument_src; the edit is undone before build returns
        # — except for a game whose SOURCE is what runs, where _source_instrumented
        # below owns it instead and holds it open through step 5.
        print(f"\n[2] checkout + build: {game.name} / {case.branch}"
              f"{'' if instr else '   (step 2a instrumentation off)'}")
        repo.ensure_clean(game)              # DirtyTreeError -> abort experiment
        with _phase(timings, "build"):
            repo.checkout(game, case.branch)

        # Step 2a for a source-served game: applied here, over the build AND the
        # replay, and reverted when the block ends. See _source_instrumented.
        with _source_instrumented(game, instr, timings):
            with _phase(timings, "build"):
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
                n = _record_or_restore(game, case, instr)
        if n == 0:
            # A row whose tapes all replayed into an EMPTY event log is a broken
            # experiment run, not an undecidable one: phi never got a chance. Both
            # ended up INCONCLUSIVE once, which read as "nothing to decide phi
            # against" and quietly cost a whole game its SUCCESS rows.
            blank = gamerv.empty_run_dirs()
            if blank:
                return result("ERROR", notes=(
                    f"{len(blank)} trace(s) recorded but every event log is empty "
                    f"— the game ran and was captured but logged no state "
                    f"({blank[-1].name})"))
            return result("INCONCLUSIVE", notes="no traces recorded (no tapes / log failed)")

        # Step 6 — validate all logs (replaying cached manual answers if --manual-auto).
        print("[6] validate all logs")
        with _phase(timings, "validate"):
            validated = gamerv.validate_all(auto=manual_auto,
                                            skip_manual=skip_manual)
        if not validated:
            return result("INCONCLUSIVE", notes="no traces to validate")

        print("    mining generically (all logged variables)")

        # Candidate order for step 8 (defaults to config).
        sort = candidate_sort or config.CANDIDATE_SORT
        print(f"    candidate sort: {sort}")

        # Step 7 — pick the trace to check phi against (the last invalid run).
        # Fault localization is intentionally skipped: compare_bug_condition ranks
        # the bug conditions itself and scores phi straight from the trace states,
        # so a separate FL pass would only duplicate that mining.
        print("[7] select last invalid trace")
        run_dir = gamerv.last_invalid_run()
        if run_dir is None:
            return result("INCONCLUSIVE", notes="no invalid trace to localize")

        # Step 7a — refresh the compound-term allow-list from this game's own
        # source, now that the step-2a window has closed (the tree is pristine
        # again, which the key table's line numbers assume) and a run exists to read
        # the game's base fields from. Cached on the instrumentation fingerprint, so
        # it costs a file read on a row that changed nothing. See term_rules.
        print("[7a] term rules (compound-term allow-list from source)")
        with _phase(timings, "localize"):
            term_rules.ensure(game, run_dir)

        # Step 8 — rank the bug conditions and decide phi against the top of them.
        print(f"[8] check phi as bug condition   (phi = {case.phi})")
        with _phase(timings, "localize"):
            cont = gamerv.compare_bug_condition(run_dir, case.phi, None,
                                                candidate_sort=sort)

        # The search metrics for the row: how many top-ranked candidate bug conditions
        # the miner produced and where the ground truth sits among them, read off the
        # records step 8 just persisted in the run dir (metrics.py). Lower-scoring
        # candidates stay in gamerv's log and out of the metrics.
        mining = metrics.read(run_dir, case.formula, case.phi)
        if mining.candidates is not None:
            rank = ("not among them" if mining.gt_rank is None
                    else f"ground truth at rank {mining.gt_rank}")
            tier = f" ({mining.gt_match})" if mining.gt_match else ""
            union = " [union-only]" if mining.union_only else ""
            print(f"    mined {mining.candidates} top-ranked candidate bug condition(s) "
                  f"— {rank}{tier}{union}")
            if case.phi.strip():
                mining.ranks = {sort: mining.gt_rank}

        # Step 8b — the same candidates, read in each further sort's order.
        extra = [s for s in rank_sorts if s != sort]
        if extra and mining.ranks and cont.status != "ERROR":
            print(f"[8b] re-rank the same candidates under: {', '.join(extra)}")
            with _phase(timings, "rerank"):
                mining.ranks.update(_rerank(run_dir, case, extra, mining, cont.status))

        # Step 9 — emit result, carrying gamerv's one-line verdict as the note.
        return result(cont.status, r_text=cont.r_text,
                      notes=cont.verdict or "see compare_bug_condition output")

    except DirtyTreeError:
        raise
    except manual_review.NeedsManualError as exc:
        # Not an ERROR: nothing broke, the row simply needs a human at step 6 and
        # --skip-manual asked for it to be left for one. See manual_review.collect.
        return result("NEEDS_MANUAL", notes=str(exc))
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
        if prev_keys_env is None:
            os.environ.pop(config.GAMERV_KEY_TABLE_ENV, None)
        else:
            os.environ[config.GAMERV_KEY_TABLE_ENV] = prev_keys_env
