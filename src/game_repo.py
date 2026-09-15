"""Game-repo side of the pipeline: git checkout, build, game lifecycle.

Every function takes the row's :class:`config.Game`, so the same logic drives any
game (mutant = a git branch of that game's repo, built to an executable). Policy
decisions baked in here (from the plan):
  * The working tree must be clean before we switch branches — if it is dirty we
    abort rather than stash or discard (``ensure_clean``).
  * Branches are assumed already reconciled with the game's ``base`` branch by
    ``prepare_branches.py``; this module just checks one out and builds it.
"""

import subprocess
import time

import buildcache
import config
import instrument
import progress
from config import Game


class DirtyTreeError(RuntimeError):
    """A game's working tree has uncommitted changes."""


class GameError(RuntimeError):
    """A git / build / launch step failed."""


def _git(game: Game, *args: str, capture: bool = True) -> str:
    """Run a git command in the game's repo; return stdout (stripped)."""
    proc = subprocess.run(
        ["git", "-C", str(game.repo_dir), *args],
        capture_output=capture, text=True,
    )
    if proc.returncode != 0:
        raise GameError(
            f"git {' '.join(args)} failed:\n{(proc.stderr or proc.stdout).strip()}"
        )
    return (proc.stdout or "").strip()


def ensure_clean(game: Game) -> None:
    """Abort the run if the game's working tree has uncommitted changes."""
    status = _git(game, "status", "--porcelain")
    if status:
        raise DirtyTreeError(
            f"{game.name} working tree is dirty — commit or stash before running:\n"
            + status
        )


def current_branch(game: Game) -> str:
    return _git(game, "rev-parse", "--abbrev-ref", "HEAD")


def head_tree_hash(game: Game) -> str:
    """The git tree hash of HEAD — an exact content key for the checked-out source.

    With a clean working tree (``ensure_clean``), this uniquely identifies every
    tracked file, so it keys the build cache (see buildcache.py).
    """
    return _git(game, "rev-parse", "HEAD^{tree}")


def build_key(game: Game, instrument_source: bool = True) -> str:
    """The content key for what a build of the checked-out source produces.

    The HEAD tree hash, plus the instrumenter's fingerprint when step 2a is on
    (the instrumented bytes are what get compiled and they do not move the tree
    hash — see ``build``).

    Only the build cache keys on this. The log cache keys on ``head_tree_hash``
    plus the built artifacts' bytes, leaving the fingerprint out wherever the
    artifacts went through a compiler and so carry it already; the exception is a
    ``source_served_at_runtime`` game, whose artifacts cannot — see
    ``pipeline._record_or_restore``.
    """
    key = head_tree_hash(game)
    if instrument_source and instrument.enabled(game):
        key = f"{key}+i{instrument.fingerprint(game)}"
    return key


def _branch_exists_local(game: Game, branch: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(game.repo_dir),
         "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True, text=True,
    ).returncode == 0


def checkout(game: Game, branch: str) -> None:
    """Check out ``branch``, creating a local tracking branch from origin if needed."""
    if _branch_exists_local(game, branch):
        _git(game, "checkout", branch)
    else:
        # Create a local branch tracking origin/<branch> (e.g. remote-only rows).
        _git(game, "checkout", "-b", branch, "--track", f"origin/{branch}")
    print(f"  [{game.name}] on branch {current_branch(game)}")


def return_to_base(game: Game) -> None:
    _git(game, "checkout", game.base_branch)


