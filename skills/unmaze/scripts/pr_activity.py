#!/usr/bin/env python3
"""Snapshot a GitHub pull request's review state, checks and comments for the board.

The board shows this snapshot when a viewer has no live GitHub connection, and as the
starting point before live data arrives. Everything here is best-effort: any failure
returns ``None`` and the board simply says it has no GitHub snapshot.

Usage:
    python3 pr_activity.py --pr https://github.com/acme/payments/pull/142 --out pr-activity.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ACTIVITY_SCHEMA_VERSION = 1
MAX_BODY_CHARS = 4000
MAX_THREADS = 200
MAX_CONVERSATION = 150
MAX_CHECKS = 150

PR_URL_RE = re.compile(r"^https?://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")

GRAPHQL_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      url
      state
      isDraft
      reviewDecision
      mergeable
      headRefOid
      latestReviews(first: 50) {
        nodes { author { login } state submittedAt url }
      }
      reviewRequests(first: 50) {
        nodes {
          requestedReviewer {
            __typename
            ... on User { login }
            ... on Team { name slug }
            ... on Bot { login }
            ... on Mannequin { login }
          }
        }
      }
      reviews(last: 100) {
        nodes { author { login } state body submittedAt url }
      }
      comments(last: 100) {
        nodes { author { login } body createdAt url }
      }
      reviewThreads(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id
          path
          line
          startLine
          originalLine
          diffSide
          isResolved
          isOutdated
          subjectType
          comments(first: 50) {
            nodes { author { login } body createdAt url }
          }
        }
      }
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup {
              state
              contexts(first: 100) {
                pageInfo { hasNextPage }
                nodes {
                  __typename
                  ... on CheckRun {
                    name
                    status
                    conclusion
                    detailsUrl
                    checkSuite { workflowRun { workflow { name } } }
                  }
                  ... on StatusContext { context state targetUrl }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

CHECK_RUN_DONE_OK = {"SUCCESS"}
CHECK_RUN_DONE_SKIP = {"SKIPPED", "NEUTRAL", "STALE"}
CHECK_RUN_DONE_FAIL = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
STATUS_CONTEXT_MAP = {"SUCCESS": "passed", "FAILURE": "failed", "ERROR": "failed", "PENDING": "pending", "EXPECTED": "pending"}

VALID_REVIEW_DECISIONS = {"APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}
VALID_PR_STATES = {"OPEN", "CLOSED", "MERGED"}
VALID_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"}


def parse_pr_url(url: str | None) -> dict[str, Any] | None:
    """Split a GitHub pull request URL into host, owner, repo and number."""
    m = PR_URL_RE.match(str(url or "").strip())
    if not m:
        return None
    return {
        "host": m.group("host"),
        "owner": m.group("owner"),
        "repo": m.group("repo"),
        "number": int(m.group("number")),
        "url": f"https://{m.group('host')}/{m.group('owner')}/{m.group('repo')}/pull/{m.group('number')}",
    }


def _login(node: Any) -> str:
    author = (node or {}).get("author") if isinstance(node, dict) else None
    login = (author or {}).get("login") if isinstance(author, dict) else None
    return str(login) if login else "ghost"


def _clip(text: Any, limit: int = MAX_BODY_CHARS) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _https(url: Any) -> str | None:
    s = str(url or "").strip()
    return s if s.startswith("https://") else None


def classify_check(node: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one GraphQL check-run or commit-status node into {name, state, url, group}."""
    kind = node.get("__typename")
    if kind == "CheckRun":
        status = str(node.get("status") or "").upper()
        conclusion = str(node.get("conclusion") or "").upper()
        if status != "COMPLETED":
            state = "pending"
        elif conclusion in CHECK_RUN_DONE_OK:
            state = "passed"
        elif conclusion in CHECK_RUN_DONE_SKIP:
            state = "skipped"
        elif conclusion in CHECK_RUN_DONE_FAIL:
            state = "failed"
        else:
            state = "pending"
        suite = node.get("checkSuite") or {}
        run = suite.get("workflowRun") or {}
        workflow = (run.get("workflow") or {}).get("name") or ""
        return {
            "name": str(node.get("name") or "check"),
            "state": state,
            "url": _https(node.get("detailsUrl")),
            "group": str(workflow),
        }
    if kind == "StatusContext":
        state = STATUS_CONTEXT_MAP.get(str(node.get("state") or "").upper(), "pending")
        return {
            "name": str(node.get("context") or "status"),
            "state": state,
            "url": _https(node.get("targetUrl")),
            "group": "",
        }
    return None


