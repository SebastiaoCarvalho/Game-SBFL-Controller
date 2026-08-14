"""Central paths and constants for the controller pipeline.

Everything else imports from here so there is a single place to retarget the
experiment (different checkout location, different Python interpreter, etc.).

The experiment is **multi-game**: each dataset row names a ``Game`` (the "Game
Name" column), which selects a :class:`Game` bundle from :data:`GAMES`. A game
groups everything that is game-specific — its source repo, build commands, game
binary, replay-tape location and timing, and the gamerv catalog name. The nine
step pipeline itself is identical for every game (mutant = a git branch you
compile, trace = replay a ``.rec`` tape through the game's ``--replay`` hook), so
only these constants vary. Add a game by appending a :class:`Game` to
:data:`_GAMES`; its ``name`` must match its gamerv ``config/games/<name>`` folder.
"""

from dataclasses import dataclass
from pathlib import Path

# --- Repositories ---------------------------------------------------------
GAMERV_DIR = Path("/home/sebastiao/nii/fl/gamerv")

# --- Inputs / outputs -----------------------------------------------------
# The code lives in src/; the data directories (results, hints, log_cache,
# build_cache, logs_archive) sit in the project root, one level up.
CONTROLLER_DIR = Path(__file__).resolve().parent.parent
DATASET = Path("/home/sebastiao/nii/fl/dataset.csv")
RESULTS_DIR = CONTROLLER_DIR / "results"
# Cached operator answers for the manual image/audio predicate prompts, one file
# per recording: hints/<game>/<branch>/<recX>.hint. Written whenever the operator
# answers a prompt; replayed with run_experiment.py --manual-auto. See hints.py
# and manual_review.py.
HINTS_DIR = CONTROLLER_DIR / "hints"
# Content-addressed cache of step-5 raw logs, one slot per game+branch:
# log_cache/<game>/<branch>/ holds pristine run dirs + meta.json (the binary+tape
# hash). A matching hash lets the pipeline skip recording and restore the cached
# logs; a changed binary/tape deletes the slot and re-records. See logcache.py.
LOG_CACHE_DIR = CONTROLLER_DIR / "log_cache"
# Content-addressed cache of built binaries, one slot per game+branch:
# build_cache/<game>/<branch>/ holds the game binary + its build-local .so plus
# meta.json (the HEAD tree hash + the instrumenter's fingerprint). A matching key
# lets game_repo.build() skip the cmake configure+build AND step 2a, restoring the
# binary; a source change → different tree hash → miss → rebuild. See buildcache.py.
BUILD_CACHE_DIR = CONTROLLER_DIR / "build_cache"

# --- Source instrumentation (step 2a) -------------------------------------
# Before compiling a branch, the controller runs gamerv's source instrumenter
# (runners.source_instrument) over the game's source so the build reports its
# function-local variables — fault localization can only refine over variables the
# snapshot carries. The edit is transient: instrument → build → revert, all inside
# game_repo.build(). See instrument.py.
#
# Master switch for the step; per game it also needs Game.instrument_src set (a
# game with no instrumentation spec in gamerv leaves it None and is unaffected).
# Overridable per run with --no-instrument.
INSTRUMENT = True
# Scratch dir for the instrumenter's manifests (one per game, alive only for the
# duration of one build). It lives here rather than in the game repo because the
# tool's default — <game-root>/.gamerv/ — would show up as an untracked directory
# in `git status` and trip game_repo.ensure_clean.
INSTRUMENT_DIR = CONTROLLER_DIR / "instrument"
# libclang's own builtin headers (stddef.h, stdarg.h, the intrinsics …) — what
# clang's -resource-dir names, passed to the instrumenter as $CLANG_RESOURCE_DIR.
#
# This is NOT optional. The libclang the tool loads is the PyPI `libclang` wheel,
# which ships the .so and no headers at all, and this machine has no clang
# install to fall back on. Without them every parse dies on the first
# `#include <cstddef>`, and the failure is SILENT AND PARTIAL: the tool still
# emits a plausible-looking set of keys, but every candidate is downgraded to
# low-confidence and semantic detail as basic as "does this local have an
# initializer" is lost — which is what hid `collision_static_constrains::movement`
# (dataset row 1's whole refinement) from the snapshot. instrument.py therefore
# treats a missing directory as a hard error rather than parsing degraded.
#
# Populated from Ubuntu's clang headers, matching the wheel's libclang 18:
#   apt-get download libclang-common-18-dev        # no root needed
#   dpkg-deb -x libclang-common-18-dev_*.deb x
#   cp -r x/usr/lib/llvm-18/lib/clang/18 ~/.local/share/gamerv-clang/
CLANG_RESOURCE_DIR = Path.home() / ".local" / "share" / "gamerv-clang" / "18"

