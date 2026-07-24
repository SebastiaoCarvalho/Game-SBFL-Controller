"""Agent recordings (.rec tapes) used to drive replays during logging (step 5).

Each .rec is a recorded autonomous-agent run the game can replay frame by frame
(via its ``--replay`` startup hook). We read the tape's frame count to know how
long a replay will take, so the controller can wait for it to finish before
stopping the log. Tapes live under a game's ``rec_dir``, one subfolder per branch.
"""

from pathlib import Path

from config import Game


def recordings_dir(game: Game, branch: str) -> Path:
    """The per-test-case tape folder: ``<game.rec_dir>/<branch>/``.

    Keeping each test case's tapes in their own folder lets distinct test cases
    produce distinct logs instead of all replaying the same shared pool of tapes.
    """
    return game.rec_dir / branch


def list_recordings(game: Game, branch: str) -> list[Path]:
    """All replay tapes for one test case, sorted by name.

    Only tapes inside ``<game.rec_dir>/<branch>/`` matching the game's
    ``rec_glob`` (``*.rec`` for SuperTux, ``*.stkagent`` for SuperTuxKart) are
    used, so each case replays its own recordings.
    """
    rec_dir = recordings_dir(game, branch)
    if not rec_dir.exists():
        return []
    return sorted(rec_dir.glob(game.rec_glob))


def frame_count(rec_path: Path) -> int:
    """Frames recorded in a tape, read from its ``frames <N>`` header line.

    Both games write a ``key value`` header ending in a ``frames <N>`` line. For
    SuperTux that's the per-frame count (v2: level/bonus/.../frames; v1: a bare
    count on line 2). For SuperTuxKart (.stkagent v6) the header lists per-kart
    tape sections, each opened by its own ``frames <N>``; every kart is captured
    on the same World update so all counts are equal, so the FIRST one is the
    replay length. Returns 0 if no count is found.
    """
    with open(rec_path, encoding="utf-8", errors="replace") as f:
        for _ in range(16):  # header is tiny; don't scan the whole tape
            line = f.readline()
            if not line:
                break
            parts = line.split()
            if len(parts) == 2 and parts[0] == "frames" and parts[1].isdigit():
                return int(parts[1])
            # v1: a bare integer on its own line right after the magic.
            if len(parts) == 1 and parts[0].isdigit():
                return int(parts[0])
    return 0


def replay_duration(game: Game, rec_path: Path) -> float:
    """Estimated wall-clock seconds for a full replay of this tape.

    A tape of N frames replays in ``N * game.seconds_per_step`` (the game's fixed
    logical-frame duration).
    """
    return frame_count(rec_path) * game.seconds_per_step