def summarize_checks(items: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"passed": 0, "failed": 0, "pending": 0, "skipped": 0}
    for item in items:
        summary[item["state"]] = summary.get(item["state"], 0) + 1
    summary["total"] = len(items)
    return summary


def normalize_graphql(data: dict[str, Any], *, fetched_at: str | None = None) -> dict[str, Any] | None:
    """Reshape the GraphQL answer into the small, stable snapshot the board reads."""
    pr = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest")
    if not isinstance(pr, dict):
        return None

    reviews: list[dict[str, Any]] = []
    for node in ((pr.get("latestReviews") or {}).get("nodes") or []):
        state = str((node or {}).get("state") or "").upper()
        if state not in VALID_REVIEW_STATES or state == "PENDING":
            continue
        reviews.append(
            {
                "author": _login(node),
                "state": state,
                "submitted_at": node.get("submittedAt"),
                "url": _https(node.get("url")),
            }
        )

    requested: list[str] = []
    for node in ((pr.get("reviewRequests") or {}).get("nodes") or []):
        reviewer = (node or {}).get("requestedReviewer") or {}
        if reviewer.get("__typename") == "Team":
            name = reviewer.get("slug") or reviewer.get("name")
            if name:
                requested.append(f"team:{name}")
        elif reviewer.get("login"):
            requested.append(str(reviewer["login"]))

    threads: list[dict[str, Any]] = []
    for node in ((pr.get("reviewThreads") or {}).get("nodes") or [])[:MAX_THREADS]:
        comments = [
            {
                "author": _login(c),
                "body": _clip(c.get("body")),
                "created_at": c.get("createdAt"),
                "url": _https(c.get("url")),
            }
            for c in (((node or {}).get("comments") or {}).get("nodes") or [])
            if isinstance(c, dict)
        ]
        if not comments:
            continue
        side = str(node.get("diffSide") or "RIGHT").upper()
        threads.append(
            {
                "id": str(node.get("id") or ""),
                "path": str(node.get("path") or ""),
                "line": node.get("line") if isinstance(node.get("line"), int) else None,
                "start_line": node.get("startLine") if isinstance(node.get("startLine"), int) else None,
                "original_line": node.get("originalLine") if isinstance(node.get("originalLine"), int) else None,
                "side": "LEFT" if side == "LEFT" else "RIGHT",
                "is_resolved": bool(node.get("isResolved")),
                "is_outdated": bool(node.get("isOutdated")),
                "is_file": str(node.get("subjectType") or "").upper() == "FILE",
                "comments": comments,
            }
        )

    conversation: list[dict[str, Any]] = []
    for node in ((pr.get("comments") or {}).get("nodes") or []):
        if not isinstance(node, dict):
            continue
        conversation.append(
            {
                "kind": "comment",
                "author": _login(node),
                "state": None,
                "body": _clip(node.get("body")),
                "created_at": node.get("createdAt"),
                "url": _https(node.get("url")),
            }
        )
    for node in ((pr.get("reviews") or {}).get("nodes") or []):
        if not isinstance(node, dict):
            continue
        state = str(node.get("state") or "").upper()
        body = str(node.get("body") or "").strip()
        # A plain "commented" review with no summary text is just the wrapper of line comments.
        if state == "PENDING" or (state == "COMMENTED" and not body):
            continue
        conversation.append(
            {
                "kind": "review",
                "author": _login(node),
                "state": state if state in VALID_REVIEW_STATES else None,
                "body": _clip(body),
                "created_at": node.get("submittedAt"),
                "url": _https(node.get("url")),
            }
        )
    conversation.sort(key=lambda c: str(c.get("created_at") or ""))
    conversation = conversation[-MAX_CONVERSATION:]

    check_items: list[dict[str, Any]] = []
    checks_truncated = False
    head_sha = pr.get("headRefOid")
    commit_nodes = ((pr.get("commits") or {}).get("nodes") or [])
    if commit_nodes:
        commit = (commit_nodes[-1] or {}).get("commit") or {}
        head_sha = commit.get("oid") or head_sha
        rollup = commit.get("statusCheckRollup") or {}
        contexts = rollup.get("contexts") or {}
        checks_truncated = bool((contexts.get("pageInfo") or {}).get("hasNextPage"))
        for node in (contexts.get("nodes") or [])[:MAX_CHECKS]:
            item = classify_check(node or {})
            if item:
                check_items.append(item)
    order = {"failed": 0, "pending": 1, "passed": 2, "skipped": 3}
    check_items.sort(key=lambda c: (order.get(c["state"], 9), c["group"], c["name"]))

    decision = str(pr.get("reviewDecision") or "").upper()
    pr_state = str(pr.get("state") or "").upper()
    return {
        "schema_version": ACTIVITY_SCHEMA_VERSION,
        "fetched_at": fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state": pr_state if pr_state in VALID_PR_STATES else "OPEN",
        "is_draft": bool(pr.get("isDraft")),
        "review_decision": decision if decision in VALID_REVIEW_DECISIONS else None,
        "mergeable": str(pr.get("mergeable") or "UNKNOWN").upper(),
        "head_sha": str(head_sha) if head_sha else None,
        "reviews": reviews,
        "requested_reviewers": requested,
        "checks": {
            "summary": summarize_checks(check_items),
            "items": check_items,
            "truncated": checks_truncated,
        },
        "threads": threads,
        "threads_truncated": bool((((pr.get("reviewThreads") or {}).get("pageInfo")) or {}).get("hasNextPage")),
        "conversation": conversation,
    }


