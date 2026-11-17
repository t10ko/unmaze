#!/usr/bin/env python3
"""Compile a validated, 100%-diff-accounted interactive HTML (and optional Markdown)
code review guide from `diff-manifest.json` + `plan.json`.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse


VALID_LAYERS = {
    "contracts": {
        "order": 1,
        "label": "Shapes & Contracts",
        "short": "Contracts",
        "symbol": "◆",
        "desc": "Data structures, schemas, types, and API boundaries",
    },
    "core": {
        "order": 2,
        "label": "Core Logic & Decisions",
        "short": "Core Logic",
        "symbol": "★",
        "desc": "Primary domain behavior, state transitions, and key tradeoffs",
    },
    "wiring": {
        "order": 3,
        "label": "Entrypoints & Wiring",
        "short": "Wiring",
        "symbol": "⇄",
        "desc": "Routes, dependency injection, CLI/UI bindings, and plumbing",
    },
    "verification": {
        "order": 4,
        "label": "Verification & Edge Cases",
        "short": "Verification",
        "symbol": "✓",
        "desc": "Automated tests, assertions, and remaining coverage gaps",
    },
}

VALID_CALLOUT_KINDS = {
    "decision": {"label": "Key Decision", "symbol": "◈"},
    "assumption": {"label": "AI Assumption to Verify", "symbol": "⚠"},
    "risk": {"label": "Risk / Subtlety", "symbol": "!"},
    "question": {"label": "Question for Author", "symbol": "?"},
}

VALID_NODE_STATUS = {
    "unchanged": {"label": "Context", "badge": "CTX"},
    "modified": {"label": "Modified", "badge": "MOD"},
    "added": {"label": "New", "badge": "NEW"},
    "removed": {"label": "Removed", "badge": "DEL"},
}


def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def safe_http_url(url: Any) -> str | None:
    if not url or not isinstance(url, str):
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme in {"https", "http"} and parsed.netloc:
        return url.strip()
    return None


class PlanValidationError(Exception):
    pass


def validate_and_normalize_plan(
    manifest: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    """Ensure 100% hunk accounting (no missing, unknown, or duplicate hunks) and valid schema."""
    hunk_map: dict[str, dict[str, Any]] = {h["id"]: h for h in manifest.get("hunks", [])}
    if not hunk_map:
        raise PlanValidationError("Manifest contains zero hunks.")

    errors: list[str] = []

    overview = plan.get("overview")
    if not isinstance(overview, dict):
        errors.append("`overview` object is required in plan.json.")
        overview = {}

    for req_field in ("tldr", "before_state", "after_state"):
        if not str(overview.get(req_field, "")).strip():
            errors.append(f"`overview.{req_field}` must be a non-empty string.")

    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("`steps` must be a non-empty array of review steps.")
        steps = []

    seen_step_ids: set[str] = set()
    assigned_where: dict[str, str] = {}

    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            errors.append(f"Step #{idx} must be an object.")
            continue
        sid = str(step.get("id") or f"step-{idx}").strip()
        step["id"] = sid
        if sid in seen_step_ids:
            errors.append(f"Duplicate step id `{sid}`.")
        seen_step_ids.add(sid)

        if not str(step.get("title", "")).strip():
            errors.append(f"Step `{sid}` is missing a `title`.")
        layer = str(step.get("layer", "")).strip().lower()
        if layer not in VALID_LAYERS:
            errors.append(
                f"Step `{sid}` has invalid `layer` `{layer}`. "
                f"Expected one of: {', '.join(VALID_LAYERS)}."
            )
        step["layer"] = layer

        hids = step.get("hunk_ids")
        if not isinstance(hids, list) or not hids:
            errors.append(f"Step `{sid}` must have a non-empty `hunk_ids` array.")
            continue

        for hid in hids:
            if hid not in hunk_map:
                errors.append(f"Step `{sid}` references unknown hunk ID `{hid}`.")
            elif hid in assigned_where:
                errors.append(
                    f"Duplicate hunk ID `{hid}` assigned in both `{assigned_where[hid]}` and `{sid}`."
                )
            else:
                assigned_where[hid] = sid

    mechanical = plan.get("mechanical") or {}
    if not isinstance(mechanical, dict):
        errors.append("`mechanical` must be an object if provided.")
        mechanical = {}

    mech_groups = mechanical.get("groups") or []
    if not isinstance(mech_groups, list):
        errors.append("`mechanical.groups` must be an array if provided.")
        mech_groups = []

    for g_idx, grp in enumerate(mech_groups, start=1):
        if not isinstance(grp, dict):
            errors.append(f"`mechanical.groups[{g_idx - 1}]` must be an object.")
            continue
        g_label = str(grp.get("title") or f"mechanical-{g_idx}").strip()
        grp["title"] = g_label
        g_hids = grp.get("hunk_ids") or []
        if not isinstance(g_hids, list) or not g_hids:
            errors.append(f"Mechanical group `{g_label}` must have a non-empty `hunk_ids` array.")
            continue
        for hid in g_hids:
            if hid not in hunk_map:
                errors.append(f"Mechanical group `{g_label}` references unknown hunk ID `{hid}`.")
            elif hid in assigned_where:
                errors.append(
                    f"Duplicate hunk ID `{hid}` assigned in both `{assigned_where[hid]}` "
                    f"and mechanical group `{g_label}`."
                )
            else:
                assigned_where[hid] = f"mechanical:{g_label}"

    missing = [hid for hid in hunk_map if hid not in assigned_where]
    if missing:
        missing_details = []
        for hid in missing:
            h = hunk_map[hid]
            missing_details.append(
                f"  - {hid}: {h['file_path']} (L{h['new_start']}, +{h['additions']}/-{h['deletions']}, hint={h['category_hint']})"
            )
        errors.append(
            f"100% Diff Coverage Check FAILED — {len(missing)} hunk(s) were not assigned to any step or mechanical group:\n"
            + "\n".join(missing_details)
        )

    arch = plan.get("architecture_map") or {}
    if isinstance(arch, dict):
        nodes = arch.get("nodes") or []
        node_ids: set[str] = set()
        for n in nodes:
            if not isinstance(n, dict) or not n.get("id"):
                errors.append("Every node in `architecture_map.nodes` must have an `id`.")
                continue
            nid = str(n["id"])
            node_ids.add(nid)
            status = str(n.get("status", "modified")).lower()
            if status not in VALID_NODE_STATUS:
                errors.append(
                    f"Architecture node `{nid}` has invalid status `{status}` "
                    f"(expected one of {', '.join(VALID_NODE_STATUS)})."
                )
            n["status"] = status
            for ref_step in n.get("steps") or []:
                if ref_step not in seen_step_ids:
                    errors.append(
                        f"Architecture node `{nid}` references unknown step `{ref_step}`."
                    )
        for edge in arch.get("edges") or []:
            if isinstance(edge, dict):
                if edge.get("from") not in node_ids or edge.get("to") not in node_ids:
                    errors.append(
                        f"Architecture edge `{edge.get('from')} -> {edge.get('to')}` references unknown node ID."
                    )

    if errors:
        raise PlanValidationError("\n".join(errors))

    return plan


def render_diff_hunk_html(
    hunk: dict[str, Any],
    *,
    note: str | None = None,
    default_open: bool = True,
) -> str:
    hid = hunk["id"]
    fpath = hunk["file_path"]
    status = hunk["file_status"].upper()
    if hunk["file_status"] == "renamed" and hunk["old_path"] != hunk["new_path"]:
        path_display = f"{esc(hunk['old_path'])} → {esc(hunk['new_path'])}"
    else:
        path_display = esc(fpath)

    loc_label = (
        f"L{hunk['new_start']}–{hunk['new_start'] + max(hunk['new_count'], 1) - 1}"
        if not hunk["is_synthetic"] and hunk["file_status"] != "deleted"
        else (f"old L{hunk['old_start']}" if not hunk["is_synthetic"] else "file")
    )
    deep_url = safe_http_url(hunk.get("deep_link"))
    link_html = (
        f'<a class="hunk-ext-link" href="{esc(deep_url)}" target="_blank" rel="noopener noreferrer">'
        f'Open in PR <span aria-hidden="true">↗</span></a>'
        if deep_url
        else ""
    )
    note_html = f'<p class="hunk-note">{esc(note)}</p>' if note else ""

    rows: list[str] = []
    for ln in hunk["lines"]:
        kind = ln["kind"]
        old_s = str(ln["old_line"]) if ln["old_line"] is not None else ""
        new_s = str(ln["new_line"]) if ln["new_line"] is not None else ""
        if kind == "add":
            sig = "+"
            cls = "dl-add"
        elif kind == "del":
            sig = "-"
            cls = "dl-del"
        elif kind == "ctx":
            sig = " "
            cls = "dl-ctx"
        else:
            sig = " "
            cls = "dl-meta"
        rows.append(
            f'<span class="dl {cls}">'
            f'<span class="ln" aria-hidden="true">{esc(old_s):>4}</span>'
            f'<span class="ln" aria-hidden="true">{esc(new_s):>4}</span>'
            f'<span class="sig" aria-hidden="true">{sig}</span>'
            f'<span class="code-txt">{esc(ln["content"])}</span>'
            f"</span>"
        )

    code_body = "\n".join(rows)
    caption_id = f"cap-{esc(hid)}"
    chk_id = f"chk-{esc(hid)}"
    open_attr = " open" if default_open else ""
    ctx_snippet = esc(hunk["section_context"] or hunk["header"])

    return f"""