# --- Refinement decision path (step 8) -----------------------------------
# How gamerv's compare_refinement decides whether phi is a valid refinement:
#   "symbolic"    — force the symbolic phi -> R containment check against the
#                   mined R (so R's thresholds matter — see THRESHOLD_STRATEGY).
#                   Verdict reads "phi IS / is NOT CONTAINED in R". SUCCESS is
#                   decided by phi -> R_all containment ONLY; when R_all does not
#                   establish phi but the data-direct test does, the row is
#                   classified NOT_FOUND (the mined R missed a real refinement).
#   "data-direct" — force the data-exact test: evaluate phi on the pooled
#                   good (P & Q) / violation (P & ~Q) states, independent of R.
#                   Verdict reads "phi IS a sound refinement, corroborated by
#                   the data". Errors on a phi that reads a non-raw predicate.
#   "auto"        — data-direct first, symbolic only as a fallback for non-raw phi
#                   (gamerv's historical behaviour).
# Overridable per run with --refinement-mode.
REFINEMENT_MODE = "symbolic"
REFINEMENT_MODES = ("auto", "data-direct", "symbolic")

# --- Refinement threshold strategy ---------------------------------------
# How R's numeric atom split-points are placed between the violation and good
# values when mining R. Only affects the displayed R and the SYMBOLIC phi -> R
# path (the data-direct verdict does not depend on R's threshold):
#   "midpoints" — sample midpoints across the pooled values and let the mining
#                 search pick (gamerv's historical behaviour).
#   "weakest"   — the loosest sound separator: > max(violation) / < min(violation)
#                 (the most permissive R; symbolic phi -> R accepts the most phis).
#   "gap-mean"  — the midpoint of the violation->nearest-good gap (a stricter R).
# Overridable per run with --threshold-strategy.
THRESHOLD_STRATEGY = "weakest"
THRESHOLD_STRATEGIES = ("midpoints", "weakest", "gap-mean")

# --- Build parallelism ----------------------------------------------------
# Parallelism for `cmake --build`. Kept modest to avoid OOM on heavy TUs. Shared
# default; a Game's build_commands may bake in a different -j.
BUILD_JOBS = 4


# --- Per-game configuration ----------------------------------------------
@dataclass(frozen=True)
class Game:
    """Everything game-specific the pipeline needs, selected by the dataset's
    "Game Name" column. ``name`` must equal the game's gamerv
    ``config/games/<name>`` folder (it is also passed to gamerv as the ``GAME``
    env var so the right predicate catalog is loaded).
    """
    name: str                                   # canonical / gamerv catalog name
    repo_dir: Path                              # game source git repo
    build_dir: Path
    binary: Path                                # the built game executable
    base_branch: str                            # branch carrying the instrumentation
    # Build commands, run in order every build (cmake configure re-globs sources,
    # then build). A game on another build system just supplies its own commands.
    build_commands: tuple[tuple[str, ...], ...]
    # Build-dir-relative files to snapshot in the build cache (binary + any
    # build-local shared libs it loads at runtime).
    cache_artifacts: tuple[str, ...]
    rec_dir: Path                               # root of the replay tapes
    rec_glob: str                               # tape filename glob (e.g. "*.rec")
    launch_args: tuple[str, ...]                # args added to every game launch
    replay_flag: str                            # CLI flag naming the tape to replay
    # How the flag carries the tape path: False -> two args "<flag> <path>"
    # (SuperTux --replay foo.rec); True -> one joined arg "<flag>=<path>"
    # (SuperTuxKart --agent-replay=foo.stkagent).
    replay_flag_joins_value: bool
    # CLI flag for the pre-replay delay, or None if the game has no such flag
    # (SuperTuxKart boots straight into the race; nothing tells it to wait).
    replay_delay_flag: str | None
    replay_delay: float                         # seconds until the replay's events start
    replay_margin: float                        # extra wait after a replay's duration
    seconds_per_step: float                     # wall-clock seconds per logical frame
    # Extra fixed args appended after the replay flag on every replay launch, for
    # games that need one. Witch Blast's replay obeys a per-run frame budget
    # (``--frames N``, default 600) and self-closes when it is spent, which would
    # truncate any tape longer than the budget — so we pass a budget larger than
    # any tape and let the tape-exhausted check end the replay instead. Empty for
    # SuperTux / SuperTuxKart, whose replays run the whole tape unconditionally.
    replay_extra_args: tuple[str, ...] = ()
    # CLI flag that turns ON closed-loop drift correction during replay (snap the
    # kart back onto the recorded transform when it diverges), or None if the game
    # has no such knob. Correction is OFF game-side by default; the pipeline
    # appends this flag only for rows whose dataset "Replay Correct" cell asks for
    # it (see TestCase.correct_drift). SuperTuxKart: "--agent-replay-correct".
    # SuperTux / Witch Blast: None — their replays are already pure input playback
    # with no drift snap-back, so a build's physics is always free to diverge.
    replay_correct_flag: str | None = None
    # How long SIGTERM is given to end the game before we SIGKILL it. The default
    # suits a game that just closes a window; raise it for one that has real work
    # to do on the way out. FreeMiner flushes its world (a LevelDB map database)
    # to disk over several seconds — killing it mid-write risks corrupting the
    # very world every tape replays in.
    shutdown_grace: float = 5.0
    # The source root gamerv's instrumenter rewrites before each build (step 2a),
    # or None to skip the step for this game. Setting it needs BOTH: an
    # instrumentation spec gamerv can resolve for the game (its
    # config/games/<name>/source_instrument.json, or gamerv's built-in SuperTux
    # default — whose scan_globs are SuperTux paths and mean nothing elsewhere),
    # AND the reporting runtime (the REPORT_LOCAL macros) already present on the
    # game's base branch, or the instrumented tree will not compile. See
    # instrument.py.
    instrument_src: Path | None = None
    # Extra flags for that run, e.g. ("--max-keys-per-class", "8") to tighten the
    # per-class key budget or ("--members",) to also report class members. Empty
    # leaves every such choice to the game's spec.
    instrument_extra_args: tuple[str, ...] = ()

    def replay_args(self, rec_path: str, correct_drift: bool = False) -> list[str]:
        """CLI args appended to a launch so it replays the tape at ``rec_path``.

        Encodes the per-game differences in "how to call a replay": whether the
        flag joins its value with ``=`` (SuperTuxKart's ``--agent-replay=<f>``) or
        takes it as a separate arg (SuperTux's ``--replay <f>``); whether a
        pre-replay delay flag exists at all (SuperTux passes ``--replay-delay <s>``;
        SuperTuxKart/Witch Blast have no such flag, so ``replay_delay`` is only the
        controller's estimate of the launch->first-event latency for the stop-timer
        math); and any fixed trailing args the game needs (``replay_extra_args``).

        ``correct_drift`` (from the row's TestCase) requests drift correction; it
        only adds an arg when the game actually has a ``replay_correct_flag``, so a
        row can safely ask for it on a game that has no such knob (it's a no-op).
        """
        if self.replay_flag_joins_value:
            args = [f"{self.replay_flag}={rec_path}"]
        else:
            args = [self.replay_flag, rec_path]
        if self.replay_delay_flag is not None:
            args += [self.replay_delay_flag, str(self.replay_delay)]
        args += list(self.replay_extra_args)
        if correct_drift and self.replay_correct_flag is not None:
            args.append(self.replay_correct_flag)
        return args


