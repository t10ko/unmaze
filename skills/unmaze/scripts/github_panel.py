"""GitHub review status on the board: approvals, checks, comment threads and a comment box.

Two layers share one renderer in the page:

* **Snapshot** — `pr_activity.py` saves the PR's reviews, checks and comments when the board
  is built. Every viewer sees it, with its age, even with no GitHub connection.
* **Live** — when a viewer has a claude.ai connector named "GitHub" (GitHub's own MCP server),
  the page reads fresh data through it every minute and can post comments as that viewer.
  Tokens never reach the page; claude.ai makes the calls with the viewer's own login.

Without a connection the header says so, and the comment box copies the text and opens the
exact line on GitHub instead.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pr_activity import parse_pr_url

DEFAULT_CONNECTOR_NAME = "GitHub"
# Tool names of GitHub's MCP server (https://github.com/github/github-mcp-server). The page reads
# each tool's schema at view time and treats anything it can't use as "not connected".
GITHUB_CONNECTOR_TOOLS = [
    "pull_request_read",
    "pull_request_review_write",
    "add_comment_to_pending_review",
    "add_issue_comment",
]

_PR_STATES = {"OPEN", "CLOSED", "MERGED"}
_DECISIONS = {"APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}
_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}
_CHECK_STATES = {"passed", "failed", "pending", "skipped"}


def _s(value: Any, limit: int = 4000) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _url(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith("https://") else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def sanitize_activity(raw: Any) -> dict[str, Any] | None:
    """Keep only well-formed fields of a `pr-activity.json` snapshot (it is shown to every viewer)."""
    if not isinstance(raw, dict):
        return None
    reviews = [
        {
            "author": _s(r.get("author"), 80) or "ghost",
            "state": str(r.get("state")).upper(),
            "submitted_at": _s(r.get("submitted_at"), 40) or None,
            "url": _url(r.get("url")),
        }
        for r in (raw.get("reviews") or [])
        if isinstance(r, dict) and str(r.get("state") or "").upper() in _REVIEW_STATES
    ][:60]
    requested = [_s(x, 80) for x in (raw.get("requested_reviewers") or []) if isinstance(x, str) and x][:30]

    checks_raw = raw.get("checks") if isinstance(raw.get("checks"), dict) else {}
    check_items = [
        {
            "name": _s(c.get("name"), 160) or "check",
            "state": c.get("state"),
            "url": _url(c.get("url")),
            "group": _s(c.get("group"), 120),
        }
        for c in (checks_raw.get("items") or [])
        if isinstance(c, dict) and c.get("state") in _CHECK_STATES
    ][:150]
    summary = {k: sum(1 for c in check_items if c["state"] == k) for k in ("passed", "failed", "pending", "skipped")}
    summary["total"] = len(check_items)

    def _comments(items: Any) -> list[dict[str, Any]]:
        return [
            {
                "author": _s(c.get("author"), 80) or "ghost",
                "body": _s(c.get("body")),
                "created_at": _s(c.get("created_at"), 40) or None,
                "url": _url(c.get("url")),
            }
            for c in (items or [])
            if isinstance(c, dict)
        ][:60]

    threads = []
    for t in (raw.get("threads") or [])[:200]:
        if not isinstance(t, dict) or not t.get("path"):
            continue
        comments = _comments(t.get("comments"))
        if not comments:
            continue
        threads.append(
            {
                "id": _s(t.get("id"), 120),
                "path": _s(t.get("path"), 400),
                "line": _int(t.get("line")),
                "start_line": _int(t.get("start_line")),
                "original_line": _int(t.get("original_line")),
                "side": "LEFT" if str(t.get("side") or "").upper() == "LEFT" else "RIGHT",
                "is_resolved": bool(t.get("is_resolved")),
                "is_outdated": bool(t.get("is_outdated")),
                "is_file": bool(t.get("is_file")),
                "comments": comments,
            }
        )

    conversation = [
        {
            "kind": "review" if c.get("kind") == "review" else "comment",
            "author": _s(c.get("author"), 80) or "ghost",
            "state": str(c.get("state")).upper() if str(c.get("state") or "").upper() in _REVIEW_STATES else None,
            "body": _s(c.get("body")),
            "created_at": _s(c.get("created_at"), 40) or None,
            "url": _url(c.get("url")),
        }
        for c in (raw.get("conversation") or [])
        if isinstance(c, dict)
    ][-150:]

    state = str(raw.get("state") or "").upper()
    decision = str(raw.get("review_decision") or "").upper()
    return {
        "fetched_at": _s(raw.get("fetched_at"), 40) or None,
        "state": state if state in _PR_STATES else "OPEN",
        "is_draft": bool(raw.get("is_draft")),
        "review_decision": decision if decision in _DECISIONS else None,
        "head_sha": _s(raw.get("head_sha"), 64) or None,
        "reviews": reviews,
        "requested_reviewers": requested,
        "checks": {"summary": summary, "items": check_items, "truncated": bool(checks_raw.get("truncated"))},
        "threads": threads,
        "threads_truncated": bool(raw.get("threads_truncated")),
        "conversation": conversation,
    }


def build_github_config(
    meta: dict[str, Any],
    activity: dict[str, Any] | None,
    connector_name: str = DEFAULT_CONNECTOR_NAME,
) -> dict[str, Any] | None:
    """Everything the page needs about the PR, or None when the board isn't about a GitHub PR."""
    parts = parse_pr_url(meta.get("pr_url"))
    if not parts:
        return None
    snapshot = sanitize_activity(activity) if activity is not None else None
    head_sha = (snapshot or {}).get("head_sha") or meta.get("head_sha")
    return {
        "pr": {
            "owner": parts["owner"],
            "repo": parts["repo"],
            "number": parts["number"],
            "url": parts["url"],
            "host": parts["host"],
            "head_sha": head_sha if isinstance(head_sha, str) and len(head_sha) == 40 else None,
        },
        "connector": connector_name or DEFAULT_CONNECTOR_NAME,
        "live_supported": parts["host"] == "github.com",
        "snapshot": snapshot,
    }


def github_capabilities(connector_name: str = DEFAULT_CONNECTOR_NAME) -> dict[str, Any]:
    """The `capabilities` value to pass to the Artifact tool when publishing a board with a GitHub PR."""
    return {"mcp": {"servers": [{"server": connector_name, "tools": list(GITHUB_CONNECTOR_TOOLS)}]}}


