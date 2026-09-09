"""Step 2a — instrument the game's source so it reports its local variables.

Fault localization can only refine an invariant over variables it can *see*, and
a variable is only visible if the game's source reports it. gamerv's
``runners.source_instrument`` (branch ``local_vars``) closes that gap: it parses
the game's C++ with libclang, picks the function-locals worth reporting, and
splices the ``REPORT_LOCAL`` calls in. This module is the controller's side of
that tool — it runs it on the freshly checked-out branch **before** the compiler
runs, so the binary every trace is recorded from reports its locals.

The edits are **transient**. The source is instrumented, built, and immediately
restored (:func:`applied`); nothing but the compiler ever sees the instrumented
tree. That keeps the invariant the rest of the controller leans on — every game
repo is clean at every branch switch (``game_repo.ensure_clean``) — and it keeps
the instrumentation off the git branches the dataset names.

Because the edit changes the compiled binary while leaving the git tree hash
untouched, the build cache would otherwise go stale: ``game_repo.build`` folds
:func:`fingerprint` — a hash of the instrumenter, its spec and our flags — into
the cache key, so a change to any of them misses the cache and rebuilds rather
than silently restoring a binary built with the previous instrumentation. The
fingerprint, not :func:`plan`'s digest, is what keys the cache: the digest is
exact but only exists *after* a libclang parse of the whole game, and that parse
is most of what the cache exists to skip.

Step 2a is expensive — the tool parses every file the spec scans as a full
translation unit (SuperTux: 74 of them, ~26 s with gamerv's parses spread over
the cores, ~61 s before that), and those parses dominate everything else it does.
So the controller asks for exactly one such pass per build: it writes without
planning first (:func:`apply`), and it turns the tool's own libclang syntax check
off, because the real compile that follows subsumes it. See :func:`plan` and
:func:`_run` for what each of those used to cost.

Opt-in per game via ``Game.instrument_src`` (``None`` = the step is skipped for
that game). Two things must be true before a game can turn it on:

* gamerv must resolve an instrumentation spec for it — ``config/games/<name>/
  source_instrument.json``, or gamerv's built-in SuperTux default, whose
  ``scan_globs`` are SuperTux paths and mean nothing to another game;
* the game's source must already carry the reporting runtime (the
  ``REPORT_LOCAL`` macros and the ``GameObject`` plumbing behind them) on its
  ``base`` branch, or the instrumented tree will not compile. Which macros
  matters, not just that some exist: with ``"array"`` in the spec's
  ``policy.emit_verdicts`` the instrumenter writes ``REPORT_LOCAL_ARRAY`` for a
  container local, which SuperTux's ``base`` gained in its "add array variables"
  commit. That is a *per-branch* property here — every dataset branch carries its
  own copy of the reporting header — so the tool probes the checkout and skips the
  container forms where the macros are absent rather than failing the row; the
  branches that skip them are the ones ``prepare_branches.py`` has not yet rebased
  onto ``base``.

A failure here fails the row (an ``ERROR``) instead of quietly building an
uninstrumented binary, which would produce results that silently lack the local
variables the experiment is about. Turn the step off with
``run_experiment.py --no-instrument`` (or ``config.INSTRUMENT = False``).
"""

import hashlib
import json
import os
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import config
from config import Game


class InstrumentError(RuntimeError):
    """The source instrumenter failed, or left the game tree in a bad state."""


# runners.source_instrument's exit codes (cli.py): OK / ERR / USAGE / FOLLOWUP.
# FOLLOWUP means "written, and there is a suggested next step" — a success for us.
_OK_EXITS = (0, 3)


@dataclass(frozen=True)
class Plan:
    """What the instrumenter *would* write to this checkout, without writing it.

    ``digest`` is a content key over the exact insertions (file, offset, text), so
    two checkouts with the same tree hash and the same digest compile to the same
    binary — which is what lets the build cache key include it.
    """
    keys: tuple[str, ...]
    files: int
    insertions: int
    digest: str

    @property
    def empty(self) -> bool:
        return self.insertions == 0


