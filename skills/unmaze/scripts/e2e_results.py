#!/usr/bin/env python3
""""Tested end to end" board section: results of end-to-end checks someone ran on the PR.

Another tool writes a results file (format: `references/e2e-results.md`) and the build reads it
with `--e2e-results`. Every string in it is untrusted text and is HTML-escaped everywhere.

Step GIFs are never embedded: the published page is capped at 16 MB. They are uploaded to the
Artifact's asset store after the first publish, and `--asset-urls` maps each file's sha256 to its
uploaded URL, so an unchanged GIF is uploaded only once across refreshes. GIFs without a URL yet
render a placeholder and are listed on the build's `e2e-uploads=` stdout line.

CLI: `e2e_results.py record-urls --asset-urls <asset-urls.json> <file>=<url> ...` merges uploaded
URLs into that file, keyed by each file's sha256.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import re
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KIND_LABELS = {"browser": "In the browser", "api": "Through the API", "job": "Background job"}
STATUS_LABELS = {"pass": "Passed", "fail": "Failed", "skipped": "Skipped"}
_STATUS_ORDER = {"fail": 0, "skipped": 1, "pass": 2}
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")

SECTION_TITLE = "Tested end to end"


class E2EResultsError(Exception):
    pass


def is_safe_asset_url(url: str) -> bool:
    return url.startswith("https://") or url.startswith("/_blob/")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Loading & validation
# ---------------------------------------------------------------------------

def _req_str(obj: dict[str, Any], key: str, where: str, errors: list[str]) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        errors.append(f"{where}: `{key}` is required (a non-empty string).")
        return ""
    return val.strip()


def _opt_str(obj: dict[str, Any], key: str, where: str, errors: list[str]) -> str:
    val = obj.get(key)
    if val is None:
        return ""
    if not isinstance(val, str):
        errors.append(f"{where}: `{key}` must be a string.")
        return ""
    return val.strip()


def _enum(obj: dict[str, Any], key: str, allowed: dict[str, str], where: str, errors: list[str]) -> str:
    val = obj.get(key)
    choices = ", ".join(allowed)
    if val is None or val == "":
        errors.append(f"{where}: `{key}` is required (one of {choices}).")
        return ""
    if val not in allowed:
        errors.append(f"{where}: `{key}` is {json.dumps(val)}; use one of {choices}.")
        return ""
    return str(val)


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        if w > 0 and h > 0:
            return int(w), int(h)
    return None


def _validate(raw: Any, base_dir: Path, errors: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        errors.append("the file must hold one JSON object.")
        return {}
    where = "top level"
    tested_sha = _req_str(raw, "tested_sha", where, errors)
    if tested_sha and not _SHA_RE.match(tested_sha):
        errors.append(f"{where}: `tested_sha` {json.dumps(tested_sha)} doesn't look like a commit sha (7-64 hex characters).")
    results: dict[str, Any] = {
        "tested_sha": tested_sha.lower(),
        "tested_at": _opt_str(raw, "tested_at", where, errors),
        "environment": _opt_str(raw, "environment", where, errors),
        "summary": _req_str(raw, "summary", where, errors),
        "scenarios": [],
    }
    raw_scenarios = raw.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        errors.append(f"{where}: `scenarios` is required (a list with at least one scenario).")
        raw_scenarios = []

    seen_ids: set[str] = set()
    for s_idx, sc in enumerate(raw_scenarios):
        where = f"scenarios[{s_idx}]"
        if not isinstance(sc, dict):
            errors.append(f"{where}: must be an object.")
            continue
        sid = _req_str(sc, "id", where, errors)
        if sid:
            where = f"scenarios[{s_idx}] ({sid})"
            if sid in seen_ids:
                errors.append(f"{where}: duplicate scenario id `{sid}`.")
            seen_ids.add(sid)
        scenario: dict[str, Any] = {
            "id": sid,
            "title": _req_str(sc, "title", where, errors),
            "why": _req_str(sc, "why", where, errors),
            "kind": _enum(sc, "kind", KIND_LABELS, where, errors),
            "status": _enum(sc, "status", STATUS_LABELS, where, errors),
            "note": _opt_str(sc, "note", where, errors),
            "files": [],
            "steps": [],
        }
        if scenario["status"] in ("fail", "skipped") and not scenario["note"]:
            errors.append(f"{where}: a `{scenario['status']}` scenario needs a `note` saying why.")
        raw_files = sc.get("files")
        if raw_files is not None:
            if not isinstance(raw_files, list) or not all(isinstance(f, str) for f in raw_files):
                errors.append(f"{where}: `files` must be a list of file paths.")
            else:
                scenario["files"] = [f.strip() for f in raw_files if f.strip()]

        raw_steps = sc.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            errors.append(f"{where}: `steps` needs at least one step.")
            raw_steps = []
        for st_idx, st in enumerate(raw_steps, start=1):
            st_where = f"{where} step {st_idx}"
            if not isinstance(st, dict):
                errors.append(f"{st_where}: must be an object.")
                continue
            step: dict[str, Any] = {
                "action": _req_str(st, "action", st_where, errors),
                "expected": _req_str(st, "expected", st_where, errors),
                "status": _enum(st, "status", STATUS_LABELS, st_where, errors),
                "request": _opt_str(st, "request", st_where, errors),
                "response": _opt_str(st, "response", st_where, errors),
                "gif": None,
            }
            gif_rel = _opt_str(st, "gif", st_where, errors)
            if gif_rel:
                gif_path = (base_dir / gif_rel).resolve()
                if not gif_path.is_file():
                    errors.append(
                        f"{st_where}: `gif` file `{gif_rel}` not found (paths are relative to the results file's folder)."
                    )
                else:
                    data = gif_path.read_bytes()
                    step["gif"] = {
                        "path": str(gif_path),
                        "name": gif_path.name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": _gif_dimensions(data),
                    }
            scenario["steps"].append(step)
        results["scenarios"].append(scenario)
    return results


def load_results(path: Path) -> dict[str, Any]:
    """Read and strictly validate a results file; each step GIF gets its absolute path and sha256."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise E2EResultsError("- the file doesn't exist.") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EResultsError(f"- the file isn't valid JSON: {exc}") from None
    errors: list[str] = []
    results = _validate(raw, path.resolve().parent, errors)
    if errors:
        raise E2EResultsError("\n".join(f"- {e}" for e in errors))
    return results