def fetch_pr_activity(
    pr_url: str | None,
    *,
    cwd: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any] | None:
    """Ask `gh` for the PR's reviews, checks and comments. Returns None on any failure."""
    parts = parse_pr_url(pr_url)
    if not parts:
        return None
    cmd = ["gh", "api", "graphql"]
    if parts["host"] != "github.com":
        cmd += ["--hostname", parts["host"]]
    cmd += [
        "-f", f"query={GRAPHQL_QUERY}",
        "-F", f"owner={parts['owner']}",
        "-F", f"name={parts['repo']}",
        "-F", f"number={parts['number']}",
    ]
    run = runner or subprocess.run
    try:
        res = run(cmd, cwd=cwd, text=True, capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    return normalize_graphql(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot a GitHub PR's reviews, checks and comments.")
    parser.add_argument("--pr", required=True, help="GitHub pull request URL")
    parser.add_argument("--out", required=True, help="Where to write the JSON snapshot")
    args = parser.parse_args(argv)

    activity = fetch_pr_activity(args.pr)
    if activity is None:
        print("Could not read reviews, checks or comments from GitHub (is `gh` signed in?).", file=sys.stderr)
        return 1
    Path(args.out).write_text(json.dumps(activity, indent=2), encoding="utf-8")
    s = activity["checks"]["summary"]
    print(
        f"GitHub snapshot: review={activity['review_decision'] or 'none'}, "
        f"checks {s['passed']} passed / {s['failed']} failed / {s['pending']} running, "
        f"{len(activity['threads'])} threads, {len(activity['conversation'])} conversation items."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
