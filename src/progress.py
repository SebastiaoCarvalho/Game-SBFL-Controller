"""One rewritable status line for long, chatty subprocesses (the game builds).

Why this exists: ``cmake --build`` on a big game (STK, FreeMiner) prints
thousands of lines, and with the build's stdout inherited they scroll the
pipeline's own step headers out of the terminal — by the time a build finishes
there is nothing on screen to say which row or which step you are in. This
module pipes the command's output instead and renders it as a single line that
is rewritten in place:

    [Super Tux] build  ████████░░░░░░░░  47%  3m04s  object/player.cpp

Nothing is thrown away. Every line still lands in a per-branch log file
(:data:`config.BUILD_LOG_DIR`), compiler ``error:`` lines are echoed through as
they happen (a failing build has to stay readable without opening a file), and a
tail of the output is dumped when the command exits non-zero without having said
anything that looked like an error.

Progress comes from whatever the build system already prints:
  * cmake's Makefile generator — ``[ 47%] Building CXX object ...`` — gives the
    percentage directly (clamped monotonic: with ``-j`` the numbers arrive out of
    order and a bar that walks backwards reads as a bug).
  * cmake's Ninja generator — ``[812/1953] Building CXX object ...`` — gives a
    count, which is turned into both a percentage and an ``n/total`` readout.
  * Anything else (the cmake *configure* pass, Asteroids' launcher script) has no
    progress to report, so the line falls back to a spinner plus the last thing
    the command said. Elapsed time ticks in every case, so a wedged build still
    looks different from a slow one.

When stdout is not a TTY (piped to a file, run under nohup) there is no cursor to
move, so the same information is emitted as an occasional plain line — one per
10% and a heartbeat while a phase reports no percentage — instead of a redraw.
"""

import os
import re
import select
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

# How often the reader wakes up when the command is quiet. Also the redraw
# ceiling: the elapsed clock and the spinner advance on this beat.
_TICK = 0.2
# Minimum wall-clock between redraws, so a build that prints hundreds of lines a
# second does not spend its time writing to the terminal.
_REDRAW = 0.1
# Non-TTY only: seconds between heartbeat lines while a phase reports no
# percentage (a percentage phase announces deciles instead).
_HEARTBEAT = 60.0
# Ring buffer of recent output, for the tail dumped on an unexplained failure.
_RING = 400
_TAIL = 30
# A build broken near the root of a header can emit thousands of errors; echo
# enough to diagnose it and leave the rest to the log file.
_MAX_ERROR_ECHO = 30

# cmake's Makefile generator: "[ 47%] Building CXX object ...".
_MAKE_PCT = re.compile(r"^\[\s*(\d{1,3})%\]\s*(.*)$")
# cmake's Ninja generator (and ninja itself): "[812/1953] Building CXX ...".
_NINJA_PCT = re.compile(r"^\[(\d+)/(\d+)\]\s*(.*)$")
# Some toolchains colorize even into a pipe; strip it for matching and display
# (the log file keeps the stripped text too — it is what you would want to grep).
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ERROR = re.compile(r"(?:^|\s)(?:error|fatal error)\s*:"
                    r"|^\s*CMake Error"
                    r"|^\s*(?:g?make|ninja)(?:\[\d+\])?:\s*\*\*\*", re.IGNORECASE)
_WARNING = re.compile(r"(?:^|\s)warning\s*:", re.IGNORECASE)
# "Building CXX object src/CMakeFiles/supertux2.dir/object/player.cpp.o" — the
# part worth showing is the source file, not the object path cmake wraps it in.
_OBJECT = re.compile(r"\bobject\s+(\S+?)\.o(?:bj)?\b")
_DIR_PART = re.compile(r"[^/]*\.dir/")

_SPIN_UTF = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPIN_ASCII = "|/-\\"