def load_asset_urls(path: Path) -> dict[str, str]:
    """sha256 -> uploaded URL. A missing file means nothing is uploaded yet; unsafe URLs are dropped."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EResultsError(f"- the file isn't valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise E2EResultsError("- the file must hold one JSON object mapping each file's sha256 to its uploaded URL.")
    return {
        str(k).strip().lower(): v.strip()
        for k, v in raw.items()
        if isinstance(v, str) and is_safe_asset_url(v.strip())
    }


def prepare(results: dict[str, Any], asset_urls: dict[str, str], head_sha: str | None) -> dict[str, Any]:
    """Order scenarios (failures, skipped, passed), attach uploaded GIF URLs and count the results."""
    model = copy.deepcopy(results)
    model["scenarios"].sort(key=lambda s: _STATUS_ORDER[s["status"]])
    uploads: list[str] = []
    has_gif = False
    for sc in model["scenarios"]:
        for st in sc["steps"]:
            gif = st["gif"]
            if not gif:
                continue
            has_gif = True
            gif["url"] = asset_urls.get(gif["sha256"])
            if not gif["url"] and gif["path"] not in uploads:
                uploads.append(gif["path"])
    head = str(head_sha or "").strip().lower()
    tested = model["tested_sha"]
    model.update(
        {
            "counts": {k: sum(1 for s in model["scenarios"] if s["status"] == k) for k in STATUS_LABELS},
            "total": len(model["scenarios"]),
            "has_gif": has_gif,
            "uploads": uploads,
            "head_sha": head or None,
            "stale": bool(head) and not (head.startswith(tested) or tested.startswith(head)),
        }
    )
    return model


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _prose(value: Any) -> str:
    """Escape text, turning `code` spans into inline code."""
    text = str(value or "")
    out: list[str] = []
    pos = 0
    for m in _CODE_SPAN_RE.finditer(text):
        out.append(_esc(text[pos : m.start()]))
        out.append(f'<code class="inline-code">{_esc(m.group(1))}</code>')
        pos = m.end()
    out.append(_esc(text[pos:]))
    return "".join(out)


def format_tested_at(raw: str) -> str:
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    day = f"{dt:%b} {dt.day}, {dt.year}"
    if "T" not in raw and " " not in raw:
        return day
    if dt.tzinfo:
        return f"{day} {dt.astimezone(timezone.utc):%H:%M} UTC"
    return f"{day} {dt:%H:%M}"


def _tone(model: dict[str, Any]) -> str:
    if model["counts"]["fail"]:
        return "fail"
    return "skipped" if model["counts"]["skipped"] else "pass"


def _passed_phrase(model: dict[str, Any]) -> str:
    passed, total = model["counts"]["pass"], model["total"]
    return f"all {total} passed" if passed == total and total > 1 else f"{passed} of {total} passed"


def stale_sentence(model: dict[str, Any]) -> str:
    return (
        f"These checks ran on an older commit ({model['tested_sha'][:7]}), "
        f"not the latest one ({(model['head_sha'] or '')[:7]})."
    )


def render_sidebar_tab_html(model: dict[str, Any], panel_index: int) -> str:
    passed, total = model["counts"]["pass"], model["total"]
    return (
        f'<button type="button" class="arc-tab arc-tab-e2e" data-panel-index="{panel_index}">'
        f'<span class="arc-ch-num" aria-hidden="true">▶</span>'
        f'<span class="arc-tab-label">{SECTION_TITLE}</span>'
        f'<span class="e2e-count is-{_tone(model)}" aria-label="{passed} of {total} passed">{passed}/{total}</span>'
        f"</button>"
    )


def render_overview_pill_html(model: dict[str, Any], panel_index: int) -> str:
    extras: list[str] = []
    if model["counts"]["fail"]:
        extras.append(f'<span class="e2e-pill-fail">{model["counts"]["fail"]} failed</span>')
    if model["counts"]["skipped"]:
        extras.append(f'<span class="e2e-pill-skip">{model["counts"]["skipped"]} skipped</span>')
    if model["stale"]:
        extras.append('<span class="e2e-pill-stale">ran on an older commit</span>')
    sep = '<span class="e2e-pill-sep" aria-hidden="true">·</span>'
    return (
        f'<div class="e2e-pill-row">'
        f'<button type="button" class="e2e-pill is-{_tone(model)}" data-goto-index="{panel_index}">'
        f'<span class="e2e-dot" aria-hidden="true"></span>'
        f"<span>{SECTION_TITLE}: <strong>{_passed_phrase(model)}</strong></span>"
        + "".join(sep + e for e in extras)
        + '<span class="e2e-pill-arrow" aria-hidden="true">→</span></button></div>'
    )


def _path_covers(changed_path: str, listed: str) -> bool:
    clean = listed[2:] if listed.startswith("./") else listed
    clean = clean.strip("/")
    return bool(clean) and (changed_path == clean or changed_path.startswith(clean + "/"))


def _render_step_media(sc: dict[str, Any], st: dict[str, Any], n: int) -> str:
    parts: list[str] = []
    gif = st["gif"]
    if gif and gif.get("url"):
        size = gif.get("size")
        dims = f' width="{size[0]}" height="{size[1]}"' if size else ""
        aspect = f"{size[0] / size[1]:.4f}" if size else "1.6000"
        parts.append(
            f'<figure class="visual-card e2e-gif">'
            f'<button type="button" class="vis-zoom-btn e2e-gif-open" aria-label="Open the recording of step {n} large">'
            f'<span class="visual-stage" style="--vis-ar: {aspect}">'
            f'<img src="{_esc(gif["url"])}" loading="lazy" alt="Recording of step {n}: {_esc(st["action"])}"{dims}>'
            f"</span></button>"
            f'<figcaption class="visual-caption">{_esc(sc["title"])} · step {n}</figcaption>'
            f"</figure>"
        )
    elif gif:
        parts.append(
            f'<div class="e2e-gif-wait" role="note">GIF appears after upload'
            f' <code>{_esc(gif["name"])}</code></div>'
        )
    if st["request"] or st["response"]:
        label = (
            "Request and response" if st["request"] and st["response"] else ("Request" if st["request"] else "Response")
        )
        body = "".join(
            f'<p class="e2e-io-label">{name}</p><pre class="e2e-pre"><code>{_esc(text)}</code></pre>'
            for name, text in (("Request", st["request"]), ("Response", st["response"]))
            if text
        )
        parts.append(
            f'<details class="e2e-io"><summary>{label}</summary><div class="e2e-io-body">{body}</div></details>'
        )
    return "".join(parts)


def _render_card(sc: dict[str, Any], html_id: str, step_files: list[tuple[str, str, set[str]]]) -> str:
    status = sc["status"]
    note_html = ""
    if sc["note"]:
        note_label = {"fail": "Why it failed", "skipped": "Why it was skipped"}.get(status, "Note")
        note_html = f'<p class="e2e-note is-{status}"><strong>{note_label}:</strong> {_prose(sc["note"])}</p>'

    steps_html: list[str] = []
    for n, st in enumerate(sc["steps"], start=1):
        steps_html.append(
            f'<li class="e2e-step is-{st["status"]}">'
            f'<div class="e2e-step-row">'
            f'<span class="e2e-step-num" aria-hidden="true">{n}</span>'
            f'<div class="e2e-step-text">'
            f'<p class="e2e-action">{_prose(st["action"])}</p>'
            f'<p class="e2e-expected"><span class="e2e-k">Expected</span> {_prose(st["expected"])}</p>'
            f"</div>"
            f'<span class="e2e-status is-{st["status"]}">{STATUS_LABELS[st["status"]]}</span>'
            f"</div>"
            f"{_render_step_media(sc, st, n)}"
            f"</li>"
        )

    hits = [
        (sid, label)
        for sid, label, paths in step_files
        if any(_path_covers(p, f) for f in sc["files"] for p in paths)
    ]
    proves_html = ""
    if hits:
        links = "".join(
            f'<button type="button" class="inline-jump" data-goto-step="{_esc(sid)}">{_esc(label)}</button>'
            for sid, label in hits
        )
        proves_html = f'<p class="e2e-proves"><span class="e2e-k">Proves the code in</span>{links}</p>'

    return (
        f'<article class="e2e-card is-{status}" id="{html_id}">'
        f'<div class="e2e-card-head">'
        f'<h3 class="e2e-card-title">{_prose(sc["title"])}</h3>'
        f'<span class="e2e-card-tags">'
        f'<span class="e2e-status is-{status}">{STATUS_LABELS[status]}</span>'
        f'<span class="e2e-kind">{KIND_LABELS[sc["kind"]]}</span>'
        f'<span class="e2e-id">{_esc(sc["id"])}</span>'
        f"</span></div>"
        f'<p class="e2e-why">{_prose(sc["why"])}</p>'
        f"{note_html}"
        f'<ol class="e2e-steps">{"".join(steps_html)}</ol>'
        f"{proves_html}"
        f"</article>"
    )


def render_section_body_html(model: dict[str, Any], step_files: list[tuple[str, str, set[str]]]) -> str:
    """Everything inside the section's card, between its eyebrow and its footer.

    `step_files` lists `(step_id, "Step N: title", {changed file paths})` for the board's steps,
    so each scenario can link the steps whose diff covers its `files`.
    """
    when = format_tested_at(model["tested_at"])
    meta = []
    if model["environment"]:
        meta.append(f'<span><span class="e2e-k">Where</span>{_prose(model["environment"])}</span>')
    meta.append(
        f'<span>Tested on commit <code>{_esc(model["tested_sha"][:7])}</code>'
        f'{" · " + _esc(when) if when else ""}</span>'
    )
    stale_html = (
        f'<div class="callout e2e-stale" role="note"><strong>Heads up:</strong> {_esc(stale_sentence(model))}</div>'
        if model["stale"]
        else ""
    )
    used: set[str] = set()
    cards: list[str] = []
    for idx, sc in enumerate(model["scenarios"], start=1):
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", sc["id"]).strip("-") or str(idx)
        html_id = f"e2e-{slug}"
        while html_id in used:
            html_id = f"{html_id}-{idx}"
        used.add(html_id)
        cards.append(_render_card(sc, html_id, step_files))
    return (
        f'<h2 id="heading-e2e" class="step-title">{SECTION_TITLE}</h2>'
        f'<p class="lead-text">{_prose(model["summary"])}</p>'
        f'<p class="e2e-meta">{"".join(meta)}</p>'
        f"{stale_html}"
        f'<div class="e2e-list">{"".join(cards)}</div>'
    )


def render_eyebrow_text(model: dict[str, Any]) -> str:
    return f"End-to-end checks · {_passed_phrase(model).capitalize()}"


def render_script(panel_index: int) -> str:
    """Keep "next" buttons that lead here named after the section; the board's filter relabels them as steps."""
    label = f"{SECTION_TITLE} →"
    return (
        "<script>(() => {"
        'const main = document.getElementById("steps-main");'
        "if (!main) return;"
        f'const fix = () => main.querySelectorAll(\'.step-next-wrap [data-goto-index="{panel_index}"]\').forEach((b) => {{'
        f'if (b.textContent !== "{label}") b.textContent = "{label}";'
        "});"
        "new MutationObserver(fix).observe(main, { childList: true, subtree: true });"
        "fix();"
        "})();</script>"
    )