def _supertux() -> Game:
    repo = Path("/home/sebastiao/supertux")
    build = repo / "build"
    return Game(
        name="Super Tux",
        repo_dir=repo,
        build_dir=build,
        binary=build / "supertux2",
        base_branch="base",
        # We re-run the cmake *configure* on every build, not just on a cache
        # miss: SuperTux gathers sources with file(GLOB) and no CONFIGURE_DEPENDS,
        # so switching branches (mutations add/remove/rename .cpp files) silently
        # drops files from a stale glob. Re-configuring re-globs the checked-out
        # tree. See buildcache.py / the pipeline memory.
        #
        # CMAKE_EXPORT_COMPILE_COMMANDS keeps build/compile_commands.json current:
        # the step-2a instrumenter parses the source with libclang and uses that
        # database for each file's real compiler flags. Without it the parse falls
        # back to the spec's synthesized flags, which resolve fewer types and so
        # silently reject candidate variables.
        build_commands=(
            ("cmake", "-S", str(repo), "-B", str(build),
             "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"),
            ("cmake", "--build", str(build), f"-j{BUILD_JOBS}"),
        ),
        # The game binary plus the only build-local shared library it links at
        # runtime (libsimplesquirrel.so — confirmed via ldd; every other dependency
        # is a system lib). Anything missing at store time is skipped.
        cache_artifacts=("supertux2", "external/simplesquirrel/libsimplesquirrel.so"),
        rec_dir=Path.home() / ".local" / "share" / "supertux2",
        rec_glob="*.rec",
        # WINDOWED, deliberately NOT --fullscreen: with a fullscreen surface,
        # GNOME/mutter direct-scans-it-out and stops feeding the PipeWire *monitor*
        # screencast gamerv records — capture then measures ~0.09 fps, the event
        # CLEAR lands after the replay already ran (wiping its events), and the
        # GStreamer teardown hangs until you Alt+Tab. A normal composited window
        # keeps mutter compositing so the screencast gets ~30 fps.
        launch_args=("--window",),
        replay_flag="--replay",
        replay_flag_joins_value=False,       # "--replay <rec>" (separate args)
        replay_delay_flag="--replay-delay",
        # Must comfortably exceed window-appear + gamerv attach + event CLEAR, or
        # the CLEAR lands mid-replay and clips early events. The FIRST tape of a
        # run can take ~8-9s to attach; later tapes are sub-second.
        replay_delay=6.0,
        replay_margin=4.0,
        # constants.hpp LOGICAL_FPS = 1000/15, i.e. a fixed 15 ms logical step, so
        # a tape of N frames replays in N * 0.015 s.
        seconds_per_step=15.0 / 1000.0,
        # Step 2a: gamerv's instrumenter rewrites this tree before every build so
        # the binary reports its function-locals. SuperTux is the game it was
        # written against — gamerv's built-in spec targets these paths
        # (object/player.cpp, badguy/badguy.cpp, …) and the REPORT_LOCAL runtime
        # already lives on `base` (supertux/game_object.hpp +
        # predicates/local_report.hpp). The edit is reverted right after the
        # compile; see instrument.py.
        instrument_src=repo / "src",
        # `--members` also reports *class members*, by extending each class's
        # existing report_state override — not just function locals. Needed
        # because a mutated flag is often a member: player_walk_on_water's
        # m_wading is one, and without this the row's Target Phi names a field no
        # trace carries. Which members are actually taken is scoped by the game's
        # gamerv spec (config/games/Super Tux/source_instrument.json) — members
        # outrank locals in the per-class key budget, so it vetoes all but the
        # ones a row needs rather than letting them swap the locals out.
        instrument_extra_args=("--members",),
    )


