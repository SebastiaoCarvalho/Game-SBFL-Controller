"""Content-addressed cache of step-5 raw logs, one slot per game+branch.

A row's recorded traces (events/video/audio/predicate trace, *before* validation)
depend only on the built game and the ``.rec`` tapes — never on the property,
refinement mode, or threshold strategy, which are all applied later in steps 6/8.
So when the same game replays the same tapes again, the recording can be skipped:
the cached logs are restored into ``logs/`` and re-validated.

"The built game" is *not* the binary alone. The game also loads assets and scripts
straight from the source tree at runtime (FreeMiner's ``builtin/*.lua``, SuperTux's
``data/levels/*.stl``), so a mutant living in one of those changes behaviour while
compiling to a byte-identical binary. The caller therefore folds the HEAD tree
hash — which covers every tracked file (``game_repo.head_tree_hash``) — into the
key alongside the artifact hashes, and for a game whose source is what *runs*
(Asteroids: no compiler, ``game.js`` served out of the repo) the step-2a
fingerprint as well, since no artifact of that build carries it. See
``compute_key`` and ``pipeline._record_or_restore``.

The cache is keyed by a content hash (the tree hash + artifact shas + the tapes)
but stored under a per-game+branch slot, so only ONE entry per branch is ever kept
(bounded disk), games can't collide on a shared branch name, and a changed
source/binary/tape simply deletes the slot and re-records:

    log_cache/<game>/<branch>/
        meta.json            # {"key": "<sha256>", "run_dirs": [...], "created": "..."}
        <run_timestamp>/     # pristine copies of the step-5 run dirs

The snapshot is taken at step 5, before validation, so the cache only ever holds
pristine logs (no ``validation_result.json``); validation re-runs cleanly on every
hit. Content hashing is self-correcting: any source or binary change → different
hash → miss → re-record.
"""

import hashlib
import json
import shutil
import time
from pathlib import Path

import config
import recordings
from config import Game

# Validation/manual artifacts never belong in a pristine snapshot. They don't exist
# at the step-5 store point, but ignore them defensively in case store ever moves.
_IGNORE = shutil.ignore_patterns(
    "validation_result.json", "manual_answers.json", "manual_assets")


def file_sha(path: Path) -> str:
    """Chunked sha256 of one file (the binary is tens of MB; sub-second)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tapes_sha(game: Game, branch: str) -> str:
    """sha256 over the branch's sorted ``.rec`` tapes (name + contents).

    Folding each tape's name in as well means an add/remove/rename invalidates the
    key even if the surviving tapes' bytes are unchanged.
    """
    h = hashlib.sha256()
    for rec in recordings.list_recordings(game, branch):
        h.update(rec.name.encode())
        h.update(file_sha(rec).encode())
    return h.hexdigest()


def compute_key(build_parts: list[str], game: Game, branch: str) -> str:
    """Cache key from the caller's build identifiers and the branch's tape hash.

    ``build_parts`` is whatever identifies the built game: the source tree hash
    followed by the artifact shas. Order matters — it is hashed as a joined list,
    not a set.
    """
    h = hashlib.sha256()
    h.update("\n".join(build_parts).encode())
    h.update(tapes_sha(game, branch).encode())
    return h.hexdigest()


def slot(game: Game, branch: str) -> Path:
    """Where this branch's cached logs live (whether or not it exists)."""
    return config.LOG_CACHE_DIR / game.name / branch


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
    """True if the branch's cached logs match ``key`` (a usable hit)."""
    return cached_key(game, branch) == key


def invalidate(game: Game, branch: str) -> None:
    """Delete the branch's cache slot (stale binary/tape, or before a fresh store)."""
    shutil.rmtree(_slot(game, branch), ignore_errors=True)


def store(game: Game, branch: str, key: str, run_dirs: list[Path]) -> None:
    """Snapshot the freshly recorded (pristine) run dirs into the branch's slot."""
    invalidate(game, branch)
    slot = _slot(game, branch)
    slot.mkdir(parents=True, exist_ok=True)
    names = []
    for d in run_dirs:
        shutil.copytree(d, slot / d.name, ignore=_IGNORE)
        names.append(d.name)
    _meta(game, branch).write_text(json.dumps({
        "key": key,
        "run_dirs": names,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }))


def restore(game: Game, branch: str) -> int:
    """Copy the branch's cached run dirs into logs/ (already cleared). Returns count."""
    slot = _slot(game, branch)
    names = json.loads(_meta(game, branch).read_text())["run_dirs"]
    config.GAMERV_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for name in names:
        dest = config.GAMERV_LOGS_DIR / name
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(slot / name, dest)
        n += 1
    return n