def enabled(game: Game) -> bool:
    """True if this game opts into source instrumentation (``instrument_src``)."""
    return game.instrument_src is not None


def _spec_files(game: Game) -> list[Path]:
    """The spec files gamerv would resolve for this game, in its own order.

    Mirrors ``runners.source_instrument.spec``: the per-game spec wins, the legacy
    standalone file is the fallback, and the built-in SuperTux default (a literal
    in ``spec.py``) is covered by hashing the tool's source. Missing files are
    kept in the list — whether a file exists is itself part of the key.
    """
    cfg = config.GAMERV_DIR / "config"
    return [cfg / "games" / game.name / "source_instrument.json",
            cfg / "source_instrument.json"]


def fingerprint(game: Game) -> str:
    """A cheap content key for the instrumentation this checkout would receive.

    :func:`plan` answers the same question exactly, but costs a libclang parse of
    the whole game — the very thing the build cache exists to skip. So the cache
    key uses this instead: what the instrumenter writes is a function of the
    source (already keyed by the git tree hash), the tool's own code, the spec it
    resolves, the flags we pass it, and the compile database it parses with.
    Hashing those is a few file reads.

    The compile database earns its place: it is what libclang resolves types
    through, and a stale or partial one does not fail — it silently yields worse
    candidates. Measured on SuperTux `base`, the same tree instrumented 11 files
    with the database cmake had left behind and 68 files after a fresh configure
    regenerated it. It also catches a change to the game's ``build_commands``,
    which live in this repo and so move neither the tree hash nor anything else
    here. Note that ``build`` computes the key *before* the build regenerates the
    database, which is right — that is the database the instrumentation actually
    used — but it means a genuine database change costs one extra rebuild before
    the key settles.

    Not covered: the libclang build itself, which is pinned per machine
    (``config.CLANG_RESOURCE_DIR``). Upgrade it and clear ``build_cache/`` by hand
    if you want the cached binaries rebuilt against it.
    """
    h = hashlib.sha256()
    tool_dir = config.GAMERV_SRC_DIR / "runners" / "source_instrument"
    for path in sorted(tool_dir.glob("*.py")):
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes())
    for path in _spec_files(game):
        h.update(str(path).encode("utf-8"))
        h.update(path.read_bytes() if path.is_file() else b"<absent>")
    # Where every game's spec puts it (spec.py's clang.compile_db defaults to
    # "{build_dir}/compile_commands.json"); absent is itself a distinct state,
    # since the tool then falls back to synthesized flags.
    db = game.build_dir / "compile_commands.json"
    h.update(db.read_bytes() if db.is_file() else b"<no-compile-db>")
    h.update(repr((str(game.instrument_src), game.instrument_extra_args))
             .encode("utf-8"))
    return h.hexdigest()[:16]


def manifest_path(game: Game) -> Path:
    """Where the instrumenter records what it wrote, so ``--revert`` is byte-exact.

    Deliberately **outside** the game repo: the tool's own default is
    ``<game-root>/.gamerv/``, and an untracked directory there shows up in ``git
    status --porcelain``, which is exactly what ``game_repo.ensure_clean`` aborts
    the experiment over. One slot per game is enough — the manifest never
    outlives a single ``build()``.
    """
    return config.INSTRUMENT_DIR / f"{game.name}.manifest.json"


def _tail(text: str, lines: int = 25) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