def _md(value: Any) -> str:
    """One line of untrusted text, safe inside a Markdown table cell."""
    text = " ".join(str(value or "").split())
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", r"\|")


def render_markdown(model: dict[str, Any]) -> list[str]:
    when = format_tested_at(model["tested_at"])
    commit = f"Tested on commit `{model['tested_sha'][:7]}`" + (f" · {_md(when)}" if when else "")
    if model["environment"]:
        commit += f" · {_md(model['environment'])}"
    counts = model["counts"]
    tally = [_passed_phrase(model).capitalize()]
    tally += [f"{counts[k]} {word}" for k, word in (("fail", "failed"), ("skipped", "skipped")) if counts[k]]
    lines = [f"## {SECTION_TITLE}", "", _md(model["summary"]), "", f"{commit}. **{', '.join(tally)}.**", ""]
    if model["stale"]:
        lines += [f"> **Heads up:** {stale_sentence(model)}", ""]
    lines += ["| Scenario | How | Result |", "| :--- | :--- | :--- |"]
    for sc in model["scenarios"]:
        result = STATUS_LABELS[sc["status"]] + (f": {_md(sc['note'])}" if sc["note"] else "")
        gifs = [
            f"[step {n}]({st['gif']['url']})"
            for n, st in enumerate(sc["steps"], start=1)
            if st["gif"] and str(st["gif"].get("url") or "").startswith("https://")
        ]
        if gifs:
            result += f" (GIFs: {', '.join(gifs)})"
        lines.append(f"| {_md(sc['title'])} | {KIND_LABELS[sc['kind']]} | {result} |")
    lines.append("")
    return lines