def render_github_data_script(cfg: dict[str, Any] | None) -> str:
    if not cfg:
        return ""
    payload = json.dumps(cfg, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/json" id="gh-data">{payload}</script>'


def render_github_header_html(cfg: dict[str, Any] | None) -> str:
    if not cfg:
        return ""
    return '<div id="gh-status" class="gh-status" role="group" aria-label="GitHub review status"></div>'


def render_github_dialog_html(cfg: dict[str, Any] | None) -> str:
    if not cfg:
        return ""
    return (
        '<dialog id="gh-dialog" class="gh-dialog" aria-labelledby="gh-dialog-title">'
        '<div class="gh-dlg-head"><h2 id="gh-dialog-title">GitHub</h2>'
        '<button type="button" class="gh-btn" data-gh-close aria-label="Close GitHub panel">Close ✕</button></div>'
        '<div id="gh-dialog-body" class="gh-dlg-body"></div>'
        "</dialog>"
    )


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "unknown time"
    try:
        dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return iso
    return dt.strftime("%Y-%m-%d %H:%M UTC")


DECISION_LABELS = {
    "APPROVED": "Approved",
    "CHANGES_REQUESTED": "Changes requested",
    "REVIEW_REQUIRED": "Review required",
}


def render_github_markdown(cfg: dict[str, Any] | None) -> list[str]:
    """A short "Review status" section for the Markdown copy of the board."""
    if not cfg:
        return []
    snap = cfg.get("snapshot")
    lines = ["## Review status", ""]
    if not snap:
        lines += [f"No GitHub snapshot was saved when this guide was built. See {cfg['pr']['url']}.", ""]
        return lines
    if snap["state"] == "MERGED":
        status = "Merged"
    elif snap["state"] == "CLOSED":
        status = "Closed"
    elif snap["is_draft"]:
        status = "Draft"
    else:
        status = DECISION_LABELS.get(snap["review_decision"] or "", "No review decision yet")
    lines.append(f"_Snapshot from {_fmt_time(snap['fetched_at'])}._")
    lines.append("")
    lines.append(f"- **Status:** {status}")
    if snap["reviews"]:
        who = ", ".join(f"{r['author']} ({r['state'].replace('_', ' ').lower()})" for r in snap["reviews"])
        lines.append(f"- **Reviews:** {who}")
    if snap["requested_reviewers"]:
        lines.append(f"- **Waiting on:** {', '.join(snap['requested_reviewers'])}")
    s = snap["checks"]["summary"]
    if s["total"]:
        lines.append(
            f"- **Checks:** {s['passed']} passed, {s['failed']} failed, {s['pending']} running, {s['skipped']} skipped"
        )
        for c in snap["checks"]["items"]:
            if c["state"] == "failed":
                lines.append(f"  - Failed: {c['name']}" + (f" ({c['url']})" if c["url"] else ""))
    open_threads = [t for t in snap["threads"] if not t["is_resolved"]]
    lines.append(f"- **Open comment threads:** {len(open_threads)} (resolved: {len(snap['threads']) - len(open_threads)})")
    for t in open_threads[:20]:
        first = t["comments"][0]
        where = f"{t['path']}:{t['line'] or t['original_line'] or '?'}"
        text = " ".join(first["body"].split())
        if len(text) > 140:
            text = text[:139] + "…"
        lines.append(f"  - `{where}` — {first['author']}: {text}")
    lines.append("")
    return lines


GH_CSS = r"""
    /* GitHub review status: header pills, side panel, inline comment threads and comment box */
    .gh-status { display: flex; flex-wrap: wrap; align-items: center; gap: 0.35rem; }
    .gh-status:empty { display: none; }
    .gh-pill {
      appearance: none;
      display: inline-flex;
      align-items: center;
      gap: 0.32rem;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      border-radius: 999px;
      padding: 0.16rem 0.62rem;
      font: 600 0.74rem/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      cursor: pointer;
      white-space: nowrap;
    }
    .gh-pill:hover { border-color: var(--text); }
    .gh-pill:focus-visible, .gh-btn:focus-visible, .gh-add-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    .gh-ico { font-weight: 800; }
    .gh-ok { color: var(--add-fg); }
    .gh-bad { color: var(--del-fg); }
    .gh-wait { color: var(--warn-fg); }
    .gh-merged { color: var(--tok-fn); }
    .gh-muted { color: var(--muted); }
    .gh-pill.gh-ok { border-color: currentColor; }
    .gh-pill.gh-bad { border-color: currentColor; }
    .gh-pill.gh-wait { border-color: currentColor; }
    .gh-pill.gh-merged { border-color: currentColor; }
    .gh-conn { border-style: solid; }
    .gh-conn.is-offline { background: var(--warn-bg); color: var(--warn-fg); border-color: transparent; }
    .gh-conn.is-ready { background: var(--accent-soft); color: var(--accent); border-color: transparent; }
    .gh-conn.is-live { color: var(--add-fg); }
    .gh-dot { inline-size: 0.5rem; block-size: 0.5rem; border-radius: 999px; background: currentColor; flex: none; }
    .gh-conn.is-checking .gh-dot, .gh-conn.is-connecting .gh-dot { opacity: 0.45; }
    .gh-age { font-weight: 500; opacity: 0.85; }
    @media (max-width: 760px) {
      .gh-long { display: none; }
      .gh-pill { padding: 0.14rem 0.5rem; }
    }

    .gh-dialog {
      margin: 0 0 0 auto;
      inline-size: min(31rem, 100vw);
      max-inline-size: 100vw;
      block-size: 100dvh;
      max-block-size: 100dvh;
      padding: 0;
      border: none;
      border-inline-start: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    .gh-dialog::backdrop { background: rgba(0, 0, 0, 0.4); }
    .gh-dlg-head {
      position: sticky;
      top: 0;
      z-index: 1;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      padding: 0.75rem 1rem;
      background: var(--rail);
      border-block-end: 1px solid var(--border);
    }
    .gh-dlg-head h2 { margin: 0; font-size: 1rem; }
    .gh-dlg-body { padding: 0.9rem 1rem 2.5rem; display: grid; gap: 1.25rem; font-size: 0.88rem; line-height: 1.55; }
    .gh-sec { scroll-margin-top: 4rem; }
    .gh-sec h3 {
      margin: 0 0 0.5rem;
      font-size: 0.74rem;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .gh-sec-note { margin: 0.35rem 0 0; color: var(--muted); font-size: 0.78rem; }
    .gh-box { border-radius: 10px; padding: 0.75rem 0.9rem; background: var(--subtle); border: 1px solid var(--border); }
    .gh-box.is-offline { background: var(--warn-bg); border-color: transparent; }
    .gh-box.is-live { background: var(--add-bg); border-color: transparent; }
    .gh-box p { margin: 0.25rem 0; }
    .gh-box strong { font-size: 0.92rem; }
    .gh-steps { margin: 0.4rem 0 0; padding-inline-start: 1.2rem; }
    .gh-list { list-style: none; margin: 0; padding: 0; }
    .gh-list li {
      display: flex;
      align-items: baseline;
      gap: 0.5rem;
      padding: 0.35rem 0;
      border-block-end: 1px solid var(--border);
      overflow-wrap: anywhere;
    }
    .gh-list li:last-child { border-block-end: none; }
    .gh-list .gh-grow { flex: 1; min-width: 0; }
    .gh-sub { color: var(--muted); font-size: 0.78rem; }
    .gh-chip {
      flex: none;
      font-size: 0.68rem;
      font-weight: 700;
      padding: 0.02rem 0.42rem;
      border-radius: 999px;
      border: 1px solid currentColor;
      white-space: nowrap;
    }
    .gh-link { color: var(--accent); text-decoration: none; white-space: nowrap; font-size: 0.78rem; }
    .gh-link:hover { text-decoration: underline; }
    .gh-btn {
      appearance: none;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      border-radius: 6px;
      padding: 0.25rem 0.7rem;
      font: 600 0.78rem/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      cursor: pointer;
    }
    .gh-btn:hover { border-color: var(--text); }
    .gh-btn-primary { background: var(--accent); border-color: var(--accent); color: var(--surface); }
    .gh-btn-primary:hover { filter: brightness(1.08); border-color: var(--accent); }
    .gh-btn[disabled] { opacity: 0.6; cursor: progress; }
    .gh-conv-item { padding: 0.55rem 0; border-block-end: 1px solid var(--border); }
    .gh-conv-item:last-child { border-block-end: none; }
    .gh-conv-head { display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem; font-size: 0.78rem; color: var(--muted); }
    .gh-conv-head strong { color: var(--text); }

    .gh-body { overflow-wrap: anywhere; }
    .gh-body .gh-p { display: block; margin: 0.2rem 0; }
    .gh-body .gh-code-block {
      display: block;
      margin: 0.35rem 0;
      padding: 0.45rem 0.6rem;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--surface);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.76rem;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-x: auto;
    }
    .gh-body .gh-code-label { display: block; font-size: 0.7rem; font-weight: 700; color: var(--add-fg); margin-top: 0.35rem; }
    .gh-body code {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.8em;
      padding: 0 0.25rem;
      border-radius: 4px;
      background: var(--surface);
    }
    .gh-body a { color: var(--accent); }

    .diff-view .dl { position: relative; }
    .gh-thread {
      position: sticky;
      left: 0;
      box-sizing: border-box;
      inline-size: 100cqi;
      max-inline-size: 100cqi;
      display: block;
      margin: 0.35rem 0;
      padding: 0.5rem 0.85rem 0.55rem 0.75rem;
      border-inline-start: 3px solid var(--warn-fg);
      background: var(--subtle);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 0.84rem;
      line-height: 1.5;
      white-space: normal;
    }
    .gh-thread.is-resolved { border-inline-start-color: var(--border); }
    .gh-thread.is-local { border-inline-start-color: var(--accent); }
    .gh-thread-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.4rem;
      font-size: 0.76rem;
      color: var(--muted);
    }
    .gh-thread-head strong { color: var(--text); }
    .gh-thread-head .gh-link { margin-inline-start: auto; }
    .gh-thread-toggle {
      appearance: none;
      border: none;
      background: none;
      color: var(--accent);
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      padding: 0;
    }
    .gh-thread.is-collapsed .gh-thread-body { display: none; }
    .gh-comment { display: block; margin-top: 0.3rem; }
    .gh-comment + .gh-comment { border-block-start: 1px solid var(--border); margin-top: 0.45rem; padding-top: 0.45rem; }
    .gh-comment-meta { display: block; font-size: 0.74rem; color: var(--muted); }
    .gh-comment-meta strong { color: var(--text); }
    .gh-thread-flash { animation: gh-flash 1.6s ease-out 1; }
    @keyframes gh-flash { 0% { box-shadow: 0 0 0 3px var(--accent); } 100% { box-shadow: 0 0 0 0 transparent; } }

    .gh-add-btn {
      position: absolute;
      left: 0.15rem;
      top: 50%;
      transform: translateY(-50%);
      inline-size: 1.15rem;
      block-size: 1.15rem;
      padding: 0;
      border: none;
      border-radius: 4px;
      background: var(--accent);
      color: var(--surface);
      font: 800 0.85rem/1 -apple-system, BlinkMacSystemFont, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      z-index: 2;
    }
    .gh-composer {
      position: sticky;
      left: 0;
      box-sizing: border-box;
      inline-size: 100cqi;
      max-inline-size: 100cqi;
      display: block;
      margin: 0.35rem 0;
      padding: 0.6rem 0.85rem;
      border: 1px solid var(--accent);
      border-radius: 8px;
      background: var(--surface);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 0.82rem;
      white-space: normal;
    }
    .gh-composer-head { display: block; color: var(--muted); font-size: 0.76rem; margin-bottom: 0.35rem; }
    .gh-composer textarea, .gh-general textarea {
      box-sizing: border-box;
      display: block;
      inline-size: 100%;
      min-block-size: 4.5rem;
      padding: 0.45rem 0.55rem;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--bg);
      color: var(--text);
      font: 0.84rem/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      resize: vertical;
    }
    .gh-composer-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 0.45rem; margin-top: 0.45rem; }
    .gh-msg { font-size: 0.76rem; color: var(--muted); }
    .gh-msg.is-error { color: var(--del-fg); }
    .gh-msg.is-ok { color: var(--add-fg); }
    .gh-nav-count {
      margin-inline-start: 0.3rem;
      font-size: 0.66rem;
      font-weight: 700;
      color: var(--warn-fg);
      white-space: nowrap;
    }
    @media (forced-colors: active) {
      .gh-thread, .gh-composer { border: 1px solid CanvasText; }
    }
"""


GH_JS = r"""
(() => {
  "use strict";
  const dataEl = document.getElementById("gh-data");
  const statusEl = document.getElementById("gh-status");
  const dialog = document.getElementById("gh-dialog");
  const dialogBody = document.getElementById("gh-dialog-body");
  if (!dataEl || !statusEl || !dialog || !dialogBody) return;
  let cfg;
  try { cfg = JSON.parse(dataEl.textContent || "{}"); } catch (_) { return; }
  const PR = cfg.pr || {};
  const SERVER = cfg.connector || "GitHub";
  const READ_TOOL = "pull_request_read";
  const REVIEW_WRITE_TOOL = "pull_request_review_write";
  const LINE_COMMENT_TOOL = "add_comment_to_pending_review";
  const ISSUE_COMMENT_TOOL = "add_issue_comment";
  const REFRESH_MS = 60000;

  const snap = cfg.snapshot || null;
  const fromSnapshot = () => ({
    prState: snap ? snap.state : null,
    isDraft: snap ? !!snap.is_draft : false,
    decision: snap ? snap.review_decision : null,
    reviews: snap ? (snap.reviews || []) : [],
    requested: snap ? (snap.requested_reviewers || []) : [],
    checks: snap ? (snap.checks || null) : null,
    threads: snap ? (snap.threads || []) : [],
    threadsTruncated: snap ? !!snap.threads_truncated : false,
    conversation: snap ? (snap.conversation || []) : [],
  });
  let model = fromSnapshot();
  const liveAt = {};        // section -> ms when live data last arrived
  const liveShapeErr = {};  // section -> true when live data came back in a shape we can't read
  const localThreads = [];  // comments posted from this page, shown until the next live refresh
  const conn = { state: "checking", reason: "", mcp: null, unsubs: [], lastUpdate: null };

  // ---------- small helpers ----------
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  function safeUrl(u) { return typeof u === "string" && /^https:\/\//.test(u) ? u : null; }
  function link(href, text, cls) {
    const a = el("a", cls || "gh-link", text);
    a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
    return a;
  }
  function ago(iso) {
    const t = Date.parse(iso || "");
    if (!isFinite(t)) return "";
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return "just now";
    const m = Math.round(s / 60); if (m < 60) return m + " min ago";
    const h = Math.round(m / 60); if (h < 48) return h + " h ago";
    const d = Math.round(h / 24); if (d < 60) return d + " days ago";
    return new Date(t).toLocaleDateString();
  }
  function absTime(iso) {
    const t = Date.parse(iso || "");
    return isFinite(t) ? new Date(t).toLocaleString() : "";
  }
  function timeEl(iso) {
    const s = el("span", "gh-time", ago(iso));
    const abs = absTime(iso); if (abs) s.title = abs;
    return s;
  }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : (many || one + "s")); }
  const STATE_LABEL = { APPROVED: "approved", CHANGES_REQUESTED: "changes requested", COMMENTED: "commented", DISMISSED: "dismissed" };
  const STATE_CLASS = { APPROVED: "gh-ok", CHANGES_REQUESTED: "gh-bad", COMMENTED: "gh-muted", DISMISSED: "gh-muted" };

  // Comment bodies are Markdown written by people: render a small, safe subset with DOM nodes only.
  function inlineMd(text, parent) {
    const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|\[([^\]\n]+)\]\((https:\/\/[^)\s]+)\)|(https:\/\/[^\s<>()]+)/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1]) parent.appendChild(el("code", null, m[1].slice(1, -1)));
      else if (m[2]) parent.appendChild(el("strong", null, m[2].slice(2, -2)));
      else if (m[3]) parent.appendChild(link(m[4], m[3], ""));
      else if (m[5]) parent.appendChild(link(m[5], m[5], ""));
      last = re.lastIndex;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }
  function renderBody(raw) {
    const box = el("span", "gh-body");
    const text = String(raw || "").replace(/\r\n?/g, "\n").trim();
    if (!text) { box.appendChild(el("span", "gh-p gh-muted", "(no text)")); return box; }
    const parts = text.split(/```([^\n`]*)\n([\s\S]*?)(?:```|$)/g);
    for (let i = 0; i < parts.length; i += 3) {
      const prose = parts[i];
      if (prose && prose.trim()) {
        prose.split(/\n{2,}/).forEach((para) => {
          const p = el("span", "gh-p");
          const lines = para.split("\n");
          lines.forEach((ln, j) => {
            inlineMd(ln.replace(/^\s*[-*]\s+/, "• ").replace(/^#{1,6}\s+/, ""), p);
            if (j < lines.length - 1) p.appendChild(document.createElement("br"));
          });
          box.appendChild(p);
        });
      }
      if (i + 2 < parts.length) {
        const lang = (parts[i + 1] || "").trim();
        if (lang === "suggestion") box.appendChild(el("span", "gh-code-label", "Suggested change"));
        box.appendChild(el("span", "gh-code-block", parts[i + 2].replace(/\n$/, "")));
      }
    }
    return box;
  }

  // ---------- header pills ----------
  function reviewPill() {
    if (model.prState === "MERGED") return { cls: "gh-merged", ico: "⇄", text: "Merged", long: "" };
    if (model.prState === "CLOSED") return { cls: "gh-muted", ico: "✕", text: "Closed", long: "" };
    if (model.isDraft) return { cls: "gh-muted", ico: "○", text: "Draft", long: "" };
    const approvals = model.reviews.filter((r) => r.state === "APPROVED").length;
    if (model.decision === "APPROVED" || (!model.decision && approvals && !model.reviews.some((r) => r.state === "CHANGES_REQUESTED")))
      return { cls: "gh-ok", ico: "✓", text: "Approved", long: approvals > 1 ? " · " + approvals : "" };
    if (model.decision === "CHANGES_REQUESTED" || model.reviews.some((r) => r.state === "CHANGES_REQUESTED"))
      return { cls: "gh-bad", ico: "✕", text: "Changes requested", long: "" };
    if (model.decision === "REVIEW_REQUIRED") return { cls: "gh-wait", ico: "●", text: "Review required", long: "" };
    return { cls: "gh-muted", ico: "○", text: "No reviews yet", long: "" };
  }
  function checksPill() {
    const c = model.checks;
    if (!c || !c.summary || !c.summary.total) return null;
    const s = c.summary;
    if (s.failed) return { cls: "gh-bad", ico: "✕", text: s.failed + " failed", long: " of " + plural(s.total, "check") };
    if (s.pending) return { cls: "gh-wait", ico: "●", text: s.pending + " running", long: " of " + plural(s.total, "check") };
    return { cls: "gh-ok", ico: "✓", text: s.total === 1 ? "Check passed" : "Checks passed", long: " · " + s.total };
  }
  function openThreads() { return allThreads().filter((t) => !t.is_resolved); }
  function allThreads() { return model.threads.concat(localThreads); }
  function pill(info, section, extraCls) {
    const b = el("button", "gh-pill " + info.cls + (extraCls ? " " + extraCls : ""));
    b.type = "button";
    b.dataset.ghOpen = section;
    b.appendChild(el("span", "gh-ico", info.ico)).setAttribute("aria-hidden", "true");
    b.appendChild(el("span", null, info.text));
    if (info.long) b.appendChild(el("span", "gh-long", info.long));
    b.setAttribute("aria-label", info.text + (info.long || "") + " — open GitHub panel");
    return b;
  }
  function connPill() {
    const b = el("button", "gh-pill gh-conn is-" + (conn.state === "ready" ? "offline" : conn.state));
    b.type = "button";
    b.dataset.ghOpen = "connection";
    b.appendChild(el("span", "gh-dot")).setAttribute("aria-hidden", "true");
    let text = "Checking GitHub…", long = "";
    if (conn.state === "live") { text = "Live"; long = conn.lastUpdate ? " · " + ago(new Date(conn.lastUpdate).toISOString()) : ""; }
    else if (conn.state === "connecting") text = "Connecting to GitHub…";
    else if (conn.state === "offline" || conn.state === "ready") {
      text = "Not connected to GitHub";
      long = snap ? " · snapshot " + ago(snap.fetched_at) : " · no snapshot";
    }
    b.appendChild(el("span", null, text));
    if (long) b.appendChild(el("span", "gh-long gh-age", long));
    b.setAttribute("aria-label", text + long + " — open GitHub panel");
    return b;
  }
  function renderHeader() {
    const frag = document.createDocumentFragment();
    const haveData = !!snap || Object.keys(liveAt).length > 0;
    if (haveData) {
      frag.appendChild(pill(reviewPill(), "reviews"));
      const cp = checksPill(); if (cp) frag.appendChild(pill(cp, "checks"));
      const open = openThreads().length;
      frag.appendChild(pill(
        { cls: open ? "gh-wait" : "gh-muted", ico: "💬", text: open ? String(open) : "0", long: open === 1 ? " open thread" : " open threads" },
        "threads"));
    }
    frag.appendChild(connPill());
    statusEl.replaceChildren(frag);
  }

  // ---------- inline threads in the walkthrough ----------
  let rowMap = null;
  function buildRowMap() {
    rowMap = new Map();
    document.querySelectorAll(".hunk-card[data-path]").forEach((card) => {
      const pNew = card.dataset.path, pOld = card.dataset.oldPath || pNew;
      card.querySelectorAll(".diff-view .dl").forEach((row) => {
        const pn = row.dataset.pn, po = row.dataset.po;
        const put = (k) => { const a = rowMap.get(k); if (a) a.push(row); else rowMap.set(k, [row]); };
        if (pn) put(pNew + "|RIGHT|" + pn);
        if (po) put(pOld + "|LEFT|" + po);
      });
    });
  }
  function bestRow(t) {
    if (t.is_outdated || !t.line) return null;
    const rows = (rowMap.get(t.path + "|" + t.side + "|" + t.line) || []).filter((r) => !r.hidden);
    if (!rows.length) return null;
    const changed = rows.find((r) => r.classList.contains("dl-add") || r.classList.contains("dl-del"));
    return changed || rows[0];
  }
  function lineLabel(t) {
    const ln = t.line || t.original_line;
    if (!ln) return "file";
    return t.start_line && t.start_line !== ln ? "L" + t.start_line + "–" + ln : "L" + ln;
  }
  function threadEl(t, idx) {
    const box = el("span", "gh-thread" + (t.is_resolved ? " is-resolved is-collapsed" : "") + (t.local ? " is-local" : ""));
    box.id = "gh-thread-" + idx;
    box.setAttribute("role", "note");
    const head = el("span", "gh-thread-head");
    head.appendChild(el("span", null, "💬")).setAttribute("aria-hidden", "true");
    const first = t.comments[0] || {};
    head.appendChild(el("strong", null, first.author || "someone"));
    head.appendChild(timeEl(first.created_at));
    head.appendChild(el("span", null, "· " + lineLabel(t)));
    if (t.comments.length > 1) head.appendChild(el("span", null, "· " + plural(t.comments.length - 1, "reply", "replies")));
    if (t.is_resolved) head.appendChild(el("span", "gh-chip gh-muted", "Resolved"));
    if (t.local) head.appendChild(el("span", "gh-chip gh-ok", "Posted"));
    if (t.is_resolved) {
      const tg = el("button", "gh-thread-toggle", "Show");
      tg.type = "button";
      tg.dataset.ghToggle = "";
      tg.setAttribute("aria-expanded", "false");
      head.appendChild(tg);
    }
    const url = safeUrl(first.url);
    if (url) head.appendChild(link(url, "Reply on GitHub ↗"));
    box.appendChild(head);
    const body = el("span", "gh-thread-body");
    t.comments.forEach((c, i) => {
      const cm = el("span", "gh-comment");
      if (i > 0) {
        const meta = el("span", "gh-comment-meta");
        meta.appendChild(el("strong", null, c.author || "someone"));
        meta.appendChild(document.createTextNode(" · "));
        meta.appendChild(timeEl(c.created_at));
        cm.appendChild(meta);
      }
      cm.appendChild(renderBody(c.body));
      body.appendChild(cm);
    });
    box.appendChild(body);
    return box;
  }
  const placed = new Map(); // thread index -> element in the walkthrough
  function renderThreads() {
    document.querySelectorAll(".gh-thread").forEach((n) => n.remove());
    document.querySelectorAll(".gh-nav-count").forEach((n) => n.remove());
    placed.clear();
    if (!rowMap) buildRowMap();
    const perPanel = new Map();
    allThreads().forEach((t, idx) => {
      if (t.is_file) return;
      const row = bestRow(t);
      if (!row) return;
      const node = threadEl(t, idx);
      row.after(node);
      placed.set(idx, node);
      if (!t.is_resolved) {
        const panel = row.closest(".step-panel");
        const pi = panel && panel.dataset.panelIndex;
        if (pi) perPanel.set(pi, (perPanel.get(pi) || 0) + 1);
      }
    });
    perPanel.forEach((n, pi) => {
      const btn = document.querySelector('.arc-subtab[data-panel-index="' + pi + '"]') ||
        document.querySelector('.arc-tab[data-panel-index="' + pi + '"]');
      if (!btn) return;
      const badge = el("span", "gh-nav-count", "💬 " + n);
      badge.title = plural(n, "open comment thread");
      btn.appendChild(badge);
    });
  }
  function jumpTo(idx) {
    const node = placed.get(idx);
    if (!node) return;
    dialog.close();
    const panel = node.closest(".step-panel");
    const pi = panel && panel.dataset.panelIndex;
    if (panel && panel.hidden && pi) {
      const btn = document.querySelector('.arc-subtab[data-panel-index="' + pi + '"]') ||
        document.querySelector('.arc-tab[data-panel-index="' + pi + '"]');
      if (btn) btn.click();
    }
    const card = node.closest("details.hunk-disclosure");
    if (card && !card.open) card.open = true;
    node.classList.remove("is-collapsed");
    setTimeout(() => {
      node.scrollIntoView({ block: "center", behavior: "smooth" });
      node.classList.remove("gh-thread-flash"); void node.offsetWidth; node.classList.add("gh-thread-flash");
    }, 60);
  }

  // ---------- side panel ----------
  const REASONS = {
    outside: "This copy of the board isn't open on claude.ai, so it can't reach GitHub.",
    "no-connectors": "This view can't use claude.ai connectors. Open the board on claude.ai, signed in to the board owner's organization.",
    "not-added": "Your claude.ai account has no connector named “" + SERVER + "”.",
    server_not_connected: "Your claude.ai account has no connector named “" + SERVER + "”.",
    server_not_found: "The “" + SERVER + "” connector no longer exists in your claude.ai account.",
    needs_reauth: "Your “" + SERVER + "” connector needs to sign in to GitHub again (claude.ai → Settings → Connectors).",
    denied: "GitHub is turned off for this board.",
    not_in_manifest: "GitHub is turned off for this board.",
    selection_required: "You have more than one connector named “" + SERVER + "”; pick one in the prompt claude.ai shows, then reload.",
    blocked_by_policy: "Your organization blocks the GitHub connector for this board.",
    approval_required: "GitHub needs your approval before this board can read it.",
    "unsupported-host": "Live updates work only for PRs on github.com.",
    "bad-shape": "The GitHub connector answered in a shape this board can't read.",
  };
  function addConnectorSteps() {
    const wrap = el("div");
    wrap.appendChild(el("p", "gh-sec-note", "No GitHub connector yet? Add one once:"));
    const ol = el("ol", "gh-steps");
    ol.appendChild(el("li", null, "In claude.ai, open Settings → Connectors → Add custom connector."));
    ol.appendChild(el("li", null, "Name it “" + SERVER + "” and use GitHub's MCP server: https://api.githubcopilot.com/mcp/"));
    ol.appendChild(el("li", null, "Sign in to GitHub when asked, then reload this board."));
    wrap.appendChild(ol);
    return wrap;
  }
  function connectionBox() {
    const box = el("div", "gh-box is-" + (conn.state === "live" ? "live" : conn.state === "offline" ? "offline" : "neutral"));
    if (conn.state === "live") {
      box.appendChild(el("strong", null, "Live from GitHub"));
      box.appendChild(el("p", null, "Reviews, checks and comments refresh every minute while this board is open." +
        (conn.lastUpdate ? " Last update: " + ago(new Date(conn.lastUpdate).toISOString()) + "." : "")));
      const b = el("button", "gh-btn", "Refresh now"); b.type = "button"; b.dataset.ghAction = "refresh";
      box.appendChild(b);
      return box;
    }
    if (conn.state === "ready") {
      box.className = "gh-box is-offline";
      box.appendChild(el("strong", null, "Not connected to GitHub"));
      box.appendChild(el("p", null, "If your claude.ai account has a connector named “" + SERVER + "”, connect it to see live reviews, checks and comments here and to comment on any line as yourself. claude.ai asks you first."));
      const b = el("button", "gh-btn gh-btn-primary", "Connect to GitHub"); b.type = "button"; b.dataset.ghAction = "connect";
      box.appendChild(b);
      box.appendChild(el("p", null, snap
        ? "Until then you're seeing a snapshot taken " + ago(snap.fetched_at) + " (" + absTime(snap.fetched_at) + "), when this board was last built."
        : "No snapshot of reviews, checks or comments was saved when this board was built."));
      box.appendChild(addConnectorSteps());
      return box;
    }
    if (conn.state === "checking" || conn.state === "connecting") {
      box.appendChild(el("strong", null, conn.state === "checking" ? "Checking for a GitHub connection…" : "Connecting to GitHub…"));
      return box;
    }
    box.appendChild(el("strong", null, "Not connected to GitHub"));
    box.appendChild(el("p", null, (REASONS[conn.reason] || ("GitHub didn't answer (" + (conn.reason || "unknown") + ")."))));
    box.appendChild(el("p", null, snap
      ? "You're seeing a snapshot taken " + ago(snap.fetched_at) + " (" + absTime(snap.fetched_at) + "), when this board was last built."
      : "No snapshot of reviews, checks or comments was saved when this board was built."));
    if (["not-added", "server_not_connected", "server_not_found", "no-connectors"].includes(conn.reason)) {
      box.appendChild(addConnectorSteps());
    }
    if (conn.reason === "denied" || conn.reason === "not_in_manifest") {
      const b = el("button", "gh-btn", "Open permissions"); b.type = "button"; b.dataset.ghAction = "permissions";
      box.appendChild(b);
    } else if (conn.reason === "approval_required" || (conn.reason && !REASONS[conn.reason])) {
      const b = el("button", "gh-btn", "Try again"); b.type = "button"; b.dataset.ghAction = "connect";
      box.appendChild(b);
    }
    box.appendChild(el("p", "gh-sec-note", "Comment boxes still work: they copy your text and open the exact line on GitHub."));
    return box;
  }
  function section(id, title) {
    const s = el("section", "gh-sec"); s.id = "gh-sec-" + id;
    s.appendChild(el("h3", null, title));
    return s;
  }
  function sourceNote(sec, key) {
    if (liveShapeErr[key]) return el("p", "gh-sec-note", "Live data for this part couldn't be read; showing the snapshot.");
    if (liveAt[key]) return null;
    return snap ? el("p", "gh-sec-note", "From the snapshot taken " + ago(snap.fetched_at) + ".") : null;
  }
  function renderDialog() {
    const frag = document.createDocumentFragment();
    const cs = section("connection", "Connection");
    cs.appendChild(connectionBox());
    frag.appendChild(cs);

    const rs = section("reviews", "Reviews");
    const rp = reviewPill();
    const head = el("p", null); head.appendChild(el("span", "gh-ico " + rp.cls, rp.ico + " "));
    head.appendChild(el("strong", rp.cls, rp.text));
    rs.appendChild(head);
    if (model.reviews.length) {
      const ul = el("ul", "gh-list");
      model.reviews.forEach((r) => {
        const li = el("li");
        li.appendChild(el("span", "gh-chip " + (STATE_CLASS[r.state] || "gh-muted"), STATE_LABEL[r.state] || r.state));
        li.appendChild(el("strong", "gh-grow", r.author));
        li.appendChild(timeEl(r.submitted_at));
        const u = safeUrl(r.url); if (u) li.appendChild(link(u, "↗"));
        ul.appendChild(li);
      });
      rs.appendChild(ul);
    } else {
      rs.appendChild(el("p", "gh-sub", "Nobody has reviewed this PR yet."));
    }
    if (model.requested.length) rs.appendChild(el("p", "gh-sub", "Waiting on: " + model.requested.map((x) => x.replace(/^team:/, "team ")).join(", ")));
    const rn = sourceNote(rs, "reviews"); if (rn) rs.appendChild(rn);
    frag.appendChild(rs);

    const ks = section("checks", "Checks");
    const c = model.checks;
    if (c && c.items && c.items.length) {
      const s = c.summary;
      ks.appendChild(el("p", null, [s.passed + " passed", s.failed + " failed", s.pending + " running", s.skipped + " skipped"].join(" · ")));
      const ul = el("ul", "gh-list");
      const ICON = { passed: ["✓", "gh-ok"], failed: ["✕", "gh-bad"], pending: ["●", "gh-wait"], skipped: ["–", "gh-muted"] };
      c.items.forEach((it) => {
        const li = el("li");
        const ic = ICON[it.state] || ["?", "gh-muted"];
        li.appendChild(el("span", "gh-ico " + ic[1], ic[0])).setAttribute("aria-label", it.state);
        const name = el("span", "gh-grow"); name.appendChild(el("span", null, it.name));
        if (it.group) name.appendChild(el("span", "gh-sub", " · " + it.group));
        li.appendChild(name);
        const u = safeUrl(it.url); if (u) li.appendChild(link(u, "Details ↗"));
        ul.appendChild(li);
      });
      ks.appendChild(ul);
      if (c.truncated) ks.appendChild(el("p", "gh-sec-note", "Only the first 100 checks are listed."));
    } else {
      ks.appendChild(el("p", "gh-sub", "No checks reported for the latest commit."));
    }
    const kn = sourceNote(ks, "checks"); if (kn) ks.appendChild(kn);
    frag.appendChild(ks);

    const ts = section("threads", "Comments on the code");
    const threads = allThreads();
    const open = threads.map((t, i) => [t, i]).filter(([t]) => !t.is_resolved);
    if (!threads.length) ts.appendChild(el("p", "gh-sub", "No comments on the code yet."));
    if (open.length) {
      const ul = el("ul", "gh-list");
      open.forEach(([t, i]) => {
        const li = el("li");
        const g = el("span", "gh-grow");
        const first = t.comments[0] || {};
        const where = el("code", null, t.path.split("/").pop() + ":" + lineLabel(t).replace(/^L/, ""));
        where.title = t.path;
        g.appendChild(where);
        g.appendChild(el("span", "gh-sub", " · " + (first.author || "someone") + " · " + ago(first.created_at)));
        const snippet = String(first.body || "").replace(/\s+/g, " ").trim();
        g.appendChild(el("span", "gh-p", snippet.length > 140 ? snippet.slice(0, 139) + "…" : snippet));
        if (t.is_outdated) g.appendChild(el("span", "gh-chip gh-muted", "On an older version"));
        li.appendChild(g);
        if (placed.has(i)) {
          const b = el("button", "gh-btn", "Show"); b.type = "button"; b.dataset.ghJump = String(i);
          li.appendChild(b);
        } else {
          const u = safeUrl(first.url); if (u) li.appendChild(link(u, "Open ↗"));
        }
        ul.appendChild(li);
      });
      ts.appendChild(ul);
    }
    const resolved = threads.length - open.length;
    if (resolved) ts.appendChild(el("p", "gh-sub", plural(resolved, "resolved thread") + " — shown folded inside the walkthrough."));
    if (model.threadsTruncated) ts.appendChild(el("p", "gh-sec-note", "Only the first 100 threads are included."));
    const tn = sourceNote(ts, "threads"); if (tn) ts.appendChild(tn);
    frag.appendChild(ts);

    const vs = section("conversation", "Conversation");
    if (model.conversation.length) {
      model.conversation.slice().reverse().forEach((item) => {
        const d = el("div", "gh-conv-item");
        const h = el("div", "gh-conv-head");
        h.appendChild(el("strong", null, item.author));
        if (item.kind === "review" && item.state) h.appendChild(el("span", "gh-chip " + (STATE_CLASS[item.state] || "gh-muted"), STATE_LABEL[item.state] || item.state));
        h.appendChild(timeEl(item.created_at));
        const u = safeUrl(item.url); if (u) h.appendChild(link(u, "↗"));
        d.appendChild(h);
        if (item.body) d.appendChild(renderBody(item.body));
        vs.appendChild(d);
      });
    } else {
      vs.appendChild(el("p", "gh-sub", "No conversation comments yet."));
    }
    const gen = el("div", "gh-general");
    const ta = el("textarea"); ta.rows = 3; ta.placeholder = "Leave a comment on the whole PR"; ta.setAttribute("aria-label", "Comment on the whole PR");
    gen.appendChild(ta);
    const acts = el("div", "gh-composer-actions");
    const post = el("button", "gh-btn gh-btn-primary", conn.state === "live" ? "Comment on GitHub" : "Copy & open PR on GitHub");
    post.type = "button"; post.dataset.ghAction = conn.state === "live" ? "post-general" : "copy-general";
    acts.appendChild(post);
    acts.appendChild(el("span", "gh-msg")).setAttribute("role", "status");
    gen.appendChild(acts);
    vs.appendChild(gen);
    const vn = sourceNote(vs, "conversation"); if (vn) vs.appendChild(vn);
    frag.appendChild(vs);

    const keep = dialogBody.querySelector(".gh-general textarea");
    const draft = keep ? keep.value : "";
    dialogBody.replaceChildren(frag);
    if (draft) { const t2 = dialogBody.querySelector(".gh-general textarea"); if (t2) t2.value = draft; }
  }
  function openDialog(sectionId) {
    renderDialog();
    if (!dialog.open) { try { dialog.showModal(); } catch (_) { dialog.setAttribute("open", ""); } }
    const target = document.getElementById("gh-sec-" + sectionId);
    if (sectionId === "connection" || !target) dialog.scrollTop = 0;
    else target.scrollIntoView({ block: "start" });
  }

  // ---------- comment box on any line ----------
  let addBtn = null;
  function eligibleRow(row) {
    return row && row.matches(".diff-view .dl") && !row.classList.contains("dl-meta") && (row.dataset.pn || row.dataset.po);
  }
  function showAddButton(row) {
    if (!eligibleRow(row)) return;
    if (!addBtn) {
      addBtn = el("button", "gh-add-btn", "+");
      addBtn.type = "button";
      addBtn.dataset.ghAdd = "";
    }
    const ln = row.dataset.pn || row.dataset.po;
    addBtn.setAttribute("aria-label", "Comment on line " + ln);
    addBtn.title = "Comment on line " + ln;
    if (addBtn.parentNode !== row) row.prepend(addBtn);
  }
  function rowTarget(row) {
    const card = row.closest(".hunk-card");
    const isOldOnly = row.classList.contains("dl-del") || !row.dataset.pn;
    const side = isOldOnly ? "LEFT" : "RIGHT";
    const line = parseInt(side === "LEFT" ? row.dataset.po : row.dataset.pn, 10);
    const path = side === "LEFT" ? (card.dataset.oldPath || card.dataset.path) : card.dataset.path;
    const jump = row.querySelector(".row-pr-jump, .ln-link");
    return { path, line, side, url: jump ? jump.href : PR.url };
  }
  function openComposer(row) {
    const next = row.nextElementSibling;
    if (next && next.classList.contains("gh-composer")) { next.querySelector("textarea").focus(); return; }
    const tgt = rowTarget(row);
    const box = el("span", "gh-composer");
    box.dataset.path = tgt.path; box.dataset.line = String(tgt.line); box.dataset.side = tgt.side; box.dataset.url = tgt.url;
    box.appendChild(el("span", "gh-composer-head", "Comment on " + (tgt.side === "LEFT" ? "removed line " : "line ") + tgt.line + " · " + tgt.path));
    const ta = el("textarea"); ta.rows = 3; ta.placeholder = "Leave a comment"; ta.setAttribute("aria-label", "Comment on line " + tgt.line);
    box.appendChild(ta);
    const acts = el("span", "gh-composer-actions");
    const live = conn.state === "live";
    const post = el("button", "gh-btn gh-btn-primary", live ? "Comment on GitHub" : "Copy & open line on GitHub");
    post.type = "button"; post.dataset.ghAction = live ? "post-line" : "copy-line";
    const cancel = el("button", "gh-btn", "Cancel"); cancel.type = "button"; cancel.dataset.ghAction = "cancel";
    acts.appendChild(post); acts.appendChild(cancel);
    acts.appendChild(el("span", "gh-msg")).setAttribute("role", "status");
    if (!live) acts.appendChild(el("span", "gh-msg", "Not connected to GitHub: your text is copied and the line opens on GitHub."));
    box.appendChild(acts);
    row.after(box);
    ta.focus();
  }
  function setMsg(container, text, kind) {
    const m = container.querySelector('.gh-msg[role="status"]');
    if (!m) return;
    m.textContent = text;
    m.className = "gh-msg" + (kind ? " is-" + kind : "");
  }
  async function copyAndOpen(text, url, container, ta) {
    let copied = false;
    try { await navigator.clipboard.writeText(text); copied = true; } catch (_) {}
    if (!copied && ta) { ta.focus(); ta.select(); }
    const target = safeUrl(url) || safeUrl(PR.url);
    if (target) window.open(target, "_blank", "noopener");
    setMsg(container, copied
      ? "Copied. Paste it into the comment box on the GitHub page that just opened."
      : "Press ⌘C / Ctrl+C to copy your text, then paste it on the GitHub page that just opened.", "ok");
  }

  // ---------- live connection through the viewer's claude.ai connector ----------
  function codeOf(e) { return (e && typeof e.code === "string") ? e.code : "upstream_error"; }
  function setConn(state, reason) {
    conn.state = state; conn.reason = reason || "";
    renderHeader();
    if (dialog.open) renderDialog();
  }
  const AUTHZ = new Set(["needs_reauth", "server_not_connected", "blocked_by_policy", "approval_required", "not_in_manifest", "selection_required", "server_not_found", "not_granted", "capability_disabled", "capability_removed"]);
  function baseInput() { return { owner: PR.owner, repo: PR.repo, pullNumber: PR.number }; }
  async function schemaEnum(tool, prop) {
    try {
      const d = await conn.mcp.describeTool(SERVER, tool);
      const p = d && d.inputSchema && d.inputSchema.properties && d.inputSchema.properties[prop];
      if (p && Array.isArray(p.enum)) return p.enum.map(String);
    } catch (_) {}
    return null;
  }
  function pick(list, wanted, pattern) {
    if (!list) return wanted[0];
    for (const w of wanted) if (list.includes(w)) return w;
    return (pattern && list.find((m) => pattern.test(m))) || null;
  }

  // Live payloads: GitHub REST-style objects; anything unrecognised returns null (keep the snapshot).
  function asList(p, keys) {
    if (Array.isArray(p)) return p;
    if (p && typeof p === "object") for (const k of keys) if (Array.isArray(p[k])) return p[k];
    return null;
  }
  function who(o) {
    const u = o && (o.user || o.author);
    if (u && typeof u === "object") return String(u.login || u.name || "ghost");
    if (typeof u === "string") return u;
    return "ghost";
  }
  function when(o) { return (o && (o.created_at || o.createdAt || o.submitted_at || o.submittedAt || o.updated_at)) || null; }
  function urlOf(o) { return safeUrl(o && (o.html_url || o.url)); }
  const norm = {
    pr(p) {
      if (!p || typeof p !== "object" || Array.isArray(p)) return null;
      const st = String(p.state || "").toUpperCase();
      if (!st) return null;
      const merged = !!(p.merged || p.merged_at || p.mergedAt);
      return {
        prState: merged ? "MERGED" : (st === "CLOSED" ? "CLOSED" : "OPEN"),
        isDraft: !!(p.draft || p.isDraft),
        head: (p.head && p.head.sha) || p.headRefOid || null,
      };
    },
    reviews(p) {
      const list = asList(p, ["reviews", "items", "nodes", "data"]);
      if (!list) return null;
      const latest = new Map();
      const conv = [];
      list.forEach((r) => {
        const st = String((r && r.state) || "").toUpperCase();
        if (!STATE_LABEL[st]) return;
        const a = who(r);
        const prev = latest.get(a);
        if (st !== "COMMENTED" || !prev) {
          if (!prev || String(when(r)) >= String(prev.submitted_at)) latest.set(a, { author: a, state: st, submitted_at: when(r), url: urlOf(r) });
        }
        const body = String(r.body || "").trim();
        if (body) conv.push({ kind: "review", author: a, state: st, body, created_at: when(r), url: urlOf(r) });
      });
      const reviews = Array.from(latest.values());
      let decision = null;
      if (reviews.some((r) => r.state === "CHANGES_REQUESTED")) decision = "CHANGES_REQUESTED";
      else if (reviews.some((r) => r.state === "APPROVED")) decision = "APPROVED";
      return { reviews, decision, reviewConversation: conv };
    },
    threads(p) {
      const list = asList(p, ["threads", "reviewThreads", "comments", "items", "nodes", "data"]);
      if (!list) return null;
      const known = new Map();
      model.threads.forEach((t) => { const u = t.comments[0] && t.comments[0].url; if (u) known.set(u, t); });
      if (list.length && list.every((x) => x && Array.isArray(x.comments || (x.comments && x.comments.nodes)))) {
        return list.map((x) => {
          const cs = Array.isArray(x.comments) ? x.comments : x.comments.nodes;
          const comments = cs.map((c) => ({ author: who(c), body: String(c.body || ""), created_at: when(c), url: urlOf(c) }));
          const side = String(x.diffSide || x.side || "RIGHT").toUpperCase() === "LEFT" ? "LEFT" : "RIGHT";
          return {
            path: String(x.path || ""), line: x.line || null, start_line: x.startLine || x.start_line || null,
            original_line: x.originalLine || x.original_line || null, side,
            is_resolved: !!(x.isResolved || x.is_resolved), is_outdated: !!(x.isOutdated || x.is_outdated),
            is_file: String(x.subjectType || x.subject_type || "").toUpperCase() === "FILE", comments,
          };
        }).filter((t) => t.path && t.comments.length);
      }
      const roots = new Map(), order = [];
      const byId = new Map(list.map((c) => [c && c.id, c]));
      list.forEach((c) => {
        if (!c || !c.path) return;
        let root = c;
        let guard = 0;
        while (root.in_reply_to_id && byId.get(root.in_reply_to_id) && guard++ < 50) root = byId.get(root.in_reply_to_id);
        const key = root.id != null ? root.id : c.id;
        if (!roots.has(key)) {
          const side = String(root.side || "RIGHT").toUpperCase() === "LEFT" ? "LEFT" : "RIGHT";
          const prior = known.get(urlOf(root));
          roots.set(key, {
            path: String(root.path), line: root.line || null, start_line: root.start_line || null,
            original_line: root.original_line || null, side,
            is_resolved: prior ? prior.is_resolved : false,
            is_outdated: root.line == null && root.position == null,
            is_file: String(root.subject_type || "").toLowerCase() === "file", comments: [],
          });
          order.push(key);
        }
        roots.get(key).comments.push({ author: who(c), body: String(c.body || ""), created_at: when(c), url: urlOf(c) });
      });
      return order.map((k) => roots.get(k));
    },
    conversation(p) {
      const list = asList(p, ["comments", "items", "nodes", "data"]);
      if (!list) return null;
      return list.filter((c) => c && typeof c === "object").map((c) => ({ kind: "comment", author: who(c), state: null, body: String(c.body || ""), created_at: when(c), url: urlOf(c) }));
    },
    checks(p) {
      let items = null;
      const runs = asList(p, ["check_runs", "checkRuns", "items"]);
      if (runs) {
        items = runs.filter((r) => r && r.name).map((r) => {
          const status = String(r.status || "").toLowerCase(), concl = String(r.conclusion || "").toLowerCase();
          let state = "pending";
          if (status === "completed") {
            if (concl === "success") state = "passed";
            else if (["skipped", "neutral", "stale"].includes(concl)) state = "skipped";
            else if (concl) state = "failed";
          }
          return { name: String(r.name), state, url: safeUrl(r.html_url || r.details_url || r.detailsUrl), group: "" };
        });
      } else if (p && Array.isArray(p.statuses)) {
        items = p.statuses.map((s) => {
          const st = String(s.state || "").toLowerCase();
          return { name: String(s.context || "status"), state: st === "success" ? "passed" : (st === "failure" || st === "error") ? "failed" : "pending", url: safeUrl(s.target_url), group: "" };
        });
      }
      if (!items) return null;
      const order = { failed: 0, pending: 1, passed: 2, skipped: 3 };
      items.sort((a, b) => (order[a.state] - order[b.state]) || a.name.localeCompare(b.name));
      const summary = { passed: 0, failed: 0, pending: 0, skipped: 0 };
      items.forEach((i) => { summary[i.state] += 1; });
      summary.total = items.length;
      return { summary, items, truncated: false };
    },
  };
  let reviewConv = null, commentConv = null;
  function applySection(key, payload, storedAt) {
    const out = norm[key](payload);
    if (out == null) { liveShapeErr[key] = true; return; }
    liveShapeErr[key] = false;
    liveAt[key] = storedAt || Date.now();
    conn.lastUpdate = Math.max(conn.lastUpdate || 0, liveAt[key]);
    if (key === "pr") { model.prState = out.prState; model.isDraft = out.isDraft; if (out.head) PR.head_sha = out.head; }
    else if (key === "reviews") {
      model.reviews = out.reviews;
      model.decision = out.decision || (model.decision === "APPROVED" || model.decision === "CHANGES_REQUESTED" ? "REVIEW_REQUIRED" : model.decision);
      reviewConv = out.reviewConversation;
    } else if (key === "threads") { model.threads = out; model.threadsTruncated = false; localThreads.length = 0; renderThreads(); }
    else if (key === "conversation") { commentConv = out; }
    else if (key === "checks") { model.checks = out; }
    if ((key === "reviews" || key === "conversation") && (reviewConv || commentConv)) {
      const merged = (commentConv || model.conversation.filter((c) => c.kind === "comment"))
        .concat(reviewConv || model.conversation.filter((c) => c.kind === "review"));
      merged.sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
      model.conversation = merged;
    }
    renderHeader();
    if (dialog.open) renderDialog();
  }
  function stopWatches() { conn.unsubs.forEach((u) => { try { u(); } catch (_) {} }); conn.unsubs = []; }
  function retract(reason) {
    stopWatches();
    Object.keys(liveAt).forEach((k) => delete liveAt[k]);
    model = fromSnapshot();
    renderThreads();
    setConn("offline", reason);
  }
  async function startLive() {
    const mcp = conn.mcp;
    const methods = await schemaEnum(READ_TOOL, "method");
    const plan = {
      pr: pick(methods, ["get"]),
      reviews: pick(methods, ["get_reviews"], /review(?!_comment)s?$/),
      threads: pick(methods, ["get_review_threads", "get_review_comments"], /review_(thread|comment)/),
      conversation: pick(methods, ["get_comments"], /^get_comments$/),
      checks: pick(methods, ["get_check_runs", "get_status"], /check|status/),
    };
    Object.entries(plan).forEach(([key, method]) => {
      if (!method) { liveShapeErr[key] = true; return; }
      const input = Object.assign({ method }, baseInput());
      try {
        const unsub = mcp.watchTool(SERVER, READ_TOOL, input, (ev) => {
          if (!ev) return;
          if (ev.type === "data") {
            const r = ev.result || {};
            applySection(key, r.payload !== undefined ? r.payload : r.structuredContent, r.cache && r.cache.storedAt);
          } else if (ev.type === "error") {
            const code = codeOf(ev.error);
            if (AUTHZ.has(code)) retract(code);
            else if (code === "tool_error" || code === "bad_request") { liveShapeErr[key] = true; renderHeader(); }
          }
        }, { refetchInterval: REFRESH_MS });
        conn.unsubs.push(unsub);
      } catch (_) { liveShapeErr[key] = true; }
    });
    setConn("live");
  }
  async function connect(fromClick) {
    if (!cfg.live_supported) return setConn("offline", "unsupported-host");
    const api = window.claude;
    if (!api || typeof api.use !== "function") return setConn("offline", "outside");
    let mcp = null;
    try { mcp = await api.use("mcp"); } catch (_) { mcp = null; }
    if (!mcp) return setConn("offline", "no-connectors");
    conn.mcp = mcp;
    let listed = null;
    try { listed = await mcp.listTools(SERVER); } catch (e) { return setConn("offline", codeOf(e)); }
    const srv = ((listed && listed.servers) || []).find((s) => s && s.server === SERVER);
    if (!srv || !Array.isArray(srv.tools) || !srv.tools.length) return setConn("offline", "not-added");
    if (srv.authStatus === "needs_reauth") return setConn("offline", "needs_reauth");
    let perm = "prompt";
    try {
      const perms = await api.use("permissions");
      if (perms) perm = await perms.state("mcp:" + SERVER).catch(() => "prompt");
    } catch (_) {}
    if (perm === "denied") return setConn("offline", "denied");
    if (perm !== "granted" && !fromClick) return setConn("ready");
    setConn("connecting");
    try {
      const res = await mcp.callTool(SERVER, READ_TOOL, Object.assign({ method: "get" }, baseInput()));
      applySection("pr", res && (res.payload !== undefined ? res.payload : res.structuredContent), res && res.cache && res.cache.storedAt);
    } catch (e) {
      const code = codeOf(e);
      if (code === "tool_error" || code === "bad_request") return setConn("offline", "bad-shape");
      return setConn("offline", code);
    }
    await startLive();
  }

  async function postLine(box) {
    const ta = box.querySelector("textarea");
    const body = ta.value.trim();
    if (!body) { setMsg(box, "Write something first.", "error"); return; }
    const btn = box.querySelector('[data-gh-action="post-line"]');
    btn.disabled = true; setMsg(box, "Posting…");
    const mcp = conn.mcp, base = baseInput();
    const line = parseInt(box.dataset.line, 10), side = box.dataset.side, path = box.dataset.path;
    try {
      const methods = await schemaEnum(REVIEW_WRITE_TOOL, "method");
      const create = pick(methods, ["create"], /^create/);
      const submit = pick(methods, ["submit_pending"], /submit/);
      const discard = pick(methods, ["delete_pending"], /delete/);
      if (!create || !submit) throw { code: "bad-shape" };
      let created = false;
      const createInput = Object.assign({ method: create }, base);
      if (PR.head_sha) createInput.commitID = PR.head_sha;
      try { await mcp.callTool(SERVER, REVIEW_WRITE_TOOL, createInput); created = true; }
      catch (e) {
        // A pending review this viewer already has on GitHub gets the comment instead.
        if (!(codeOf(e) === "tool_error" && /pending/i.test(String(e.message || "")))) throw e;
      }
      try {
        await mcp.callTool(SERVER, LINE_COMMENT_TOOL, Object.assign({ path, body, subjectType: "LINE", line, side }, base));
      } catch (e) {
        if (created && discard) { try { await mcp.callTool(SERVER, REVIEW_WRITE_TOOL, Object.assign({ method: discard }, base)); } catch (_) {} }
        throw e;
      }
      await mcp.callTool(SERVER, REVIEW_WRITE_TOOL, Object.assign({ method: submit, event: "COMMENT" }, base));
      localThreads.push({ local: true, path, line, side, start_line: null, original_line: line, is_resolved: false, is_outdated: false, is_file: false,
        comments: [{ author: "you", body, created_at: new Date().toISOString(), url: null }] });
      box.remove();
      renderThreads(); renderHeader();
      try { await mcp.invalidate(SERVER); } catch (_) {}
    } catch (e) {
      btn.disabled = false;
      const code = codeOf(e);
      if (code === "server_unavailable" || code === "upstream_error" || code === "cancelled")
        setMsg(box, "GitHub didn't confirm the comment. Check the PR before trying again.", "error");
      else if (code === "bad-shape")
        setMsg(box, "This GitHub connector can't post line comments. Use “Open in PR” instead.", "error");
      else setMsg(box, "Couldn't post (" + code + "): " + String((e && e.message) || "").slice(0, 160), "error");
    }
  }
  async function postGeneral(container) {
    const ta = container.querySelector("textarea");
    const body = ta.value.trim();
    if (!body) { setMsg(container, "Write something first.", "error"); return; }
    const btn = container.querySelector('[data-gh-action="post-general"]');
    btn.disabled = true; setMsg(container, "Posting…");
    try {
      await conn.mcp.callTool(SERVER, ISSUE_COMMENT_TOOL, { owner: PR.owner, repo: PR.repo, issue_number: PR.number, body });
      ta.value = "";
      model.conversation = model.conversation.concat([{ kind: "comment", author: "you", state: null, body, created_at: new Date().toISOString(), url: null }]);
      try { await conn.mcp.invalidate(SERVER); } catch (_) {}
      renderDialog();
      setMsg(dialogBody.querySelector(".gh-general"), "Posted to the PR conversation.", "ok");
    } catch (e) {
      btn.disabled = false;
      const code = codeOf(e);
      setMsg(container, (code === "server_unavailable" || code === "upstream_error")
        ? "GitHub didn't confirm the comment. Check the PR before trying again."
        : "Couldn't post (" + code + ").", "error");
    }
  }

  // ---------- events ----------
  document.addEventListener("mouseover", (e) => {
    const row = e.target.closest && e.target.closest(".diff-view .dl");
    if (row) showAddButton(row);
  });
  document.addEventListener("focusin", (e) => {
    const row = e.target.closest && e.target.closest(".diff-view .dl");
    if (row) showAddButton(row);
  });
  document.addEventListener("click", async (e) => {
    const t = e.target;
    if (!t || !t.closest) return;
    const opener = t.closest("[data-gh-open]");
    if (opener) { openDialog(opener.dataset.ghOpen); return; }
    if (t.closest("[data-gh-add]")) { e.preventDefault(); const row = t.closest(".dl"); if (row) openComposer(row); return; }
    const toggle = t.closest("[data-gh-toggle]");
    if (toggle) {
      const th = toggle.closest(".gh-thread");
      const collapsed = th.classList.toggle("is-collapsed");
      toggle.textContent = collapsed ? "Show" : "Hide";
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      return;
    }
    const jump = t.closest("[data-gh-jump]");
    if (jump) { jumpTo(parseInt(jump.dataset.ghJump, 10)); return; }
    if (t.closest("[data-gh-close]")) { dialog.close(); return; }
    const act = t.closest("[data-gh-action]");
    if (act) {
      const a = act.dataset.ghAction;
      if (a === "connect") { connect(true); return; }
      if (a === "refresh") { if (conn.mcp) { try { await conn.mcp.invalidate(SERVER); } catch (_) {} } return; }
      if (a === "permissions") {
        try { const perms = await window.claude.use("permissions"); if (perms) { await perms.manage(); connect(false); } }
        catch (_) { const box = act.closest(".gh-box"); if (box) box.appendChild(el("p", "gh-sec-note", "Open the board's Permissions menu in claude.ai and turn GitHub back on.")); }
        return;
      }
      const box = act.closest(".gh-composer");
      if (a === "cancel" && box) { box.remove(); return; }
      if (a === "copy-line" && box) { copyAndOpen(box.querySelector("textarea").value, box.dataset.url, box, box.querySelector("textarea")); return; }
      if (a === "post-line" && box) { postLine(box); return; }
      const gen = act.closest(".gh-general");
      if (a === "copy-general" && gen) { copyAndOpen(gen.querySelector("textarea").value, PR.url, gen, gen.querySelector("textarea")); return; }
      if (a === "post-general" && gen) { postGeneral(gen); return; }
    }
    // Touch screens: tapping a line shows its "+" button.
    const row = t.closest(".diff-view .dl");
    if (row && !t.closest("a")) showAddButton(row);
  });
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
  document.addEventListener("toggle", (e) => {
    if (e.target && e.target.matches && e.target.matches("details.hunk-disclosure")) { rowMap = null; }
  }, true);
  setInterval(() => { renderHeader(); }, 60000);

  buildRowMap();
  renderThreads();
  renderHeader();
  connect(false);
})();
"""