def _supertuxkart() -> Game:
    repo = Path("/home/sebastiao/stk/stk-code")
    build = repo / "cmake_build"
    return Game(
        name="Super Tux Kart",
        repo_dir=repo,
        build_dir=build,
        # CMAKE_RUNTIME_OUTPUT_DIRECTORY puts the binary under cmake_build/bin/.
        binary=build / "bin" / "supertuxkart",
        # TBD: STK currently has only `master`. The event-log-server instrumentation
        # and the launch-time replay hook (mirroring SuperTux) will live on a `base`
        # branch the user creates; the pipeline builds/records against it.
        base_branch="base",
        # Same two-step cmake configure+build as SuperTux. Re-running configure every
        # build re-globs the checked-out tree (STK's sources.cmake does use
        # file(GLOB_RECURSE), so this is load-bearing here too).
        #
        # CMAKE_EXPORT_COMPILE_COMMANDS is what step 2a's instrumenter parses the
        # source with: without the database libclang falls back to the spec's
        # synthesized flags, which resolve fewer types and so silently reject
        # candidate variables. cmake_build/ is gitignored, so the file it writes
        # there never trips ensure_clean.
        build_commands=(
            ("cmake", "-S", str(repo), "-B", str(build),
             "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"),
            ("cmake", "--build", str(build), f"-j{BUILD_JOBS}"),
        ),
        # Just the binary: ldd shows supertuxkart links only system libs (no
        # build-local .so to snapshot, unlike SuperTux's libsimplesquirrel.so).
        cache_artifacts=("bin/supertuxkart",),
        # STK saves agent tapes under file_manager->getReplayDir() =
        # ~/.local/share/supertuxkart/replay/ (gameplay_recorder.cpp). We point
        # rec_dir at that replay/ dir so the per-branch tape folder is
        # replay/<branch>/, mirroring SuperTux's layout. Tapes are *.stkagent
        # (not *.rec) but still carry a `frames <N>` header line, so recordings.py
        # reads their length unchanged (its glob comes from rec_glob).
        rec_dir=Path.home() / ".local" / "share" / "supertuxkart" / "replay",
        rec_glob="*.stkagent",
        # STK -w/--windowed (main.cpp). Windowed for the same reason as SuperTux: a
        # fullscreen surface makes GNOME/mutter direct-scan-out and starve gamerv's
        # PipeWire *monitor* screencast; a composited window keeps ~30 fps capture.
        launch_args=("--windowed",),
        # STK's replay flag joins its value with '=' and takes a bare filename or
        # full path (agent_config.cpp loadReplay resolves it, falling back to the
        # replay dir): --agent-replay=<file>. It boots straight into the recorded
        # race (main.cpp sets m_no_start_screen) — there is NO delay flag, so
        # replay_delay_flag is None and replay_delay below is only the controller's
        # stop-timer estimate of how long track-load + the race countdown take
        # before the replay's events begin.
        replay_flag="--agent-replay",
        replay_flag_joins_value=True,
        replay_delay_flag=None,
        # STK has no --replay-delay to make the game wait for gamerv to attach; the
        # natural launch->race-start latency (track load + "Ready/Set/Go" countdown)
        # is what covers the attach. This value only drives the stop-timer math
        # (finish_at = launch + replay_delay + replay_dur + margin) and the
        # "attached late" warning; tune it if early race events get clipped.
        replay_delay=5.0,
        replay_margin=2.0,
        # STK physics runs at a fixed 120 fps (stk_config.xml <physics fps="120">),
        # and World::updateWorld captures one agent frame per physics update for
        # every kart, so a tape of N frames (per kart; all karts share the same
        # count) replays in N/120 s. Confirmed against real .stkagent tapes
        # (24118 frames = ~201 s for a 3-lap cornfield race).
        seconds_per_step=1.0 / 120.0,
        # STK's replay snaps a drifting kart back onto the recorded transform; that
        # correction masks any physics divergence between base and mutant (a fixed
        # wall collision the tape recorded going through, etc.), so it is OFF by
        # default. A row that needs the recorded trajectory reproduced exactly can
        # re-enable it via the dataset "Replay Correct" column, which appends this.
        replay_correct_flag="--agent-replay-correct",
        # Step 2a, as for SuperTux: gamerv's instrumenter rewrites this tree before
        # every build so the binary reports its function-locals, and the edit is
        # reverted right after the compile (instrument.py). Both preconditions
        # hold: the REPORT_LOCAL runtime lives on `base` (karts/moveable.hpp +
        # predicates/local_report.hpp), and gamerv resolves a spec for this game
        # (config/games/Super Tux Kart/source_instrument.json — the built-in
        # default's scan globs are SuperTux paths and would find nothing here).
        # Measured: 291 keys over 5 files, and the instrumented tree really
        # compiles.
        instrument_src=repo / "src",
        # No `--members`, unlike SuperTux. Members are reported by extending each
        # class's report_state override, and STK has none: the snapshot builder
        # writes a kart's fields itself (event_log_server.cpp's write_kart), so
        # there is no per-class hook for the instrumenter to extend.
    )


