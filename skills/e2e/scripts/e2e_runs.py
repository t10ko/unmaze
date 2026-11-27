#!/usr/bin/env python3
"""Where unmaze:e2e keeps a branch's end-to-end checks, and small helpers around them.

Everything lives inside the repository's git directory, so it is never committed, survives
across sessions, and is shared by every worktree of the same clone:

    <git-common-dir>/unmaze/e2e/<branch-slug>/scenarios.md    # the QA plan (only the QA subagent writes it)
    <git-common-dir>/unmaze/e2e/<branch-slug>/results.json    # the latest run (references/e2e-results.md)
    <git-common-dir>/unmaze/e2e/<branch-slug>/media/          # one GIF per browser step

Commands:
    dir       print (and create) the branch's folder
    status    JSON: the folder, the results file if any, the commit it tested, and whether it's stale
    check     validate results.json, including "a passing browser step has its GIF"
    take-gif  move a GIF the browser just downloaded into media/
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import shutil
import sys
import time
from typing import Any

UNMAZE_SCRIPTS = Path(__file__).resolve().parents[2] / "unmaze" / "scripts"
sys.path.insert(0, str(UNMAZE_SCRIPTS))

from board_registry import _git, current_branch, git_state_dir, slugify  # noqa: E402
import e2e_results  # noqa: E402


def run_dir(repo: str, branch: str | None) -> Path:
    state_dir = git_state_dir(repo)
    if not state_dir:
        raise SystemExit("Error: not inside a git repository.")
    branch = branch or current_branch(repo)
    if not branch:
        raise SystemExit("Error: detached HEAD (for example mid-rebase); pass --branch <the PR's head branch>.")
    return state_dir / "e2e" / slugify(branch)


def status(repo: str, branch: str | None, sha: str | None = None) -> dict[str, Any]:
    """`stale` means the checks ran on a different commit than `sha` (default: HEAD), so they need a
    rerun. Uncommitted changes alone never make a run stale: rerunning on a dirty tree would only
    produce another dirty run, so they're reported separately."""
    folder = run_dir(repo, branch)
    results_path = folder / "results.json"
    target = _git(["rev-parse", sha or "HEAD"], repo)
    out: dict[str, Any] = {
        "dir": str(folder),
        "scenarios": str(folder / "scenarios.md") if (folder / "scenarios.md").is_file() else None,
        "results": None,
        "tested_sha": None,
        "compared_with": target,
        "stale": False,
        "stale_reason": None,
        "uncommitted_changes": False,
        "counts": None,
    }
    if not results_path.is_file():
        return out
    out["results"] = str(results_path)
    try:
        raw = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    if not isinstance(raw, dict):
        out.update(stale=True, stale_reason="unreadable")
        return out
    tested = str(raw.get("tested_sha") or "").strip().lower()
    out["tested_sha"] = tested or None
    out["uncommitted_changes"] = raw.get("uncommitted_changes") is True
    if not tested or not target or not target.lower().startswith(tested):
        out.update(stale=True, stale_reason="older_commit")
    scenarios = raw.get("scenarios") if isinstance(raw.get("scenarios"), list) else []
    out["counts"] = {
        k: sum(1 for s in scenarios if isinstance(s, dict) and s.get("status") == k)
        for k in ("pass", "fail", "skipped")
    }
    return out


_PLAN_SCENARIO_RE = re.compile(r"^###\s+(S\d+)\b", re.M)
_PLAN_KIND_RE = re.compile(r"^-\s*Kind:\s*(browser|api|job)\b", re.M)