def run(cmd: list[str], *, cwd: Path, timeout: float | None, tag: str,
        log_path: Path | None = None, stream=None) -> int:
    """Run ``cmd``, rendering its output as a single self-updating status line.

    Returns the exit code (the caller decides what a non-zero one means). Raises
    :class:`subprocess.TimeoutExpired` after killing the process, matching what
    ``subprocess.run(timeout=...)`` would have done — the deadline is checked on
    every tick rather than only between lines, so a command that goes silent
    still hits it.

    ``log_path`` receives every line the command printed, plus a header naming
    the command and a footer naming the exit code. One file per caller-chosen
    path, truncated on each run: it is a "what did the last build say" file, not
    an archive.
    """
    stream = stream if stream is not None else sys.stdout
    ren = _Renderer(stream, tag, _phase(cmd))
    log = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("w", encoding="utf-8", errors="replace")
        log.write(f"$ {' '.join(cmd)}\n(cwd {cwd})\n\n")

    started = time.monotonic()
    deadline = started + timeout if timeout else None
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                bufsize=0)
    except OSError:
        if log is not None:
            log.close()
        raise
    tail: deque[str] = deque(maxlen=_RING)
    buf = b""
    pct = None          # monotonic percentage, None until the build reports one
    done = total = 0    # ninja's n/total, 0 when the generator does not report it
    steps = 0           # progress lines seen == compile/link steps completed
    errors = warnings = 0
    activity = ""        # last progress step (a source file, usually)
    note = ""            # last line of any kind, for phases with no progress
    try:
        fd = proc.stdout.fileno()
        while True:
            if deadline is not None and time.monotonic() > deadline:
                _kill(proc)
                ren.abandon(f"timed out after {_hms(time.monotonic() - started)}")
                raise subprocess.TimeoutExpired(cmd, timeout)
            ready, _, _ = select.select([fd], [], [], _TICK)
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    if buf.strip():   # a last line with no terminator
                        text = _ANSI.sub("", buf.decode("utf-8", "replace")).rstrip()
                        if log is not None:
                            log.write(text + "\n")
                        tail.append(text)
                    break
                # A \r-terminated line is a progress redraw from a tool that
                # thought it had a terminal; treat it as a line like any other.
                parts = re.split(rb"\r\n|\r|\n", buf + chunk)
                buf = parts.pop()
                for raw in parts:
                    text = _ANSI.sub("", raw.decode("utf-8", "replace")).rstrip()
                    if log is not None:
                        log.write(text + "\n")
                    tail.append(text)
                    step_pct, step_done, step_total, what = _parse(text)
                    if what is not None:
                        steps += 1
                        activity = what
                    elif text.strip():
                        # A phase with no percentage (cmake's configure) still
                        # has something to say; show its latest word.
                        note = text.strip()[:60]
                    if step_pct is not None:
                        # Clamp: -j finishes objects out of order, and cmake's
                        # own percentages can arrive slightly unsorted.
                        pct = step_pct if pct is None else max(pct, step_pct)
                    if step_total:
                        done, total = max(done, step_done), step_total
                    if _WARNING.search(text):
                        warnings += 1
                    elif _ERROR.search(text):
                        errors += 1
                        if errors <= _MAX_ERROR_ECHO:
                            ren.emit(text)
                        elif errors == _MAX_ERROR_ECHO + 1:
                            ren.emit(f"    ... more errors suppressed; full log: "
                                     f"{log_path}")
            ren.update(pct, done, total, activity or note)
        try:
            # Its stdout is at EOF, so the process has all but exited; the
            # timeout is only here so a wedged child cannot hang the batch.
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _kill(proc)
            rc = proc.returncode if proc.returncode is not None else -1
    except KeyboardInterrupt:
        _kill(proc)
        ren.abandon("interrupted")
        raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if log is not None:
            log.write(f"\n[exit {proc.returncode}]\n")
            log.close()

    if rc == 0:
        ren.finish(f"{steps} step(s)" if steps else "done",
                   warnings, log_path if warnings else None)
    else:
        ren.abandon(f"exit {rc}")
        if errors == 0:
            # Nothing on the way out looked like an error, so the caller would
            # otherwise be left with a bare exit code: show the last of it.
            for line in list(tail)[-_TAIL:]:
                stream.write(f"    | {line}\n")
        if log_path is not None:
            stream.write(f"    full build log: {log_path}\n")
        stream.flush()
    return rc


def _parse(text: str) -> tuple[int | None, int, int, str | None]:
    """Pull ``(percent, done, total, activity)`` out of one line of build output.

    ``activity`` is None for a line that is not a progress step, which is also
    what makes the step counter count steps rather than lines.
    """
    m = _MAKE_PCT.match(text)
    if m:
        return int(m.group(1)), 0, 0, _activity(m.group(2))
    m = _NINJA_PCT.match(text)
    if m:
        n, tot = int(m.group(1)), int(m.group(2))
        return (int(100 * n / tot) if tot else None), n, tot, _activity(m.group(3))
    return None, 0, 0, None


