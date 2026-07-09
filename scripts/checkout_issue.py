#!/usr/bin/env python
"""Check out the SuperTux source to the state where a reported bug is reproducible.

Reads one issue from the dataset CSV produced by ``fetch_supertux_issues.py``
and checks out ``/home/sebastiao/supertux`` to the git state where the bug
supposedly happens. This is a first, simple version — just the checkout; building
and logging come later.

How the target ref is chosen (combined strategy):
  * FIXED issue -> the commit that fixed it, if one is discoverable (a ``closed``
    event's commit, or a cross-referenced merged PR). We check out its PARENT —
    the last commit that still has the bug (the fix commit itself no longer
    reproduces it). If no fix commit is found, fall through to the version field.
  * OPEN / CLOSED issue (or fixed without a discoverable fix) -> the issue body's
    ``### SuperTux Version`` template field, resolved to either an embedded commit
    hash (git-describe strings like ``v0.6.3-1606-g3af6b30f2``) or a release tag.

The local repo's ``origin`` is a fork, so the upstream ``SuperTux/supertux``
remote is added and fetched to resolve release tags and reported commits.

If no usable version can be resolved, the script reports it and does nothing
(never guesses a state). The checkout is a detached HEAD, so the instrumented
``base`` / mutation branches are left untouched; ``git checkout base`` restores.

    python checkout_issue.py --issue 3565            # checkout repro state for #3565
    python checkout_issue.py --row 1                 # ...for the 1st CSV data row
    python checkout_issue.py --issue 3565 --dry-run  # resolve + print, no checkout
    python checkout_issue.py --issue 3565 --token ghp_xxx
"""

import argparse
import csv
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = "SuperTux/supertux"
DEFAULT_CSV = Path(__file__).resolve().parent / "supertux_ubuntu_bugs.csv"
DEFAULT_SUPERTUX_DIR = Path("/home/sebastiao/supertux")
DEFAULT_UPSTREAM_URL = "https://github.com/SuperTux/supertux.git"
BASE_BRANCH = "base"

# Extract the value of the "### SuperTux Version" template field from an issue body.
_VERSION_FIELD = re.compile(r"###\s*SuperTux Version\s*\n+(.+?)(?:\n|$)", re.IGNORECASE)
# git-describe hash suffix, e.g. "...-g3af6b30f2"; also a standalone 7-40 hex token.
_DESCRIBE_HASH = re.compile(r"[-\s]g([0-9a-f]{7,40})\b", re.IGNORECASE)
_BARE_HASH = re.compile(r"\b([0-9a-f]{7,40})\b", re.IGNORECASE)
# A version number, with optional beta/rc suffix: 0.6.4, 0.7.0 beta 1, 0.7.0-beta.1
_VERSION_NUM = re.compile(
    r"(\d+\.\d+(?:\.\d+)?)(?:[\s.\-]*(beta|rc)[\s.\-]*(\d+))?", re.IGNORECASE
)


class CheckoutError(RuntimeError):
    pass


# --------------------------------------------------------------------------- git


def _git(supertux_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(supertux_dir), *args],
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise CheckoutError(
            f"git {' '.join(args)} failed:\n{(proc.stderr or proc.stdout).strip()}"
        )
    return proc


def ensure_clean(supertux_dir: Path) -> None:
    """Abort if the working tree has uncommitted changes (mirrors supertux.py)."""
    if _git(supertux_dir, "status", "--porcelain").stdout.strip():
        raise CheckoutError(
            f"SuperTux working tree ({supertux_dir}) is dirty — commit or stash "
            "before checking out a reproduction state."
        )


def ensure_upstream(supertux_dir: Path, remote: str, url: str) -> None:
    """Add the upstream remote if absent, then fetch it (with tags)."""
    remotes = _git(supertux_dir, "remote").stdout.split()
    if remote not in remotes:
        print(f"  [git] adding remote {remote} -> {url}")
        _git(supertux_dir, "remote", "add", remote, url)
    print(f"  [git] fetching {remote} (tags + commits) ...")
    _git(supertux_dir, "fetch", "--tags", remote)


