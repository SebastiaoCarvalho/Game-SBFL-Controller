#!/usr/bin/env python
"""SECONDARY maintenance script — reconcile dataset branches with `base`.

The dataset (mutation) branches still carry the OLD binary `game_state_sender`
instrumentation, while gamerv needs the NEW JSON `event_log_server` protocol that
lives on `base`. This script brings each branch up to date by rebasing it onto
`base` (replaying the branch's mutation commits on top of base's new
instrumentation).

    python prepare_branches.py             # every branch of the (single) game
    python prepare_branches.py --game "Super Tux"   # a specific game's branches
    python prepare_branches.py --branch X  # just one branch
    python prepare_branches.py --dry-run   # report status only, no rebase

With a multi-game dataset, ``--game`` selects which game's repo/base to reconcile
against (defaults to the only registered game when there is exactly one).

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


def main(argv: list[str]) -> int:
    global GAME
    ap = argparse.ArgumentParser(description="Reconcile dataset branches with base")
    ap.add_argument("--game", default=None,
                    help="which game's repo/base to reconcile (default: the only "
                         "registered game)")
    ap.add_argument("--branch", help="only process this branch")
    ap.add_argument("--dry-run", action="store_true",
                    help="report status without rebasing")
    args = ap.parse_args(argv)

    if args.game is not None:
        GAME = config.get_game(args.game)
    elif len(config.GAMES) == 1:
        GAME = next(iter(config.GAMES.values()))
    else:
        print(f"ABORT: multiple games registered {sorted(config.GAMES)}; "
              "pass --game to pick one.", file=sys.stderr)
        return 2

    if not args.dry_run and not is_clean():
        print(f"ABORT: {GAME.name} working tree is dirty — commit or stash first.",
              file=sys.stderr)
        return 2

    if args.branch:
        branches = [args.branch]
    else:
        # Unique branches for this game, preserving dataset order.
        seen, branches = set(), []
        for c in read_cases():
            try:
                if config.get_game(c.game).name != GAME.name:
                    continue
            except config.UnknownGameError:
                continue
            if c.branch not in seen:
                seen.add(c.branch)
                branches.append(c.branch)

    start = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    outcomes: dict[str, str] = {}
    try:
        for b in branches:
            status = reconcile(b, args.dry_run)
            outcomes[b] = status
            print(f"  {b:<45} {status}")
    finally:
        git("checkout", start or GAME.base_branch)

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
