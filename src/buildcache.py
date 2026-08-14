"""Content-addressed cache of built game binaries, one slot per game+branch.

A branch's compiled binary depends only on its source content. The controller
checks out each branch with a clean working tree (``game_repo.ensure_clean``
aborts a dirty run), so the HEAD tree hash (``git rev-parse HEAD^{tree}``) is an
exact content key — it covers every tracked file, ``CMakeLists.txt`` included. So
when the same source tree is built again, the configure+build can be skipped
entirely: the cached binary is restored into the build dir and the game runs from
it. A changed source tree simply yields a different key, missing the cache and
rebuilding (self-correcting, exactly like ``logcache``).

The cache is keyed by the tree hash but stored under a per-game+branch slot, so
only ONE entry per branch is ever kept (bounded disk), games can't collide on a
shared branch name (e.g. ``base``), and a changed tree deletes the slot and
rebuilds:

    build_cache/<game>/<branch>/
        meta.json            # {"key": "<tree-sha>", "artifacts": [...], "created": "..."}
        <binary>             # the game binary (game.cache_artifacts[0])
        <build-local .so>    # any build-local shared libs it loads at runtime

Only the game binary and the build-local shared libraries it loads (``game.
cache_artifacts``) are snapshotted, not the whole build dir. The game finds its
data in the source tree, not the build dir, so a cache hit still relies on the
caller having checked out the matching branch.
"""

import json
import shutil
import time
from pathlib import Path

import config
from config import Game


def slot(game: Game, branch: str) -> Path:
    """Where this branch's cached artifacts live (whether or not it exists)."""
    return config.BUILD_CACHE_DIR / game.name / branch


def _slot(game: Game, branch: str) -> Path:
    return slot(game, branch)


def _meta(game: Game, branch: str) -> Path:
    return _slot(game, branch) / "meta.json"


def cached_key(game: Game, branch: str) -> str | None:
    """The key currently cached for this branch, or None if no valid slot."""
    meta = _meta(game, branch)
    if not meta.exists():
        return None
    try:
        return json.loads(meta.read_text()).get("key")
    except (json.JSONDecodeError, OSError):
        return None


def has(game: Game, branch: str, key: str) -> bool:
    """True if the branch's cached binary matches ``key`` (a usable hit)."""
    return cached_key(game, branch) == key


def invalidate(game: Game, branch: str) -> None:
    """Delete the branch's cache slot (stale tree, or before a fresh store)."""
    shutil.rmtree(_slot(game, branch), ignore_errors=True)


def store(game: Game, branch: str, key: str) -> None:
    """Snapshot the freshly built artifacts from the build dir into the slot."""
    invalidate(game, branch)
    slot = _slot(game, branch)
    slot.mkdir(parents=True, exist_ok=True)
    stored = []
    for rel in game.cache_artifacts:
        src = game.build_dir / rel
        if not src.exists():
            continue
        dest = slot / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        stored.append(rel)
    _meta(game, branch).write_text(json.dumps({
        "key": key,
        "artifacts": stored,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }))


def restore(game: Game, branch: str) -> bool:
    """Copy the branch's cached artifacts back into the build dir, overwriting.

    Returns False if the binary did not land (corrupt/partial slot) so the caller
    can fall back to a real build.
    """
    slot = _slot(game, branch)
    try:
        artifacts = json.loads(_meta(game, branch).read_text())["artifacts"]
    except (json.JSONDecodeError, OSError, KeyError):
        return False
    for rel in artifacts:
        src = slot / rel
        if not src.exists():
            continue
        dest = game.build_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return game.binary.exists()