def _witchblast() -> Game:
    repo = Path("/home/sebastiao/witchblast")
    return Game(
        name="Witch Blast",
        repo_dir=repo,
        # Witch Blast builds IN-SOURCE: CMakeCache.txt / Makefile / CMakeFiles/
        # and the binary all land in the repo root (RUNTIME_OUTPUT_DIRECTORY =
        # CMAKE_CURRENT_BINARY_DIR = the source dir). So build_dir == repo_dir.
        # All build artifacts are gitignored, so this does not dirty the tree.
        build_dir=repo,
        binary=repo / "Witch_Blast",
        # The event-log-server + agent (record/replay) instrumentation lives on a
        # `base` branch (mirroring SuperTux), which mutant branches rebase onto.
        base_branch="base",
        # Same two-step cmake configure+build as SuperTux, and the configure step
        # matters for the same reason: CMakeLists gathers sources with
        # file(GLOB_RECURSE) and no CONFIGURE_DEPENDS, so a branch switch that
        # adds/removes .cpp files needs a re-glob or the stale build links wrong.
        # -S and -B are both the repo (in-source build).
        # CMAKE_EXPORT_COMPILE_COMMANDS is what step 2a's instrumenter parses the
        # source with: without the database libclang falls back to the spec's
        # synthesized flags, which resolve fewer types and so silently reject
        # candidate variables. The database lands in the repo root here (the build
        # is in-source) and is gitignored on `base`, so it does not trip
        # ensure_clean.
        build_commands=(
            ("cmake", "-S", str(repo), "-B", str(repo),
             "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"),
            ("cmake", "--build", str(repo), f"-j{BUILD_JOBS}"),
        ),
        # Just the binary: ldd shows Witch_Blast links only system libs (SFML,
        # libstdc++, …) — no build-local .so to snapshot.
        cache_artifacts=("Witch_Blast",),
        # Tapes are saved under agent::userDataDir() = the XDG data dir,
        # ~/.local/share/WitchBlast/ (user_dir.cpp), as NAME.rec. Per-branch tapes
        # go in a <branch>/ subfolder, mirroring the other games.
        rec_dir=Path.home() / ".local" / "share" / "WitchBlast",
        rec_glob="*.rec",
        # No windowing flag: Witch Blast reads fullscreen from config.dat in its
        # cwd (the repo root), where it is currently `fullscreen 0` (windowed).
        # Windowed is required for the same reason as SuperTux/STK — a fullscreen
        # surface makes mutter direct-scan-out and starve gamerv's PipeWire
        # *monitor* screencast. Keep config.dat's `fullscreen 0`.
        launch_args=(),
        # Witch Blast replays a tape with `--replay <name-or-path>` (main.cpp;
        # separate args, not joined). It resolves a bare name against the data dir
        # or takes a full path, so we pass the tape's full path.
        replay_flag="--replay",
        replay_flag_joins_value=False,
        # Like SuperTux, Witch Blast honours --replay-delay <s>: the replay boots
        # straight into gameplay and fires its first events (enter-level, first
        # room) the instant it is set up, so the game holds a blank window for this
        # long first, letting gamerv attach and CLEAR the event channel before the
        # replay begins (main.cpp --replay-delay -> setReplayDelay -> the pre-roll
        # loop in startGame). The same value is the controller's stop-timer offset
        # (finish_at = launch + replay_delay + replay_dur + replay_margin). Must
        # comfortably exceed window-appear + gamerv attach + CLEAR; the first tape
        # of a run can take ~8-9 s to attach, later tapes are sub-second.
        replay_delay_flag="--replay-delay",
        replay_delay=6.0,
        replay_margin=4.0,
        # The simulation runs a fixed logical timestep of 1/60 s
        # (agent/determinism.hpp FIXED_DT = 1/60, LOGICAL_FPS = 60), one tape frame
        # per step, so a tape of N frames replays in N/60 s.
        seconds_per_step=1.0 / 60.0,
        # Witch Blast's replay honours a `--frames N` budget (default 600) and
        # self-closes when it is spent; a real tape can be longer (e.g. demo.rec is
        # 1200 frames = 20 s), which the 600 budget would cut in half. Pass a
        # budget larger than any tape so the tape-exhausted check ends the replay
        # instead. (SuperTux/STK have no such budget, so they leave this empty.)
        replay_extra_args=("--frames", "1000000"),
        # Step 2a, as for SuperTux: gamerv's instrumenter rewrites this tree before
        # every build so the binary reports its function-locals, and the edit is
        # reverted right after the compile (instrument.py). Both preconditions
        # hold: the REPORT_LOCAL runtime lives on `base` (sfml_game/GameEntity.h +
        # predicates/local_report.hpp), and gamerv resolves a spec for this game
        # (config/games/Witch Blast/source_instrument.json — the built-in default's
        # scan globs are SuperTux paths and would find nothing here). Measured: 766
        # keys over 17 files, and the instrumented tree really compiles.
        instrument_src=repo / "src",
        # No `--members`: Witch Blast's entities already report their members
        # through reportState overrides that the game itself maintains, and the
        # instrumenter's member path would only duplicate them (its keys drop no
        # prefix here — fields are plain x/y/velocity — so a member key could also
        # collide with a reserved consumer key).
    )