def build(game: Game, instrument_source: bool = True) -> None:
    """Build the game, restoring a cached binary when the source is unchanged.

    Step 2a runs first: unless the game opts out (``Game.instrument_src is None``)
    or the caller passes ``instrument_source=False``, gamerv's source instrumenter
    writes the local-variable reporting calls into the checked-out tree, the build
    compiles *that*, and the edit is reverted immediately afterwards — pass or
    fail — so the repo is clean again for the next checkout. See instrument.py.

    The build cache is keyed by the HEAD tree hash (an exact content key for the
    checked-out source — see build_key / head_tree_hash / buildcache.py) **plus a
    fingerprint of the instrumenter**, since the instrumented bytes are what
    actually get compiled and they do not move the tree hash. The fingerprint is not the plan's
    digest: deriving that means running the instrumenter (a libclang parse of the
    whole game), which is most of what the cache is here to skip. It hashes the
    inputs that decide the plan instead — the tool's code, the game's spec, our
    flags — so it is a few file reads and the lookup happens *before* step 2a.
    See instrument.fingerprint.

    On a hit the cached binary is restored into the build dir and both the
    instrumentation and the expensive configure+build are skipped — which is what
    makes the key's cost worth minding: step 2a is a libclang parse of the whole
    game (~100 s for SuperTux), so a key that cannot be computed without running
    it would pay for the thing it is meant to avoid.

    On a miss (or a corrupt slot) we build for real and snapshot the result. The
    instrumentation runs once, inside ``instrument.applied`` — the write pass
    discovers what to insert as part of inserting it, so there is no dry run
    first. The stale slot is left in place until the snapshot succeeds
    (``buildcache.store`` replaces it), so a failed or interrupted build does not
    throw away a binary the next run could still have used. The cache lives inside
    build() so every call site benefits.
    """
    branch = current_branch(game)
    instrumenting = instrument_source and instrument.enabled(game)
    key = build_key(game, instrument_source)
    if buildcache.has(game, branch, key):
        if buildcache.restore(game, branch):
            # The key table travels with the binary. Step 2a is skipped on a hit, so
            # without this the kept table would still be the one the last *miss*
            # produced — a sibling branch's vocabulary, missing exactly this
            # mutation's own locals. See instrument.stash_keys.
            kept = instrument.restore_keys(game, buildcache.slot(game, branch))
            print("  [build-cache] hit — restoring binary, skipping build"
                  + ("" if kept or not instrumenting
                     else " (slot predates the key table; term rules may be stale)"))
            return
        print("  [build-cache] restored slot incomplete — rebuilding")
    else:
        print("  [build-cache] miss — building")
    if instrumenting and not game.source_served_at_runtime:
        with instrument.applied(game):
            _configure_and_build(game, branch)
    else:
        # Either nothing to instrument, or a game whose SOURCE is what runs (see
        # Game.source_served_at_runtime). In the second case the caller owns the
        # instrumentation's lifetime and has already applied it — reverting here
        # would strip the edit before the tape ever replays, which is exactly the
        # bug this branch exists to avoid.
        _configure_and_build(game, branch)
    buildcache.store(game, branch, key)
    # After store(), which replaces the slot wholesale.
    instrument.stash_keys(game, buildcache.slot(game, branch))


def _configure_and_build(game: Game, branch: str) -> None:
    """Run the game's build commands in order. Binary lands at ``game.binary``.

    The commands (cmake configure, then build, for SuperTux) come from
    ``game.build_commands`` and are re-run in full on every build — for SuperTux
    the configure step re-globs sources so a branch switch that adds/removes .cpp
    files is picked up (file(GLOB) has no CONFIGURE_DEPENDS). See config.Game.

    ``branch`` only names the build log; the tree it compiles is whatever is
    checked out (the caller just switched to it).
    """
    for cmd in game.build_commands:
        _run_build(game, list(cmd), branch)
    if not game.binary.exists():
        raise GameError(f"build finished but {game.binary} is missing")


def _run_build(game: Game, cmd: list[str], branch: str) -> None:
    """One build command, condensed to a single self-updating progress line.

    The full output is not lost — it goes to ``config.build_log_path`` and, if
    the command fails, its errors (or its tail) are printed. Set
    CONTROLLER_BUILD_OUTPUT=raw to inherit the command's stdout instead, which is
    what this used to do unconditionally; a build of a big game then floods the
    terminal and buries the pipeline's own step headers. See progress.py.
    """
    if not config.BUILD_PROGRESS:
        print(f"  [{game.name}] $ {' '.join(cmd)}")
        rc = subprocess.run(cmd, cwd=str(game.repo_dir),
                            text=True, timeout=config.BUILD_TIMEOUT).returncode
    else:
        rc = progress.run(cmd, cwd=game.repo_dir, timeout=config.BUILD_TIMEOUT,
                          tag=game.name,
                          log_path=config.build_log_path(game.name, branch))
    if rc != 0:
        raise GameError(f"command failed: {' '.join(cmd)}")


def launch(game: Game, extra_args: list[str] | None = None) -> subprocess.Popen:
    """Launch the game and return the process handle.

    The event-log UDP server lives inside the game, so the process must stay
    alive across a logging+replay cycle. The caller is responsible for calling
    ``shutdown`` when done. ``extra_args`` appends to ``game.launch_args`` — the
    controller uses it to pass the per-tape ``--replay <rec> --replay-delay <s>``.
    """
    # NOTE (SuperTux): do NOT force SDL_VIDEODRIVER=x11 here. Under XWayland the
    # game becomes a true fullscreen window that mutter promotes to direct scanout
    # (unredirect), which stalls the PipeWire monitor screencast gamerv relies on —
    # capture delivers a single frame and no events. Native Wayland (the game
    # filling only a corner) keeps mutter compositing, so capture works.
    args = [str(game.binary), *game.launch_args, *(extra_args or [])]
    proc = subprocess.Popen(args, cwd=str(game.repo_dir))
    print(f"  [{game.name}] launched: {' '.join(args)} (pid {proc.pid})")
    return proc


def shutdown(proc: subprocess.Popen | None, grace: float = 5.0) -> None:
    """Terminate the game process, escalating to kill if it ignores SIGTERM.

    ``grace`` normally comes from the row's ``Game.shutdown_grace``: a game that
    saves state on the way out (FreeMiner writes its world back to disk) needs
    more of it than one that just closes a window.
    """
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    proc.kill()
    proc.wait(timeout=grace)
