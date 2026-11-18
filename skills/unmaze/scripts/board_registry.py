#!/usr/bin/env python3
"""Remember published unmaze boards per branch and nudge Claude to refresh them after new pushes.

State lives inside the repository's git directory (`<git-common-dir>/unmaze/`), so it is never
committed, survives across sessions, and is shared by every worktree of the same clone:

    <git-common-dir>/unmaze/boards.json          # {branch: {url, work_dir, built_sha, ...}}
    <git-common-dir>/unmaze/boards/<slug>/       # durable WORK_DIR: plan.json, visuals/, manifests

Commands:
    work-dir  --key <name>                     print (and create) a durable work dir for a review
    record    --work-dir <dir> --url <artifact> record a published board for its branch
    status                                     show the board for the current branch as JSON
    pause | resume                             turn automatic refresh nudges off / on for this branch
    hook                                       Claude Code hook entry point (reads hook JSON on stdin)
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

# `git push`, `git push -u origin x`, `git -C repo push`, `cd a && git push --force-with-lease`
PUSH_CMD_RE = re.compile(r"\bgit\b(?:\s+-{1,2}[\w-]+(?:[= ]\S+)?)*\s+push\b")
SCRIPT_PATH = Path(__file__).resolve()


def _git(args: list[str], cwd: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def git_state_dir(cwd: str) -> Path | None:
    common = _git(["rev-parse", "--git-common-dir"], cwd)
    if not common:
        return None
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (Path(cwd) / common_path).resolve()
    return common_path / "unmaze"


def current_branch(cwd: str) -> str | None:
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return branch if branch and branch != "HEAD" else None


def pushed_sha(cwd: str) -> str | None:
    """The commit the remote branch points at (what reviewers see), else local HEAD."""
    return _git(["rev-parse", "@{upstream}"], cwd) or _git(["rev-parse", "HEAD"], cwd)


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:80] or "review"


def load_registry(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "boards.json"
    if not path.is_file():
        return {"boards": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"boards": {}}
    if not isinstance(data, dict) or not isinstance(data.get("boards"), dict):
        return {"boards": {}}
    return data


def save_registry(state_dir: Path, data: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "boards.json.tmp"
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(state_dir / "boards.json")


def board_status(cwd: str) -> dict[str, Any] | None:
    state_dir = git_state_dir(cwd)
    branch = current_branch(cwd)
    if not state_dir or not branch:
        return None
    entry = load_registry(state_dir)["boards"].get(branch)
    if not isinstance(entry, dict):
        return None
    now_sha = pushed_sha(cwd)
    built = str(entry.get("built_sha") or "")
    new_commits = None
    if built and now_sha and built != now_sha:
        count = _git(["rev-list", "--count", f"{built}..{now_sha}"], cwd)
        new_commits = int(count) if count and count.isdigit() else None
    return {
        "branch": branch,
        **entry,
        "current_sha": now_sha,
        "stale": bool(built and now_sha and built != now_sha),
        "new_commits": new_commits,
    }


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_work_dir(args: argparse.Namespace) -> int:
    state_dir = git_state_dir(args.repo)
    if not state_dir:
        print("Error: not inside a git repository; use a temporary WORK_DIR instead.", file=sys.stderr)
        return 1
    key = args.key or current_branch(args.repo) or "detached"
    work_dir = state_dir / "boards" / slugify(key)
    work_dir.mkdir(parents=True, exist_ok=True)
    print(work_dir)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir).resolve()
    manifest_path = work_dir / "diff-manifest.json"
    meta: dict[str, Any] = {}
    if manifest_path.is_file():
        meta = json.loads(manifest_path.read_text(encoding="utf-8")).get("meta") or {}
    state_dir = git_state_dir(args.repo)
    if not state_dir:
        print("Error: not inside a git repository, so the board can't be tracked.", file=sys.stderr)
        return 1
    branch = args.branch or meta.get("head_branch") or current_branch(args.repo)
    if not branch:
        print("Error: couldn't tell which branch this board reviews; pass --branch.", file=sys.stderr)
        return 1
    refresh_args = meta.get("refresh_args")
    if not refresh_args:
        print(
            "Note: this review came from staged/working-tree changes or a patch file, so it can't be "
            "refreshed automatically; recording it anyway.",
            file=sys.stderr,
        )
    data = load_registry(state_dir)
    previous = data["boards"].get(branch) or {}
    data["boards"][branch] = {
        "url": args.url,
        "work_dir": str(work_dir),
        "built_sha": args.head_sha or meta.get("head_sha") or pushed_sha(args.repo),
        "pr_url": meta.get("pr_url"),
        "refresh_args": refresh_args,
        "auto_refresh": previous.get("auto_refresh", True),
        "notified_sha": None,
        "updated_at": int(time.time()),
    }
    save_registry(state_dir, data)
    print(json.dumps({"branch": branch, **data["boards"][branch]}, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    status = board_status(args.repo)
    if status is None:
        print(json.dumps({"board": None}))
        return 0
    print(json.dumps(status, indent=2))
    return 0


def _set_auto_refresh(args: argparse.Namespace, enabled: bool) -> int:
    state_dir = git_state_dir(args.repo)
    branch = current_branch(args.repo)
    if not state_dir or not branch:
        print("Error: not on a branch inside a git repository.", file=sys.stderr)
        return 1
    data = load_registry(state_dir)
    entry = data["boards"].get(branch)
    if not entry:
        print(f"No published board is recorded for `{branch}`.", file=sys.stderr)
        return 1
    entry["auto_refresh"] = enabled
    save_registry(state_dir, data)
    print(f"Automatic board refresh for `{branch}` is now {'on' if enabled else 'off'}.")
    return 0


# ---------------------------------------------------------------------------
# Claude Code hook (PostToolUse on Bash `git push`, and SessionStart)
# ---------------------------------------------------------------------------

def refresh_instructions(status: dict[str, Any], *, reason: str) -> str:
    short = lambda sha: str(sha or "")[:7]  # noqa: E731
    commits = status.get("new_commits")
    commit_txt = f"{commits} new commit{'s' if commits != 1 else ''}" if commits else "new commits"
    return (
        f"unmaze: {reason} The published review board for branch `{status['branch']}` "
        f"({status['url']}) was built at {short(status.get('built_sha'))}, and the branch is now at "
        f"{short(status.get('current_sha'))} ({commit_txt}). Refresh the board now, without waiting for "
        f"the user to ask: run the unmaze skill with `--refresh` (its \"Refresh a Published Board\" section). "
        f"Reuse work dir `{status['work_dir']}`, and republish to the same URL (read it with the Artifact "
        f"tool first, then publish with that `url`) so every link to it keeps working. If the user says "
        f"they don't want automatic refreshes, run `python3 \"{SCRIPT_PATH}\" pause`."
    )


def cmd_hook(_: argparse.Namespace) -> int:
    """Never fails loudly: any problem means 'say nothing'."""
    if os.environ.get("UNMAZE_AUTO_REFRESH", "1").strip().lower() in {"0", "false", "off", "no"}:
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    event = str(payload.get("hook_event_name") or "")
    cwd = str(payload.get("cwd") or os.getcwd())

    if event == "PostToolUse":
        if payload.get("tool_name") != "Bash":
            return 0
        command = str((payload.get("tool_input") or {}).get("command") or "")
        if not PUSH_CMD_RE.search(command):
            return 0
        reason = "You just pushed new commits."
    elif event == "SessionStart":
        reason = "New commits were pushed since the board was last built."
    else:
        return 0

    try:
        status = board_status(cwd)
    except Exception:  # noqa: BLE001 - a hook must never break the session
        return 0
    if (
        not status
        or not status.get("stale")
        or not status.get("auto_refresh", True)
        or not status.get("refresh_args")
        or status.get("notified_sha") == status.get("current_sha")
    ):
        return 0

    state_dir = git_state_dir(cwd)
    if state_dir:
        data = load_registry(state_dir)
        entry = data["boards"].get(status["branch"])
        if isinstance(entry, dict):
            entry["notified_sha"] = status.get("current_sha")
            try:
                save_registry(state_dir, data)
            except OSError:
                pass

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": refresh_instructions(status, reason=reason),
                }
            }
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=".", help="Path inside the git repository (default: current directory)")
    parser = argparse.ArgumentParser(description="Track published unmaze boards and keep them fresh.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_wd = sub.add_parser("work-dir", parents=[common], help="Print (and create) a durable work dir for a review")
    p_wd.add_argument("--key", help="Review key, e.g. `pr-142` or a branch name (default: current branch)")

    p_rec = sub.add_parser("record", parents=[common], help="Record a published board for its branch")
    p_rec.add_argument("--work-dir", required=True)
    p_rec.add_argument("--url", required=True, help="The board's claude.ai artifact URL")
    p_rec.add_argument("--branch", help="Branch the board reviews (default: from the manifest, else current)")
    p_rec.add_argument("--head-sha", help="Commit the board was built from (default: from the manifest)")

    sub.add_parser("status", parents=[common], help="Show the board recorded for the current branch")
    sub.add_parser("pause", parents=[common], help="Stop automatic refresh nudges for the current branch")
    sub.add_parser("resume", parents=[common], help="Turn automatic refresh nudges back on for the current branch")
    sub.add_parser("hook", parents=[common], help="Claude Code hook entry point (reads hook JSON on stdin)")

    args = parser.parse_args(argv)
    if args.command == "work-dir":
        return cmd_work_dir(args)
    if args.command == "record":
        return cmd_record(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "pause":
        return _set_auto_refresh(args, False)
    if args.command == "resume":
        return _set_auto_refresh(args, True)
    if args.command == "hook":
        return cmd_hook(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
