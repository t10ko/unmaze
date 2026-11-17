#!/usr/bin/env python3
"""Deterministic Git/PR diff parser and deep-link inventory builder for unmaze.

Parses unified diffs from GitHub PRs (`gh pr diff`), git revision ranges, staged/working
changes, or `.patch` files into a structured `diff-manifest.json` with stable Hunk IDs
(`H001`, `H002`, ...) and exact PR/blob deep links, plus a token-efficient
`hunks-readable.txt` digest for the reviewing agent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any


HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?$"
)
DIFF_GIT_RE = re.compile(r"^diff --git (.+)$")

LOCKFILE_NAMES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
    "Podfile.lock",
    "mix.lock",
    "pubspec.lock",
    "flake.lock",
}

GENERATED_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".snap",
    ".pb.go",
    "_pb2.py",
    "_pb2.pyi",
    ".generated.ts",
    ".generated.tsx",
    ".g.dart",
    ".freezed.dart",
    ".designer.cs",
)


def run_cmd(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def unquote_git_path(raw: str) -> str:
    """Unquote a git diff path (handles C-style quoted paths with spaces/escapes)."""
    raw = raw.strip()
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        inner = raw[1:-1]
        out = bytearray()
        i = 0
        escapes = {
            "a": 0x07,
            "b": 0x08,
            "t": 0x09,
            "n": 0x0A,
            "v": 0x0B,
            "f": 0x0C,
            "r": 0x0D,
            '"': ord('"'),
            "\\": ord("\\"),
        }
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner):
                nxt = inner[i + 1]
                if nxt in escapes:
                    out.append(escapes[nxt])
                    i += 2
                    continue
                if nxt in "01234567" and i + 3 < len(inner):
                    octal = inner[i + 1 : i + 4]
                    if all(c in "01234567" for c in octal):
                        out.append(int(octal, 8))
                        i += 4
                        continue
                out.extend(nxt.encode("utf-8"))
                i += 2
            else:
                out.extend(ch.encode("utf-8"))
                i += 1
        return out.decode("utf-8", errors="replace")
    return raw


def strip_ab_prefix(path: str, prefix: str) -> str:
    if path == "/dev/null":
        return "/dev/null"
    if path.startswith(prefix):
        return path[len(prefix) :]
    return path


def parse_diff_git_paths(rest: str) -> tuple[str, str]:
    """Extract (old_path, new_path) from `diff --git <old> <new>`."""
    rest = rest.strip()
    if rest.startswith('"'):
        # Parse first quoted token
        i = 1
        while i < len(rest):
            if rest[i] == "\\":
                i += 2
                continue
            if rest[i] == '"':
                break
            i += 1
        first = unquote_git_path(rest[: i + 1])
        second = unquote_git_path(rest[i + 1 :].strip())
        return strip_ab_prefix(first, "a/"), strip_ab_prefix(second, "b/")

    # Standard unquoted `a/foo b/foo`: look for ` b/` boundary
    # If multiple ` b/` exist, prefer symmetric path if old==new
    b_indices = [m.start() for m in re.finditer(r" b/", rest)]
    for idx in b_indices:
        left = rest[:idx]
        right = rest[idx + 1 :]
        if left.startswith("a/") and right.startswith("b/") and left[2:] == right[2:]:
            return left[2:], right[2:]
    if b_indices:
        idx = b_indices[-1]
        return strip_ab_prefix(rest[:idx], "a/"), strip_ab_prefix(rest[idx + 1 :], "b/")

    parts = rest.split(None, 1)
    if len(parts) == 2:
        return strip_ab_prefix(parts[0], "a/"), strip_ab_prefix(parts[1], "b/")
    return rest, rest


def classify_file_hint(path: str) -> str:
    """Provide a lightweight non-binding category hint to help orient the agent."""
    p = PurePosixPath(path)
    name = p.name
    lower_path = path.lower()
    lower_name = name.lower()
    parts = [seg.lower() for seg in p.parts]

    if name in LOCKFILE_NAMES or lower_name.endswith(GENERATED_SUFFIXES):
        return "generated_or_lockfile"
    if any(seg in {"dist", "build", "vendor", ".next", "coverage", "__snapshots__"} for seg in parts):
        return "generated_or_lockfile"

    if (
        any(seg in {"test", "tests", "__tests__", "spec", "specs", "e2e"} for seg in parts)
        or ".test." in lower_name
        or ".spec." in lower_name
        or lower_name.endswith(("_test.py", "_test.go", "_test.rs", "test.java", "tests.cs"))
        or (lower_name.startswith("test_") and lower_name.endswith(".py"))
    ):
        return "test"

    if (
        any(seg in {"migrations", "schema", "schemas", "proto", "protos", "graphql", "types", "DTO", "dto", "contracts"} for seg in parts)
        or lower_name.endswith((".proto", ".graphql", ".gql", ".sql", ".prisma", ".d.ts"))
        or any(tok in lower_name for tok in ("schema", "types.", "interface", "contract", "dto.", "model."))
    ):
        return "contracts"

    if (
        lower_name in {
            "package.json",
            "pyproject.toml",
            "cargo.toml",
            "go.mod",
            "dockerfile",
            "docker-compose.yml",
            "makefile",
            "index.ts",
            "index.tsx",
            "index.js",
            "__init__.py",
            "mod.rs",
        }
        or lower_path.startswith(".github/")
        or any(seg in {"routes", "router", "controllers", "cli", "cmd", "di", "config"} for seg in parts)
        or any(tok in lower_name for tok in ("route", "router", "wiring", "bootstrap", "main.", "app.", "server."))
    ):
        return "wiring"

    return "core"


def normalize_repo_web_url(remote_url: str) -> tuple[str | None, str | None]:
    """Convert git remote URL to (web_base_url, host_kind)."""
    raw = remote_url.strip()
    if not raw:
        return None, None

    # git@host:owner/repo.git
    ssh_match = re.match(r"^git@([^:]+):(.+?)(?:\.git)?/?$", raw)
    if ssh_match:
        host, repo_path = ssh_match.group(1), ssh_match.group(2)
        web = f"https://{host}/{repo_path}"
    else:
        ssh_url_match = re.match(r"^ssh://(?:git@)?([^/]+)/(.+?)(?:\.git)?/?$", raw)
        if ssh_url_match:
            host, repo_path = ssh_url_match.group(1), ssh_url_match.group(2)
            web = f"https://{host}/{repo_path}"
        else:
            http_match = re.match(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", raw)
            if not http_match:
                return None, None
            host, repo_path = http_match.group(1), http_match.group(2)
            web = f"https://{host}/{repo_path}"

    lower_host = host.lower()
    if "github" in lower_host:
        kind = "github"
    elif "gitlab" in lower_host:
        kind = "gitlab"
    elif "bitbucket" in lower_host:
        kind = "bitbucket"
    else:
        kind = "generic"
    return web, kind


def compute_deep_link(
    *,
    repo_web_url: str | None,
    host_kind: str | None,
    pr_url: str | None,
    head_ref: str | None,
    status: str,
    old_path: str,
    new_path: str,
    old_start: int,
    new_start: int,
    new_count: int,
    is_synthetic: bool,
) -> str | None:
    target_path = old_path if status == "deleted" else new_path
    if not target_path or target_path == "/dev/null":
        return None

    if pr_url and host_kind == "github":
        digest = hashlib.sha256(target_path.encode("utf-8")).hexdigest()
        base = pr_url.rstrip("/")
        if is_synthetic:
            return f"{base}/files#diff-{digest}"
        if status == "deleted" or (new_count == 0 and old_start > 0):
            return f"{base}/files#diff-{digest}L{max(1, old_start)}"
        return f"{base}/files#diff-{digest}R{max(1, new_start)}"

    if pr_url and host_kind == "gitlab":
        digest = hashlib.sha1(target_path.encode("utf-8")).hexdigest()
        base = pr_url.rstrip("/")
        if is_synthetic:
            return f"{base}/diffs#{digest}"
        return f"{base}/diffs#{digest}_{max(1, old_start)}_{max(1, new_start)}"

    if pr_url and host_kind == "bitbucket":
        base = pr_url.rstrip("/")
        return f"{base}/diff#chg-{target_path}"

    if repo_web_url and head_ref and status != "deleted":
        if host_kind == "github":
            if is_synthetic or new_start <= 0:
                return f"{repo_web_url}/blob/{head_ref}/{target_path}"
            end_line = max(new_start, new_start + max(new_count, 1) - 1)
            return f"{repo_web_url}/blob/{head_ref}/{target_path}#L{new_start}-L{end_line}"
        if host_kind == "gitlab":
            if is_synthetic or new_start <= 0:
                return f"{repo_web_url}/-/blob/{head_ref}/{target_path}"
            end_line = max(new_start, new_start + max(new_count, 1) - 1)
            return f"{repo_web_url}/-/blob/{head_ref}/{target_path}#L{new_start}-{end_line}"
        if host_kind == "bitbucket":
            if is_synthetic or new_start <= 0:
                return f"{repo_web_url}/src/{head_ref}/{target_path}"
            return f"{repo_web_url}/src/{head_ref}/{target_path}#lines-{new_start}"

    return pr_url


def parse_unified_diff(
    diff_text: str,
    *,
    repo_web_url: str | None = None,
    host_kind: str | None = None,
    pr_url: str | None = None,
    head_ref: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse unified diff text into `(files, hunks)` with sequential Hunk IDs."""
    lines = diff_text.splitlines()
    files: list[dict[str, Any]] = []
    hunks: list[dict[str, Any]] = []

    cur_file: dict[str, Any] | None = None
    cur_hunk: dict[str, Any] | None = None
    old_ln = 0
    new_ln = 0
    hunk_counter = 0

    def next_hunk_id() -> str:
        nonlocal hunk_counter
        hunk_counter += 1
        return f"H{hunk_counter:03d}"

    def finalize_hunk() -> None:
        nonlocal cur_hunk
        if cur_hunk is None or cur_file is None:
            return
        hunks.append(cur_hunk)
        cur_file["hunk_ids"].append(cur_hunk["id"])
        cur_file["additions"] += cur_hunk["additions"]
        cur_file["deletions"] += cur_hunk["deletions"]
        cur_hunk = None

    def finalize_file() -> None:
        nonlocal cur_file
        finalize_hunk()
        if cur_file is None:
            return
        # Ensure files with pure renames, binary changes, or empty additions still get a Hunk ID
        if not cur_file["hunk_ids"]:
            hid = next_hunk_id()
            status = cur_file["status"]
            display_path = cur_file["path"]
            if cur_file["is_binary"]:
                summary_line = f"[Binary file {status}: {display_path}]"
            elif status == "renamed":
                summary_line = f"[Pure file rename: {cur_file['old_path']} -> {cur_file['new_path']} (no content changes)]"
            elif status == "added":
                summary_line = f"[Empty file added: {display_path}]"
            elif status == "deleted":
                summary_line = f"[Empty file deleted: {cur_file['old_path']}]"
            else:
                mode_note = cur_file.get("mode_change") or "metadata updated"
                summary_line = f"[File {mode_note}: {display_path}]"

            synthetic = {
                "id": hid,
                "file_path": display_path,
                "old_path": cur_file["old_path"],
                "new_path": cur_file["new_path"],
                "file_status": status,
                "category_hint": cur_file["category_hint"],
                "is_binary": cur_file["is_binary"],
                "is_synthetic": True,
                "header": summary_line,
                "section_context": "",
                "old_start": 0,
                "old_count": 0,
                "new_start": 0,
                "new_count": 0,
                "additions": 0,
                "deletions": 0,
                "deep_link": compute_deep_link(
                    repo_web_url=repo_web_url,
                    host_kind=host_kind,
                    pr_url=pr_url,
                    head_ref=head_ref,
                    status=status,
                    old_path=cur_file["old_path"],
                    new_path=cur_file["new_path"],
                    old_start=0,
                    new_start=0,
                    new_count=0,
                    is_synthetic=True,
                ),
                "lines": [
                    {
                        "kind": "meta",
                        "old_line": None,
                        "new_line": None,
                        "content": summary_line,
                    }
                ],
            }
            hunks.append(synthetic)
            cur_file["hunk_ids"].append(hid)

        files.append(cur_file)
        cur_file = None

    i = 0
    while i < len(lines):
        line = lines[i]

        m_git = DIFF_GIT_RE.match(line)
        if m_git:
            finalize_file()
            old_path, new_path = parse_diff_git_paths(m_git.group(1))
            display_path = new_path if new_path != "/dev/null" else old_path
            cur_file = {
                "path": display_path,
                "old_path": old_path,
                "new_path": new_path,
                "status": "modified",
                "is_binary": False,
                "mode_change": None,
                "category_hint": classify_file_hint(display_path),
                "additions": 0,
                "deletions": 0,
                "hunk_ids": [],
            }
            i += 1
            continue

        if cur_file is None:
            i += 1
            continue

        # File-level metadata headers (only when not inside a hunk, or if a metadata line appears before @@)
        if cur_hunk is None:
            if line.startswith("new file mode "):
                cur_file["status"] = "added"
                cur_file["mode_change"] = line
                i += 1
                continue
            if line.startswith("deleted file mode "):
                cur_file["status"] = "deleted"
                cur_file["mode_change"] = line
                i += 1
                continue
            if line.startswith(("old mode ", "new mode ")):
                prev = cur_file["mode_change"]
                cur_file["mode_change"] = f"{prev}; {line}" if prev else line
                i += 1
                continue
            if line.startswith("rename from "):
                cur_file["status"] = "renamed"
                cur_file["old_path"] = unquote_git_path(line[len("rename from ") :])
                i += 1
                continue
            if line.startswith("rename to "):
                cur_file["status"] = "renamed"
                cur_file["new_path"] = unquote_git_path(line[len("rename to ") :])
                cur_file["path"] = cur_file["new_path"]
                cur_file["category_hint"] = classify_file_hint(cur_file["path"])
                i += 1
                continue
            if line.startswith("--- "):
                raw_old = unquote_git_path(line[4:])
                parsed_old = strip_ab_prefix(raw_old, "a/")
                if parsed_old == "/dev/null":
                    cur_file["status"] = "added"
                else:
                    cur_file["old_path"] = parsed_old
                i += 1
                continue
            if line.startswith("+++ "):
                raw_new = unquote_git_path(line[4:])
                parsed_new = strip_ab_prefix(raw_new, "b/")
                if parsed_new == "/dev/null":
                    cur_file["status"] = "deleted"
                    cur_file["path"] = cur_file["old_path"]
                else:
                    cur_file["new_path"] = parsed_new
                    cur_file["path"] = parsed_new
                cur_file["category_hint"] = classify_file_hint(cur_file["path"])
                i += 1
                continue
            if line.startswith("Binary files ") or line == "GIT binary patch":
                cur_file["is_binary"] = True
                if line == "GIT binary patch":
                    # Advance until next `diff --git`
                    i += 1
                    while i < len(lines) and not DIFF_GIT_RE.match(lines[i]):
                        i += 1
                    continue
                i += 1
                continue

        m_hunk = HUNK_HEADER_RE.match(line)
        if m_hunk:
            finalize_hunk()
            old_start = int(m_hunk.group(1))
            old_count = int(m_hunk.group(2)) if m_hunk.group(2) is not None else 1
            new_start = int(m_hunk.group(3))
            new_count = int(m_hunk.group(4)) if m_hunk.group(4) is not None else 1
            section_ctx = (m_hunk.group(5) or "").strip()

            old_ln = old_start
            new_ln = new_start
            hid = next_hunk_id()
            status = cur_file["status"]
            cur_hunk = {
                "id": hid,
                "file_path": cur_file["path"],
                "old_path": cur_file["old_path"],
                "new_path": cur_file["new_path"],
                "file_status": status,
                "category_hint": cur_file["category_hint"],
                "is_binary": False,
                "is_synthetic": False,
                "header": line,
                "section_context": section_ctx,
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
                "additions": 0,
                "deletions": 0,
                "deep_link": compute_deep_link(
                    repo_web_url=repo_web_url,
                    host_kind=host_kind,
                    pr_url=pr_url,
                    head_ref=head_ref,
                    status=status,
                    old_path=cur_file["old_path"],
                    new_path=cur_file["new_path"],
                    old_start=old_start,
                    new_start=new_start,
                    new_count=new_count,
                    is_synthetic=False,
                ),
                "lines": [],
            }
            i += 1
            continue

        if cur_hunk is not None:
            if line.startswith("+"):
                cur_hunk["lines"].append(
                    {
                        "kind": "add",
                        "old_line": None,
                        "new_line": new_ln,
                        "content": line[1:],
                    }
                )
                cur_hunk["additions"] += 1
                new_ln += 1
            elif line.startswith("-"):
                cur_hunk["lines"].append(
                    {
                        "kind": "del",
                        "old_line": old_ln,
                        "new_line": None,
                        "content": line[1:],
                    }
                )
                cur_hunk["deletions"] += 1
                old_ln += 1
            elif line.startswith(" "):
                cur_hunk["lines"].append(
                    {
                        "kind": "ctx",
                        "old_line": old_ln,
                        "new_line": new_ln,
                        "content": line[1:],
                    }
                )
                old_ln += 1
                new_ln += 1
            elif line.startswith("\\"):
                cur_hunk["lines"].append(
                    {
                        "kind": "meta",
                        "old_line": None,
                        "new_line": None,
                        "content": line,
                    }
                )
            elif line == "":
                # Blank context line where trailing space was stripped by an editor/mailer
                cur_hunk["lines"].append(
                    {
                        "kind": "ctx",
                        "old_line": old_ln,
                        "new_line": new_ln,
                        "content": "",
                    }
                )
                old_ln += 1
                new_ln += 1
            else:
                cur_hunk["lines"].append(
                    {
                        "kind": "meta",
                        "old_line": None,
                        "new_line": None,
                        "content": line,
                    }
                )

        i += 1

    finalize_file()
    return files, hunks