def rev_parse(supertux_dir: Path, ref: str) -> str | None:
    """Return the full commit sha ``ref`` resolves to, or None if it doesn't."""
    proc = _git(supertux_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                check=False)
    sha = proc.stdout.strip()
    return sha or None


# ---------------------------------------------------------------------- GitHub


def _headers(token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(url: str, token: str | None, params: dict | None = None):
    resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
    if resp.status_code != 200:
        if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = resp.headers.get("X-RateLimit-Reset")
            when = ""
            if reset:
                ts = datetime.fromtimestamp(int(reset), tz=timezone.utc).astimezone()
                when = f" (resets at {ts:%Y-%m-%d %H:%M:%S %Z})"
            sys.exit(f"GitHub rate limit exceeded{when}. Pass --token to raise it.")
        try:
            msg = resp.json().get("message", resp.text)
        except ValueError:
            msg = resp.text
        sys.exit(f"GitHub API error {resp.status_code} for {url}: {msg}")
    return resp.json()


def fetch_issue(number: int, token: str | None) -> dict:
    return _get(f"https://api.github.com/repos/{REPO}/issues/{number}", token)


def find_fix_commit(number: int, token: str | None) -> str | None:
    """Discover the commit that fixed a fixed issue, or None.

    Looks at the issue timeline for a ``closed`` event carrying a commit, else a
    cross-referenced merged pull request's merge commit.
    """
    events = _get(f"https://api.github.com/repos/{REPO}/issues/{number}/timeline",
                  token, params={"per_page": 100})
    if not isinstance(events, list):
        return None
    for e in events:
        if e.get("event") == "closed" and e.get("commit_id"):
            return e["commit_id"]
    for e in events:
        if e.get("event") == "cross-referenced":
            iss = e.get("source", {}).get("issue", {})
            if "pull_request" in iss and iss.get("number"):
                pr = _get(f"https://api.github.com/repos/{REPO}/pulls/{iss['number']}",
                          token)
                if pr.get("merged") and pr.get("merge_commit_sha"):
                    return pr["merge_commit_sha"]
    return None


# ---------------------------------------------------------------- resolution


def parse_version_field(body: str) -> str | None:
    m = _VERSION_FIELD.search(body or "")
    return m.group(1).strip() if m else None


def _tag_candidates(version_text: str) -> list[str]:
    """Ordered git-tag candidates for a reported version string."""
    m = _VERSION_NUM.search(version_text)
    if not m:
        return []
    num, pre, prenum = m.group(1), m.group(2), m.group(3)
    cands = []
    if pre and prenum:  # beta/rc: try the specific pre-release tag first
        suffix = f"-{pre.lower()}.{prenum}"
        cands += [f"v{num}{suffix}", f"{num}{suffix}"]
    cands += [f"v{num}", f"{num}"]
    return cands


def resolve_from_version(supertux_dir: Path, version_text: str) -> tuple[str, str] | None:
    """Resolve a reported version string to (ref, how). Prefers an embedded commit.

    Returns None if nothing resolves against the (already fetched) repo.
    """
    # 1) Embedded commit hash — most precise. Prefer the git-describe "g<hash>".
    for pat in (_DESCRIBE_HASH, _BARE_HASH):
        for h in pat.findall(version_text):
            if rev_parse(supertux_dir, h):
                return h, f"commit {h} (from version {version_text!r})"
    # 2) Release tag.
    for tag in _tag_candidates(version_text):
        if rev_parse(supertux_dir, f"refs/tags/{tag}"):
            return f"refs/tags/{tag}", f"tag {tag} (from version {version_text!r})"
    return None


def resolve_target(supertux_dir: Path, issue: dict, state: str,
                   token: str | None) -> tuple[str, str]:
    """Return (ref, how) for the reproduction checkout, or raise CheckoutError."""
    number = issue["number"]
    if state == "fixed":
        fix = find_fix_commit(number, token)
        if fix:
            parent = rev_parse(supertux_dir, f"{fix}^")
            if parent:
                return parent, f"parent of fix commit {fix[:9]} (last buggy commit)"
            # Fix commit not in local history even after fetch — fall through.
            print(f"  [warn] fix commit {fix[:9]} not found locally; "
                  "falling back to reported version")
        else:
            print("  [info] no fix commit discoverable; using reported version")

    version_text = parse_version_field(issue.get("body") or "")
    if not version_text:
        raise CheckoutError("no '### SuperTux Version' field in the issue body")
    resolved = resolve_from_version(supertux_dir, version_text)
    if not resolved:
        raise CheckoutError(
            f"could not resolve reported version {version_text!r} to a tag or commit"
        )
    return resolved


def derive_state(issue: dict) -> str:
    if issue.get("state") == "open":
        return "open"
    return "fixed" if issue.get("state_reason") == "completed" else "closed"


# --------------------------------------------------------------------- CSV


def issue_number_for_row(csv_path: Path, row: int) -> int:
    """The issue number in column 0 of the ``row``-th data line (1-based)."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for i, cells in enumerate(reader, start=1):
            if i == row:
                if not cells or not cells[0].strip().isdigit():
                    raise CheckoutError(f"CSV row {row} has no issue number: {cells}")
                return int(cells[0].strip())
    raise CheckoutError(f"CSV has fewer than {row} data rows")


def csv_numbers(csv_path: Path) -> set[int]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        return {int(c[0].strip()) for c in reader if c and c[0].strip().isdigit()}


# --------------------------------------------------------------------- main


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sel = p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--issue", type=int, help="GitHub issue number")
    sel.add_argument("--row", type=int, help="1-based data row in the CSV")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                   help=f"dataset CSV (default {DEFAULT_CSV})")
    p.add_argument("--supertux-dir", type=Path, default=DEFAULT_SUPERTUX_DIR,
                   help=f"SuperTux git repo (default {DEFAULT_SUPERTUX_DIR})")
    p.add_argument("--upstream-remote", default="upstream",
                   help="name for the upstream SuperTux/supertux remote (default upstream)")
    p.add_argument("--upstream-url", default=DEFAULT_UPSTREAM_URL)
    p.add_argument("--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
                   help="GitHub token (default $GITHUB_TOKEN / $GH_TOKEN)")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve and print the target without checking out")
    args = p.parse_args(argv)

    supertux_dir = args.supertux_dir
    if not (supertux_dir / ".git").exists():
        sys.exit(f"{supertux_dir} is not a git repository (pass --supertux-dir)")

    # Select the issue.
    if args.issue is not None:
        number = args.issue
        if args.csv.exists() and number not in csv_numbers(args.csv):
            print(f"  [warn] issue #{number} is not in {args.csv}")
    else:
        number = issue_number_for_row(args.csv, args.row)

    issue = fetch_issue(number, args.token)
    state = derive_state(issue)
    print(f"Issue #{number} [{state}] {issue.get('title','')}")
    print(f"  {issue.get('html_url','')}")

    # Bring upstream refs in so tags/commits resolve, then resolve the target.
    if not args.dry_run:
        ensure_clean(supertux_dir)
    ensure_upstream(supertux_dir, args.upstream_remote, args.upstream_url)

    try:
        ref, how = resolve_target(supertux_dir, issue, state, args.token)
    except CheckoutError as e:
        print(f"UNRESOLVABLE: {e} — nothing checked out.")
        return 1

    sha = rev_parse(supertux_dir, ref) or ref
    print(f"  target: {how}")
    print(f"  ref: {ref}  ->  {sha[:12]}")

    if args.dry_run:
        print("  (dry run — not checking out)")
        return 0

    _git(supertux_dir, "checkout", "--detach", ref)
    print(f"  [git] checked out detached HEAD at {sha[:12]}")
    print(f"  restore with: git -C {supertux_dir} checkout {BASE_BRANCH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
