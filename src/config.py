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
# per recording: hints/<game>/<branch>/<recX>.hint (baseline-contrast rows
# distinguish base vs mutant). Written whenever the operator answers a prompt;
# replayed with run_experiment.py --manual-auto. See hints.py and manual_review.py.
HINTS_DIR = CONTROLLER_DIR / "hints"
# Content-addressed cache of step-5 raw logs, one slot per game+branch:
# log_cache/<game>/<branch>/ holds pristine run dirs + meta.json (the binary+tape
# hash). A matching hash lets the pipeline skip recording and restore the cached
# logs; a changed binary/tape deletes the slot and re-records. See logcache.py.
LOG_CACHE_DIR = CONTROLLER_DIR / "log_cache"
# Content-addressed cache of built binaries, one slot per game+branch:
# build_cache/<game>/<branch>/ holds the game binary + its build-local .so plus
# meta.json (the HEAD tree hash). A matching tree hash lets game_repo.build() skip
# the cmake configure+build and restore the binary; a source change → different
# tree hash → miss → rebuild. See buildcache.py.
BUILD_CACHE_DIR = CONTROLLER_DIR / "build_cache"

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
    rec_dir: Path                               # root of the .rec replay tapes
    launch_args: tuple[str, ...]                # args added to every game launch
    replay_flag: str                            # CLI flag naming the tape to replay
    replay_delay_flag: str                      # CLI flag for the pre-replay delay
    replay_delay: float                         # seconds the game waits before replay
    replay_margin: float                        # extra wait after a replay's duration
    seconds_per_step: float                     # wall-clock seconds per logical frame
    baseline_contrast_branches: frozenset[str]  # always-on mutations (record on base+mutant)


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
        build_commands=(
            ("cmake", "-S", str(repo), "-B", str(build)),
            ("cmake", "--build", str(build), f"-j{BUILD_JOBS}"),
        ),
        # The game binary plus the only build-local shared library it links at
        # runtime (libsimplesquirrel.so — confirmed via ldd; every other dependency
        # is a system lib). Anything missing at store time is skipped.
        cache_artifacts=("supertux2", "external/simplesquirrel/libsimplesquirrel.so"),
        rec_dir=Path.home() / ".local" / "share" / "supertux2",
        # WINDOWED, deliberately NOT --fullscreen: with a fullscreen surface,
        # GNOME/mutter direct-scans-it-out and stops feeding the PipeWire *monitor*
        # screencast gamerv records — capture then measures ~0.09 fps, the event
        # CLEAR lands after the replay already ran (wiping its events), and the
        # GStreamer teardown hangs until you Alt+Tab. A normal composited window
        # keeps mutter compositing so the screencast gets ~30 fps.
        launch_args=("--window",),
        replay_flag="--replay",
        replay_delay_flag="--replay-delay",
        # Must comfortably exceed window-appear + gamerv attach + event CLEAR, or
        # the CLEAR lands mid-replay and clips early events. The FIRST tape of a
        # run can take ~8-9s to attach; later tapes are sub-second.
        replay_delay=12.0,
        replay_margin=4.0,
        # constants.hpp LOGICAL_FPS = 1000/15, i.e. a fixed 15 ms logical step, so
        # a tape of N frames replays in N * 0.015 s.
        seconds_per_step=15.0 / 1000.0,
        # Always-on mutations: the mutation breaks its property on EVERY occurrence,
        # so the mutant trace holds only violation states (P & ~Q) and no good
        # states (P & Q). For these the controller records the row's tapes on BOTH
        # base and the mutant into one logs/ dir; gamerv's cross-trace pool supplies
        # base's good states vs the mutant's violations. See pipeline.py.
        baseline_contrast_branches=frozenset({"change_music_config"}),
    )


# The game roster. Append a Game here to support a new game; its ``name`` must
# match its gamerv config/games/<name> folder.
_GAMES: tuple[Game, ...] = (_supertux(),)
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
GAMERV_WORKER = GAMERV_DIR / "src" / "gui" / "worker.py"


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
VALIDATE_TIMEOUT = 60 * 30   # gamerv validate fans out to LLM/DeepFace
GAMERV_OP_TIMEOUT = 60 * 30