<article class="hunk-card" id="hunk-{esc(hid)}" data-hunk-id="{esc(hid)}" data-file="{esc(fpath.lower())}">
  <div class="hunk-toolbar">
    <div class="hunk-meta">
      <span class="hunk-id-pill">{esc(hid)}</span>
      <code class="hunk-filepath" id="{caption_id}">{path_display}</code>
      <span class="status-pill status-{esc(hunk['file_status'])}">{esc(status)}</span>
      <span class="hunk-loc">{esc(loc_label)}</span>
      <span class="diff-stat"><span class="stat-add">+{hunk['additions']}</span> <span class="stat-del">−{hunk['deletions']}</span></span>
    </div>
    <div class="hunk-actions">
      {link_html}
      <label class="reviewed-toggle" for="{chk_id}">
        <input type="checkbox" id="{chk_id}" class="hunk-check" data-hunk="{esc(hid)}">
        <span>Reviewed</span>
      </label>
    </div>
  </div>
  {note_html}
  <details class="hunk-details"{open_attr}>
    <summary class="hunk-summary">
      <span class="summary-ctx">{ctx_snippet}</span>
    </summary>
    <pre class="diff-pre" role="region" aria-labelledby="{caption_id}" tabindex="0"><code>{code_body}</code></pre>
  </details>