def detect_default_branch(cwd: str) -> str:
    res = run_cmd(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd, check=False)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().split("/")[-1]
    for candidate in ("main", "master", "develop", "trunk"):
        chk = run_cmd(["git", "rev-parse", "--verify", candidate], cwd=cwd, check=False)
        if chk.returncode == 0:
            return candidate
    return "HEAD~1"


def collect_diff_source(args: argparse.Namespace) -> dict[str, Any]:
    cwd = os.path.abspath(args.repo)
    meta: dict[str, Any] = {
        "source_type": "unknown",
        "title": args.title or "",
        "pr_number": None,
        "pr_url": args.pr_url,
        "repo_web_url": args.repo_url,
        "host_kind": None,
        "base_ref": None,
        "head_ref": None,
        "head_sha": None,
    }

    # Try detecting git remote URL if inside a git worktree
    git_check = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd, check=False)
    in_git = git_check.returncode == 0

    if in_git and not meta["repo_web_url"]:
        rem = run_cmd(["git", "remote", "get-url", "origin"], cwd=cwd, check=False)
        if rem.returncode == 0 and rem.stdout.strip():
            web_url, host_kind = normalize_repo_web_url(rem.stdout.strip())
            meta["repo_web_url"] = web_url
            meta["host_kind"] = host_kind
    elif meta["repo_web_url"]:
        web_url, host_kind = normalize_repo_web_url(meta["repo_web_url"])
        meta["repo_web_url"] = web_url or meta["repo_web_url"]
        meta["host_kind"] = host_kind

    if meta["pr_url"] and not meta["host_kind"]:
        _, host_kind = normalize_repo_web_url(meta["pr_url"])
        meta["host_kind"] = host_kind

    # 1. Explicit patch file or stdin
    if args.patch:
        meta["source_type"] = "patch"
        if args.patch == "-":
            diff_text = sys.stdin.read()
            meta["title"] = meta["title"] or "Stdin Diff Review"
        else:
            patch_path = Path(args.patch)
            diff_text = patch_path.read_text(encoding="utf-8", errors="replace")
            meta["title"] = meta["title"] or f"Patch Review: {patch_path.name}"
        return {"meta": meta, "diff_text": diff_text}

    # 2. GitHub PR via `gh`
    if args.pr:
        meta["source_type"] = "github_pr"
        pr_target = str(args.pr).strip()
        view_res = run_cmd(
            [
                "gh",
                "pr",
                "view",
                pr_target,
                "--json",
                "number,title,url,baseRefName,headRefName,headRefOid",
            ],
            cwd=cwd if in_git else None,
            check=False,
        )
        if view_res.returncode != 0:
            raise RuntimeError(
                f"Failed to fetch PR metadata via `gh pr view {pr_target}`:\n{view_res.stderr.strip()}"
            )
        pr_info = json.loads(view_res.stdout)
        meta["pr_number"] = pr_info.get("number")
        meta["title"] = meta["title"] or f"PR #{pr_info.get('number')}: {pr_info.get('title', '')}"
        meta["pr_url"] = meta["pr_url"] or pr_info.get("url")
        meta["base_ref"] = pr_info.get("baseRefName")
        meta["head_ref"] = pr_info.get("headRefOid") or pr_info.get("headRefName")
        meta["head_sha"] = pr_info.get("headRefOid")
        if meta["pr_url"]:
            pr_repo_match = re.match(r"^(https?://[^/]+/[^/]+/[^/]+)/pull/\d+", meta["pr_url"])
            if pr_repo_match:
                meta["repo_web_url"] = pr_repo_match.group(1)
                meta["host_kind"] = "github"

        diff_res = run_cmd(["gh", "pr", "diff", pr_target], cwd=cwd if in_git else None, check=False)
        if diff_res.returncode != 0:
            raise RuntimeError(
                f"Failed to fetch PR diff via `gh pr diff {pr_target}`:\n{diff_res.stderr.strip()}"
            )
        return {"meta": meta, "diff_text": diff_res.stdout}

    if not in_git:
        raise RuntimeError(
            f"Directory `{cwd}` is not a git repository. Pass `--patch <file>` or run inside a git repository."
        )

    head_sha_res = run_cmd(["git", "rev-parse", "HEAD"], cwd=cwd, check=False)
    head_sha = head_sha_res.stdout.strip() if head_sha_res.returncode == 0 else None
    branch_res = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, check=False)
    cur_branch = branch_res.stdout.strip() if branch_res.returncode == 0 else "HEAD"
    meta["head_sha"] = head_sha
    meta["head_ref"] = head_sha or cur_branch

    # 3. Staged or working tree
    if args.staged:
        meta["source_type"] = "staged"
        meta["title"] = meta["title"] or f"Staged Changes on `{cur_branch}`"
        diff_res = run_cmd(["git", "diff", "--cached", "--find-renames"], cwd=cwd)
        return {"meta": meta, "diff_text": diff_res.stdout}

    if args.working:
        meta["source_type"] = "working_tree"
        meta["title"] = meta["title"] or f"Working Tree Changes on `{cur_branch}`"
        diff_res = run_cmd(["git", "diff", "HEAD", "--find-renames"], cwd=cwd)
        return {"meta": meta, "diff_text": diff_res.stdout}

    # 4. Explicit git range or smart auto-detection
    git_range = args.range
    if not git_range:
        default_branch = detect_default_branch(cwd)
        if cur_branch != default_branch and cur_branch != "HEAD":
            # Also check if there is an open PR for the current branch via `gh` (best effort)
            gh_probe = run_cmd(
                ["gh", "pr", "view", "--json", "number,title,url,baseRefName,headRefOid"],
                cwd=cwd,
                check=False,
            )
            if gh_probe.returncode == 0 and gh_probe.stdout.strip():
                try:
                    pr_info = json.loads(gh_probe.stdout)
                    meta["pr_number"] = pr_info.get("number")
                    meta["pr_url"] = meta["pr_url"] or pr_info.get("url")
                    if pr_info.get("title") and not meta["title"]:
                        meta["title"] = f"PR #{pr_info.get('number')}: {pr_info.get('title')}"
                    if meta["pr_url"] and "github" in meta["pr_url"]:
                        meta["host_kind"] = "github"
                    default_branch = pr_info.get("baseRefName") or default_branch
                except json.JSONDecodeError:
                    pass
            git_range = f"{default_branch}...HEAD"
        else:
            # On default branch: check uncommitted changes first, else HEAD~1..HEAD
            wt = run_cmd(["git", "diff", "HEAD", "--find-renames"], cwd=cwd, check=False)
            if wt.returncode == 0 and wt.stdout.strip():
                meta["source_type"] = "working_tree"
                meta["title"] = meta["title"] or f"Uncommitted Changes on `{cur_branch}`"
                return {"meta": meta, "diff_text": wt.stdout}
            git_range = "HEAD~1..HEAD"

    meta["source_type"] = "git_range"
    meta["base_ref"] = git_range
    meta["title"] = meta["title"] or f"Changes in `{git_range}` ({cur_branch})"
    diff_res = run_cmd(["git", "diff", "--find-renames", git_range], cwd=cwd, check=False)
    if diff_res.returncode != 0:
        raise RuntimeError(f"`git diff {git_range}` failed:\n{diff_res.stderr.strip()}")
    return {"meta": meta, "diff_text": diff_res.stdout}