def _run(game: Game, args: list[str], what: str) -> subprocess.CompletedProcess:
    """Invoke ``python -m runners.source_instrument`` in the activated gamerv env.

    Run from gamerv's ``src/`` because ``-m`` resolves the package against the
    working directory (gamerv's own worker is launched the same way, one level up).
    """
    module_dir = config.GAMERV_SRC_DIR / "runners" / "source_instrument"
    if not module_dir.is_dir():
        raise InstrumentError(
            f"gamerv has no source instrumenter at {module_dir} — check out the "
            f"branch that carries it (local_vars), or run with --no-instrument "
            f"to build without local-variable reporting")
    # Refuse to parse without libclang's builtin headers. A degraded parse does
    # not fail — it quietly yields fewer and worse candidates — so the only way
    # to keep the instrumentation reproducible is to make this a precondition.
    # Only for a game libclang actually parses: gamerv dispatches on its spec's
    # "language", and the JS backend (esprima) never opens a system header.
    if game.instrument_language == "cpp" and not (
            config.CLANG_RESOURCE_DIR / "include" / "stddef.h").is_file():
        raise InstrumentError(
            f"no libclang builtin headers at {config.CLANG_RESOURCE_DIR} — "
            f"libclang would fail on the first #include and silently drop "
            f"variables. See config.CLANG_RESOURCE_DIR for how to populate it")

    manifest_path(game).parent.mkdir(parents=True, exist_ok=True)
    cmd = config.gamerv_cmd([
        "-m", "runners.source_instrument",
        str(game.instrument_src),
        "--game", game.name,                     # resolve THIS game's spec
        "--build-dir", str(game.build_dir),      # for compile_commands.json
        "--manifest", str(manifest_path(game)),
        # The tool's default check re-parses every edited file twice with libclang
        # (once before the edit for baseline diagnostics, once after) — measured at
        # ~40 s for SuperTux. We compile the whole tree with the real compiler
        # seconds later, which is a strictly stronger check, so this one only buys
        # a better error message: a broken edit now surfaces as a failed build
        # rather than "Syntax check FAILED". The tree is safe either way — the
        # revert in `applied` runs whether the build passes or fails.
        #
        # NOT for a game with no compiler behind it. Asteroids' "build" only
        # stamps a launcher, so nothing downstream would ever look at the edited
        # source — a statement that does not parse would reach the browser, take
        # the whole file down with it, and surface as a game that silently logs
        # no events. There the tool's own re-parse is the only check there is,
        # and it is cheap (esprima, a whole game in a fraction of a second)
        # rather than the ~40 s libclang double-parse this exists to avoid.
        *(("--compile-check", "off") if game.instrument_language == "cpp" else ()),
        *game.instrument_extra_args,
        *args,
    ])
    proc = subprocess.run(cmd, cwd=str(config.GAMERV_SRC_DIR),
                          env={**os.environ,
                               "CLANG_RESOURCE_DIR": str(config.CLANG_RESOURCE_DIR)},
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=config.INSTRUMENT_TIMEOUT)
    if proc.returncode not in _OK_EXITS:
        raise InstrumentError(
            f"source instrumentation ({what}) failed with exit {proc.returncode}:\n"
            f"{_tail(proc.stdout)}\n{_tail(proc.stderr, 10)}")
    return proc


# What the tool prints, instead of a JSON plan, when every candidate it found is
# already reported in the source — an OK exit with nothing to emit (cli.main).
_NOTHING_TO_DO = "Nothing to do."


def _json_blob(text: str) -> dict | None:
    """The instrumenter's ``--format json`` object, pulled out of its log output.

    It is pretty-printed over many lines and sits between ordinary progress lines,
    so this scans for the first ``{`` that decodes into an object carrying
    ``insertions`` rather than reading a single line. ``None`` if there is none.
    """
    decoder = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = decoder.raw_decode(text, i)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and "insertions" in obj:
            return obj
        i = text.find("{", i + 1)
    return None