def _asteroids() -> Game:
    repo = Path("/home/sebastiao/asteroids")
    build = repo / "build"
    return Game(
        name="Asteroids",
        repo_dir=repo,
        build_dir=build,
        # Asteroids is a browser game — there is nothing to compile. The "binary"
        # is a generated launcher (build/asteroids-run) that boots the bridge
        # (predicates/bridge.py: serves the files + owns the UDP event channel) and
        # a visible Chrome window on the game, then holds both alive until the
        # controller SIGTERMs it. See agent/gamerv_launch.py.
        binary=build / "asteroids-run",
        # The event-log bridge, the agent (record/replay), the ?replay= auto-start
        # hook and this launcher all live on `base`; mutant branches rebase onto it.
        base_branch="base",
        # No compile step: agent/build_launcher.sh just (re)generates
        # build/asteroids-run, stamping it with the current source tree hash. That
        # stamp is the whole point — the log cache keys on file_sha(binary), and a
        # game that never compiles would otherwise hand it a constant hash that is
        # blind to game.js edits (→ risk of restoring another revision's logs). The
        # stamp gives the launcher a hash that tracks the game source, exactly like
        # a compiled binary does for the other games. Re-run every build (cheap).
        build_commands=(
            ("bash", "agent/build_launcher.sh"),
        ),
        # The generated launcher is the only artifact to snapshot (it execs the
        # committed agent/gamerv_launch.py, which the checked-out tree supplies).
        cache_artifacts=("asteroids-run",),
        # Tapes live outside the repo (mirroring the native games' data dirs), one
        # subfolder per branch: ~/.local/share/asteroids/<branch>/*.demo.txt. The
        # launcher copies the requested tape under the bridge's served root so the
        # page can fetch it; the controller never needs it web-reachable itself.
        rec_dir=Path.home() / ".local" / "share" / "asteroids",
        rec_glob="*.demo.txt",
        # No launch args: all browser flags (app window, throwaway profile, windowed
        # — never fullscreen/kiosk, which starves GNOME's monitor screencast the
        # same way it does the native games) are baked into the launcher.
        launch_args=(),
        # The launcher takes the tape as `--replay <path>` (separate args) and
        # republishes it to the page as a URL parameter.
        replay_flag="--replay",
        replay_flag_joins_value=False,
        # The launcher accepts `--replay-delay <s>` for parity but ignores it: the
        # page starts the replay off GameRV's CLEAR (its "attached, channel reset"
        # signal), not a fixed in-game pre-roll, so there is nothing to make the
        # game wait for. This value is purely the controller's stop-timer estimate
        # of launch → browser-up → gamerv-attach → CLEAR latency (finish_at =
        # launch + replay_delay + replay_dur + replay_margin) and the "attached
        # late" warning. The first tape's cold Chrome start pushes attach out, so
        # keep it generous.
        replay_delay_flag="--replay-delay",
        replay_delay=12.0,
        replay_margin=6.0,
        # determinism.js fixes a 16 ms virtual step (DEFAULT_STEP_MS = 16; the tape
        # header records `step 16`), one tape frame per step, so a tape of N frames
        # replays in N * 0.016 s.
        seconds_per_step=16.0 / 1000.0,
        # instrument_src stays None, and not for want of a spec: gamerv's
        # instrumenter is a libclang tool (runners.source_instrument parses C++ via
        # clang.cindex and its spec's "language" is "cpp"), and this game is
        # JavaScript. Its reporting runtime exists all the same — GameRV.local /
        # localArray / localObject in predicates/local_report.js — so a local here
        # is reported by writing the call at the site, the way SuperTux's
        # player.cpp does by hand. Nothing about step 2a would fail for Asteroids;
        # it simply has nothing to run.
    )