</article>"""


def render_architecture_map_html(arch: dict[str, Any] | None) -> str:
    if not arch or not arch.get("nodes"):
        return ""
    nodes = arch.get("nodes") or []
    edges = arch.get("edges") or []

    node_labels = {str(n["id"]): str(n.get("label") or n["id"]) for n in nodes}

    node_cards: list[str] = []
    for n in nodes:
        nid = str(n["id"])
        label = str(n.get("label") or nid)
        role = str(n.get("role") or "")
        status = str(n.get("status") or "modified")
        status_info = VALID_NODE_STATUS[status]
        step_refs = [str(s) for s in (n.get("steps") or [])]
        steps_csv = ",".join(step_refs)
        first_step = step_refs[0] if step_refs else ""

        tag_open = (
            f'<button type="button" class="arch-node arch-{esc(status)}" '
            f'data-node-id="{esc(nid)}" data-steps="{esc(steps_csv)}" data-jump-step="{esc(first_step)}">'
            if first_step
            else f'<div class="arch-node arch-{esc(status)}" data-node-id="{esc(nid)}" data-steps="">'
        )
        tag_close = "</button>" if first_step else "</div>"
        step_hint = (
            f'<span class="arch-step-ref">→ {", ".join(esc(s) for s in step_refs)}</span>'
            if step_refs
            else '<span class="arch-step-ref">Unchanged anchor</span>'
        )
        node_cards.append(
            f"""{tag_open}
        <span class="arch-node-top">
          <span class="arch-badge arch-badge-{esc(status)}">{esc(status_info['badge'])}</span>
          <strong class="arch-node-title">{esc(label)}</strong>
        </span>
        {f'<span class="arch-node-role">{esc(role)}</span>' if role else ''}
        {step_hint}
      {tag_close}"""
        )

    edge_items: list[str] = []
    for e in edges:
        frm = node_labels.get(str(e.get("from")), str(e.get("from", "")))
        to = node_labels.get(str(e.get("to")), str(e.get("to", "")))
        lbl = str(e.get("label") or "").strip()
        lbl_html = f' <span class="edge-label">({esc(lbl)})</span>' if lbl else ""
        edge_items.append(
            f'<li class="arch-edge-item"><code>{esc(frm)}</code> <span aria-hidden="true">→</span> '
            f'<code>{esc(to)}</code>{lbl_html}</li>'
        )

    edges_block = (
        f'<ul class="arch-edges" aria-label="Component data flow">{"".join(edge_items)}</ul>'
        if edge_items
        else ""
    )

    return f"""
    <div class="arch-map" aria-label="System architecture map">
      <div class="arch-nodes-grid">
        {"".join(node_cards)}
      </div>
      {edges_block}
    </div>"""


def build_html_artifact(manifest: dict[str, Any], plan: dict[str, Any]) -> str:
    meta = manifest["meta"]
    stats = manifest["stats"]
    hunk_map: dict[str, dict[str, Any]] = {h["id"]: h for h in manifest["hunks"]}

    title = str(plan.get("title") or meta.get("title") or "PR Code Review Guide")
    pr_url = safe_http_url(meta.get("pr_url"))
    overview = plan["overview"]

    # Compute cognitive load breakdown
    layer_lines = {"contracts": 0, "core": 0, "wiring": 0, "verification": 0, "mechanical": 0}
    steps = plan["steps"]
    for st in steps:
        lyr = st["layer"]
        for hid in st["hunk_ids"]:
            h = hunk_map[hid]
            layer_lines[lyr] += h["additions"] + h["deletions"]

    mech_groups = (plan.get("mechanical") or {}).get("groups") or []
    mech_hunk_count = 0
    for grp in mech_groups:
        for hid in grp["hunk_ids"]:
            h = hunk_map[hid]
            layer_lines["mechanical"] += h["additions"] + h["deletions"]
            mech_hunk_count += 1

    high_focus_lines = layer_lines["contracts"] + layer_lines["core"]
    support_lines = layer_lines["wiring"] + layer_lines["verification"]
    mech_lines = layer_lines["mechanical"]

    # Build left navigation items & main step panels
    nav_items_html: list[str] = []
    step_panels_html: list[str] = []

    for idx, st in enumerate(steps, start=1):
        sid = st["id"]
        lyr = st["layer"]
        lyr_info = VALID_LAYERS[lyr]
        slice_tag = str(st.get("slice") or "").strip()
        slice_html = f'<span class="slice-pill">{esc(slice_tag)}</span>' if slice_tag else ""

        st_add = sum(hunk_map[hid]["additions"] for hid in st["hunk_ids"])
        st_del = sum(hunk_map[hid]["deletions"] for hid in st["hunk_ids"])
        hunk_csv = ",".join(st["hunk_ids"])

        is_first = idx == 1
        aria_cur = ' aria-current="step"' if is_first else ""
        active_cls = " is-active" if is_first else ""

        nav_items_html.append(
            f"""<li>
          <button type="button" class="step-nav-btn{active_cls}" data-step-target="{esc(sid)}" data-step-index="{idx - 1}"{aria_cur}>
            <span class="step-num">{idx}</span>
            <span class="step-nav-body">
              <span class="step-nav-layer layer-tag-{esc(lyr)}">{esc(lyr_info['symbol'])} {esc(lyr_info['short'])}</span>
              <span class="step-nav-title">{esc(st['title'])}</span>
              <span class="step-nav-stats">+{st_add}/−{st_del} · <span class="step-done-count" data-step-progress="{esc(sid)}">0/{len(st['hunk_ids'])}</span></span>
            </span>
          </button>
        </li>"""
        )

        # Callouts
        callouts_html_list: list[str] = []
        for co in st.get("callouts") or []:
            if not isinstance(co, dict) or not co.get("text"):
                continue
            ckind = str(co.get("kind") or "decision").lower()
            cinfo = VALID_CALLOUT_KINDS.get(ckind, VALID_CALLOUT_KINDS["decision"])
            callouts_html_list.append(
                f'<div class="callout callout-{esc(ckind)}">'
                f'<strong class="callout-badge"><span aria-hidden="true">{esc(cinfo["symbol"])}</span> {esc(cinfo["label"])}:</strong> '
                f'<span>{esc(co["text"])}</span></div>'
            )

        # Untested gaps (especially useful on verification steps)
        gaps = [str(g).strip() for g in (st.get("untested_gaps") or []) if str(g).strip()]
        gaps_html = ""
        if gaps:
            gap_lis = "".join(f"<li>{esc(g)}</li>" for g in gaps)
            gaps_html = (
                f'<div class="callout callout-risk">'
                f'<strong class="callout-badge"><span aria-hidden="true">!</span> Untested Scenarios / Coverage Gaps:</strong>'
                f'<ul class="gaps-list">{gap_lis}</ul></div>'
            )

        hunk_notes = st.get("hunk_notes") or {}
        hunks_rendered = "\n".join(
            render_diff_hunk_html(
                hunk_map[hid],
                note=hunk_notes.get(hid) if isinstance(hunk_notes, dict) else None,
                default_open=True,
            )
            for hid in st["hunk_ids"]
        )

        why_now = str(st.get("why_now") or "").strip()
        why_html = (
            f'<p class="why-now"><strong>Why read this now:</strong> {esc(why_now)}</p>'
            if why_now
            else ""
        )
        explanation = str(st.get("explanation") or "").strip()
        expl_html = f'<p class="step-explanation">{esc(explanation)}</p>' if explanation else ""

        prev_btn = (
            f'<button type="button" class="pager-btn" data-goto-index="{idx - 2}">← Step {idx - 1}</button>'
            if idx > 1
            else '<span></span>'
        )
        if idx < len(steps):
            next_btn = (
                f'<button type="button" class="pager-btn pager-btn-primary" data-goto-index="{idx}">'
                f'Next: Step {idx + 1} ({esc(VALID_LAYERS[steps[idx]["layer"]]["short"])}) →</button>'
            )
        elif mech_groups:
            next_btn = (
                '<button type="button" class="pager-btn pager-btn-primary" data-goto-step="step-mechanical">'
                "Next: Mechanical & Boilerplate →</button>"
            )
        else:
            next_btn = '<span class="complete-pill">✓ End of Guided Sequence</span>'

        hidden_attr = "" if is_first else ' hidden="until-found"'
        step_panels_html.append(
            f"""<section class="step-panel" id="{esc(sid)}" data-step-id="{esc(sid)}" data-step-index="{idx - 1}" data-hunks="{esc(hunk_csv)}" aria-labelledby="heading-{esc(sid)}"{hidden_attr}>
        <div class="step-card">
          <div class="step-header">
            <div class="step-eyebrow">
              <span class="step-counter">Step {idx} of {len(steps)}</span>
              <span class="layer-pill layer-{esc(lyr)}"><span aria-hidden="true">{esc(lyr_info['symbol'])}</span> {esc(lyr_info['label'])}</span>
              {slice_html}
            </div>
            <h2 id="heading-{esc(sid)}" class="step-title">{esc(st['title'])}</h2>
            {why_now and why_html}
            {expl_html}
            {"".join(callouts_html_list)}
            {gaps_html}
          </div>
          <div class="step-hunks">
            {hunks_rendered}
          </div>
          <div class="step-pager">
            {prev_btn}
            {next_btn}
          </div>
        </div>
      </section>"""
        )

    # Mechanical drawer section (if any)
    if mech_groups:
        all_mech_hids = [hid for grp in mech_groups for hid in grp["hunk_ids"]]
        nav_items_html.append(
            f"""<li>
          <button type="button" class="step-nav-btn" data-step-target="step-mechanical" data-step-index="{len(steps)}">
            <span class="step-num">⚙</span>
            <span class="step-nav-body">
              <span class="step-nav-layer layer-tag-mechanical">⚙ Mechanical</span>
              <span class="step-nav-title">Boilerplate & Noise ({mech_hunk_count} hunks)</span>
              <span class="step-nav-stats">{mech_lines} lines · <span class="step-done-count" data-step-progress="step-mechanical">0/{mech_hunk_count}</span></span>
            </span>
          </button>
        </li>"""
        )

        mech_group_blocks: list[str] = []
        for grp in mech_groups:
            g_title = grp["title"]
            g_reason = str(grp.get("reason") or "Mechanical / non-behavioral changes").strip()
            g_hunks_html = "\n".join(
                render_diff_hunk_html(hunk_map[hid], default_open=False)
                for hid in grp["hunk_ids"]
            )
            mech_group_blocks.append(
                f"""<div class="mech-group">
              <h3 class="mech-group-title">{esc(g_title)} <span class="mech-count">({len(grp['hunk_ids'])} hunks)</span></h3>
              <p class="mech-reason">{esc(g_reason)}</p>
              {g_hunks_html}
            </div>"""
            )

        step_panels_html.append(
            f"""<section class="step-panel" id="step-mechanical" data-step-id="step-mechanical" data-step-index="{len(steps)}" data-hunks="{esc(','.join(all_mech_hids))}" aria-labelledby="heading-mechanical" hidden="until-found">
        <div class="step-card">
          <div class="step-header">
            <div class="step-eyebrow">
              <span class="step-counter">Appendix</span>
              <span class="layer-pill layer-mechanical"><span aria-hidden="true">⚙</span> Mechanical & Boilerplate</span>
            </div>
            <h2 id="heading-mechanical" class="step-title">Mechanical Changes & Boilerplate (100% Diff Accounted)</h2>
            <p class="step-explanation">These {mech_hunk_count} hunks ({mech_lines} changed lines) were separated from the core reading flow so they don't drain your focus early. Every hunk from the raw diff is still preserved below for full auditability.</p>
          </div>
          <div class="step-hunks">
            {"".join(mech_group_blocks)}
          </div>
          <div class="step-pager">
            <button type="button" class="pager-btn" data-goto-index="{len(steps) - 1}">← Step {len(steps)}</button>
            <span class="complete-pill">✓ 100% of PR Diff Accounted ({stats['total_hunks']}/{stats['total_hunks']} hunks)</span>
          </div>
        </div>
      </section>"""
        )

    arch_html = render_architecture_map_html(plan.get("architecture_map"))
    global_ctx = str(overview.get("global_context") or "").strip()
    global_ctx_html = (
        f'<p class="global-ctx"><strong>Where we are in the codebase:</strong> {esc(global_ctx)}</p>'
        if global_ctx
        else ""
    )
    pr_header_link = (
        f'<a class="pr-ext-btn" href="{esc(pr_url)}" target="_blank" rel="noopener noreferrer">'
        f'Open PR <span aria-hidden="true">↗</span></a>'
        if pr_url
        else ""
    )

    total_hunks = stats["total_hunks"]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light dark">
  <script>
    try {{
      const savedScheme = localStorage.getItem("unmaze-color-scheme");
      if (savedScheme === "light" || savedScheme === "dark") {{
        document.querySelector('meta[name="color-scheme"]').content = savedScheme;
      }}
    }} catch (_) {{}}
  </script>
  <title>{esc(title)} — unmaze Review Guide</title>
  <style>
    :root {{
      --bg-light: #f6f8fa;
      --bg-dark: #0d1117;
      --surface-light: #ffffff;
      --surface-dark: #161b22;
      --surface-subtle-light: #f0f3f6;
      --surface-subtle-dark: #1c2128;
      --border-light: #d0d7de;
      --border-dark: #30363d;
      --text-light: #1f2328;
      --text-dark: #e6edf3;
      --muted-light: #59636e;
      --muted-dark: #8b949e;
      --accent-light: #0969da;
      --accent-dark: #58a6ff;
      --accent-soft-light: #ddf4ff;
      --accent-soft-dark: #13233a;

      --add-bg-light: #dafbe1;
      --add-bg-dark: #12261e;
      --add-fg-light: #1a7f37;
      --add-fg-dark: #3fb950;
      --del-bg-light: #ffebe9;
      --del-bg-dark: #2d1619;
      --del-fg-light: #cf222e;
      --del-fg-dark: #f85149;

      --contracts-light: #8250df;
      --contracts-dark: #bc8cff;
      --core-light: #bf3989;
      --core-dark: #f778ba;
      --wiring-light: #0969da;
      --wiring-dark: #58a6ff;
      --verify-light: #1a7f37;
      --verify-dark: #3fb950;
      --warn-bg-light: #fff8c5;
      --warn-bg-dark: #2b230f;
      --warn-border-light: #d4a72c;
      --warn-border-dark: #9e6a03;

      --bg: var(--bg-light);
      --surface: var(--surface-light);
      --surface-subtle: var(--surface-subtle-light);
      --border: var(--border-light);
      --text: var(--text-light);
      --muted: var(--muted-light);
      --accent: var(--accent-light);
      --accent-soft: var(--accent-soft-light);
      --add-bg: var(--add-bg-light);
      --add-fg: var(--add-fg-light);
      --del-bg: var(--del-bg-light);
      --del-fg: var(--del-fg-light);
      --contracts: var(--contracts-light);
      --core: var(--core-light);
      --wiring: var(--wiring-light);
      --verify: var(--verify-light);
      --warn-bg: var(--warn-bg-light);
      --warn-border: var(--warn-border-light);
      --scroll-thumb: #6b7280;
      --scroll-track: #e5e7eb;

      @media (prefers-color-scheme: dark) {{
        --bg: var(--bg-dark);
        --surface: var(--surface-dark);
        --surface-subtle: var(--surface-subtle-dark);
        --border: var(--border-dark);
        --text: var(--text-dark);
        --muted: var(--muted-dark);
        --accent: var(--accent-dark);
        --accent-soft: var(--accent-soft-dark);
        --add-bg: var(--add-bg-dark);
        --add-fg: var(--add-fg-dark);
        --del-bg: var(--del-bg-dark);
        --del-fg: var(--del-fg-dark);
        --contracts: var(--contracts-dark);
        --core: var(--core-dark);
        --wiring: var(--wiring-dark);
        --verify: var(--verify-dark);
        --warn-bg: var(--warn-bg-dark);
        --warn-border: var(--warn-border-dark);
        --scroll-thumb: #8b949e;
        --scroll-track: #161b22;
      }}

      color-scheme: light dark;

      &:has(> head > meta[name="color-scheme"][content="dark"]) {{
        color-scheme: dark;
        --bg: var(--bg-dark);
        --surface: var(--surface-dark);
        --surface-subtle: var(--surface-subtle-dark);
        --border: var(--border-dark);
        --text: var(--text-dark);
        --muted: var(--muted-dark);
        --accent: var(--accent-dark);
        --accent-soft: var(--accent-soft-dark);
        --add-bg: var(--add-bg-dark);
        --add-fg: var(--add-fg-dark);
        --del-bg: var(--del-bg-dark);
        --del-fg: var(--del-fg-dark);
        --contracts: var(--contracts-dark);
        --core: var(--core-dark);
        --wiring: var(--wiring-dark);
        --verify: var(--verify-dark);
        --warn-bg: var(--warn-bg-dark);
        --warn-border: var(--warn-border-dark);
        --scroll-thumb: #8b949e;
        --scroll-track: #161b22;
      }}

      &:has(> head > meta[name="color-scheme"][content="light"]) {{
        color-scheme: light;
        --bg: var(--bg-light);
        --surface: var(--surface-light);
        --surface-subtle: var(--surface-subtle-light);
        --border: var(--border-light);
        --text: var(--text-light);
        --muted: var(--muted-light);
        --accent: var(--accent-light);
        --accent-soft: var(--accent-soft-light);
        --add-bg: var(--add-bg-light);
        --add-fg: var(--add-fg-light);
        --del-bg: var(--del-bg-light);
        --del-fg: var(--del-fg-light);
        --contracts: var(--contracts-light);
        --core: var(--core-light);
        --wiring: var(--wiring-light);
        --verify: var(--verify-light);
        --warn-bg: var(--warn-bg-light);
        --warn-border: var(--warn-border-light);
        --scroll-thumb: #6b7280;
        --scroll-track: #e5e7eb;
      }}

      @supports (color: light-dark(white, black)) {{
        --bg: light-dark(var(--bg-light), var(--bg-dark));
        --surface: light-dark(var(--surface-light), var(--surface-dark));
        --surface-subtle: light-dark(var(--surface-subtle-light), var(--surface-subtle-dark));
        --border: light-dark(var(--border-light), var(--border-dark));
        --text: light-dark(var(--text-light), var(--text-dark));
        --muted: light-dark(var(--muted-light), var(--muted-dark));
        --accent: light-dark(var(--accent-light), var(--accent-dark));
        --accent-soft: light-dark(var(--accent-soft-light), var(--accent-soft-dark));
        --add-bg: light-dark(var(--add-bg-light), var(--add-bg-dark));
        --add-fg: light-dark(var(--add-fg-light), var(--add-fg-dark));
        --del-bg: light-dark(var(--del-bg-light), var(--del-bg-dark));
        --del-fg: light-dark(var(--del-fg-light), var(--del-fg-dark));
        --contracts: light-dark(var(--contracts-light), var(--contracts-dark));
        --core: light-dark(var(--core-light), var(--core-dark));
        --wiring: light-dark(var(--wiring-light), var(--wiring-dark));
        --verify: light-dark(var(--verify-light), var(--verify-dark));
        --warn-bg: light-dark(var(--warn-bg-light), var(--warn-bg-dark));
        --warn-border: light-dark(var(--warn-border-light), var(--warn-border-dark));
        --scroll-thumb: light-dark(#6b7280, #8b949e);
        --scroll-track: light-dark(#e5e7eb, #161b22);
      }}

      scrollbar-color: var(--scroll-thumb) var(--scroll-track);
      accent-color: var(--accent);
    }}

    @supports not (scrollbar-color: auto) {{
      ::-webkit-scrollbar {{
        width: 10px;
        height: 10px;
      }}
      ::-webkit-scrollbar-thumb {{
        background: var(--scroll-thumb);
      }}
      ::-webkit-scrollbar-track {{
        background: var(--scroll-track);
      }}
    }}

    #expand-all-btn[aria-expanded="true"] .lbl-expand,
    #expand-all-btn[aria-expanded="false"] .lbl-collapse {{
      display: none;
    }}

    *, *::before, *::after {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}

    code, pre {{
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
    }}

    /* Top Bar */
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 20;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0.75rem 1.25rem;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }}

    .topbar-title-group {{
      display: flex;
      align-items: center;
      gap: 0.65rem;
      min-width: 0;
    }}

    .brand-pill {{
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.78rem;
      font-weight: 700;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      border: 1px solid var(--accent);
      flex-shrink: 0;
    }}

    .pr-heading {{
      margin: 0;
      font-size: 1.02rem;
      font-weight: 650;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .topbar-controls {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
    }}

    .filter-input {{
      padding: 0.35rem 0.65rem;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--surface-subtle);
      color: var(--text);
      font-size: 0.82rem;
      min-width: 11rem;
    }}

    .ctrl-btn, .pr-ext-btn, .pager-btn {{
      appearance: none;
      border: 1px solid var(--border);
      background: var(--surface-subtle);
      color: var(--text);
      border-radius: 6px;
      padding: 0.35rem 0.68rem;
      font-size: 0.82rem;
      font-weight: 550;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
    }}

    .ctrl-btn:hover, .pr-ext-btn:hover, .pager-btn:hover {{
      border-color: var(--accent);
    }}

    .ctrl-btn[aria-pressed="true"], .pager-btn-primary {{
      background: var(--accent);
      border-color: var(--accent);
      color: #ffffff;
    }}

    .progress-chip {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.8rem;
      color: var(--muted);
      padding: 0.25rem 0.55rem;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--surface-subtle);
    }}

    .progress-chip progress {{
      inline-size: 4.5rem;
      block-size: 0.55rem;
    }}

    /* Overview / Step 0 Map */
    .overview-band {{
      max-width: 1440px;
      margin: 1rem auto 0;
      padding: 0 1.25rem;
    }}

    .overview-details {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
    }}

    .overview-summary {{
      padding: 0.85rem 1.1rem;
      font-weight: 650;
      font-size: 0.94rem;
    }}

    .overview-content {{
      padding: 0 1.1rem 1.1rem;
      border-top: 1px solid var(--border);
    }}

    .tldr-text {{
      margin: 0.85rem 0 0.65rem;
      font-size: 0.96rem;
    }}

    .global-ctx {{
      margin: 0 0 0.85rem;
      font-size: 0.88rem;
      color: var(--muted);
    }}

    .state-compare {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 0.75rem;
      margin-bottom: 0.9rem;
    }}

    .state-box {{
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.7rem 0.85rem;
      font-size: 0.86rem;
    }}

    .state-box strong {{
      display: block;
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      margin-bottom: 0.25rem;
    }}

    .budget-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.55rem;
      margin-bottom: 0.85rem;
      font-size: 0.81rem;
    }}

    .budget-pill {{
      padding: 0.25rem 0.6rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--surface-subtle);
    }}

    /* Architecture Map */
    .arch-map {{
      margin-top: 0.6rem;
    }}

    .arch-nodes-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(185px, 1fr));
      gap: 0.6rem;
    }}

    .arch-node {{
      text-align: left;
      font: inherit;
      color: var(--text);
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.6rem 0.75rem;
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
      transition: border-color 0.15s, box-shadow 0.15s;
    }}

    button.arch-node {{ cursor: pointer; }}
    button.arch-node:hover {{ border-color: var(--accent); }}

    .arch-unchanged {{ border-style: dashed; opacity: 0.85; }}

    .arch-node.is-step-highlight {{
      border-color: var(--accent);
      box-shadow: 0 0 0 2px var(--accent);
      background: var(--accent-soft);
      opacity: 1;
    }}

    .arch-node-top {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }}

    .arch-badge {{
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.68rem;
      font-weight: 700;
      padding: 0.08rem 0.35rem;
      border-radius: 4px;
      border: 1px solid var(--border);
    }}

    .arch-badge-added {{ color: var(--add-fg); border-color: var(--add-fg); }}
    .arch-badge-modified {{ color: var(--accent); border-color: var(--accent); }}
    .arch-badge-removed {{ color: var(--del-fg); border-color: var(--del-fg); }}
    .arch-badge-unchanged {{ color: var(--muted); }}

    .arch-node-title {{ font-size: 0.85rem; }}
    .arch-node-role {{ font-size: 0.78rem; color: var(--muted); }}
    .arch-step-ref {{ font-size: 0.73rem; color: var(--accent); font-family: ui-monospace, SFMono-Regular, monospace; }}

    .arch-edges {{
      list-style: none;
      padding: 0;
      margin: 0.65rem 0 0;
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem 0.9rem;
      font-size: 0.79rem;
      color: var(--muted);
    }}

    /* Workspace Layout */
    .workspace {{
      max-width: 1440px;
      margin: 1rem auto 2.5rem;
      padding: 0 1.25rem;
      display: grid;
      grid-template-columns: 290px minmax(0, 1fr);
      gap: 1.15rem;
      align-items: start;
    }}

    @media (max-width: 940px) {{
      .workspace {{ grid-template-columns: 1fr; }}
      .step-rail {{ position: static; max-height: none; }}
    }}

    /* Step Rail */
    .step-rail {{
      position: sticky;
      top: 4.3rem;
      max-height: calc(100vh - 5rem);
      overflow-y: auto;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.65rem;
    }}

    .rail-heading {{
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      margin: 0.2rem 0.4rem 0.55rem;
      font-weight: 700;
    }}

    .step-nav-list {{
      list-style: none;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
    }}

    .step-nav-btn {{
      width: 100%;
      text-align: left;
      border: 1px solid transparent;
      background: transparent;
      color: var(--text);
      border-radius: 8px;
      padding: 0.5rem 0.55rem;
      cursor: pointer;
      display: flex;
      align-items: flex-start;
      gap: 0.55rem;
      font: inherit;
    }}

    .step-nav-btn:hover {{
      background: var(--surface-subtle);
    }}

    .step-nav-btn.is-active {{
      background: var(--accent-soft);
      border-color: var(--accent);
    }}

    .step-num {{
      inline-size: 1.5rem;
      block-size: 1.5rem;
      border-radius: 999px;
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.76rem;
      font-weight: 700;
      flex-shrink: 0;
      margin-top: 0.1rem;
    }}

    .step-nav-btn.is-step-complete .step-num {{
      background: var(--add-bg);
      color: var(--add-fg);
      border-color: var(--add-fg);
    }}

    .step-nav-body {{
      display: flex;
      flex-direction: column;
      gap: 0.12rem;
      min-width: 0;
    }}

    .step-nav-layer {{
      font-size: 0.71rem;
      font-weight: 700;
      letter-spacing: 0.02em;
    }}

    .layer-tag-contracts {{ color: var(--contracts); }}
    .layer-tag-core {{ color: var(--core); }}
    .layer-tag-wiring {{ color: var(--wiring); }}
    .layer-tag-verification {{ color: var(--verify); }}
    .layer-tag-mechanical {{ color: var(--muted); }}

    .step-nav-title {{
      font-size: 0.83rem;
      font-weight: 560;
      line-height: 1.3;
    }}

    .step-nav-stats {{
      font-size: 0.73rem;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, monospace;
    }}

    /* Step Cards (note: padding/borders live on .step-card, NOT on [hidden="until-found"] .step-panel) */
    .steps-column {{
      display: flex;
      flex-direction: column;
      gap: 1.15rem;
      min-width: 0;
    }}

    .step-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.15rem 1.25rem;
    }}

    .step-eyebrow {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
      margin-bottom: 0.45rem;
    }}

    .step-counter {{
      font-size: 0.78rem;
      font-weight: 650;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, monospace;
    }}

    .layer-pill, .slice-pill {{
      font-size: 0.76rem;
      font-weight: 650;
      padding: 0.15rem 0.55rem;
      border-radius: 999px;
      border: 1px solid currentColor;
    }}

    .layer-contracts {{ color: var(--contracts); }}
    .layer-core {{ color: var(--core); }}
    .layer-wiring {{ color: var(--wiring); }}
    .layer-verification {{ color: var(--verify); }}
    .layer-mechanical {{ color: var(--muted); }}
    .slice-pill {{ color: var(--muted); border-color: var(--border); background: var(--surface-subtle); }}

    .step-title {{
      margin: 0 0 0.55rem;
      font-size: 1.22rem;
      line-height: 1.3;
    }}

    .why-now {{
      margin: 0 0 0.6rem;
      padding: 0.5rem 0.75rem;
      border-left: 3px solid var(--accent);
      background: var(--surface-subtle);
      border-radius: 0 6px 6px 0;
      font-size: 0.87rem;
    }}

    .step-explanation {{
      margin: 0 0 0.85rem;
      font-size: 0.92rem;
    }}

    .callout {{
      margin: 0 0 0.75rem;
      padding: 0.65rem 0.85rem;
      border-radius: 8px;
      border: 1px solid var(--warn-border);
      background: var(--warn-bg);
      font-size: 0.87rem;
    }}

    .callout-decision {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}

    .gaps-list {{
      margin: 0.35rem 0 0;
      padding-left: 1.2rem;
    }}

    /* Hunk Cards & Disclosure */
    .step-hunks {{
      display: flex;
      flex-direction: column;
      gap: 0.9rem;
      margin-top: 0.9rem;
    }}

    .hunk-card {{
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      background: var(--surface);
    }}

    .hunk-card.is-reviewed {{
      opacity: 0.72;
    }}

    .hunk-card.is-filtered-out {{
      display: none;
    }}

    .hunk-toolbar {{
      padding: 0.5rem 0.75rem;
      background: var(--surface-subtle);
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
    }}

    .hunk-meta {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.45rem;
      min-width: 0;
    }}

    .hunk-id-pill {{
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.72rem;
      font-weight: 700;
      padding: 0.1rem 0.38rem;
      border-radius: 4px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--muted);
    }}

    .hunk-filepath {{
      font-size: 0.83rem;
      font-weight: 600;
      word-break: break-all;
    }}

    .status-pill {{
      font-size: 0.68rem;
      font-weight: 700;
      padding: 0.08rem 0.36rem;
      border-radius: 4px;
      border: 1px solid var(--border);
      color: var(--muted);
    }}

    .status-added {{ color: var(--add-fg); border-color: var(--add-fg); }}
    .status-deleted {{ color: var(--del-fg); border-color: var(--del-fg); }}

    .hunk-loc, .diff-stat {{
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.76rem;
      color: var(--muted);
    }}

    .stat-add {{ color: var(--add-fg); font-weight: 650; }}
    .stat-del {{ color: var(--del-fg); font-weight: 650; }}

    .hunk-actions {{
      display: flex;
      align-items: center;
      gap: 0.65rem;
      font-size: 0.8rem;
    }}

    .hunk-ext-link {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 550;
    }}

    .hunk-ext-link:hover {{ text-decoration: underline; }}

    .reviewed-toggle {{
      display: inline-flex;
      align-items: center;
      gap: 0.32rem;
      cursor: pointer;
      user-select: none;
      font-weight: 550;
    }}

    .hunk-note {{
      margin: 0;
      padding: 0.45rem 0.75rem;
      font-size: 0.83rem;
      background: var(--accent-soft);
      border-bottom: 1px solid var(--border);
    }}

    /* Native <details> + <summary> styling per modern-web-guidance */
    summary {{
      list-style: none;
      display: flex;
      align-items: center;
      gap: 0.5ch;
      cursor: pointer;
    }}

    summary::-webkit-details-marker {{ display: none; }}

    summary::before {{
      content: "";
      inline-size: 0.95em;
      block-size: 0.95em;
      flex-shrink: 0;
      background-color: currentColor;
      mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M4.427 6.427l3.396 3.396a.25.25 0 00.354 0l3.396-3.396A.25.25 0 0011.396 6H4.604a.25.25 0 00-.177.427z'/%3E%3C/svg%3E") no-repeat center / contain;
      rotate: -90deg;
    }}

    details[open] > summary::before {{
      rotate: 0deg;
    }}

    @media (prefers-reduced-motion: no-preference) {{
      summary::before {{ transition: rotate 0.18s; }}
      details::details-content {{
        interpolate-size: allow-keywords;
        overflow: clip;
        contain: layout;
        transition: block-size 0.22s ease-in-out, content-visibility 0.22s ease-in-out;
        transition-behavior: allow-discrete;
      }}
      details:not([open])::details-content {{
        block-size: 0;
      }}
    }}

    .hunk-summary {{
      padding: 0.35rem 0.75rem;
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.76rem;
      color: var(--muted);
      background: var(--surface);
      border-bottom: 1px solid var(--border);
    }}

    .diff-pre {{
      margin: 0;
      padding: 0.35rem 0;
      overflow-x: auto;
      font-size: 0.8rem;
      line-height: 1.45;
      background: var(--surface);
    }}

    .diff-pre code {{
      display: block;
      min-width: max-content;
    }}

    .dl {{
      display: flex;
      padding: 0 0.75rem 0 0.35rem;
    }}

    .dl-add {{ background: var(--add-bg); }}
    .dl-del {{ background: var(--del-bg); }}
    .dl-meta {{ color: var(--muted); font-style: italic; }}

    .ln {{
      inline-size: 3.1rem;
      text-align: right;
      padding-right: 0.55rem;
      color: var(--muted);
      user-select: none;
      flex-shrink: 0;
      opacity: 0.78;
    }}

    .sig {{
      inline-size: 1.1rem;
      user-select: none;
      flex-shrink: 0;
      font-weight: 700;
    }}

    .dl-add .sig {{ color: var(--add-fg); }}
    .dl-del .sig {{ color: var(--del-fg); }}

    .code-txt {{
      white-space: pre;
    }}

    .mech-group {{
      margin-bottom: 1.1rem;
    }}

    .mech-group-title {{
      margin: 0 0 0.2rem;
      font-size: 0.98rem;
    }}

    .mech-count {{
      color: var(--muted);
      font-weight: 500;
      font-size: 0.85rem;
    }}

    .mech-reason {{
      margin: 0 0 0.55rem;
      font-size: 0.85rem;
      color: var(--muted);
    }}

    .step-pager {{
      margin-top: 1.15rem;
      padding-top: 0.9rem;
      border-top: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }}

    .complete-pill {{
      font-size: 0.83rem;
      font-weight: 650;
      color: var(--add-fg);
    }}

    .kbd-hint {{
      max-width: 1440px;
      margin: 0 auto 2rem;
      padding: 0 1.25rem;
      font-size: 0.78rem;
      color: var(--muted);
      text-align: center;
    }}

    .kbd-hint kbd {{
      padding: 0.1rem 0.35rem;
      border: 1px solid var(--border);
      border-radius: 4px;
      background: var(--surface);
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.74rem;
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-title-group">
      <span class="brand-pill">unmaze</span>
      <h1 class="pr-heading">{esc(title)}</h1>
      {pr_header_link}
    </div>
    <div class="topbar-controls">
      <search>
        <input type="search" id="file-filter" class="filter-input" placeholder="Filter by file path…" aria-label="Filter diff hunks by file path">
      </search>
      <div class="progress-chip" aria-live="polite">
        <label for="review-progress">Reviewed:</label>
        <progress id="review-progress" value="0" max="{total_hunks}"></progress>
        <span id="review-progress-text">0 / {total_hunks}</span>
      </div>
      <button type="button" id="mode-toggle" class="ctrl-btn" aria-pressed="true">Guided Step-by-Step</button>
      <button type="button" id="expand-all-btn" class="ctrl-btn" aria-expanded="true">
        <span class="lbl-expand">Expand All Diffs</span>
        <span class="lbl-collapse">Collapse All Diffs</span>
      </button>
      <button type="button" id="theme-toggle" class="ctrl-btn" aria-label="Switch color theme">Theme</button>
    </div>
  </header>

  <section class="overview-band" aria-label="Global mental model and architecture">
    <details class="overview-details" open>
      <summary class="overview-summary">Step 0 — Global Mental Model &amp; System Map ({stats['total_files']} files, +{stats['total_additions']}/−{stats['total_deletions']})</summary>
      <div class="overview-content">
        <p class="tldr-text"><strong>TL;DR:</strong> {esc(overview['tldr'])}</p>
        {global_ctx_html}
        <div class="state-compare">
          <div class="state-box">
            <strong>Before this PR</strong>
            <span>{esc(overview['before_state'])}</span>
          </div>
          <div class="state-box">
            <strong>After this PR</strong>
            <span>{esc(overview['after_state'])}</span>
          </div>
        </div>
        <div class="budget-strip" aria-label="Cognitive load breakdown">
          <span class="budget-pill"><strong>Core Focus (Contracts + Logic):</strong> {high_focus_lines} lines</span>
          <span class="budget-pill"><strong>Wiring &amp; Verification:</strong> {support_lines} lines</span>
          <span class="budget-pill"><strong>Mechanical / Boilerplate:</strong> {mech_lines} lines</span>
          <span class="budget-pill"><strong>Diff Accounting:</strong> 100% ({total_hunks}/{total_hunks} hunks)</span>
        </div>
        {arch_html}
      </div>
    </details>
  </section>

  <div class="workspace">
    <nav class="step-rail" aria-label="Human reading sequence">
      <div class="rail-heading">Reading Sequence</div>
      <ol class="step-nav-list">
        {"".join(nav_items_html)}
      </ol>
    </nav>

    <main id="steps-main" class="steps-column">
      {"".join(step_panels_html)}
    </main>
  </div>

  <footer class="kbd-hint">
    Generated by <strong>unmaze</strong> · Shortcuts: <kbd>[</kbd> / <kbd>]</kbd> previous/next step · <kbd>m</kbd> toggle Guided vs. All-Steps view · Native <kbd>⌘F</kbd> / <kbd>Ctrl+F</kbd> searches across hidden steps automatically.
  </footer>

  <script>
    (() => {{
      const supportsUntilFound = "onbeforematch" in HTMLElement.prototype;
      const panels = Array.from(document.querySelectorAll(".step-panel"));
      const navButtons = Array.from(document.querySelectorAll(".step-nav-btn"));
      const archNodes = Array.from(document.querySelectorAll(".arch-node"));
      const modeToggle = document.getElementById("mode-toggle");
      const expandAllBtn = document.getElementById("expand-all-btn");
      const themeToggle = document.getElementById("theme-toggle");
      const filterInput = document.getElementById("file-filter");
      const progressEl = document.getElementById("review-progress");
      const progressText = document.getElementById("review-progress-text");
      const metaScheme = document.querySelector('meta[name="color-scheme"]');

      let guidedMode = supportsUntilFound;
      let activeIndex = 0;
      let memoryScheme = null;

      function getStoredScheme() {{
        try {{
          return localStorage.getItem("unmaze-color-scheme") || memoryScheme;
        }} catch (_) {{
          return memoryScheme;
        }}
      }}

      function setStoredScheme(val) {{
        memoryScheme = val;
        try {{
          if (val === null) {{
            localStorage.removeItem("unmaze-color-scheme");
          }} else {{
            localStorage.setItem("unmaze-color-scheme", val);
          }}
        }} catch (_) {{}}
      }}

      function syncArchHighlights(stepId) {{
        for (const node of archNodes) {{
          const steps = (node.getAttribute("data-steps") || "").split(",").filter(Boolean);
          node.classList.toggle("is-step-highlight", steps.includes(stepId));
        }}
      }}

      function activateStep(index, scrollIntoView = true) {{
        if (index < 0 || index >= panels.length) return;
        activeIndex = index;
        const activePanel = panels[index];
        const activeStepId = activePanel.getAttribute("data-step-id") || "";

        panels.forEach((panel, idx) => {{
          if (guidedMode) {{
            if (idx === index) {{
              panel.removeAttribute("hidden");
            }} else {{
              panel.setAttribute("hidden", "until-found");
            }}
          }} else {{
            panel.removeAttribute("hidden");
          }}
        }});

        navButtons.forEach((btn, idx) => {{
          const isCurrent = idx === index;
          btn.classList.toggle("is-active", isCurrent);
          if (isCurrent) {{
            btn.setAttribute("aria-current", "step");
          }} else {{
            btn.removeAttribute("aria-current");
          }}
        }});

        syncArchHighlights(activeStepId);

        if (scrollIntoView) {{
          activePanel.scrollIntoView({{ behavior: "smooth", block: "start" }});
        }}
      }}

      function setGuidedMode(enabled) {{
        guidedMode = enabled;
        modeToggle.setAttribute("aria-pressed", String(guidedMode));
        modeToggle.textContent = guidedMode ? "Guided Step-by-Step" : "Showing All Steps";
        activateStep(activeIndex, false);
      }}

      // Initialize mode (falls back to linear view if browser lacks hidden="until-found")
      setGuidedMode(supportsUntilFound);

      // Sync UI state when native browser Find-in-Page reveals a hidden="until-found" step
      const mainEl = document.getElementById("steps-main");
      mainEl.addEventListener("beforematch", (event) => {{
        const matchedPanel = event.target.closest(".step-panel");
        if (!matchedPanel) return;
        const idx = Number(matchedPanel.getAttribute("data-step-index"));
        if (!Number.isNaN(idx)) {{
          activateStep(idx, false);
        }}
      }});

      // Left rail step navigation
      for (const btn of navButtons) {{
        btn.addEventListener("click", () => {{
          const idx = Number(btn.getAttribute("data-step-index"));
          if (!Number.isNaN(idx)) activateStep(idx, true);
        }});
      }}

      // Step pager buttons
      document.addEventListener("click", (event) => {{
        const gotoIdxBtn = event.target.closest("[data-goto-index]");
        if (gotoIdxBtn) {{
          const idx = Number(gotoIdxBtn.getAttribute("data-goto-index"));
          if (!Number.isNaN(idx)) activateStep(idx, true);
          return;
        }}
        const gotoStepBtn = event.target.closest("[data-goto-step]");
        if (gotoStepBtn) {{
          const targetId = gotoStepBtn.getAttribute("data-goto-step");
          const idx = panels.findIndex((p) => p.getAttribute("data-step-id") === targetId);
          if (idx !== -1) activateStep(idx, true);
          return;
        }}
        const archBtn = event.target.closest("button.arch-node[data-jump-step]");
        if (archBtn) {{
          const targetId = archBtn.getAttribute("data-jump-step");
          const idx = panels.findIndex((p) => p.getAttribute("data-step-id") === targetId);
          if (idx !== -1) activateStep(idx, true);
        }}
      }});

      modeToggle.addEventListener("click", () => {{
        setGuidedMode(!guidedMode);
      }});

      // Expand / Collapse all hunk disclosures
      function allHunkDetails() {{
        return Array.from(document.querySelectorAll(".hunk-details"));
      }}

      function syncExpandButton() {{
        const list = allHunkDetails();
        const everyOpen = list.length > 0 && list.every((d) => d.open);
        expandAllBtn.setAttribute("aria-expanded", String(everyOpen));
      }}

      expandAllBtn.addEventListener("click", () => {{
        const shouldOpen = expandAllBtn.getAttribute("aria-expanded") !== "true";
        for (const d of allHunkDetails()) {{
          d.open = shouldOpen;
        }}
        syncExpandButton();
      }});

      document.addEventListener("toggle", (event) => {{
        if (event.target instanceof HTMLDetailsElement && event.target.classList.contains("hunk-details")) {{
          syncExpandButton();
        }}
      }}, {{ capture: true }});
      syncExpandButton();

      // Two-state dark-mode override reverting cleanly to OS preference
      function updateThemeButtonLabel() {{
        const stored = getStoredScheme();
        const sysDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        const renderedDark = stored ? stored === "dark" : sysDark;
        themeToggle.textContent = renderedDark ? "☀ Light" : "☾ Dark";
      }}

      themeToggle.addEventListener("click", () => {{
        const stored = getStoredScheme();
        const sysDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        const renderedDark = stored ? stored === "dark" : sysDark;
        const target = renderedDark ? "light" : "dark";
        const sysCurrent = sysDark ? "dark" : "light";

        if (target === sysCurrent) {{
          setStoredScheme(null);
          metaScheme.content = "light dark";
        }} else {{
          setStoredScheme(target);
          metaScheme.content = target;
        }}
        updateThemeButtonLabel();
      }});

      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", updateThemeButtonLabel);
      window.addEventListener("storage", (event) => {{
        if (event.key === "unmaze-color-scheme") {{
          metaScheme.content = event.newValue || "light dark";
          updateThemeButtonLabel();
        }}
      }});
      updateThemeButtonLabel();

      // Reviewed checkboxes & per-step / global progress
      const checkboxes = Array.from(document.querySelectorAll(".hunk-check"));
      function updateProgress() {{
        const total = checkboxes.length;
        const done = checkboxes.filter((c) => c.checked).length;
        progressEl.value = done;
        progressText.textContent = `${{done}} / ${{total}}`;

        for (const panel of panels) {{
          const sid = panel.getAttribute("data-step-id");
          const panelChecks = Array.from(panel.querySelectorAll(".hunk-check"));
          const panelDone = panelChecks.filter((c) => c.checked).length;
          const badge = document.querySelector(`[data-step-progress="${{CSS.escape(sid)}}"]`);
          if (badge) {{
            badge.textContent = `${{panelDone}}/${{panelChecks.length}}`;
          }}
          const navBtn = document.querySelector(`.step-nav-btn[data-step-target="${{CSS.escape(sid)}}"]`);
          if (navBtn) {{
            navBtn.classList.toggle("is-step-complete", panelChecks.length > 0 && panelDone === panelChecks.length);
          }}
        }}
      }}

      for (const chk of checkboxes) {{
        chk.addEventListener("change", () => {{
          const card = chk.closest(".hunk-card");
          if (card) card.classList.toggle("is-reviewed", chk.checked);
          updateProgress();
        }});
      }}

      // File path filter
      filterInput.addEventListener("input", () => {{
        const q = filterInput.value.trim().toLowerCase();
        const cards = document.querySelectorAll(".hunk-card");
        for (const card of cards) {{
          const fp = card.getAttribute("data-file") || "";
          card.classList.toggle("is-filtered-out", q.length > 0 && !fp.includes(q));
        }}
      }});

      // Keyboard navigation ([ / ] for prev/next step, m to toggle mode)
      document.addEventListener("keydown", (event) => {{
        if (event.metaKey || event.ctrlKey || event.altKey) return;
        const tag = (event.target && event.target.tagName) || "";
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
        if (event.key === "]") {{
          activateStep(Math.min(panels.length - 1, activeIndex + 1), true);
        }} else if (event.key === "[") {{
          activateStep(Math.max(0, activeIndex - 1), true);
        }} else if (event.key === "m") {{
          setGuidedMode(!guidedMode);
        }}
      }});
    }})();
  </script>
</body>
</html>
"""