def plan(game: Game) -> Plan:
    """Derive the pending edit without touching the tree (the tool's dry run).

    **Not on the build path.** ``game_repo.build`` used to call this before every
    write, back when its ``digest`` keyed the build cache; the whole pass costs a
    libclang parse of every scanned file (measured: ~100 s for SuperTux's 74
    translation units), and the write pass repeats it, so running both doubled the
    cost of step 2a to produce a log line. :func:`fingerprint` keys the cache now
    and :func:`apply` writes directly.

    Kept because it is the only way to see what a checkout *would* get without
    editing it, and because its digest is the exact answer the fingerprint
    approximates — if you suspect the fingerprint is missing an input, compare two
    checkouts' digests against their fingerprints.
    """
    out = _run(game, ["--dry-run", "--format", "json"], "plan").stdout
    obj = _json_blob(out)
    if obj is None:
        # A tree whose candidates are all reported already emits no plan at all.
        # That is a legitimate empty edit, not a parse failure — but only when the
        # tool says so, so genuinely unreadable output still fails the row.
        if _NOTHING_TO_DO in out:
            obj = {}
        else:
            raise InstrumentError(
                f"could not parse the instrumenter's plan (no JSON object in its "
                f"output):\n{_tail(out)}")
    insertions = obj.get("insertions") or []
    keys = tuple(sorted(str(k) for k in (obj.get("keys") or [])))
    # Sorted + projected to the fields that decide the compiled bytes, so the
    # digest does not move with the tool's reporting order.
    canonical = json.dumps({
        "keys": list(keys),
        "insertions": sorted((str(i.get("rel")), int(i.get("offset", -1)),
                              str(i.get("text"))) for i in insertions),
    }, sort_keys=True)
    return Plan(
        keys=keys,
        files=len({str(i.get("rel")) for i in insertions}),
        insertions=len(insertions),
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
    )


