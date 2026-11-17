#!/usr/bin/env python3
"""Helper for Step 3.5 (Fresh-Eyes Semantic Concept & Link Sweep).

Subcommands:
  1. `dump --plan plan.json`
     Outputs a compact JSON digest containing ONLY `concepts[]` and numbered/path-keyed
     prose strings (`prose_slots`) — zero diff hunks or screenshot coordinates — so a
     fresh-eyes subagent/pass can quickly spot missing task concepts and add semantic
     `[[concept-id|natural phrase]]` links wherever a concept is talked about.

  2. `apply --plan plan.json --patch concept-links.json`
     Merges `new_concepts`, validates every `[[concept-id|...]]` reference and slot path
     in `rewrites`, updates `plan.json` without touching hunk slices or visual coordinates,
     and marks `"concept_sweep_completed": true`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

BRACKET_CONCEPT_RE = re.compile(r"\[\[([^\]]+)\]\]")
SLOT_PATH_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)(?:\[(\d+)\])?$")


def normalize_concept_id(raw: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(raw).strip().lower()).strip("-")


def collect_prose_slots(plan: dict[str, Any]) -> dict[str, str]:
    """Collect every user-facing prose string in `plan.json` keyed by exact path."""
    slots: dict[str, str] = {}

    ov = plan.get("overview")
    if isinstance(ov, dict):
        for k in ("whole_task", "tldr", "global_context", "this_pr_part", "before_state", "after_state"):
            val = ov.get(k)
            if isinstance(val, str) and val.strip():
                # Avoid duplicate dump when whole_task and tldr hold the exact same string
                if k == "tldr" and isinstance(ov.get("whole_task"), str) and ov["whole_task"].strip():
                    continue
                slots[f"overview.{k}"] = val
        for v_i, vis in enumerate(ov.get("visuals") or []):
            if isinstance(vis, dict) and isinstance(vis.get("caption"), str) and vis["caption"].strip():
                slots[f"overview.visuals[{v_i}].caption"] = vis["caption"]

    for c_i, ch in enumerate(plan.get("chapters") or []):
        if isinstance(ch, dict) and isinstance(ch.get("summary"), str) and ch["summary"].strip():
            slots[f"chapters[{c_i}].summary"] = ch["summary"]

    for s_i, st in enumerate(plan.get("steps") or []):
        if not isinstance(st, dict):
            continue
        for k in ("title", "explanation", "why_now"):
            val = st.get(k)
            if isinstance(val, str) and val.strip():
                slots[f"steps[{s_i}].{k}"] = val

        tbl = st.get("table")
        if isinstance(tbl, dict) and isinstance(tbl.get("rows"), list):
            for r_i, row in enumerate(tbl["rows"]):
                if isinstance(row, list):
                    for col_i, cell in enumerate(row):
                        if isinstance(cell, str) and cell.strip():
                            slots[f"steps[{s_i}].table.rows[{r_i}][{col_i}]"] = cell

        for p_i, pt in enumerate(st.get("points") or []):
            if isinstance(pt, dict):
                if isinstance(pt.get("text"), str) and pt["text"].strip():
                    slots[f"steps[{s_i}].points[{p_i}].text"] = pt["text"]
            elif isinstance(pt, str) and pt.strip():
                slots[f"steps[{s_i}].points[{p_i}]"] = pt

        for h_i, hl in enumerate(st.get("diff_highlights") or []):
            if isinstance(hl, dict) and isinstance(hl.get("explanation"), str) and hl["explanation"].strip():
                slots[f"steps[{s_i}].diff_highlights[{h_i}].explanation"] = hl["explanation"]

        for v_i, vis in enumerate(st.get("visuals") or []):
            if isinstance(vis, dict) and isinstance(vis.get("caption"), str) and vis["caption"].strip():
                slots[f"steps[{s_i}].visuals[{v_i}].caption"] = vis["caption"]

        for co_i, co in enumerate(st.get("callouts") or []):
            if isinstance(co, dict) and isinstance(co.get("text"), str) and co["text"].strip():
                slots[f"steps[{s_i}].callouts[{co_i}].text"] = co["text"]

        for g_i, gap in enumerate(st.get("untested_gaps") or []):
            if isinstance(gap, str) and gap.strip():
                slots[f"steps[{s_i}].untested_gaps[{g_i}]"] = gap

        hnotes = st.get("hunk_notes")
        if isinstance(hnotes, dict):
            for hk, hv in hnotes.items():
                if isinstance(hv, str) and hv.strip():
                    slots[f"steps[{s_i}].hunk_notes.{hk}"] = hv

    mech = plan.get("mechanical")
    if isinstance(mech, dict):
        for g_i, grp in enumerate(mech.get("groups") or []):
            if isinstance(grp, dict) and isinstance(grp.get("reason"), str) and grp["reason"].strip():
                slots[f"mechanical.groups[{g_i}].reason"] = grp["reason"]

    return slots


def set_slot_value(plan: dict[str, Any], path_expr: str, new_text: str) -> None:
    """Assign `new_text` at `path_expr` (e.g. `steps[1].table.rows[0][1]` or `steps[0].hunk_notes.H001`)."""
    if ".hunk_notes." in path_expr:
        prefix, hk = path_expr.split(".hunk_notes.", 1)
        m = re.fullmatch(r"steps\[(\d+)\]", prefix)
        if not m:
            raise KeyError(f"Unsupported hunk_notes slot path `{path_expr}`.")
        plan["steps"][int(m.group(1))]["hunk_notes"][hk] = new_text
        return

    parts = path_expr.split(".")
    cur: Any = plan
    for idx, part in enumerate(parts):
        is_last = idx == len(parts) - 1
        # Support 2D index like `rows[0][1]`
        m2 = re.fullmatch(r"([a-zA-Z_][a-zA-Z0-9_]*)\[(\d+)\]\[(\d+)\]", part)
        if m2:
            key, r_i, c_i = m2.group(1), int(m2.group(2)), int(m2.group(3))
            if not is_last:
                cur = cur[key][r_i][c_i]
            else:
                cur[key][r_i][c_i] = new_text
            continue

        m1 = SLOT_PATH_RE.fullmatch(part)
        if not m1:
            raise KeyError(f"Invalid slot path segment `{part}` in `{path_expr}`.")
        key = m1.group(1)
        arr_idx = int(m1.group(2)) if m1.group(2) is not None else None
        if arr_idx is not None:
            if is_last:
                cur[key][arr_idx] = new_text
            else:
                cur = cur[key][arr_idx]
        else:
            if is_last:
                cur[key] = new_text
                if path_expr == "overview.whole_task" and "tldr" in cur:
                    cur["tldr"] = new_text
            else:
                cur = cur[key]


def cmd_dump(plan_path: Path) -> int:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    concepts_summary = [
        {
            "id": normalize_concept_id(c.get("id") or c.get("term") or ""),
            "term": c.get("term"),
            "origin": c.get("origin", "existing"),
            "aliases": c.get("aliases") or [],
            "what_it_is": c.get("what_it_is"),
        }
        for c in (plan.get("concepts") or [])
        if isinstance(c, dict)
    ]
    step_ids = [st.get("id") for st in (plan.get("steps") or []) if isinstance(st, dict)]
    payload = {
        "valid_step_ids": step_ids,
        "concepts": concepts_summary,
        "prose_slots": collect_prose_slots(plan),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_apply(plan_path: Path, patch_path: Path) -> int:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    if not isinstance(patch, dict):
        print("ERROR: Patch JSON must be an object with optional `new_concepts` and `rewrites`.", file=sys.stderr)
        return 2

    concepts: list[dict[str, Any]] = list(plan.get("concepts") or [])
    known_ids: dict[str, dict[str, Any]] = {}
    for c in concepts:
        if isinstance(c, dict):
            cid = normalize_concept_id(c.get("id") or c.get("term") or "")
            c["id"] = cid
            known_ids[cid] = c

    new_concepts = patch.get("new_concepts") or []
    if not isinstance(new_concepts, list):
        print("ERROR: `new_concepts` in patch must be a list.", file=sys.stderr)
        return 2

    for nc in new_concepts:
        if not isinstance(nc, dict):
            print("ERROR: Each entry in `new_concepts` must be an object.", file=sys.stderr)
            return 2
        cid = normalize_concept_id(nc.get("id") or nc.get("term") or "")
        if not cid or not str(nc.get("term") or "").strip() or not str(nc.get("what_it_is") or "").strip():
            print(f"ERROR: New concept `{cid}` requires `id`, `term`, and `what_it_is`.", file=sys.stderr)
            return 2
        nc["id"] = cid
        nc.setdefault("origin", "introduced")
        if cid in known_ids:
            known_ids[cid].update(nc)
        else:
            concepts.append(nc)
            known_ids[cid] = nc

    plan["concepts"] = concepts

    valid_slots = collect_prose_slots(plan)
    rewrites = patch.get("rewrites") or {}
    if not isinstance(rewrites, dict):
        print("ERROR: `rewrites` in patch must be an object mapping slot paths to strings.", file=sys.stderr)
        return 2

    errors: list[str] = []
    for slot_key, new_val in rewrites.items():
        if slot_key not in valid_slots:
            errors.append(f"Unknown prose slot `{slot_key}`. Run `concept_sweep.py dump` to see exact slot keys.")
            continue
        if not isinstance(new_val, str) or not new_val.strip():
            errors.append(f"Rewrite for `{slot_key}` must be a non-empty string.")
            continue
        for m in BRACKET_CONCEPT_RE.finditer(new_val):
            raw_ref = m.group(1).strip()
            ref_id = normalize_concept_id(raw_ref.split("|", 1)[0])
            if ref_id not in known_ids:
                errors.append(
                    f"Slot `{slot_key}` links unknown concept `[[{raw_ref}]]` "
                    f"(known concept IDs: {', '.join(sorted(known_ids))})."
                )

    if errors:
        print("ERROR: Semantic concept patch failed validation:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    for slot_key, new_val in rewrites.items():
        set_slot_value(plan, slot_key, new_val)

    plan["concept_sweep_completed"] = True
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"✓ Applied semantic concept sweep ({len(new_concepts)} new concept(s), "
        f"{len(rewrites)} rewritten text slot(s), {len(concepts)} total concept(s))."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump or apply semantic concept links on plan.json.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dump = sub.add_parser("dump", help="Dump concepts and prose slots from plan.json.")
    p_dump.add_argument("--plan", required=True, type=Path)

    p_apply = sub.add_parser("apply", help="Apply semantic concept patch onto plan.json.")
    p_apply.add_argument("--plan", required=True, type=Path)
    p_apply.add_argument("--patch", required=True, type=Path)

    args = parser.parse_args()
    if args.cmd == "dump":
        return cmd_dump(args.plan)
    return cmd_apply(args.plan, args.patch)


if __name__ == "__main__":
    raise SystemExit(main())
