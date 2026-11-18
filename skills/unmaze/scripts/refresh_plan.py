#!/usr/bin/env python3
"""Carry an existing unmaze plan over to a newer diff of the same PR or branch.

New commits renumber hunks (`H007` becomes `H009`) and shift line numbers, so a plan written for the
old diff no longer claims the right lines. This script matches every changed line of the old diff to
the new diff (per file, in order, by content) and rewrites `hunk_ids`, line slices and
`diff_highlights` so each step keeps exactly the lines it had.

New lines that land inside a hunk a step already owns join that step automatically. Lines in brand-new
hunks stay unclaimed: `build_artifact.py` will list them, and the reviewing agent places them.

Usage:
    python3 refresh_plan.py --old-manifest prev.json --new-manifest diff-manifest.json \\
        --plan plan.json --out plan.json
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_artifact import (  # noqa: E402
    compute_hunk_changed_anchors,
    match_slice_rows,
    parse_hunk_spec,
    parse_line_ranges,
)

Row = tuple[str, int]  # (hunk_id, row_index)


def changed_rows_by_file(manifest: dict[str, Any]) -> tuple[dict[str, list[Row]], dict[str, dict[int, int]]]:
    """Every changed row per file in diff order, plus each hunk's `{row_idx: anchor_line}` map."""
    anchors = {h["id"]: compute_hunk_changed_anchors(h) for h in manifest["hunks"]}
    hunk_map = {h["id"]: h for h in manifest["hunks"]}
    per_file: dict[str, list[Row]] = {}
    for f in manifest["files"]:
        rows: list[Row] = []
        for hid in f["hunk_ids"]:
            rows.extend((hid, r_idx) for r_idx in sorted(anchors[hid]))
        per_file[f["path"]] = rows
    for hid, h in hunk_map.items():
        per_file.setdefault(h["file_path"], [])
    return per_file, anchors


def row_key(manifest_hunks: dict[str, dict[str, Any]], row: Row) -> tuple[str, str]:
    hunk = manifest_hunks[row[0]]
    lines = hunk.get("lines") or []
    if hunk.get("is_synthetic") or row[1] >= len(lines):
        return ("synthetic", hunk.get("file_status") or "")
    ln = lines[row[1]]
    return (ln["kind"], ln["content"])


def collapse_ranges(values: list[int]) -> str:
    nums = sorted(set(values))
    parts: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = n
    parts.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(parts)