def planned_scenarios(text: str) -> dict[str, str | None]:
    """Scenario id -> kind, read from the QA plan's `### S<n> — title` blocks and `- Kind:` lines."""
    heads = list(_PLAN_SCENARIO_RE.finditer(text))
    plan: dict[str, str | None] = {}
    for i, m in enumerate(heads):
        block = text[m.end() : heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        block = block.split("\n## ", 1)[0]
        kind = _PLAN_KIND_RE.search(block)
        plan[m.group(1)] = kind.group(1) if kind else None
    return plan


def check(repo: str, branch: str | None) -> list[str]:
    folder = run_dir(repo, branch)
    try:
        model = e2e_results.load_results(folder / "results.json")
    except e2e_results.E2EResultsError as exc:
        return [str(exc)]
    problems = []
    scenarios_md = folder / "scenarios.md"
    if scenarios_md.is_file():
        plan = planned_scenarios(scenarios_md.read_text(encoding="utf-8"))
        ran = {sc["id"]: sc["kind"] for sc in model["scenarios"]}
        for sid, kind in plan.items():
            if sid not in ran:
                problems.append(f"- {sid} is in scenarios.md but not in results.json; run it or mark it skipped with a note.")
            elif kind and ran[sid] != kind:
                problems.append(f"- {sid} is a {kind} scenario in scenarios.md but `{ran[sid]}` in results.json.")
    on_current_branch = branch is None or branch == current_branch(repo)
    head = _git(["rev-parse", "HEAD"], repo)
    if on_current_branch and head and not head.lower().startswith(model["tested_sha"]):
        problems.append(f"- `tested_sha` is {model['tested_sha']}, but HEAD is {head}; record the commit you tested.")
    for sc in model["scenarios"]:
        if sc["status"] == "pass" and any(st["status"] != "pass" for st in sc["steps"]):
            problems.append(f"- {sc['id']} is marked pass, but not every step passed.")
        for n, st in enumerate(sc["steps"], 1):
            if st["status"] != "pass":
                continue
            if sc["kind"] == "browser" and not st["gif"]:
                problems.append(f"- {sc['id']} step {n}: a passing browser step needs its GIF in media/.")
            if sc["kind"] in ("api", "job") and not st.get("response"):
                problems.append(f"- {sc['id']} step {n}: a passing {sc['kind']} step needs the `response` it got.")
    return problems


def take_gif(repo: str, branch: str | None, name: str, downloads: Path, max_age: float) -> Path:
    """Move the newest `<name>` (or a browser-renamed `<stem> (1).gif`) from Downloads into media/."""
    stem = Path(name).stem
    now = time.time()
    candidates = [
        p for p in downloads.glob(f"{stem}*.gif")
        if p.is_file() and (p.name == name or p.name.startswith(f"{stem} (")) and now - p.stat().st_mtime <= max_age
    ]
    if not candidates:
        raise SystemExit(
            f"Error: no {name} in {downloads} from the last {int(max_age)} seconds; export the GIF again."
        )
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    media = run_dir(repo, branch) / "media"
    media.mkdir(parents=True, exist_ok=True)
    target = media / name
    shutil.move(str(newest), target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=os.getcwd(), help="Any path inside the repository (default: cwd)")
    common.add_argument("--branch", default=None, help="Branch name (default: the checked-out branch)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dir", parents=[common], help="Print (and create) the branch's folder")
    p_status = sub.add_parser("status", parents=[common], help="Show the branch's saved checks as JSON")
    p_status.add_argument("--sha", default=None, help="Commit to compare with (default: HEAD), e.g. the PR's head")
    sub.add_parser("check", parents=[common], help="Validate results.json")
    p_gif = sub.add_parser("take-gif", parents=[common], help="Move a just-downloaded GIF into media/")
    p_gif.add_argument("name", help="File name the GIF was exported as, e.g. S1-step2.gif")
    p_gif.add_argument("--downloads", default=str(Path.home() / "Downloads"))
    p_gif.add_argument("--max-age", type=float, default=300, help="Ignore files older than this many seconds")
    args = parser.parse_args(argv)

    if args.cmd == "dir":
        folder = run_dir(args.repo, args.branch)
        (folder / "media").mkdir(parents=True, exist_ok=True)
        print(folder)
        return 0
    if args.cmd == "status":
        print(json.dumps(status(args.repo, args.branch, args.sha), indent=2))
        return 0
    if args.cmd == "check":
        problems = check(args.repo, args.branch)
        if problems:
            print("results.json needs fixing:\n" + "\n".join(problems), file=sys.stderr)
            return 2
        counts = status(args.repo, args.branch)["counts"] or {}
        print(
            f"✓ results.json is valid: {counts.get('pass', 0)} passed, {counts.get('fail', 0)} failed, "
            f"{counts.get('skipped', 0)} skipped."
        )
        return 0
    if args.cmd == "take-gif":
        print(take_gif(args.repo, args.branch, args.name, Path(args.downloads), args.max_age))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