def _git(game: Game, *args: str, raw: bool = False) -> str:
    """A git call in the game repo.

    This module deliberately does not import ``game_repo``: that module imports
    *this* one (``build`` instruments before compiling), and the cycle would be a
    genuine import-time one. Two git invocations are cheaper than the indirection.

    ``raw`` keeps the output byte-exact. The default strips it, which is right for
    a one-line answer and *wrong* for ``status --porcelain``: every line there
    begins with a two-character status field, and an unstaged modification's is
    " M", so stripping silently eats the first line's leading space and shifts its
    path by one character. That produced a real failure — a revert fell back to
    ``git checkout -- rc/items/bowling.cpp`` and aborted the row on a pathspec
    error. It stayed hidden for a long time because the manifest revert normally
    succeeds and the git fallback never runs.
    """
    proc = subprocess.run(["git", "-C", str(game.repo_dir), *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise InstrumentError(
            f"git {' '.join(args)} failed:\n{(proc.stderr or proc.stdout).strip()}")
    out = proc.stdout or ""
    return out if raw else out.strip()


def _tracked_dirty(game: Game) -> frozenset[str]:
    """Paths of *tracked* files that differ from HEAD (staged or not).

    Untracked files are deliberately not counted. The instrumenter only ever
    edits files in place — its manifest lives outside the repo
    (:func:`manifest_path`) exactly so it creates nothing untracked — so an
    untracked file is never its doing. That distinction is load-bearing rather
    than pedantic: Witch Blast builds in-source, so cmake's
    ``compile_commands.json`` sits in the repo root, and counting it as dirt made
    a perfectly good revert look failed. Whether untracked files are tolerable at
    all is ``game_repo.ensure_clean``'s call, not this module's.
    """
    out = _git(game, "status", "--porcelain", "--untracked-files=no", raw=True)
    paths = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        # "XY <path>" — a fixed two-character status field, then one space. Both
        # characters can legitimately BE a space (" M" is an unstaged
        # modification), so the path starts at a fixed offset and must not be
        # found by splitting on whitespace either.
        path = line[3:]
        if " -> " in path:              # a rename: the live path is the target
            path = path.split(" -> ", 1)[1]
        paths.add(path.strip().strip('"'))
    return frozenset(paths)


def _clear_stale(game: Game) -> None:
    """Drop a manifest a previous run left behind (crash between write and revert).

    The tree is clean at this point (``ensure_clean`` gates step 2), so the stale
    manifest's insertions are already gone and its revert is expected to fail —
    it is attempted only for the case where they are not, and the file is removed
    either way so this pass starts from an empty manifest.
    """
    path = manifest_path(game)
    if not path.exists():
        return
    print("  [instrument] stale manifest from an earlier run — clearing it")
    try:
        _run(game, ["--revert", "--write"], "stale revert")
    except InstrumentError:
        pass
    path.unlink(missing_ok=True)


#: The write pass's own summary line, which is where the counts come from now
#: that no dry run precedes it.
_WROTE = re.compile(r"Wrote (\d+) key\(s\) into (\d+) file\(s\)")


def apply(game: Game) -> None:
    """Write the reporting calls into the checked-out source.

    Goes straight to the write pass: it discovers what to insert as part of
    writing it, so asking :func:`plan` first only pays for that discovery twice.
    The counts come out of the tool's own summary afterwards instead of a plan
    beforehand — the same numbers, printed once the work is done.
    """
    _clear_stale(game)
    out = _run(game, ["--write"], "write").stdout
    wrote = _WROTE.search(out)
    if wrote:
        print(f"  [instrument] wrote {wrote.group(1)} key(s) into "
              f"{wrote.group(2)} file(s)")
    elif _NOTHING_TO_DO in out:
        print("  [instrument] nothing to add — source already reports its locals")
    else:
        # An OK exit that says neither. Not fatal — the tool succeeded — but the
        # whole point of the step is the keys, so do not let it pass silently.
        print(f"  [instrument] [warn] the write pass reported neither keys nor "
              f"'{_NOTHING_TO_DO}':\n{_tail(out, 5)}")


def revert(game: Game, baseline: frozenset[str] = frozenset()) -> None:
    """Undo the edit and leave the repo as it was, or raise.

    The manifest revert is byte-exact and is what normally restores the tree; the
    git fallback is the safety net, because a tree left dirty here is not a local
    problem — the next row's ``ensure_clean`` aborts the whole experiment. Both
    are no-ops when nothing was written, so this is safe to call unconditionally.

    ``baseline`` is the set of tracked files that already differed from HEAD
    *before* the instrumentation was applied — normally empty, since
    ``ensure_clean`` gates step 2. Anything in it is left strictly alone. The
    fallback used to be ``git checkout -- .``, which restores the whole tree: that
    is right only while "the tree was clean before" holds, and when it does not it
    silently deletes work the instrumenter never touched (observed: it discarded
    an uncommitted change in a game repo, twice). Checking out only the files that
    were NOT already modified keeps the safety net and removes the foot-gun.

    A file that was in ``baseline`` and that the instrumenter also edited is
    restored by the manifest revert alone — byte-exactly, back to its modified
    state, not to HEAD. If that revert fails, this raises rather than reaching for
    git, so the failure is loud and the work survives.
    """
    path = manifest_path(game)
    reverted = False
    if path.exists():
        try:
            _run(game, ["--revert", "--write"], "revert")
            reverted = True
        except InstrumentError as exc:
            print(f"  [instrument] [warn] revert failed, falling back to git: {exc}")
        path.unlink(missing_ok=True)

    stray = _tracked_dirty(game) - baseline
    if not stray:
        if reverted:
            print("  [instrument] reverted — tree is back to how it was")
        return

    print(f"  [instrument] [warn] {len(stray)} file(s) still modified after revert "
          f"— restoring them with git checkout")
    _git(game, "checkout", "--", *sorted(stray))
    stray = _tracked_dirty(game) - baseline
    if stray:
        raise InstrumentError(
            f"{game.name} tree is still dirty after reverting the instrumentation "
            f"— clean it by hand before rerunning:\n" + "\n".join(sorted(stray)))


@contextmanager
def applied(game: Game):
    """Instrument the source for the duration of the block, then restore it.

    The revert runs even when the body raises (a failed build) *and* when the
    write itself fails partway, so neither can leave the repo dirty for the next
    row — which would abort the whole experiment at the next ``ensure_clean``.

    What "as it was" means is pinned *here*, before anything is written: whatever
    already differed from HEAD is not this module's to restore (see
    :func:`revert`). In the pipeline that set is empty, because ``ensure_clean``
    has already run.
    """
    baseline = _tracked_dirty(game)
    try:
        apply(game)
        yield
    finally:
        revert(game, baseline)