E2E_CSS = r"""
    /* "Tested end to end": results of end-to-end checks someone ran on this PR */
    .arc-tab-e2e { margin-top: 0.45rem; }
    .e2e-count {
      flex-shrink: 0;
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.66rem;
      font-weight: 700;
      padding: 0.06rem 0.38rem;
      border-radius: 999px;
    }
    .e2e-count.is-pass, .e2e-status.is-pass { background: var(--add-bg); color: var(--add-fg); }
    .e2e-count.is-fail, .e2e-status.is-fail { background: var(--del-bg); color: var(--del-fg); }
    .e2e-count.is-skipped, .e2e-status.is-skipped { background: var(--subtle); color: var(--muted); box-shadow: inset 0 0 0 1px var(--border); }

    .e2e-pill-row { margin: 0 0 1.1rem; }
    .e2e-pill {
      appearance: none;
      font: inherit;
      font-size: 0.85rem;
      text-align: left;
      cursor: pointer;
      display: inline-flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.2rem 0.45rem;
      padding: 0.32rem 0.8rem;
      border: 1px solid var(--border);
      border-radius: 1.1rem;
      background: var(--surface);
      color: var(--text);
    }
    .e2e-pill:hover { border-color: var(--accent); }
    .e2e-pill strong { font-weight: 680; }
    .e2e-pill.is-pass strong { color: var(--add-fg); }
    .e2e-dot { inline-size: 0.55rem; block-size: 0.55rem; border-radius: 999px; background: var(--muted); flex-shrink: 0; }
    .e2e-pill.is-pass .e2e-dot { background: var(--add-fg); }
    .e2e-pill.is-fail .e2e-dot { background: var(--del-fg); }
    .e2e-pill-fail { color: var(--del-fg); font-weight: 650; }
    .e2e-pill-skip { color: var(--muted); font-weight: 600; }
    .e2e-pill-stale { color: var(--warn-fg); font-weight: 650; }
    .e2e-pill-sep { color: var(--muted); }
    .e2e-pill-arrow { color: var(--accent); }

    .e2e-k {
      font-size: 0.68rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      margin-right: 0.4rem;
    }
    .e2e-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 0.3rem 1.2rem;
      margin: -0.6rem 0 1.15rem;
      font-size: 0.85rem;
      color: var(--muted);
    }
    .e2e-meta code { font-size: 0.82rem; color: var(--text); }
    .e2e-stale { max-width: none; }

    .e2e-list { display: flex; flex-direction: column; gap: 0.9rem; }
    .e2e-card {
      display: flex;
      flex-direction: column;
      gap: 0.65rem;
      padding: 0.9rem 1.05rem 1rem;
      border: 1px solid var(--border);
      border-inline-start: 3px solid var(--add-fg);
      border-radius: 10px;
      background: var(--surface);
    }
    .e2e-card.is-fail { border-inline-start-color: var(--del-fg); }
    .e2e-card.is-skipped { border-inline-start-color: var(--muted); }
    .e2e-card-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 0.35rem 0.9rem; }
    .e2e-card-title { margin: 0; font-size: 1rem; font-weight: 660; line-height: 1.4; overflow-wrap: anywhere; }
    .e2e-card-tags { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 0.35rem; }
    .e2e-status {
      font-size: 0.66rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      padding: 0.06rem 0.45rem;
      border-radius: 999px;
      white-space: nowrap;
    }
    .e2e-kind { font-size: 0.75rem; color: var(--muted); padding: 0.02rem 0.5rem; border: 1px solid var(--border); border-radius: 999px; white-space: nowrap; }
    .e2e-id { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 0.72rem; color: var(--muted); }
    .e2e-why { margin: 0; max-width: 75ch; font-size: 0.93rem; line-height: 1.6; }
    .e2e-note {
      margin: 0;
      max-width: 75ch;
      padding: 0.5rem 0.8rem;
      border-left: 3px solid var(--muted);
      border-radius: 0 6px 6px 0;
      background: var(--subtle);
      font-size: 0.88rem;
      line-height: 1.55;
    }
    .e2e-note.is-fail { border-left-color: var(--del-fg); background: var(--del-bg); }

    .e2e-steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
    .e2e-step { display: flex; flex-direction: column; gap: 0.55rem; padding: 0.55rem 0.7rem; border-radius: 8px; background: var(--subtle); }
    .e2e-step-row { display: flex; align-items: flex-start; gap: 0.6rem; }
    .e2e-step-num {
      flex-shrink: 0;
      inline-size: 1.4rem;
      block-size: 1.4rem;
      margin-top: 0.08rem;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.72rem;
      font-weight: 700;
      color: var(--muted);
      background: var(--surface);
      border: 1px solid var(--border);
    }
    .e2e-step-text { flex: 1; min-width: 0; }
    .e2e-action { margin: 0; font-size: 0.9rem; font-weight: 600; line-height: 1.5; overflow-wrap: anywhere; }
    .e2e-expected { margin: 0.1rem 0 0; font-size: 0.86rem; line-height: 1.5; overflow-wrap: anywhere; }
    .e2e-step-row > .e2e-status { margin-top: 0.18rem; }

    .e2e-gif { max-inline-size: 760px; background: var(--surface); }
    .e2e-gif .e2e-gif-open { display: block; inline-size: 100%; padding: 0; border: 0; border-radius: 0; background: var(--surface); cursor: zoom-in; }
    .e2e-gif .visual-stage { max-inline-size: calc(min(60vh, 480px) * var(--vis-ar, 1.6)); }
    .e2e-gif .visual-caption { display: none; }
    .e2e-gif-wait {
      align-self: flex-start;
      display: inline-flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.4rem;
      padding: 0.4rem 0.7rem;
      border: 1px dashed var(--border);
      border-radius: 7px;
      background: var(--surface);
      font-size: 0.8rem;
      color: var(--muted);
    }

    .e2e-io { border: 1px solid var(--border); border-radius: 7px; background: var(--surface); min-width: 0; }
    .e2e-io > summary { padding: 0.35rem 0.65rem; font-size: 0.8rem; font-weight: 600; color: var(--muted); }
    .e2e-io-body { padding: 0 0.65rem 0.65rem; display: flex; flex-direction: column; gap: 0.3rem; }
    .e2e-io-label { margin: 0.3rem 0 0; font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
    .e2e-pre {
      margin: 0;
      max-block-size: 22rem;
      overflow: auto;
      padding: 0.55rem 0.7rem;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--subtle);
      font-size: 0.78rem;
      line-height: 1.5;
      white-space: pre;
    }

    .e2e-proves { margin: 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.25rem 0.9rem; font-size: 0.86rem; }

    /* Narrow cards: the step's status moves under its text so the text keeps the full width. */
    @container (max-width: 560px) {
      .e2e-card { padding: 0.8rem 0.8rem 0.9rem; }
      .e2e-step { padding: 0.5rem 0.55rem; }
      .e2e-step-row { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 0.3rem 0.55rem; }
      .e2e-step-row > .e2e-status { grid-column: 2; justify-self: start; margin-top: 0; }
    }
"""