def build_markdown_summary(manifest: dict[str, Any], plan: dict[str, Any]) -> str:
    """Emit a clean Markdown review guide suitable for PR descriptions or terminal reading."""
    meta = manifest["meta"]
    stats = manifest["stats"]
    hunk_map: dict[str, dict[str, Any]] = {h["id"]: h for h in manifest["hunks"]}
    title = str(plan.get("title") or meta.get("title") or "PR Code Review Guide")
    ov = plan["overview"]

    lines: list[str] = [
        f"# {title}",
        "",
        f"> **TL;DR:** {ov['tldr']}",
        "",
        f"- **Before:** {ov['before_state']}",
        f"- **After:** {ov['after_state']}",
    ]
    if ov.get("global_context"):
        lines.append(f"- **Global Context:** {ov['global_context']}")
    lines.append(
        f"- **Diff Coverage:** 100% accounted ({stats['total_hunks']} hunks across "
        f"{stats['total_files']} files, +{stats['total_additions']}/−{stats['total_deletions']})"
    )
    lines.append("")
    lines.append("## Guided Reading Order")
    lines.append("")

    for idx, st in enumerate(plan["steps"], start=1):
        lyr = VALID_LAYERS[st["layer"]]
        lines.append(f"### Step {idx}: {st['title']} _({lyr['label']})_")
        if st.get("why_now"):
            lines.append(f"> **Why read this now:** {st['why_now']}")
        if st.get("explanation"):
            lines.append("")
            lines.append(str(st["explanation"]))
        for co in st.get("callouts") or []:
            if isinstance(co, dict) and co.get("text"):
                ckind = VALID_CALLOUT_KINDS.get(str(co.get("kind", "")).lower(), VALID_CALLOUT_KINDS["decision"])
                lines.append(f"- **{ckind['label']}:** {co['text']}")
        for gap in st.get("untested_gaps") or []:
            lines.append(f"- **Untested Gap:** {gap}")
        lines.append("")
        lines.append("**Hunks in this step:**")
        hunk_notes = st.get("hunk_notes") or {}
        for hid in st["hunk_ids"]:
            h = hunk_map[hid]
            loc = f"L{h['new_start']}" if not h["is_synthetic"] else "file"
            link = safe_http_url(h.get("deep_link"))
            ref = f"[`{h['file_path']}:{loc}`]({link})" if link else f"`{h['file_path']}:{loc}`"
            note_suffix = f" — {hunk_notes[hid]}" if isinstance(hunk_notes, dict) and hid in hunk_notes else ""
            lines.append(f"- `{hid}` {ref} (+{h['additions']}/−{h['deletions']}){note_suffix}")
        lines.append("")

    mech_groups = (plan.get("mechanical") or {}).get("groups") or []
    if mech_groups:
        lines.append("### Mechanical & Boilerplate")
        for grp in mech_groups:
            hids = ", ".join(f"`{hid}`" for hid in grp["hunk_ids"])
            reason = grp.get("reason") or "Mechanical changes"
            lines.append(f"- **{grp['title']}** ({hids}): {reason}")
        lines.append("")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate 100% diff hunk coverage and compile the unmaze HTML/Markdown artifact."
    )
    parser.add_argument(
        "--manifest",
        default=".unmaze/diff-manifest.json",
        help="Path to diff-manifest.json produced by parse_diff.py",
    )
    parser.add_argument(
        "--plan",
        default=".unmaze/plan.json",
        help="Path to cognitive review plan.json produced by the agent",
    )
    parser.add_argument(
        "--out",
        default=".unmaze/unmaze-review.html",
        help="Output path for the self-contained HTML artifact",
    )
    parser.add_argument(
        "--markdown-out",
        default=".unmaze/unmaze-review.md",
        help="Optional output path for the companion Markdown guide",
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    plan_path = Path(args.plan)

    if not manifest_path.exists():
        print(f"Error: Manifest not found at `{manifest_path}`. Run `parse_diff.py` first.", file=sys.stderr)
        return 1
    if not plan_path.exists():
        print(f"Error: Plan not found at `{plan_path}`.", file=sys.stderr)
        return 1

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Error: Invalid JSON input: {exc}", file=sys.stderr)
        return 1

    try:
        validated_plan = validate_and_normalize_plan(manifest, plan)
    except PlanValidationError as exc:
        print(f"Plan validation error:\n{exc}", file=sys.stderr)
        return 2

    html_content = build_html_artifact(manifest, validated_plan)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_content, encoding="utf-8")

    print(
        f"✓ Verified 100% diff coverage ({manifest['stats']['total_hunks']}/"
        f"{manifest['stats']['total_hunks']} hunks accounted across {len(validated_plan['steps'])} steps)."
    )
    print(f"✓ Interactive HTML artifact written to: {out_path} ({len(html_content):,} bytes)")

    if args.markdown_out:
        md_path = Path(args.markdown_out)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_content = build_markdown_summary(manifest, validated_plan)
        md_path.write_text(md_content, encoding="utf-8")
        print(f"✓ Companion Markdown summary written to: {md_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