def format_readable_digest(manifest: dict[str, Any], max_inline_lines: int = 250) -> str:
    """Create a clean, token-efficient text view of all hunks for the reviewing agent."""
    meta = manifest["meta"]
    stats = manifest["stats"]
    out: list[str] = []
    out.append(f"# unmaze Diff Inventory: {meta.get('title') or 'Untitled Diff'}")
    out.append(
        f"Total: {stats['total_files']} files, {stats['total_hunks']} hunks "
        f"(+{stats['total_additions']} / -{stats['total_deletions']})"
    )
    if meta.get("pr_url"):
        out.append(f"PR URL: {meta['pr_url']}")
    out.append("")
    out.append("## File & Hunk Index (Every Hunk ID MUST be accounted for in plan.json)")
    for f in manifest["files"]:
        hids = ", ".join(f["hunk_ids"])
        out.append(
            f"- [{f['category_hint']}] `{f['path']}` ({f['status']}, +{f['additions']}/-{f['deletions']}) -> {hids}"
        )

    out.append("")
    out.append("## Hunk Contents")
    for h in manifest["hunks"]:
        loc = (
            f"new L{h['new_start']}..{h['new_start'] + max(h['new_count'], 1) - 1}"
            if not h["is_synthetic"] and h["file_status"] != "deleted"
            else (f"old L{h['old_start']}" if not h["is_synthetic"] else "file-level")
        )
        ctx = f" | {h['section_context']}" if h["section_context"] else ""
        out.append(
            f"\n=== [{h['id']}] {h['file_path']} ({h['file_status']}, {loc}, "
            f"+{h['additions']}/-{h['deletions']}, hint={h['category_hint']}){ctx} ==="
        )
        if h["category_hint"] == "generated_or_lockfile" and len(h["lines"]) > 20:
            out.append(
                f"  [Omitted {len(h['lines'])} lockfile/generated diff lines to save context; "
                f"full lines are preserved in diff-manifest.json and will render in the HTML artifact.]"
            )
            continue

        shown = h["lines"][:max_inline_lines]
        for ln in shown:
            kind = ln["kind"]
            if kind == "add":
                prefix = f"+{ln['new_line'] or '':>4} | +"
            elif kind == "del":
                prefix = f"-{ln['old_line'] or '':>4} | -"
            elif kind == "ctx":
                prefix = f" {ln['new_line'] or '':>4} |  "
            else:
                prefix = "      |  "
            out.append(f"{prefix}{ln['content']}")
        if len(h["lines"]) > max_inline_lines:
            omitted = len(h["lines"]) - max_inline_lines
            out.append(
                f"  ... [{omitted} additional lines in {h['id']} stored in diff-manifest.json; "
                f"inspect `{h['file_path']}` directly if deeper context is needed]"
            )

    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse a git/PR diff into a deterministic hunk manifest for unmaze."
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--pr", help="GitHub PR number, branch, or URL (uses `gh pr diff`)")
    source_group.add_argument("--range", help="Git diff range, e.g. `main...HEAD` or `HEAD~2..HEAD`")
    source_group.add_argument("--staged", action="store_true", help="Review staged changes (`git diff --cached`)")
    source_group.add_argument("--working", action="store_true", help="Review working tree changes (`git diff HEAD`)")
    source_group.add_argument("--patch", help="Path to a unified `.patch`/`.diff` file (or `-` for stdin)")

    parser.add_argument("--repo", default=".", help="Path to git repository (default: current directory)")
    parser.add_argument("--title", help="Override review title")
    parser.add_argument("--pr-url", help="Explicit PR web URL for deep links (optional)")
    parser.add_argument("--repo-url", help="Explicit repository web URL for deep links (optional)")
    parser.add_argument(
        "--out-dir",
        default=".unmaze",
        help="Directory to write `diff-manifest.json` and `hunks-readable.txt` (default: .unmaze)",
    )
    args = parser.parse_args(argv)

    try:
        collected = collect_diff_source(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    meta = collected["meta"]
    diff_text = collected["diff_text"]
    if not diff_text.strip():
        print("Error: The selected diff is empty (no changes found).", file=sys.stderr)
        return 2

    files, hunks = parse_unified_diff(
        diff_text,
        repo_web_url=meta.get("repo_web_url"),
        host_kind=meta.get("host_kind"),
        pr_url=meta.get("pr_url"),
        head_ref=meta.get("head_ref"),
    )

    total_add = sum(f["additions"] for f in files)
    total_del = sum(f["deletions"] for f in files)
    by_hint: dict[str, int] = {}
    for h in hunks:
        hint = h["category_hint"]
        by_hint[hint] = by_hint.get(hint, 0) + 1

    manifest = {
        "schema_version": "1.0",
        "meta": meta,
        "stats": {
            "total_files": len(files),
            "total_hunks": len(hunks),
            "total_additions": total_add,
            "total_deletions": total_del,
            "hunks_by_hint": by_hint,
        },
        "files": files,
        "hunks": hunks,
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "diff-manifest.json"
    readable_path = out_dir / "hunks-readable.txt"

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    readable_path.write_text(format_readable_digest(manifest), encoding="utf-8")

    print(f"Parsed {len(files)} files and {len(hunks)} hunks (+{total_add} / -{total_del}).")
    print(f"Manifest written to: {manifest_path}")
    print(f"Readable digest written to: {readable_path}")
    print("\nFile summary:")
    for f in files:
        print(
            f"  [{f['category_hint']:<21}] {f['path']} "
            f"({f['status']}, +{f['additions']}/-{f['deletions']}) -> {', '.join(f['hunk_ids'])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