def _freeminer() -> Game:
    repo = Path("/home/sebastiao/freeminer")
    build = repo / "build"
    return Game(
        name="FreeMiner",
        repo_dir=repo,
        build_dir=build,
        binary=build / "freeminer",
        # The GameRV event-log server (src/client/predicates/), the record/replay
        # agent (src/client/agent/) and the --replay-delay / --gamerv launch hooks
        # all live on `base`; mutant branches rebase onto it.
        base_branch="base",
        # Same two-step cmake configure+build as the other native games. Unlike
        # SuperTux, FreeMiner lists its sources explicitly (only *headers* are
        # globbed), so a branch switch cannot silently drop a .cpp — but the
        # configure is re-run anyway: it is cheap next to the compile and keeps
        # a branch that does touch a CMakeLists honest.
        # CMAKE_EXPORT_COMPILE_COMMANDS is what step 2a's instrumenter parses the
        # source with: without the database libclang falls back to the spec's
        # synthesized flags, which resolve fewer types and so silently reject
        # candidate variables. build/ is gitignored, so the file it writes there
        # never trips ensure_clean.
        build_commands=(
            ("cmake", "-S", str(repo), "-B", str(build),
             "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"),
            ("cmake", "--build", str(build), f"-j{BUILD_JOBS}"),
        ),
        # Just the binary: ldd shows freeminer links only system libs (its
        # in-tree deps — irrlicht, luajit, … — are static).
        cache_artifacts=("freeminer",),
        # Tapes live outside the repo (the game's own default record path is
        # <repo>/records/, which we deliberately do not use — the controller
        # aborts on a dirty tree). One subfolder per branch, as everywhere else:
        # ~/.local/share/freeminer/<branch>/*.fmdemo. The .fmact action script
        # the recorder writes next to each tape is ignored by rec_glob.
        rec_dir=Path.home() / ".local" / "share" / "freeminer",
        rec_glob="*.fmdemo",
        # --go skips the main menu and --worldname picks the world the tapes were
        # recorded in. That pair is what makes the world path the game computes
        # (porting::path_user + "/worlds/<name>") byte-identical to the one in the
        # tape header — AgentRuntime::restoreStartState refuses a tape recorded in
        # a different world, comparing the two paths as strings.
        #
        # --gamerv turns the UDP event responder on for this run only (the
        # gamerv_event_log setting is off by default, so without it the game
        # answers nothing and every trace comes back empty).
        #
        # No fullscreen flag: windowed, for the same reason as every other game
        # here — a fullscreen surface makes GNOME/mutter direct-scan-out and
        # starve gamerv's PipeWire *monitor* screencast.
        launch_args=("--go", "--worldname", "main", "--gamerv"),
        replay_flag="--replay",
        replay_flag_joins_value=False,       # "--replay <tape>" (separate args)
        # FreeMiner honours --replay-delay <s>, but not as a plain sleep: it holds
        # the autostarted tape until gamerv's CLEAR lands (the "attached, channel
        # reset" signal) plus a 1 s settle, and only falls back to the flat delay
        # if no CLEAR arrives — the Asteroids trigger rather than the SuperTux
        # pre-roll. It has to be that way here because the wait starts when the
        # WORLD is up, and how long the map/media load takes varies far too much
        # to cover with a fixed number.
        replay_delay_flag="--replay-delay",
        # The controller's own stop-timer offset (finish_at = launch +
        # replay_delay + replay_dur + replay_margin) and its "attached late"
        # warning threshold: launch → world up → gamerv attach → CLEAR.
        replay_delay=15.0,
        replay_margin=10.0,
        # Unlike the other games FreeMiner has NO fixed logical timestep: it
        # renders as fast as it can and a tape stores each frame's real dtime, so
        # a replay of N frames takes N / (frames per second actually achieved)
        # seconds of wall clock — the recorded dtimes set how far the simulation
        # moves, not how long that takes. This value is therefore a conservative
        # floor of ~20 fps rather than a physical constant (a real tape recorded
        # ~29 fps: 557 frames over 19.2 s of recorded dtime). Over-waiting is
        # safe — the game freezes itself when the tape runs out, so the extra
        # seconds add frozen frames, not events — whereas under-waiting truncates
        # the trace, since a game that never exits gives the poll loop nothing to
        # stop early on.
        seconds_per_step=1.0 / 20.0,
        # On SIGTERM the client disconnects, the embedded server shuts down and
        # the world's LevelDB map database is flushed — measured at ~5-8 s, i.e.
        # right at the default grace. A SIGKILL landing in the middle of that
        # write can corrupt worlds/main, and every tape refuses to replay in any
        # other world, so give it room.
        shutdown_grace=25.0,
        # Step 2a, as for SuperTux: gamerv's instrumenter rewrites this tree before
        # every build so the binary reports its function-locals, and the edit is
        # reverted right after the compile (instrument.py). Both preconditions
        # hold: the REPORT_LOCAL runtime lives on `base` (the predicates::LocalHost
        # mixin on LocalPlayer and ClientActiveObject), and gamerv resolves a spec
        # for this game (config/games/FreeMiner/source_instrument.json — the
        # built-in default's scan globs are SuperTux paths and would find nothing
        # here). Measured: 229 keys over 2 files, and the instrumented tree really
        # compiles.
        #
        # That spec scans only localplayer.cpp and content_cao.cpp, deliberately:
        # a FreeMiner snapshot spends its byte budget (gamerv_max_snapshot_bytes)
        # on map-node pseudo-objects, and reported locals are written inside that
        # budget — so every key here is paid for in nodes. Widening the globs is a
        # trade against node coverage, not a free win.
        instrument_src=repo / "src",
        # No `--members`: the CAOs' members are already written by their own
        # report_state overrides, and the local player's by snapshot.cpp directly
        # (which is not a per-class hook the instrumenter can extend).
    )