# ---------------------------------------------------------------------------
# CLI: record uploaded URLs
# ---------------------------------------------------------------------------

def record_urls(asset_urls_path: Path, pairs: list[str]) -> dict[str, str]:
    """Merge `<file>=<url>` pairs into the asset-urls file, keyed by each file's sha256."""
    existing: dict[str, str] = {}
    if asset_urls_path.exists():
        raw = json.loads(asset_urls_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise E2EResultsError(f"`{asset_urls_path}` must hold one JSON object.")
        existing = {str(k): str(v) for k, v in raw.items()}
    for pair in pairs:
        file_part, sep, url = pair.rpartition("=")
        if not sep or not file_part or not is_safe_asset_url(url.strip()):
            raise E2EResultsError(f"`{pair}` must look like <file>=<https:// or /_blob/ url>.")
        path = Path(file_part)
        if not path.is_file():
            raise E2EResultsError(f"`{file_part}` not found.")
        existing[sha256_file(path)] = url.strip()
    asset_urls_path.parent.mkdir(parents=True, exist_ok=True)
    asset_urls_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return existing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helpers for the board's \"Tested end to end\" section.")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record-urls", help="Save uploaded GIF URLs into the asset-urls file, keyed by sha256")
    rec.add_argument("--asset-urls", required=True, help="Path to asset-urls.json (created if missing)")
    rec.add_argument("pairs", nargs="+", help="<file>=<url> for each uploaded file")
    args = parser.parse_args(argv)
    try:
        saved = record_urls(Path(args.asset_urls), args.pairs)
    except (E2EResultsError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"✓ {len(saved)} uploaded file URL(s) in {args.asset_urls}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
