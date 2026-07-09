#!/usr/bin/env python
"""Build a dataset CSV of SuperTux bug issues that target Ubuntu.

Queries the GitHub Issues Search API for bug-labeled issues in the SuperTux
repository whose text mentions Ubuntu, and writes one CSV row per issue.

SuperTux has no "Ubuntu" label (`os:ubuntu-touch` is the mobile OS; `os:linux`
is desktop Linux generally), so "targets Ubuntu" is a free-text match on the
issue title/body/comments. Because that catches every Ubuntu version, each row
also carries an `ubuntu_24` flag (regex over title+body) so Ubuntu 24 / 24.04
issues can be preferred — flagged rows sort to the top.

The default bug scope is any `type:*bug*` label: `type:bug`, `type:bugfix`,
`type:crash` (OR-matched); issues labeled `notourbug` are excluded. Use
`--require-labels` for labels that must ALL be present (AND-matched), e.g.
`category:code`.

The platform is the free-text `--keyword` (default `ubuntu`); pass
`--any-platform` to drop it and search bugs on every platform.

The `state` column has three values, derived from GitHub's `state_reason`:
`open`, `fixed` (closed as *completed* — a real fix landed), and `closed`
(closed as *not planned* / *duplicate* — not solved or wrongly reported).

Output columns: number, name (issue title), state (open/fixed/closed), url,
labels, ubuntu_24.

    python fetch_supertux_issues.py                       # 50-ish rows -> scripts/supertux_ubuntu_bugs.csv
    python fetch_supertux_issues.py --state open          # only open issues
    python fetch_supertux_issues.py --state fixed         # only issues that got a fix
    python fetch_supertux_issues.py --keyword "ubuntu 24" # narrow to the 24-specific set
    python fetch_supertux_issues.py --require-labels category:code --any-platform \
        --output scripts/supertux_code_bugs.csv           # code bugs, any platform
    python fetch_supertux_issues.py --token ghp_xxx       # authenticated (higher rate limit)

Runs unauthenticated by default; pass --token or set GITHUB_TOKEN / GH_TOKEN to
raise the search rate limit (10 -> 30 requests/min).
"""

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SEARCH_URL = "https://api.github.com/search/issues"
DEFAULT_REPO = "SuperTux/supertux"
DEFAULT_LABELS = "type:bug,type:bugfix,type:crash"
EXCLUDE_LABELS = ["notourbug"]
DEFAULT_KEYWORD = "ubuntu"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "supertux_ubuntu_bugs.csv"

# Ubuntu 24 / 24.04 / 24.10 mention, e.g. "Ubuntu 24.04", "ubuntu24", "24.10".
_UBUNTU_24 = re.compile(r"ubuntu\s*24|24\.(04|10)", re.IGNORECASE)

FIELDNAMES = ["number", "name", "state", "url", "labels", "ubuntu_24"]


def build_query(repo: str, labels: str, require_labels: list[str],
                keyword: str, state: str) -> str:
    """Assemble the GitHub search `q` string.

    The bug-scope ``labels`` become the OR form ``label:"a","b"`` (a comma inside
    a single ``label:`` qualifier means OR on GitHub's search syntax). Each
    ``require_labels`` entry is emitted as its OWN ``label:`` qualifier, which
    GitHub AND-matches — so ``category:code`` narrows to bugs that also carry that
    label. ``fixed`` and ``closed`` are both GitHub-closed issues (told apart
    later by :func:`derive_state`), so both map to the ``state:closed`` qualifier.
    """
    parts = [f"repo:{repo}", "is:issue"]
    label_list = [l.strip() for l in labels.split(",") if l.strip()]
    if label_list:
        parts.append("label:" + ",".join(f'"{l}"' for l in label_list))
    parts += [f'label:"{l}"' for l in require_labels]  # AND-matched
    parts += [f'-label:"{l}"' for l in EXCLUDE_LABELS]
    gh_state = {"open": "open", "closed": "closed", "fixed": "closed"}.get(state)
    if gh_state:
        parts.append(f"state:{gh_state}")
    if keyword.strip():
        parts.append(keyword.strip())
    return " ".join(parts)


