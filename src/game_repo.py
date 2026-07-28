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


def build(game: Game) -> None:
    """Build the game, restoring a cached binary when the source is unchanged.

    The build cache is keyed by the HEAD tree hash (an exact content key for the
    checked-out source — see head_tree_hash / buildcache.py). On a hit the cached
    binary is restored into the build dir and the expensive configure+build is
    skipped; on a miss (or a corrupt slot) we build for real and snapshot the
    result. The cache lives inside build() so every call site benefits — the normal
    step-2 build and both builds of a baseline-contrast row.
    """
    branch = current_branch(game)
    key = head_tree_hash(game)
    if buildcache.has(game, branch, key) and buildcache.restore(game, branch):
        print("  [build-cache] hit — restoring binary, skipping build")
        if game.binary.exists():
            return
        print("  [build-cache] restored slot incomplete — rebuilding")
    print("  [build-cache] miss — building")
    buildcache.invalidate(game, branch)
    _configure_and_build(game)
    buildcache.store(game, branch, key)


def _configure_and_build(game: Game) -> None:
    """Run the game's build commands in order. Binary lands at ``game.binary``.

    The commands (cmake configure, then build, for SuperTux) come from
    ``game.build_commands`` and are re-run in full on every build — for SuperTux
    the configure step re-globs sources so a branch switch that adds/removes .cpp
    files is picked up (file(GLOB) has no CONFIGURE_DEPENDS). See config.Game.
    """
    for cmd in game.build_commands:
        print(f"  [{game.name}] $ {' '.join(cmd)}")
        _run_build(game, list(cmd))
    if not game.binary.exists():
        raise GameError(f"build finished but {game.binary} is missing")


def _run_build(game: Game, cmd: list[str]) -> None:
    proc = subprocess.run(cmd, cwd=str(game.repo_dir),
                          text=True, timeout=config.BUILD_TIMEOUT)
    if proc.returncode != 0:
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
