#!/usr/bin/env python
"""SECONDARY maintenance script — reconcile dataset branches with `base`.

The dataset (mutation) branches still carry the OLD binary `game_state_sender`
instrumentation, while gamerv needs the NEW JSON `event_log_server` protocol that
lives on `base`. This script brings each branch up to date by rebasing it onto
`base` (replaying the branch's mutation commits on top of base's new
instrumentation).

    python prepare_branches.py             # every branch of every game
    python prepare_branches.py --game "Super Tux"   # a specific game's branches
    python prepare_branches.py --branch X  # just one branch
    python prepare_branches.py --dry-run   # report status only, no rebase

With a multi-game dataset, ``--game`` selects a single game's repo/base to
reconcile against; with no ``--game`` every registered game is processed in turn,
each against its own repo/base. Each game's working tree is checked (and, for a
real run, its HEAD restored) independently, so a dirty tree in one game only skips
that game — the others still run.

Because the instrumentation diverged hard (each branch deletes `event_log_server`
and adds `game_state_sender`), CONFLICTS ARE EXPECTED on most branches. On a
conflict the rebase is aborted and the branch is reported as NEEDS-MANUAL — this
script never force-resolves. Rebasing rewrites history relative to origin, so
pushing afterwards (`git push --force-with-lease`) is left to you.

NOTE on direction: this implements "bring base's new instrumentation into each
mutation branch" as `git checkout <branch> && git rebase base`. If you intended
the literal opposite, change REBASE_ONTO below.
"""

import argparse
import subprocess
import sys
from pathlib import Path

# This script lives in scripts/; the pipeline modules it reuses live in src/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config
from dataset import read_cases

# The game whose repo/base we reconcile against — set in main() from --game.
GAME: config.Game | None = None


def _game() -> config.Game:
    assert GAME is not None, "GAME must be set before use"
    return GAME


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(_game().repo_dir), *args],
        capture_output=True, text=True,
    )


def _ok(cp: subprocess.CompletedProcess) -> bool:
    return cp.returncode == 0


def is_clean() -> bool:
    return not git("status", "--porcelain").stdout.strip()


def branch_exists_local(branch: str) -> bool:
    return _ok(git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"))


def branch_exists_remote(branch: str) -> bool:
    return _ok(git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"))


def is_up_to_date(ref: str) -> bool:
    """True if base is already an ancestor of ref (so ref has base's hook + instr)."""
    return _ok(git("merge-base", "--is-ancestor", _game().base_branch, ref))


def ensure_local(branch: str) -> bool:
    """Make sure a local branch exists, creating it from origin if needed."""
    if branch_exists_local(branch):
        return True
    if branch_exists_remote(branch):
        return _ok(git("branch", "--track", branch, f"origin/{branch}"))
    return False


def reconcile(branch: str, dry_run: bool) -> str:
    """Return one of: ALREADY-CURRENT | MISSING | UPDATED | NEEDS-MANUAL | WOULD-REBASE."""
    # Pick a ref to inspect without mutating: local branch if it exists, else
    # the remote-tracking ref (so --dry-run never creates a local branch).
    if branch_exists_local(branch):
        ref = branch
    elif branch_exists_remote(branch):
        ref = f"origin/{branch}"
    else:
        return "MISSING"

    if is_up_to_date(ref):
        return "ALREADY-CURRENT"
    if dry_run:
        return "WOULD-REBASE"

    if not ensure_local(branch):  # materialize a local branch for the rebase
        return "MISSING"
    if not _ok(git("checkout", branch)):
        return "NEEDS-MANUAL"
    rebase = git("rebase", _game().base_branch)
    if _ok(rebase):
        return "UPDATED"
    # Conflict (or other failure) — back out cleanly, leave for manual handling.
    git("rebase", "--abort")
    return "NEEDS-MANUAL"


def dataset_branches(game: config.Game) -> list[str]:
    """Unique dataset branches belonging to ``game``, in dataset order."""
    seen, branches = set(), []
    for c in read_cases():
        try:
            if config.get_game(c.game).name != game.name:
                continue
        except config.UnknownGameError:
            continue
        if c.branch not in seen:
            seen.add(c.branch)
            branches.append(c.branch)
    return branches


def process_game(game: config.Game, branch: str | None, dry_run: bool) -> dict[str, str]:
    """Reconcile one game's branches against its base, returning per-branch outcomes.

    Sets the module-global ``GAME`` (git() / reconcile() operate on it) for the
    duration, checks the game's own working tree, and restores its starting HEAD.
    A dirty tree skips the game (empty outcomes) rather than raising, so a
    multi-game run keeps going.
    """
    global GAME
    GAME = game

    print(f"\n=== {game.name}  ({game.repo_dir}) ===")
    if not dry_run and not is_clean():
        print(f"  SKIP: working tree is dirty — commit or stash first.",
              file=sys.stderr)
        return {}

    branches = [branch] if branch else dataset_branches(game)
    if not branches:
        print("  (no branches to process)")
        return {}

    start = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    outcomes: dict[str, str] = {}
    try:
        for b in branches:
            status = reconcile(b, dry_run)
            outcomes[b] = status
            print(f"  {b:<45} {status}")
    finally:
        git("checkout", start or game.base_branch)
    return outcomes


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Reconcile dataset branches with base")
    ap.add_argument("--game", default=None,
                    help="which game's repo/base to reconcile (default: every "
                         "registered game)")
    ap.add_argument("--branch", help="only process this branch")
    ap.add_argument("--dry-run", action="store_true",
                    help="report status without rebasing")
    args = ap.parse_args(argv)

    if args.game is not None:
        games = [config.get_game(args.game)]
    else:
        games = list(config.GAMES.values())

    # A bare --branch with several games can't tell which repo it lives in; route
    # it to the games whose dataset actually lists it (skip the rest silently).
    if args.branch and args.game is None and len(games) > 1:
        games = [g for g in games if args.branch in dataset_branches(g)]
        if not games:
            print(f"ABORT: no registered game has a dataset branch {args.branch!r}; "
                  "pass --game to target a repo directly.", file=sys.stderr)
            return 2

    # Per-game outcomes, keyed "<game>/<branch>" so same-named branches don't clash.
    outcomes: dict[str, str] = {}
    for game in games:
        for b, status in process_game(game, args.branch, args.dry_run).items():
            outcomes[f"{game.name}/{b}"] = status

    print("\nSummary:")
    counts: dict[str, int] = {}
    for s in outcomes.values():
        counts[s] = counts.get(s, 0) + 1
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    needs = [b for b, s in outcomes.items() if s in ("NEEDS-MANUAL", "MISSING")]
    if needs:
        print("\nBranches needing attention:")
        for b in needs:
            print(f"  - {b}  ({outcomes[b]})")
    return 0 if not needs else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
