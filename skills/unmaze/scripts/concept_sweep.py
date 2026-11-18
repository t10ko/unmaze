#!/usr/bin/env python3
"""Two-Sweep Concept Extraction, Grounded Explanation & Semantic Linking Pipeline.

Enforces a strict two-sweep separation so concepts are spotted with cold reviewer eyes
from the written prose, filtered down to real concepts worth explaining, and then
explained accurately using actual repository/diff context:

  Sweep 1 — Cold-Reader Candidate Extraction & Filtering (prose only, zero code bias):
    1. `dump-prose --plan plan.json`
       Dumps ONLY the written prose slots (`prose_slots`) — no code diffs or existing
       explanations — for a cold-reader pass.
    2. `save-candidates --plan plan.json --candidates concept-candidates.json`
       Validates extracted candidates (`keep: true | false`), drops generic tech noise
       (`keep: false`), records the confirmed shortlist on `plan.json`, and prints the
       confirmed concepts + `prose_slots` ready for Sweep 2.

  Sweep 2 — Repo-Grounded Explanation & Semantic Link Tagging (with codebase access):
    3. `apply --plan plan.json --patch concept-links.json`
       Verifies Sweep 1 ran first, verifies every confirmed `keep: true` concept has a
       grounded explanation (`what_it_is` + `how_it_works`), validates all semantic
       `[[concept-id|natural phrase]]` links across `rewrites`, and marks both
       `concept_candidates_extracted` and `concept_sweep_completed` as `true`.
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


# "the report", "this key", "it": a determiner plus at most one word says nothing on its own,
# so a dotted link on it is noise. Name the thing instead, or leave the phrase unlinked.
VAGUE_LINK_PHRASE_RE = re.compile(
    r"^(?:(?:the|this|that|these|those|its|it's|our|their|a|an)\s+\S+|it|they|them|this|that)$",
    re.IGNORECASE,
)


def normalize_concept_id(raw: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(raw).strip().lower()).strip("-")


def normalize_origin(raw: Any) -> str:
    val = str(raw or "existing").strip().lower()
    return "introduced" if val in {"introduced", "new", "this_pr", "task", "added"} else "existing"


def collect_prose_slots(plan: dict[str, Any]) -> dict[str, str]:
    """Collect every user-facing prose string in `plan.json` keyed by exact path."""
    slots: dict[str, str] = {}

    ov = plan.get("overview")
    if isinstance(ov, dict):
        for k in ("whole_task", "tldr", "global_context", "this_pr_part", "before_state", "after_state"):
            val = ov.get(k)
            if isinstance(val, str) and val.strip():
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


def cmd_dump_prose(plan_path: Path) -> int:
    """Sweep 1 input: dump ONLY prose slots (zero code diffs or pre-baked explanations)."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    step_ids = [st.get("id") for st in (plan.get("steps") or []) if isinstance(st, dict)]
    seeded_terms = [
        {
            "id": normalize_concept_id(c.get("id") or c.get("term") or ""),
            "term": c.get("term"),
            "origin": normalize_origin(c.get("origin")),
        }
        for c in (plan.get("concepts") or [])
        if isinstance(c, dict) and str(c.get("term") or "").strip()
    ]
    payload: dict[str, Any] = {
        "valid_step_ids": step_ids,
        "seeded_terms": seeded_terms,
        "prose_slots": collect_prose_slots(plan),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_save_candidates(plan_path: Path, candidates_path: Path) -> int:
    """Sweep 1 output validator: filter raw prose candidates down to confirmed concepts for Sweep 2."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    cand_doc = json.loads(candidates_path.read_text(encoding="utf-8"))
    if not isinstance(cand_doc, dict):
        print(
            "ERROR: Candidates JSON must be an object containing `candidates` (array of {id, term, origin, keep, reason}).",
            file=sys.stderr,
        )
        return 2

    raw_list = cand_doc.get("candidates")
    if raw_list is None:
        raw_list = cand_doc.get("extracted_candidates")
    if not isinstance(raw_list, list) or not raw_list:
        print(
            "ERROR: `candidates` must be a non-empty array extracted from the written prose.",
            file=sys.stderr,
        )
        return 2

    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    seen_ids: set[str] = set()
    errors: list[str] = []

    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            errors.append(f"`candidates[{idx}]` must be an object.")
            continue
        term = str(item.get("term") or "").strip()
        cid = normalize_concept_id(item.get("id") or term)
        if not cid or not term:
            errors.append(f"`candidates[{idx}]` requires `term` (and valid `id`).")
            continue
        if "keep" not in item or not isinstance(item.get("keep"), bool):
            errors.append(
                f"Candidate `{term}` (`{cid}`) must explicitly set boolean `\"keep\": true` or `\"keep\": false`."
            )
            continue
        if cid in seen_ids:
            continue
        seen_ids.add(cid)

        if item["keep"]:
            kept.append(
                {
                    "id": cid,
                    "term": term,
                    "origin": normalize_origin(item.get("origin")),
                    "aliases": [str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()],
                    "why_explain": str(item.get("reason") or item.get("why_explain") or "").strip(),
                    "sample_mentions": [str(m).strip() for m in (item.get("sample_mentions") or []) if str(m).strip()],
                }
            )
        else:
            dropped.append(term)

    no_concepts_just = str(cand_doc.get("no_concepts_justification") or plan.get("no_concepts_justification") or "").strip()
    if not kept and len(no_concepts_just) < 15:
        errors.append(
            "Sweep 1 kept zero concepts without a `no_concepts_justification` (>= 15 chars)."
        )

    if errors:
        print("ERROR: Candidate extraction validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    plan["concept_candidates_extracted"] = True
    plan["concept_sweep_completed"] = False
    plan["_confirmed_candidates"] = kept
    if no_concepts_just:
        plan["no_concepts_justification"] = no_concepts_just

    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {
        "status": "sweep_1_complete",
        "kept_count": len(kept),
        "dropped_noise": dropped,
        "confirmed_concepts_to_explain_in_sweep_2": kept,
        "valid_step_ids": [st.get("id") for st in (plan.get("steps") or []) if isinstance(st, dict)],
        "prose_slots": collect_prose_slots(plan),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cmd_apply(plan_path: Path, patch_path: Path) -> int:
    """Sweep 2 validator & applier: enforce Sweep 1 ran, enforce grounded explanations, apply semantic links."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not bool(plan.get("concept_candidates_extracted")):
        print(
            "ERROR: Sweep 1 has not run yet (`concept_candidates_extracted` is not true)!\n"
            "  Run Sweep 1 first to extract & filter candidates from the written prose:\n"
            "    1. `concept_sweep.py dump-prose --plan plan.json`\n"
            "    2. `concept_sweep.py save-candidates --plan plan.json --candidates concept-candidates.json`",
            file=sys.stderr,
        )
        return 2

    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    if not isinstance(patch, dict):
        print("ERROR: Sweep 2 patch JSON must be an object with `concepts` (or `explained_concepts`) and `rewrites`.", file=sys.stderr)
        return 2

    # Merge existing concepts + Sweep 2 explained concepts
    existing_by_id: dict[str, dict[str, Any]] = {}
    for c in plan.get("concepts") or []:
        if isinstance(c, dict):
            cid = normalize_concept_id(c.get("id") or c.get("term") or "")
            if cid:
                c["id"] = cid
                existing_by_id[cid] = c

    incoming_list = patch.get("concepts")
    if incoming_list is None:
        incoming_list = patch.get("explained_concepts")
    if incoming_list is None:
        incoming_list = patch.get("new_concepts") or []
    if not isinstance(incoming_list, list):
        print("ERROR: `concepts` in Sweep 2 patch must be a list.", file=sys.stderr)
        return 2

    errors: list[str] = []
    for idx, c_item in enumerate(incoming_list):
        if not isinstance(c_item, dict):
            errors.append(f"`concepts[{idx}]` in Sweep 2 patch must be an object.")
            continue
        cid = normalize_concept_id(c_item.get("id") or c_item.get("term") or "")
        if not cid:
            errors.append(f"`concepts[{idx}]` is missing `id` / `term`.")
            continue
        c_item["id"] = cid
        c_item["origin"] = normalize_origin(c_item.get("origin") or existing_by_id.get(cid, {}).get("origin"))
        if cid in existing_by_id:
            merged = dict(existing_by_id[cid])
            merged.update(c_item)
            existing_by_id[cid] = merged
        else:
            existing_by_id[cid] = c_item

    # Verify EVERY confirmed `keep: true` candidate from Sweep 1 was actually explained!
    confirmed = plan.get("_confirmed_candidates") or []
    ordered_concepts: list[dict[str, Any]] = []
    seen_ordered: set[str] = set()

    for cand in confirmed:
        cid = cand["id"]
        explained = existing_by_id.get(cid)
        if not explained:
            errors.append(
                f"Confirmed candidate `{cand['term']}` (`{cid}`) from Sweep 1 is missing its explanation in Sweep 2!"
            )
            continue
        explained.setdefault("term", cand["term"])
        explained["origin"] = normalize_origin(explained.get("origin") or cand.get("origin"))
        if cand.get("aliases") and not explained.get("aliases"):
            explained["aliases"] = cand["aliases"]
        if not str(explained.get("what_it_is") or "").strip():
            errors.append(f"Concept `{cid}` is missing `what_it_is` (short casual definition).")
        how_list = [str(x).strip() for x in (explained.get("how_it_works") or []) if str(x).strip()]
        if not how_list:
            errors.append(
                f"Concept `{cid}` is missing `how_it_works` bullets grounded in the codebase/diff."
            )
        ordered_concepts.append(explained)
        seen_ordered.add(cid)

    # Keep any extra explained concepts in deterministic order
    for cid, c_obj in existing_by_id.items():
        if cid not in seen_ordered:
            if not str(c_obj.get("what_it_is") or "").strip():
                errors.append(f"Concept `{cid}` is missing `what_it_is`.")
            ordered_concepts.append(c_obj)
            seen_ordered.add(cid)

    known_ids = {c["id"]: c for c in ordered_concepts}
    valid_slots = collect_prose_slots(plan)
    rewrites = patch.get("rewrites") or {}
    if not isinstance(rewrites, dict):
        errors.append("`rewrites` in Sweep 2 patch must be an object mapping slot paths to strings.")
        rewrites = {}

    for slot_key, new_val in rewrites.items():
        if slot_key not in valid_slots:
            errors.append(f"Unknown prose slot `{slot_key}`. Check `prose_slots` from Sweep 1.")
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
                    f"(confirmed concept IDs: {', '.join(sorted(known_ids))})."
                )
            phrase = raw_ref.split("|", 1)[1].strip().strip("`") if "|" in raw_ref else ""
            if phrase and VAGUE_LINK_PHRASE_RE.match(phrase):
                errors.append(
                    f"Slot `{slot_key}` links the vague phrase `{phrase}` to `{ref_id}`. Link a phrase that "
                    f"names the thing (e.g. `the Impact Report tab`, `frees the lock`) or leave it as plain text."
                )

    if errors:
        print("ERROR: Sweep 2 (Grounded Explanation & Semantic Linking) failed validation:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    plan["concepts"] = ordered_concepts
    for slot_key, new_val in rewrites.items():
        set_slot_value(plan, slot_key, new_val)

    plan.pop("_confirmed_candidates", None)
    plan["concept_candidates_extracted"] = True
    plan["concept_sweep_completed"] = True
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"✓ Completed Sweep 2 ({len(ordered_concepts)} repo-grounded concept(s), "
        f"{len(rewrites)} semantically linked text slot(s))."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Two-sweep concept extraction, repo-grounded explanation, and semantic linking."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    for alias in ("dump-prose", "dump"):
        p_dump = sub.add_parser(alias, help="Sweep 1 input: dump written prose slots from plan.json.")
        p_dump.add_argument("--plan", required=True, type=Path)

    p_cand = sub.add_parser(
        "save-candidates",
        help="Sweep 1 output: validate & filter extracted concept candidates (`keep: true | false`).",
    )
    p_cand.add_argument("--plan", required=True, type=Path)
    p_cand.add_argument("--candidates", required=True, type=Path)

    p_apply = sub.add_parser(
        "apply",
        help="Sweep 2 output: validate repo-grounded explanations + semantic `[[concept-id|phrase]]` rewrites.",
    )
    p_apply.add_argument("--plan", required=True, type=Path)
    p_apply.add_argument("--patch", required=True, type=Path)

    args = parser.parse_args()
    if args.cmd in {"dump-prose", "dump"}:
        return cmd_dump_prose(args.plan)
    if args.cmd == "save-candidates":
        return cmd_save_candidates(args.plan, args.candidates)
    return cmd_apply(args.plan, args.patch)


if __name__ == "__main__":
    raise SystemExit(main())