def derive_state(item: dict) -> str:
    """Map a GitHub issue to one of ``open`` / ``fixed`` / ``closed``.

    GitHub itself only has open/closed; the ``state_reason`` field distinguishes
    a real resolution (``completed`` -> ``fixed``) from a close without a fix
    (``not_planned``, ``duplicate``, or anything else -> ``closed``).
    """
    if item.get("state") == "open":
        return "open"
    return "fixed" if item.get("state_reason") == "completed" else "closed"


def _headers(token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch_issues(query: str, token: str | None) -> list[dict]:
    """Fetch every issue matching `query`, following pagination.

    GitHub's search API caps results at 1000; SuperTux's bug set is far smaller,
    so a plain page loop suffices.
    """
    headers = _headers(token)
    items: list[dict] = []
    page = 1
    total = None
    while True:
        params = {"q": query, "per_page": 100, "page": page}
        resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            _fail_on_error(resp)
        data = resp.json()
        if total is None:
            total = data.get("total_count", 0)
        batch = data.get("items", [])
        if not batch:
            break
        items.extend(batch)
        if len(items) >= total or len(batch) < 100:
            break
        page += 1
    return items


def _fail_on_error(resp: requests.Response) -> None:
    """Print a helpful message for a non-200 response and exit non-zero."""
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        reset = resp.headers.get("X-RateLimit-Reset")
        when = ""
        if reset:
            ts = datetime.fromtimestamp(int(reset), tz=timezone.utc).astimezone()
            when = f" (resets at {ts:%Y-%m-%d %H:%M:%S %Z})"
        sys.exit(f"GitHub rate limit exceeded{when}. "
                 f"Pass --token or set GITHUB_TOKEN to raise the limit.")
    try:
        msg = resp.json().get("message", resp.text)
    except ValueError:
        msg = resp.text
    sys.exit(f"GitHub API error {resp.status_code}: {msg}")


def to_row(item: dict) -> dict:
    """Turn a search API item into a CSV row dict."""
    title = item.get("title") or ""
    body = item.get("body") or ""
    labels = ";".join(l.get("name", "") for l in item.get("labels", []))
    return {
        "number": item.get("number"),
        "name": title,
        "state": derive_state(item),
        "url": item.get("html_url"),
        "labels": labels,
        "ubuntu_24": bool(_UBUNTU_24.search(f"{title}\n{body}")),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name (default {DEFAULT_REPO})")
    p.add_argument("--labels", default=DEFAULT_LABELS,
                   help=f"bug-scope labels, OR-matched (default {DEFAULT_LABELS})")
    p.add_argument("--require-labels", default="",
                   help="extra labels that must ALL be present, AND-matched, "
                        "comma-separated (e.g. category:code)")
    p.add_argument("--keyword", default=DEFAULT_KEYWORD,
                   help=f'free-text/platform search term (default "{DEFAULT_KEYWORD}")')
    p.add_argument("--any-platform", action="store_true",
                   help="search all platforms: ignore --keyword (drop the ubuntu/text filter)")
    p.add_argument("--state", default="all", choices=["all", "open", "fixed", "closed"],
                   help="issue state filter: fixed=closed as completed, "
                        "closed=closed as not planned/duplicate (default all)")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                   help=f"output CSV path (default {DEFAULT_OUTPUT})")
    p.add_argument("--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
                   help="GitHub token (default $GITHUB_TOKEN / $GH_TOKEN)")
    args = p.parse_args(argv)

    require_labels = [l.strip() for l in args.require_labels.split(",") if l.strip()]
    keyword = "" if args.any_platform else args.keyword
    query = build_query(args.repo, args.labels, require_labels, keyword, args.state)
    print(f"Searching: {query}")
    items = fetch_issues(query, args.token)

    # Drop pull requests (is:issue already excludes them; belt and suspenders).
    rows = [to_row(it) for it in items if "pull_request" not in it]
    # fixed/closed share one GitHub query (both state:closed); split them here.
    if args.state in ("fixed", "closed"):
        rows = [r for r in rows if r["state"] == args.state]
    # Prefer Ubuntu-24 issues: flagged first, then by issue number.
    rows.sort(key=lambda r: (not r["ubuntu_24"], r["number"] or 0))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    flagged = sum(1 for r in rows if r["ubuntu_24"])
    breakdown = ", ".join(f"{s}={sum(r['state'] == s for r in rows)}"
                          for s in ("open", "fixed", "closed"))
    print(f"Wrote {len(rows)} issue(s) [{breakdown}] "
          f"({flagged} flagged for Ubuntu 24) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