def _activity(text: str) -> str:
    """The short "what is it compiling" half of the status line.

    cmake names an object file inside a ``<target>.dir/`` tree; the source path
    under it is the part a human reads, so unwrap it. Everything else (Linking,
    Generating, ...) is already short enough to show as-is.
    """
    m = _OBJECT.search(text)
    if m:
        path = _DIR_PART.sub("", m.group(1))
        return "/".join(Path(path).parts[-2:])
    return text.strip()


def _phase(cmd: list[str]) -> str:
    """A two-word name for what this command is, for the status line's label."""
    if not cmd:
        return "run"
    if "--build" in cmd:
        return "build"
    exe = os.path.basename(cmd[0])
    if exe == "cmake":
        return "configure"
    if exe in ("bash", "sh") and len(cmd) > 1:
        return os.path.basename(cmd[1])
    return exe


def _kill(proc: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL if the build ignores it. Mirrors game_repo.shutdown."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _hms(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


class _Renderer:
    """The rewritable line itself, and its non-TTY stand-in.

    All terminal writing goes through here so that the two things that must not
    interleave — the status line and an echoed error — cannot: :meth:`emit`
    clears the line before printing and the next :meth:`update` redraws it.
    """

    def __init__(self, stream, tag: str, phase: str):
        self.stream = stream
        self.tag = tag
        self.phase = phase
        self.tty = hasattr(stream, "isatty") and stream.isatty()
        self.utf = "utf" in (getattr(stream, "encoding", "") or "").lower()
        self.start = time.monotonic()
        self.drawn = False       # a status line is currently on screen
        self.last_draw = 0.0
        self.decile = -1         # highest 10% mark already announced (non-TTY)
        self.last_beat = self.start
        self.frame = 0

    # -- rendering ---------------------------------------------------------
    def _elapsed(self) -> float:
        return time.monotonic() - self.start

    def _bar(self, pct: int, cells: int = 16) -> str:
        filled = max(0, min(cells, round(cells * pct / 100)))
        full, empty = ("█", "░") if self.utf else ("#", ".")
        return full * filled + empty * (cells - filled)

    def _spinner(self) -> str:
        frames = _SPIN_UTF if self.utf else _SPIN_ASCII
        self.frame += 1
        return frames[self.frame % len(frames)]

    def _text(self, pct, done, total, activity) -> str:
        head = f"  [{self.tag}] {self.phase}"
        if pct is None:
            parts = [head, self._spinner(), _hms(self._elapsed())]
        else:
            parts = [head, self._bar(pct), f"{pct:3d}%"]
            if total:
                parts.append(f"{done}/{total}")
            parts.append(_hms(self._elapsed()))
        if activity:
            parts.append(activity)
        return "  ".join(parts)

    def update(self, pct, done, total, activity, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_draw < _REDRAW:
            return
        self.last_draw = now
        if self.tty:
            width = shutil.get_terminal_size(fallback=(100, 24)).columns
            self.stream.write("\r\x1b[K" + self._text(pct, done, total,
                                                      activity)[:width - 1])
            self.stream.flush()
            self.drawn = True
            return
        # No cursor to move: a decile mark, or a heartbeat while a phase has no
        # percentage to cross a mark with.
        if pct is not None and pct // 10 > self.decile:
            self.decile = pct // 10
            self.last_beat = now
            self._line(self._text(pct, done, total, activity))
        elif now - self.last_beat >= _HEARTBEAT:
            self.last_beat = now
            self._line(self._text(pct, done, total, activity))

    def _line(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def clear(self) -> None:
        if self.tty and self.drawn:
            self.stream.write("\r\x1b[K")
            self.stream.flush()
            self.drawn = False

    # -- punctuation -------------------------------------------------------
    def emit(self, text: str) -> None:
        """Print a line that must survive (a compiler error), keeping the line."""
        self.clear()
        self._line(text)

    def finish(self, summary: str, warnings: int, log_path) -> None:
        self.clear()
        note = f", {warnings} warning(s)" if warnings else ""
        where = f" — {log_path}" if log_path is not None else ""
        self._line(f"  [{self.tag}] {self.phase}: {summary} in "
                   f"{_hms(self._elapsed())}{note}{where}")

    def abandon(self, why: str) -> None:
        """End the line on a failure/interrupt: no summary, just why it stopped."""
        self.clear()
        self._line(f"  [{self.tag}] {self.phase}: {why} after "
                   f"{_hms(self._elapsed())}")