def refresh_plan(
    old_manifest: dict[str, Any], new_manifest: dict[str, Any], plan: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = copy.deepcopy(plan)
    old_hunks = {h["id"]: h for h in old_manifest["hunks"]}
    new_hunks = {h["id"]: h for h in new_manifest["hunks"]}
    old_rows_by_file, old_anchors = changed_rows_by_file(old_manifest)
    new_rows_by_file, new_anchors = changed_rows_by_file(new_manifest)
    new_hunk_order = {h["id"]: i for i, h in enumerate(new_manifest["hunks"])}

    # 1. Who owned each old changed row (steps and mechanical groups), and in what spec order.
    owners: list[tuple[str, dict[str, Any]]] = []
    for st in plan.get("steps") or []:
        owners.append((str(st.get("id")), st))
    for g_idx, grp in enumerate((plan.get("mechanical") or {}).get("groups") or []):
        owners.append((f"mechanical[{g_idx}]", grp))

    old_owner: dict[Row, str] = {}
    owner_old_specs: dict[str, list[list[Row]]] = {}
    for label, obj in owners:
        owner_old_specs[label] = []
        for raw in obj.get("hunk_ids") or []:
            try:
                hid, ranges = parse_hunk_spec(str(raw))
            except ValueError:
                continue
            if hid not in old_anchors:
                continue
            rows = [(hid, r) for r in match_slice_rows(old_anchors[hid], ranges)]
            for row in rows:
                old_owner.setdefault(row, label)
            owner_old_specs[label].append(rows)

    # 2. Match old changed rows to new changed rows, per file, by content and order.
    old_to_new: dict[Row, Row] = {}
    for path in set(old_rows_by_file) | set(new_rows_by_file):
        old_rows = old_rows_by_file.get(path, [])
        new_rows = new_rows_by_file.get(path, [])
        if not old_rows or not new_rows:
            continue
        matcher = difflib.SequenceMatcher(
            None,
            [row_key(old_hunks, r) for r in old_rows],
            [row_key(new_hunks, r) for r in new_rows],
            autojunk=False,
        )
        for block in matcher.get_matching_blocks():
            for k in range(block.size):
                old_to_new[old_rows[block.a + k]] = new_rows[block.b + k]

    new_owner: dict[Row, str] = {}
    for old_row, label in old_owner.items():
        if old_row in old_to_new:
            new_owner[old_to_new[old_row]] = label
    dropped_lines = sum(1 for row in old_owner if row not in old_to_new)

    # 3. New rows inside a hunk someone already owns join the nearest owner in that hunk.
    auto_assigned: dict[str, int] = {}
    unclaimed: list[dict[str, Any]] = []
    for hid, h in new_hunks.items():
        rows = [(hid, r) for r in sorted(new_anchors[hid])]
        owned = [new_owner.get(row) for row in rows]
        if not any(owned):
            unclaimed.append(
                {
                    "hunk_id": hid,
                    "file": h["file_path"],
                    "lines": collapse_ranges(list(new_anchors[hid].values())),
                    "status": h.get("file_status"),
                }
            )
            continue
        for i, row in enumerate(rows):
            if owned[i]:
                continue
            before = next((owned[j] for j in range(i - 1, -1, -1) if owned[j]), None)
            after = next((owned[j] for j in range(i + 1, len(rows)) if owned[j]), None)
            label = before or after
            new_owner[row] = label
            auto_assigned[label] = auto_assigned.get(label, 0) + 1

    # 4. Rows sharing one anchor line can't be split by a line slice: the first row's owner keeps them.
    merged_rows = 0
    for hid in new_hunks:
        first_owner_by_anchor: dict[int, str] = {}
        for r_idx in sorted(new_anchors[hid]):
            row = (hid, r_idx)
            if row not in new_owner:
                continue
            anchor = new_anchors[hid][r_idx]
            keeper = first_owner_by_anchor.setdefault(anchor, new_owner[row])
            if new_owner[row] != keeper:
                new_owner[row] = keeper
                merged_rows += 1

    # 5. Rewrite each owner's `hunk_ids`, keeping the old card order where possible.
    rows_by_owner_hunk: dict[str, dict[str, list[int]]] = {}
    for (hid, r_idx), label in new_owner.items():
        rows_by_owner_hunk.setdefault(label, {}).setdefault(hid, []).append(r_idx)

    touched_steps: list[str] = []
    empty_owners: list[str] = []
    for label, obj in owners:
        owned_hunks = rows_by_owner_hunk.get(label, {})
        ordered: list[str] = []
        for old_spec_rows in owner_old_specs.get(label, []):
            targets = sorted(
                {old_to_new[r][0] for r in old_spec_rows if r in old_to_new and new_owner.get(old_to_new[r]) == label},
                key=lambda h: new_hunk_order[h],
            )
            for hid in targets:
                if hid not in ordered:
                    ordered.append(hid)
        for hid in sorted(owned_hunks, key=lambda h: new_hunk_order[h]):
            if hid not in ordered:
                ordered.append(hid)

        new_specs: list[str] = []
        for hid in ordered:
            mine = owned_hunks.get(hid, [])
            if not mine:
                continue
            if len(mine) == len(new_anchors[hid]):
                new_specs.append(hid)
            else:
                new_specs.append(f"{hid}:{collapse_ranges([new_anchors[hid][r] for r in mine])}")

        old_count = sum(1 for lbl in old_owner.values() if lbl == label)
        new_count = sum(len(v) for v in owned_hunks.values())
        lost = sum(1 for row, lbl in old_owner.items() if lbl == label and row not in old_to_new)
        if not new_specs:
            empty_owners.append(label)
        if lost or new_count != old_count - lost or auto_assigned.get(label):
            touched_steps.append(label)
        obj["hunk_ids"] = new_specs

    # 6. Move `diff_highlights` along with their lines (dropped if their changed lines are gone).
    dropped_highlights: list[str] = []
    for label, obj in owners:
        hls = obj.get("diff_highlights")
        if not isinstance(hls, list):
            continue
        kept: list[dict[str, Any]] = []
        for hl in hls:
            old_hid = str(hl.get("hunk_id") or "")
            if old_hid not in old_anchors:
                dropped_highlights.append(f"{label}: {hl.get('title') or old_hid}")
                continue
            try:
                ranges = parse_line_ranges(str(hl.get("lines") or ""), "diff_highlight")
            except ValueError:
                dropped_highlights.append(f"{label}: {hl.get('title') or old_hid}")
                continue
            old_rows = [(old_hid, r) for r in match_slice_rows(old_anchors[old_hid], ranges)]
            mapped = [old_to_new[r] for r in old_rows if r in old_to_new]
            new_hids = {r[0] for r in mapped}
            if not mapped or len(new_hids) != 1:
                dropped_highlights.append(f"{label}: {hl.get('title') or old_hid}")
                continue
            new_hid = new_hids.pop()
            first_old = next(r for r in old_rows if r in old_to_new)
            first_new = old_to_new[first_old]
            delta = new_anchors[new_hid][first_new[1]] - old_anchors[first_old[0]][first_old[1]]
            lo = min(r[0] for r in ranges) + delta
            hi = max(r[1] for r in ranges) + delta
            kept.append({**hl, "hunk_id": new_hid, "lines": f"{max(1, lo)}-{max(1, hi)}" if hi > lo else str(max(1, lo))})
        obj["diff_highlights"] = kept

    total_old = len(old_owner)
    report = {
        "old_head": (old_manifest.get("meta") or {}).get("head_sha"),
        "new_head": (new_manifest.get("meta") or {}).get("head_sha"),
        "carried_lines": total_old - dropped_lines,
        "total_old_lines": total_old,
        "dropped_lines": dropped_lines,
        "auto_assigned": auto_assigned,
        "merged_shared_anchor_rows": merged_rows,
        "unclaimed_hunks": unclaimed,
        "steps_to_reread": touched_steps,
        "empty_steps": empty_owners,
        "dropped_highlights": dropped_highlights,
    }
    return plan, report


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"Refresh: {str(report['old_head'] or '?')[:7]} -> {str(report['new_head'] or '?')[:7]}",
        f"- Carried over {report['carried_lines']}/{report['total_old_lines']} changed lines "
        f"({report['dropped_lines']} no longer in the diff).",
    ]
    if report["auto_assigned"]:
        joined = ", ".join(f"{k} (+{v})" for k, v in report["auto_assigned"].items())
        lines.append(f"- New lines joined the step that already owns their hunk: {joined}.")
    if report["unclaimed_hunks"]:
        lines.append("- UNCLAIMED new hunks (add each to a step or `mechanical` in plan.json):")
        for u in report["unclaimed_hunks"]:
            lines.append(f"    {u['hunk_id']}  {u['file']}  lines {u['lines']}  ({u['status']})")
    if report["steps_to_reread"]:
        lines.append(f"- Re-read and update the text of: {', '.join(report['steps_to_reread'])}.")
    if report["empty_steps"]:
        lines.append(f"- These lost every line (delete them or give them new lines): {', '.join(report['empty_steps'])}.")
    if report["dropped_highlights"]:
        lines.append(f"- Diff highlights dropped (their lines changed): {'; '.join(report['dropped_highlights'])}.")
    if report["merged_shared_anchor_rows"]:
        lines.append(
            f"- {report['merged_shared_anchor_rows']} rows shared a line number with another step's rows "
            f"and moved to that step."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Carry an unmaze plan over to a newer diff.")
    parser.add_argument("--old-manifest", required=True, help="Manifest the plan was written for")
    parser.add_argument("--new-manifest", required=True, help="Freshly parsed manifest")
    parser.add_argument("--plan", required=True, help="Existing plan.json")
    parser.add_argument("--out", required=True, help="Where to write the refreshed plan.json")
    parser.add_argument("--report-json", help="Optional path for the machine-readable report")
    args = parser.parse_args(argv)

    old_manifest = json.loads(Path(args.old_manifest).read_text(encoding="utf-8"))
    new_manifest = json.loads(Path(args.new_manifest).read_text(encoding="utf-8"))
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))

    refreshed, report = refresh_plan(old_manifest, new_manifest, plan)
    Path(args.out).write_text(json.dumps(refreshed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.report_json:
        Path(args.report_json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