# The game roster. Append a Game here to support a new game; its ``name`` must
# match its gamerv config/games/<name> folder.
_GAMES: tuple[Game, ...] = (_supertux(), _supertuxkart(), _witchblast(),
                            _asteroids(), _freeminer())
GAMES: dict[str, Game] = {g.name: g for g in _GAMES}
_GAMES_BY_KEY: dict[str, Game] = {g.name.strip().casefold(): g for g in _GAMES}


class UnknownGameError(KeyError):
    """The dataset named a game with no :class:`Game` bundle in :data:`GAMES`."""


def get_game(name: str) -> Game:
    """Resolve a dataset "Game Name" to its :class:`Game` (case-insensitive).

    Raises :class:`UnknownGameError` for a blank or unregistered name so the
    pipeline can turn it into a per-row ERROR rather than silently mis-building.
    """
    g = _GAMES_BY_KEY.get((name or "").strip().casefold())
    if g is None:
        raise UnknownGameError(
            f"unknown game {name!r}; known games: {sorted(GAMES)}")
    return g


# --- GameRV ---------------------------------------------------------------
# gamerv's worker must run inside the *activated* `gamerv` conda env: the `log`
# op uses GStreamer via GObject-introspection, whose typelibs are only found once
# conda activation sets GI_TYPELIB_PATH (and friends). Running the env's python
# binary directly skips activation, so we go through `conda activate` in a shell.
CONDA_ENV = "gamerv"
CONDA_SH = Path.home() / "miniconda3" / "etc" / "profile.d" / "conda.sh"
GAMERV_SRC_DIR = GAMERV_DIR / "src"
GAMERV_WORKER = GAMERV_SRC_DIR / "gui" / "worker.py"


def gamerv_cmd(args: list[str]) -> list[str]:
    """Wrap a ``python <args...>`` call so it runs in the activated gamerv env.

    ``exec`` replaces the shell with python, so python is the direct child and a
    SIGINT (used to stop the logger) reaches it. The ``"$@"`` hand-off avoids
    quoting issues with spaces in args (rec paths, formulas).
    """
    # `-u`: unbuffered stdout. The `log` op prints "Recording started" then goes
    # silent; without -u that line sits in the pipe's block buffer and the
    # controller's reader never sees it (logging then appears to run forever).
    script = f'source "{CONDA_SH}" && conda activate {CONDA_ENV} && exec "$@"'
    return ["bash", "-c", script, "bash", "python", "-u", *args]
GAMERV_LOGS_DIR = GAMERV_DIR / "logs"
# Cleared logs are moved here (under a per-clear timestamped subdir) instead of
# being deleted, so a previous run's traces can be restored after the pipeline.
GAMERV_LOGS_ARCHIVE_DIR = CONTROLLER_DIR / "logs_archive"
GAMERV_PROPERTIES = GAMERV_DIR / "config" / "properties.json"
# Where the original properties.json is parked while the experiment runs.
GAMERV_PROPERTIES_BACKUP = CONTROLLER_DIR / ".properties_backup.json"
# Env var gamerv reads (game_config.active_game) to pick the active game's catalog;
# it overrides gamerv's config/active_game.txt, so setting it per row needs no
# backup/restore. The value is the row's Game.name.
GAMERV_GAME_ENV = "GAME"

# --- Timeouts (seconds) ---------------------------------------------------
BUILD_TIMEOUT = 60 * 30      # a clean cmake build can be slow
# One source_instrument invocation (parse + select + emit + its libclang syntax
# check). Seconds in practice for SuperTux; the ceiling is only there so a wedged
# libclang fails the row instead of hanging the batch.
INSTRUMENT_TIMEOUT = 60 * 15
VALIDATE_TIMEOUT = 60 * 30   # gamerv validate fans out to LLM/DeepFace
GAMERV_OP_TIMEOUT = 60 * 30
