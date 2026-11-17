#!/usr/bin/env python3
"""Compile a validated, 100%-line-accounted, syntax-highlighted interactive HTML
(and optional Markdown) code review guide from `diff-manifest.json` + `plan.json`.

Features:
- Arc-style vertical chapter & step rail (Virtual Stacked PRs)
- Cumulative timeline file evolution (each step shows the file at THAT point in the PR timeline)
- Sub-hunk line slicing (`H007:1-31,52`) with 100% changed-line accounting
- Clickable project concept modals explaining repo components from scratch
- Dynamic visual formatting rotation (1-sentence BLUF, tables, anchored bullets, callouts)
- Annotated Figma & real-app screenshots (red circles, boxes, directional arrows)
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import math
import mimetypes
from pathlib import Path, PurePosixPath
import re
import struct
import sys
from typing import Any
from urllib.parse import urlparse
import urllib.request


EXPAND_CHUNK = 15

VALID_LAYERS = {
    "contracts": {"order": 1, "label": "Contracts & Shapes", "short": "Contracts"},
    "core": {"order": 2, "label": "Core Logic", "short": "Core"},
    "wiring": {"order": 3, "label": "Wiring", "short": "Wiring"},
    "verification": {"order": 4, "label": "Tests & Verification", "short": "Tests"},
}

VALID_CALLOUT_KINDS = {
    "decision": {"label": "Decision", "symbol": "◈"},
    "assumption": {"label": "Verify AI Assumption", "symbol": "⚠"},
    "risk": {"label": "Watch Out", "symbol": "!"},
    "question": {"label": "Question", "symbol": "?"},
}

VALID_NODE_STATUS = {"unchanged", "modified", "added", "removed"}
VALID_ANNOTATION_KINDS = {"circle", "arrow", "box"}

HUNK_SPEC_RE = re.compile(r"^(H\d{3,})(?::([\d,\-\s]+))?$")


# ---------------------------------------------------------------------------
# Zero-dependency, XSS-safe multi-language syntax highlighter
# ---------------------------------------------------------------------------

JS_TS_KEYWORDS = {
    "import", "from", "export", "default", "as", "type", "interface", "enum",
    "implements", "extends", "class", "function", "const", "let", "var",
    "return", "if", "else", "for", "of", "in", "while", "do", "switch", "case",
    "break", "continue", "try", "catch", "finally", "throw", "new", "async",
    "await", "typeof", "instanceof", "readonly", "private", "public",
    "protected", "static", "yield", "void", "delete", "super", "this",
}

PY_KEYWORDS = {
    "def", "class", "import", "from", "as", "return", "if", "elif", "else",
    "for", "in", "while", "try", "except", "finally", "raise", "with", "async",
    "await", "yield", "lambda", "pass", "break", "continue", "and", "or",
    "not", "is", "global", "nonlocal", "assert", "del", "match", "case",
}

GO_RS_JAVA_KEYWORDS = {
    "func", "package", "struct", "interface", "type", "defer", "go", "chan",
    "select", "map", "range", "fallthrough", "fn", "pub", "mod", "use", "impl",
    "trait", "mut", "let", "const", "static", "match", "where", "crate", "self",
    "super", "enum", "unsafe", "async", "await", "move", "ref", "return", "if",
    "else", "for", "in", "while", "loop", "break", "continue", "class", "void",
    "public", "private", "protected", "final", "new", "try", "catch", "throw",
}

SQL_KEYWORDS = {
    "CREATE", "ALTER", "DROP", "TABLE", "INDEX", "VIEW", "UNIQUE", "PRIMARY",
    "FOREIGN", "KEY", "REFERENCES", "INSERT", "INTO", "VALUES", "UPDATE", "SET",
    "DELETE", "SELECT", "FROM", "WHERE", "JOIN", "LEFT", "RIGHT", "INNER",
    "OUTER", "ON", "CONFLICT", "DO", "NOTHING", "RETURNING", "AND", "OR", "NOT",
    "IN", "IS", "NULL", "DEFAULT", "CHECK", "CONSTRAINT", "ORDER", "BY",
    "GROUP", "HAVING", "LIMIT", "OFFSET", "AS", "DISTINCT", "EXISTS", "CASE",
    "WHEN", "THEN", "ELSE", "END", "CASCADE", "IF", "WITH", "BEGIN", "COMMIT",
}

SQL_TYPES = {
    "TEXT", "VARCHAR", "CHAR", "INTEGER", "INT", "SMALLINT", "BIGINT",
    "BOOLEAN", "BOOL", "TIMESTAMPTZ", "TIMESTAMP", "DATE", "TIME", "UUID",
    "JSON", "JSONB", "NUMERIC", "DECIMAL", "REAL", "FLOAT", "SERIAL",
    "BIGSERIAL", "BYTEA",
}

COMMON_TYPES = {
    "string", "number", "boolean", "unknown", "any", "never", "object",
    "symbol", "bigint", "str", "int", "float", "bool", "bytes", "list", "dict",
    "tuple", "set", "u8", "u16", "u32", "u64", "usize", "i8", "i16", "i32",
    "i64", "isize", "f32", "f64",
}

LITERAL_CONSTS = {
    "true", "false", "null", "undefined", "True", "False", "None", "nil", "NaN",
}

TOKEN_SLASHS_RE = re.compile(
    r"(?P<cmt>//.*$|/\*.*?\*/)"
    r"|(?P<str>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)"
    r"|(?P<dec>@[A-Za-z_]\w*)"
    r"|(?P<num>\b(?:0x[0-9a-fA-F_]+|\d[\d_]*(?:\.[\d_]+)?)\b)"
    r"|(?P<ident>\b[A-Za-z_]\w*\b)"
)

TOKEN_HASH_RE = re.compile(
    r"(?P<cmt>#.*$)"
    r"|(?P<str>\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
    r"|(?P<dec>@[A-Za-z_]\w*)"
    r"|(?P<num>\b(?:0x[0-9a-fA-F_]+|\d[\d_]*(?:\.[\d_]+)?)\b)"
    r"|(?P<ident>\b[A-Za-z_]\w*\b)"
)

TOKEN_SQL_RE = re.compile(
    r"(?P<cmt>--.*$|/\*.*?\*/)"
    r"|(?P<str>'(?:[^']|'')*'|\"(?:[^\"\\]|\\.)*\")"
    r"|(?P<num>\b\d[\d_]*(?:\.[\d_]+)?\b|\$\d+)"
    r"|(?P<ident>\b[A-Za-z_]\w*\b)"
)

TOKEN_DATA_RE = re.compile(
    r"(?P<cmt>#.*$)"
    r"|(?P<key>^\s*\"(?:[^\"\\]|\\.)*\"(?=\s*:)|^\s*[A-Za-z0-9_.-]+(?=\s*:))"
    r"|(?P<str>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
    r"|(?P<num>\b-?\d+(?:\.\d+)?\b)"
    r"|(?P<ident>\b(?:true|false|null|yes|no)\b)"
)


def detect_lang_family(file_path: str) -> str:
    ext = PurePosixPath(file_path).suffix.lower()
    if ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}:
        return "jsts"
    if ext in {".py", ".pyi"}:
        return "py"
    if ext in {".sql"}:
        return "sql"
    if ext in {".go", ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".proto", ".scala"}:
        return "clike"
    if ext in {".json", ".yaml", ".yml", ".toml"}:
        return "data"
    if ext in {".sh", ".bash", ".zsh", ".rb", ".pl"}:
        return "hash"
    return "plain"


def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


PROSE_TOKEN_RE = re.compile(r"\[\[([^\]]+)\]\]|`([^`\n]+)`")
BRACKET_CLEAN_RE = re.compile(r"\[\[([^\]]+)\]\]")
SENTENCE_BREAK_RE = re.compile(r"[.!?](?:\s|$)|\n")


def strip_concept_markup(value: Any) -> str:
    """Strip `[[concept-id|Label]]` -> `Label` for plain-text sidebar tabs and Markdown export."""
    if value is None:
        return ""

    def _repl(m: re.Match[str]) -> str:
        raw = m.group(1).strip()
        return raw.split("|", 1)[1].strip() if "|" in raw else raw

    return BRACKET_CLEAN_RE.sub(_repl, str(value))


def fmt_prose(value: Any, concept_lookup: dict[str, str] | None = None) -> str:
    """Escape prose safely while turning semantic `[[concept-id|phrase]]` and `` `Term` `` into clickable links.

    Deduplicates repeated mentions of the exact same concept within a single sentence
    (preferring explicit `[[concept-id|phrase]]` tags over implicit backtick aliases in the
    same sentence), while resetting at every sentence boundary so every new sentence, bullet,
    table cell, caption, or highlight note stays clickable.
    """
    if value is None:
        return ""
    text = str(value)
    lookup = concept_lookup or {}

    # Split into (sentence_chunk, delimiter) pairs so explicit `[[cid|...]]` inside a sentence
    # takes priority over an earlier implicit `` `alias` `` in that same sentence.
    parts = re.split(r"([.!?](?:\s|$)|\n)", text)
    out: list[str] = []
    for chunk_idx in range(0, len(parts), 2):
        sent = parts[chunk_idx]
        delim = parts[chunk_idx + 1] if chunk_idx + 1 < len(parts) else ""
        explicit_cids_in_sent: set[str] = set()
        for bm in BRACKET_CLEAN_RE.finditer(sent):
            raw_b = bm.group(1).strip().split("|", 1)[0].strip()
            norm_b = re.sub(r"[^a-z0-9_-]+", "-", raw_b.lower()).strip("-")
            resolved = lookup.get(norm_b) or lookup.get(raw_b.lower())
            if resolved:
                explicit_cids_in_sent.add(resolved)

        seen_in_sentence: set[str] = set()
        pos = 0
        for m in PROSE_TOKEN_RE.finditer(sent):
            start, end = m.span()
            if start > pos:
                out.append(esc(sent[pos:start]))
            if m.group(1) is not None:
                raw_ref = m.group(1).strip()
                if "|" in raw_ref:
                    cid_part, label_part = raw_ref.split("|", 1)
                    cid_key = re.sub(r"[^a-z0-9_-]+", "-", cid_part.strip().lower()).strip("-")
                    display = label_part.strip()
                else:
                    cid_key = re.sub(r"[^a-z0-9_-]+", "-", raw_ref.lower()).strip("-")
                    display = raw_ref
                is_code_lbl = display.startswith("`") and display.endswith("`") and len(display) > 2
                if is_code_lbl:
                    display = display[1:-1]
                cid = lookup.get(cid_key) or lookup.get(raw_ref.split("|", 1)[0].strip().lower())
                if cid and cid not in seen_in_sentence:
                    seen_in_sentence.add(cid)
                    cls = "concept-link concept-link-code" if is_code_lbl else "concept-link"
                    out.append(
                        f'<button type="button" class="{cls}" data-concept="{esc(cid)}">'
                        f"{esc(display)}</button>"
                    )
                elif is_code_lbl:
                    out.append(f'<code class="inline-code">{esc(display)}</code>')
                else:
                    out.append(esc(display))
            else:
                code_txt = m.group(2)
                key_low = code_txt.strip().lower()
                cid = lookup.get(key_low) or lookup.get(key_low.removesuffix("()"))
                if cid and cid not in seen_in_sentence and cid not in explicit_cids_in_sent:
                    seen_in_sentence.add(cid)
                    out.append(
                        f'<button type="button" class="concept-link concept-link-code" data-concept="{esc(cid)}">'
                        f"{esc(code_txt)}</button>"
                    )
                else:
                    out.append(f'<code class="inline-code">{esc(code_txt)}</code>')
            pos = end

        if pos < len(sent):
            out.append(esc(sent[pos:]))
        if delim:
            out.append(esc(delim))
    return "".join(out)


def highlight_code_line(text: str, lang: str) -> str:
    """Return XSS-safe HTML spans for a single source line."""
    if not text or lang == "plain":
        return esc(text)

    if lang == "sql":
        pattern = TOKEN_SQL_RE
        kw_set = SQL_KEYWORDS
    elif lang in {"py", "hash"}:
        pattern = TOKEN_HASH_RE
        kw_set = PY_KEYWORDS
    elif lang == "data":
        pattern = TOKEN_DATA_RE
        kw_set = set()
    else:
        pattern = TOKEN_SLASHS_RE
        kw_set = JS_TS_KEYWORDS if lang == "jsts" else GO_RS_JAVA_KEYWORDS

    out: list[str] = []
    pos = 0
    length = len(text)

    for m in pattern.finditer(text):
        start, end = m.span()
        if start > pos:
            out.append(esc(text[pos:start]))
        tok = m.group(0)
        group = m.lastgroup

        if group == "cmt":
            out.append(f'<span class="tok-cmt">{esc(tok)}</span>')
        elif group == "str":
            out.append(f'<span class="tok-str">{esc(tok)}</span>')
        elif group == "key":
            out.append(f'<span class="tok-fn">{esc(tok)}</span>')
        elif group == "dec":
            out.append(f'<span class="tok-typ">{esc(tok)}</span>')
        elif group == "num":
            out.append(f'<span class="tok-num">{esc(tok)}</span>')
        elif group == "ident":
            upper_tok = tok.upper()
            if lang == "sql":
                if upper_tok in SQL_KEYWORDS:
                    out.append(f'<span class="tok-kw">{esc(tok)}</span>')
                elif upper_tok in SQL_TYPES:
                    out.append(f'<span class="tok-typ">{esc(tok)}</span>')
                elif end < length and text[end] == "(":
                    out.append(f'<span class="tok-fn">{esc(tok)}</span>')
                else:
                    out.append(esc(tok))
            elif lang == "data":
                out.append(f'<span class="tok-num">{esc(tok)}</span>')
            else:
                if tok in kw_set:
                    out.append(f'<span class="tok-kw">{esc(tok)}</span>')
                elif tok in LITERAL_CONSTS:
                    out.append(f'<span class="tok-num">{esc(tok)}</span>')
                elif tok in COMMON_TYPES or (tok[0].isupper() and any(c.islower() for c in tok[1:])):
                    out.append(f'<span class="tok-typ">{esc(tok)}</span>')
                elif end < length and text[end] == "(":
                    out.append(f'<span class="tok-fn">{esc(tok)}</span>')
                else:
                    out.append(esc(tok))
        else:
            out.append(esc(tok))
        pos = end

    if pos < length:
        out.append(esc(text[pos:]))

    return "".join(out)


def safe_http_url(url: Any) -> str | None:
    if not url or not isinstance(url, str):
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme in {"https", "http"} and parsed.netloc:
        return url.strip()
    return None


# ---------------------------------------------------------------------------
# Sub-hunk line-slice parsing & 100% changed-line accounting
# ---------------------------------------------------------------------------

def compute_hunk_changed_anchors(hunk: dict[str, Any]) -> dict[int, int]:
    """Return `{row_idx: anchor_line}` for every changed (`add`/`del`/`meta` synthetic) row in `hunk`."""
    if hunk.get("is_synthetic"):
        return {0: 1}

    pure_deletion = hunk.get("new_count", 0) == 0 or hunk.get("file_status") == "deleted"
    cursor_new = max(1, int(hunk.get("new_start") or 1))
    anchors: dict[int, int] = {}

    for idx, ln in enumerate(hunk.get("lines") or []):
        kind = ln["kind"]
        if kind == "ctx":
            if ln.get("new_line") is not None:
                cursor_new = int(ln["new_line"]) + 1
        elif kind == "add":
            nln = int(ln["new_line"]) if ln.get("new_line") is not None else cursor_new
            anchors[idx] = nln
            cursor_new = nln + 1
        elif kind == "del":
            if pure_deletion:
                anchors[idx] = int(ln["old_line"] or 1)
            else:
                anchors[idx] = cursor_new

    # Fallback if a hunk somehow had only context/meta lines
    if not anchors and hunk.get("lines"):
        anchors[0] = cursor_new
    return anchors


CIRCLED_NUMERALS = ("①", "②", "③", "④", "⑤")


def parse_line_ranges(range_part: str, context_label: str = "") -> list[tuple[int, int]]:
    """Parse `'10-25,40'` into `[(10, 25), (40, 40)]`."""
    ctx_suffix = f" in `{context_label}`" if context_label else ""
    ranges: list[tuple[int, int]] = []
    for piece in str(range_part).split(","):
        tok = piece.strip()
        if not tok:
            continue
        if "-" in tok:
            lo_s, hi_s = tok.split("-", 1)
            try:
                lo, hi = int(lo_s.strip()), int(hi_s.strip())
            except ValueError as exc:
                raise ValueError(f"Invalid line range `{tok}`{ctx_suffix}.") from exc
            if lo <= 0 or hi < lo:
                raise ValueError(f"Invalid line range `{tok}`{ctx_suffix}.")
            ranges.append((lo, hi))
        else:
            try:
                ln = int(tok)
            except ValueError as exc:
                raise ValueError(f"Invalid line number `{tok}`{ctx_suffix}.") from exc
            if ln <= 0:
                raise ValueError(f"Invalid line number `{tok}`{ctx_suffix}.")
            ranges.append((ln, ln))
    if not ranges:
        raise ValueError(f"Empty line range{ctx_suffix}.")
    return ranges


def parse_hunk_spec(raw_spec: str) -> tuple[str, list[tuple[int, int]] | None]:
    """Parse `'H007'` or `'H007:1-31,52'` into `(hunk_id, ranges_or_None)`."""
    spec = str(raw_spec).strip()
    m = HUNK_SPEC_RE.match(spec)
    if not m:
        raise ValueError(
            f"Invalid hunk selector `{raw_spec}`. Expected `H001` or `H001:10-25,40`."
        )
    hid = m.group(1)
    range_part = m.group(2)
    if not range_part:
        return hid, None
    return hid, parse_line_ranges(range_part, raw_spec)


def match_slice_rows(
    anchors: dict[int, int], ranges: list[tuple[int, int]] | None
) -> list[int]:
    if ranges is None:
        return sorted(anchors.keys())
    matched: list[int] = []
    for row_idx, anchor in sorted(anchors.items()):
        if any(lo <= anchor <= hi for lo, hi in ranges):
            matched.append(row_idx)
    return matched


# ---------------------------------------------------------------------------
# Cumulative Timeline File Evolution Engine (F_0 -> F^(1) -> ... -> F^(M))
# ---------------------------------------------------------------------------

def build_file_timeline_tokens(
    file_obj: dict[str, Any],
    file_hunks: list[dict[str, Any]],
    row_step_order: dict[tuple[str, int], int],
) -> list[dict[str, Any]]:
    """Build a unified ordered token stream representing a file's entire evolution across steps."""
    post_lines: list[str] | None = file_obj.get("post_lines")
    is_added_file = file_obj.get("status") == "added"
    tokens: list[dict[str, Any]] = []

    if post_lines and all(not h["is_synthetic"] for h in file_hunks):
        sorted_hunks = sorted(file_hunks, key=lambda h: (h["new_start"], h["old_start"]))
        post_cursor = 1
        old_cursor = 1
        total_post = len(post_lines)

        for h in sorted_hunks:
            hid = h["id"]
            h_start = max(1, int(h["new_start"]))
            while post_cursor < h_start and post_cursor <= total_post:
                tokens.append(
                    {
                        "type": "unchanged",
                        "content": post_lines[post_cursor - 1],
                        "hunk_id": None,
                        "row_idx": None,
                        "step_order": 0,
                        "pr_old_line": None if is_added_file else old_cursor,
                        "pr_new_line": post_cursor,
                    }
                )
                post_cursor += 1
                old_cursor += 1

            if not is_added_file and int(h.get("old_start") or 0) > 0:
                old_cursor = int(h["old_start"])

            for r_idx, ln in enumerate(h["lines"]):
                kind = ln["kind"]
                if kind == "ctx":
                    pr_ol = (
                        int(ln["old_line"])
                        if ln.get("old_line") is not None
                        else (None if is_added_file else old_cursor)
                    )
                    pr_nl = int(ln["new_line"]) if ln.get("new_line") is not None else post_cursor
                    tokens.append(
                        {
                            "type": "unchanged",
                            "content": ln["content"],
                            "hunk_id": hid,
                            "row_idx": r_idx,
                            "step_order": 0,
                            "pr_old_line": pr_ol,
                            "pr_new_line": pr_nl,
                        }
                    )
                    if pr_ol is not None:
                        old_cursor = pr_ol + 1
                    post_cursor = pr_nl + 1
                elif kind == "del":
                    pr_ol = int(ln["old_line"]) if ln.get("old_line") is not None else old_cursor
                    tokens.append(
                        {
                            "type": "del",
                            "content": ln["content"],
                            "hunk_id": hid,
                            "row_idx": r_idx,
                            "step_order": row_step_order.get((hid, r_idx), 1),
                            "pr_old_line": pr_ol,
                            "pr_new_line": None,
                        }
                    )
                    old_cursor = pr_ol + 1
                elif kind == "add":
                    pr_nl = int(ln["new_line"]) if ln.get("new_line") is not None else post_cursor
                    tokens.append(
                        {
                            "type": "add",
                            "content": ln["content"],
                            "hunk_id": hid,
                            "row_idx": r_idx,
                            "step_order": row_step_order.get((hid, r_idx), 1),
                            "pr_old_line": None,
                            "pr_new_line": pr_nl,
                        }
                    )
                    post_cursor = pr_nl + 1

        while post_cursor <= total_post:
            tokens.append(
                {
                    "type": "unchanged",
                    "content": post_lines[post_cursor - 1],
                    "hunk_id": None,
                    "row_idx": None,
                    "step_order": 0,
                    "pr_old_line": None if is_added_file else old_cursor,
                    "pr_new_line": post_cursor,
                }
            )
            post_cursor += 1
            old_cursor += 1
        return tokens

    # Fallback when full post_lines are unavailable (deleted files, synthetic files, or partial patches)
    for h_idx, h in enumerate(file_hunks):
        if h_idx > 0:
            tokens.append(
                {
                    "type": "gap",
                    "content": "⋯",
                    "hunk_id": None,
                    "row_idx": None,
                    "step_order": 0,
                }
            )
        hid = h["id"]
        for r_idx, ln in enumerate(h["lines"]):
            kind = ln["kind"]
            if kind == "ctx":
                tokens.append(
                    {
                        "type": "unchanged",
                        "content": ln["content"],
                        "hunk_id": hid,
                        "row_idx": r_idx,
                        "step_order": 0,
                        "fallback_old": ln.get("old_line"),
                        "fallback_new": ln.get("new_line"),
                        "pr_old_line": ln.get("old_line"),
                        "pr_new_line": ln.get("new_line"),
                    }
                )
            elif kind == "del":
                tokens.append(
                    {
                        "type": "del",
                        "content": ln["content"],
                        "hunk_id": hid,
                        "row_idx": r_idx,
                        "step_order": row_step_order.get((hid, r_idx), 1),
                        "fallback_old": ln.get("old_line"),
                        "pr_old_line": ln.get("old_line"),
                        "pr_new_line": None,
                    }
                )
            elif kind == "add":
                tokens.append(
                    {
                        "type": "add",
                        "content": ln["content"],
                        "hunk_id": hid,
                        "row_idx": r_idx,
                        "step_order": row_step_order.get((hid, r_idx), 1),
                        "fallback_new": ln.get("new_line"),
                        "pr_old_line": None,
                        "pr_new_line": ln.get("new_line"),
                    }
                )
            else:
                tokens.append(
                    {
                        "type": "meta",
                        "content": ln["content"],
                        "hunk_id": hid,
                        "row_idx": r_idx,
                        "step_order": row_step_order.get((hid, r_idx), 1),
                    }
                )
    return tokens


def evaluate_file_at_step(
    tokens: list[dict[str, Any]],
    *,
    has_full_file: bool,
    step_order: int,
    active_keys: set[tuple[str, int]],
) -> list[dict[str, Any]]:
    """Project `tokens` onto timeline step `step_order` (`F^(t-1) -> F^(t)`).

    - Changes from steps `< step_order` are already merged into `F^(t-1)` and `F^(t)`.
    - Changes in `active_keys` (at `step_order`) appear as `+` / `-` diff rows.
    - Changes from future steps `> step_order` are NOT applied yet (`add` omitted, `del` kept as unchanged).
    """
    prev_ln = 1
    curr_ln = 1
    rows: list[dict[str, Any]] = []

    for tok in tokens:
        ttype = tok["type"]
        key = (tok["hunk_id"], tok["row_idx"])
        is_active = key in active_keys
        t_step = tok["step_order"]

        if ttype == "gap":
            rows.append(
                {
                    "kind": "meta",
                    "old_line": None,
                    "new_line": None,
                    "content": tok["content"],
                    "active": False,
                }
            )
            continue

        if ttype == "meta":
            if is_active or t_step <= step_order:
                rows.append(
                    {
                        "kind": "meta",
                        "old_line": None,
                        "new_line": None,
                        "content": tok["content"],
                        "active": is_active,
                    }
                )
            continue

        if ttype == "unchanged":
            ol = prev_ln if has_full_file else tok.get("fallback_old")
            nl = curr_ln if has_full_file else tok.get("fallback_new")
            rows.append(
                {
                    "kind": "ctx",
                    "old_line": ol,
                    "new_line": nl,
                    "pr_old_line": tok.get("pr_old_line"),
                    "pr_new_line": tok.get("pr_new_line"),
                    "content": tok["content"],
                    "active": False,
                }
            )
            prev_ln += 1
            curr_ln += 1

        elif ttype == "add":
            if t_step < step_order:
                # Added in an earlier step -> already part of the file at this point in the timeline!
                ol = prev_ln if has_full_file else tok.get("fallback_new")
                nl = curr_ln if has_full_file else tok.get("fallback_new")
                rows.append(
                    {
                        "kind": "ctx",
                        "old_line": ol,
                        "new_line": nl,
                        "pr_old_line": tok.get("pr_old_line"),
                        "pr_new_line": tok.get("pr_new_line"),
                        "content": tok["content"],
                        "active": False,
                        "from_earlier_step": True,
                    }
                )
                prev_ln += 1
                curr_ln += 1
            elif t_step == step_order:
                nl = curr_ln if has_full_file else tok.get("fallback_new")
                if is_active:
                    rows.append(
                        {
                            "kind": "add",
                            "old_line": None,
                            "new_line": nl,
                            "pr_old_line": tok.get("pr_old_line"),
                            "pr_new_line": tok.get("pr_new_line"),
                            "content": tok["content"],
                            "active": True,
                        }
                    )
                else:
                    # Another slice in the same step
                    rows.append(
                        {
                            "kind": "ctx",
                            "old_line": prev_ln if has_full_file else None,
                            "new_line": nl,
                            "pr_old_line": tok.get("pr_old_line"),
                            "pr_new_line": tok.get("pr_new_line"),
                            "content": tok["content"],
                            "active": False,
                        }
                    )
                curr_ln += 1
            else:
                # Future step (`t_step > step_order`) -> does NOT exist in the file yet!
                pass

        elif ttype == "del":
            if t_step < step_order:
                # Deleted in an earlier step -> already gone!
                pass
            elif t_step == step_order:
                ol = prev_ln if has_full_file else tok.get("fallback_old")
                if is_active:
                    rows.append(
                        {
                            "kind": "del",
                            "old_line": ol,
                            "new_line": None,
                            "pr_old_line": tok.get("pr_old_line"),
                            "pr_new_line": tok.get("pr_new_line"),
                            "content": tok["content"],
                            "active": True,
                        }
                    )
                prev_ln += 1
            else:
                # Will be deleted in a future step -> still present as normal code right now!
                ol = prev_ln if has_full_file else tok.get("fallback_old")
                nl = curr_ln if has_full_file else ol
                rows.append(
                    {
                        "kind": "ctx",
                        "old_line": ol,
                        "new_line": nl,
                        "pr_old_line": tok.get("pr_old_line"),
                        "pr_new_line": tok.get("pr_new_line"),
                        "content": tok["content"],
                        "active": False,
                    }
                )
                prev_ln += 1
                curr_ln += 1

    return rows


# ---------------------------------------------------------------------------
# Pure-stdlib image dimension reader + base64 embedder + SVG red annotations
# ---------------------------------------------------------------------------

def parse_image_dimensions(data: bytes, mime: str = "") -> tuple[int, int] | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        if w > 0 and h > 0:
            return int(w), int(h)
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        if w > 0 and h > 0:
            return int(w), int(h)
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if chunk == b"VP8 " and len(data) >= 30:
            w, h = struct.unpack("<HH", data[26:30])
            return (w & 0x3FFF), (h & 0x3FFF)
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        idx = 2
        while idx + 9 < len(data):
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            if marker == 0xFF:
                idx += 1
                continue
            if marker in {0xD8, 0xD9} or (0xD0 <= marker <= 0xD7):
                idx += 2
                continue
            seg_len = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
            if seg_len < 2 or idx + 2 + seg_len > len(data):
                break
            if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
                h, w = struct.unpack(">HH", data[idx + 5 : idx + 9])
                if w > 0 and h > 0:
                    return int(w), int(h)
            idx += 2 + seg_len
    if "svg" in mime or data.lstrip()[:5] in (b"<svg ", b"<?xml"):
        txt = data[:4096].decode("utf-8", errors="ignore")
        vb = re.search(r'viewBox=["\']\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)', txt)
        if vb:
            w, h = float(vb.group(1)), float(vb.group(2))
            if w > 0 and h > 0:
                return int(round(w)), int(round(h))
    return None


def resolve_visual_asset(
    visual: dict[str, Any], *, search_dirs: list[Path]
) -> tuple[str | None, int, int]:
    default_w = int(visual.get("width") or 1200)
    default_h = int(visual.get("height") or 720)
    raw_src = str(visual.get("src") or "").strip()
    if not raw_src:
        return None, default_w, default_h

    if raw_src.startswith("data:image/"):
        m = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", raw_src, re.DOTALL)
        if m and "svg" not in m.group(1).lower():
            try:
                raw_bytes = base64.b64decode(m.group(2))
                dims = parse_image_dimensions(raw_bytes, m.group(1).lower())
                if dims:
                    return raw_src, dims[0], dims[1]
            except Exception:
                pass
            return raw_src, default_w, default_h
        return None, default_w, default_h

    safe_remote = safe_http_url(raw_src)
    if safe_remote:
        try:
            req = urllib.request.Request(safe_remote, headers={"User-Agent": "unmaze/0.3"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype.startswith("image/") and "svg" not in ctype:
                    body = resp.read(8 * 1024 * 1024 + 1)
                    if len(body) <= 8 * 1024 * 1024:
                        dims = parse_image_dimensions(body, ctype) or (default_w, default_h)
                        b64 = base64.b64encode(body).decode("ascii")
                        return f"data:{ctype};base64,{b64}", dims[0], dims[1]
        except Exception:
            pass
        return safe_remote, default_w, default_h

    cand_path = Path(raw_src)
    resolved_file: Path | None = None
    if cand_path.is_absolute():
        if cand_path.is_file():
            resolved_file = cand_path
    else:
        for base_dir in search_dirs:
            probe = (base_dir / cand_path).resolve()
            if probe.is_file():
                resolved_file = probe
                break

    if resolved_file is None:
        return None, default_w, default_h

    raw_bytes = resolved_file.read_bytes()
    guessed_mime, _ = mimetypes.guess_type(resolved_file.name)
    mime = guessed_mime if (guessed_mime and guessed_mime.startswith("image/")) else "image/png"
    dims = parse_image_dimensions(raw_bytes, mime) or (default_w, default_h)
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}", dims[0], dims[1]


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _render_svg_label_pill(cx: float, cy: float, text: str, vh: float) -> str:
    clean = text.strip()
    if not clean:
        return ""
    est_w = max(48.0, len(clean) * 7.6 + 20.0)
    pill_h = 24.0
    half_w = est_w / 2.0
    clamped_cx = _clamp(cx, half_w + 6.0, 1000.0 - half_w - 6.0)
    clamped_cy = _clamp(cy, pill_h / 2.0 + 6.0, vh - pill_h / 2.0 - 6.0)
    return (
        f'<g class="ann-label">'
        f'<rect x="{clamped_cx - half_w:.1f}" y="{clamped_cy - pill_h / 2.0:.1f}" '
        f'width="{est_w:.1f}" height="{pill_h:.1f}" rx="6" fill="#e11d48" stroke="#ffffff" stroke-width="2"/>'
        f'<text x="{clamped_cx:.1f}" y="{clamped_cy + 4.5:.1f}" text-anchor="middle" '
        f'fill="#ffffff" font-size="13" font-weight="700" '
        f'font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif">{esc(clean)}</text>'
        f"</g>"
    )


def render_annotations_svg(annotations: list[dict[str, Any]], width: int, height: int) -> str:
    if not annotations:
        return ""
    vh = max(120.0, round(1000.0 * float(height) / max(1.0, float(width)), 2))
    shapes: list[str] = []
    labels: list[str] = []

    for ann in annotations:
        kind = str(ann.get("kind") or "circle").lower()
        label = str(ann.get("label") or "").strip()

        if kind == "circle":
            cx = float(ann.get("x", 50.0)) * 10.0
            cy = float(ann.get("y", 50.0)) * vh / 100.0
            if "rx" in ann or "ry" in ann:
                rx = max(12.0, float(ann.get("rx", ann.get("r", 7.0))) * 10.0)
                ry = max(12.0, float(ann.get("ry", ann.get("r", 7.0))) * vh / 100.0)
            else:
                r = max(14.0, float(ann.get("r", 6.5)) * 10.0)
                rx, ry = r, r
            shapes.append(
                f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{rx:.1f}" ry="{ry:.1f}" '
                f'fill="rgba(225, 29, 72, 0.08)" stroke="#ffffff" stroke-width="7.5"/>'
                f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{rx:.1f}" ry="{ry:.1f}" '
                f'fill="none" stroke="#e11d48" stroke-width="4"/>'
            )
            if label:
                lbl_y = cy - ry - 18.0 if cy - ry > 34.0 else cy + ry + 18.0
                labels.append(_render_svg_label_pill(cx, lbl_y, label, vh))

        elif kind == "box":
            x = float(ann.get("x", 10.0)) * 10.0
            y = float(ann.get("y", 10.0)) * vh / 100.0
            w = max(20.0, float(ann.get("w", 25.0)) * 10.0)
            h = max(18.0, float(ann.get("h", 15.0)) * vh / 100.0)
            shapes.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="8" '
                f'fill="rgba(225, 29, 72, 0.06)" stroke="#ffffff" stroke-width="7.5"/>'
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="8" '
                f'fill="none" stroke="#e11d48" stroke-width="4"/>'
            )
            if label:
                lbl_y = y - 16.0 if y > 32.0 else y + h + 18.0
                labels.append(_render_svg_label_pill(x + w / 2.0, lbl_y, label, vh))

        elif kind == "arrow":
            x1 = float(ann.get("from_x", 25.0)) * 10.0
            y1 = float(ann.get("from_y", 25.0)) * vh / 100.0
            x2 = float(ann.get("to_x", 50.0)) * 10.0
            y2 = float(ann.get("to_y", 50.0)) * vh / 100.0
            dx, dy = x2 - x1, y2 - y1
            dist = math.hypot(dx, dy)
            if dist < 4.0:
                continue
            ux, uy = dx / dist, dy / dist
            px, py = -uy, ux
            head_len = min(22.0, max(13.0, dist * 0.28))
            head_w = head_len * 0.54
            bx, by = x2 - ux * head_len, y2 - uy * head_len
            pts = (
                f"{x2:.1f},{y2:.1f} "
                f"{bx + px * head_w:.1f},{by + py * head_w:.1f} "
                f"{bx - px * head_w:.1f},{by - py * head_w:.1f}"
            )
            shapes.append(
                f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{bx:.1f}" y2="{by:.1f}" '
                f'stroke="#ffffff" stroke-width="8" stroke-linecap="round"/>'
                f'<polygon points="{pts}" fill="#ffffff" stroke="#ffffff" stroke-width="5" stroke-linejoin="round"/>'
                f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{bx:.1f}" y2="{by:.1f}" '
                f'stroke="#e11d48" stroke-width="4.2" stroke-linecap="round"/>'
                f'<polygon points="{pts}" fill="#e11d48"/>'
            )
            if label:
                labels.append(_render_svg_label_pill(x1 - ux * 14.0, y1 - uy * 16.0, label, vh))

    return (
        f'<svg class="visual-overlay" viewBox="0 0 1000 {vh}" aria-hidden="true">'
        f'{"".join(shapes)}{"".join(labels)}</svg>'
    )


def render_visuals_block_html(
    visuals: list[dict[str, Any]] | None,
    *,
    search_dirs: list[Path],
    concept_lookup: dict[str, str],
    eager: bool = False,
) -> str:
    if not visuals:
        return ""
    cards: list[str] = []
    for vis in visuals:
        if not isinstance(vis, dict):
            continue
        caption = str(vis.get("caption") or "").strip()
        figma_url = safe_http_url(vis.get("figma_url"))
        source_label = str(vis.get("source_label") or ("Figma Reference" if figma_url else "UI Reference")).strip()
        annotations = [a for a in (vis.get("annotations") or []) if isinstance(a, dict)]

        resolved_src, w, h = resolve_visual_asset(vis, search_dirs=search_dirs)
        figma_link_html = (
            f'<a class="vis-ext-link" href="{esc(figma_url)}" target="_blank" rel="noopener noreferrer" '
            f'aria-label="Open in Figma: {esc(source_label)}">'
            f'Open in Figma <span aria-hidden="true">↗</span></a>'
            if figma_url
            else ""
        )

        if not resolved_src:
            if figma_url:
                cards.append(
                    f'<div class="figma-only-card">'
                    f'<div><span class="vis-badge">{esc(source_label)}</span> '
                    f'<span>{fmt_prose(caption or "Design specification for this step", concept_lookup)}</span></div>'
                    f"{figma_link_html}</div>"
                )
            continue

        ann_labels = [str(a.get("label")).strip() for a in annotations if a.get("label")]
        if ann_labels:
            alt_text = f"{source_label} — red marks on: {', '.join(ann_labels)}"
        elif caption:
            alt_text = source_label
        else:
            alt_text = f"{source_label} screenshot"

        overlay_svg = render_annotations_svg(annotations, w, h)
        is_remote = resolved_src.startswith(("http://", "https://"))
        loading_attr = (
            ' fetchpriority="high"'
            if (is_remote and eager)
            else (' loading="lazy"' if is_remote else "")
        )

        cards.append(
            f"""<figure class="visual-card">
  <div class="visual-head">
    <span class="vis-badge">{esc(source_label)}</span>
    <div class="visual-actions">
      {figma_link_html}
      <button type="button" class="vis-zoom-btn" aria-label="Zoom {esc(source_label)} screenshot">Zoom</button>
    </div>
  </div>
  <div class="visual-stage">
    <img src="{esc(resolved_src)}" alt="{esc(alt_text)}" width="{w}" height="{h}"{loading_attr}>
    {overlay_svg}
  </div>
  {f'<figcaption class="visual-caption">{fmt_prose(caption, concept_lookup)}</figcaption>' if caption else ''}
</figure>"""
        )

    return f'<div class="visuals-stack">{"".join(cards)}</div>' if cards else ""


# ---------------------------------------------------------------------------
# Plan Validation (Chapters, Concepts, Visuals, 100% Changed-Line Coverage)
# ---------------------------------------------------------------------------

class PlanValidationError(Exception):
    pass


def _validate_visuals_list(
    visuals: Any,
    owner_name: str,
    errors: list[str],
    *,
    search_dirs: list[Path] | None = None,
) -> int:
    """Validate visuals array and return count of annotated raster/vector screenshot cards."""
    if visuals is None:
        return 0
    if not isinstance(visuals, list):
        errors.append(f"`{owner_name}.visuals` must be an array if provided.")
        return 0
    annotated_count = 0
    for v_idx, vis in enumerate(visuals):
        if not isinstance(vis, dict):
            errors.append(f"`{owner_name}.visuals[{v_idx}]` must be an object.")
            continue
        raw_src = str(vis.get("src") or "").strip()
        has_src = bool(raw_src)
        has_figma = bool(str(vis.get("figma_url") or "").strip())
        if not has_src and not has_figma:
            errors.append(f"`{owner_name}.visuals[{v_idx}]` must specify `src` and/or `figma_url`.")
        src_ok = True
        if has_src and search_dirs is not None:
            resolved_src, _, _ = resolve_visual_asset(vis, search_dirs=search_dirs)
            if resolved_src is None:
                src_ok = False
                errors.append(
                    f"`{owner_name}.visuals[{v_idx}].src` (`{raw_src}`) was not found on disk "
                    f"(searched: {', '.join(str(d) for d in search_dirs)}). "
                    f"Save screenshots into `$WORK_DIR/visuals/` before compiling."
                )
        anns = vis.get("annotations")
        if has_src and (not isinstance(anns, list) or len(anns) == 0):
            errors.append(
                f"`{owner_name}.visuals[{v_idx}]` attaches screenshot `{raw_src}` with zero red marks! "
                f"Every screenshot MUST include at least one `circle`, `arrow`, or `box` in `annotations[]` "
                f"pointing at the exact UI part this code is for."
            )
            continue
        if anns is not None:
            if not isinstance(anns, list):
                errors.append(f"`{owner_name}.visuals[{v_idx}].annotations` must be an array.")
                continue
            for a_idx, ann in enumerate(anns):
                if not isinstance(ann, dict):
                    errors.append(f"`{owner_name}.visuals[{v_idx}].annotations[{a_idx}]` must be an object.")
                    continue
                kind = str(ann.get("kind") or "circle").lower()
                if kind not in VALID_ANNOTATION_KINDS:
                    errors.append(
                        f"`{owner_name}.visuals[{v_idx}].annotations[{a_idx}]` has invalid kind `{kind}`."
                    )
                ann["kind"] = kind
                coord_keys = (
                    ("x", "y")
                    if kind == "circle"
                    else (("from_x", "from_y", "to_x", "to_y") if kind == "arrow" else ("x", "y", "w", "h"))
                )
                for ck in coord_keys:
                    if ck not in ann or not isinstance(ann[ck], (int, float)):
                        errors.append(
                            f"`{owner_name}.visuals[{v_idx}].annotations[{a_idx}]` ({kind}) requires numeric `{ck}`."
                        )
            if has_src and src_ok and len(anns) > 0:
                annotated_count += 1
    return annotated_count


def validate_and_normalize_plan(
    manifest: dict[str, Any],
    plan: dict[str, Any],
    *,
    search_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    """Validate 100% changed-line accounting across hunks/slices, chapters, concepts, and visuals."""
    hunk_map: dict[str, dict[str, Any]] = {h["id"]: h for h in manifest.get("hunks", [])}
    if not hunk_map:
        raise PlanValidationError("Manifest contains zero hunks.")

    hunk_anchors: dict[str, dict[int, int]] = {
        hid: compute_hunk_changed_anchors(h) for hid, h in hunk_map.items()
    }

    errors: list[str] = []

    overview = plan.get("overview")
    if not isinstance(overview, dict):
        errors.append("`overview` object is required in plan.json.")
        overview = {}
    whole_task = str(overview.get("whole_task") or overview.get("tldr") or "").strip()
    if not whole_task:
        errors.append(
            "`overview.whole_task` (or `overview.tldr`) must be a non-empty string explaining the whole task."
        )
    overview["whole_task"] = whole_task
    overview["tldr"] = whole_task
    for req_field in ("before_state", "after_state"):
        if not str(overview.get(req_field, "")).strip():
            errors.append(f"`overview.{req_field}` must be a non-empty string.")
    this_pr_part_raw = overview.get("this_pr_part")
    if this_pr_part_raw is None:
        this_pr_part_raw = overview.get("pr_part") or overview.get("this_pr_scope")
    if this_pr_part_raw is not None and not isinstance(this_pr_part_raw, str):
        errors.append("`overview.this_pr_part` must be a string when provided.")
    overview["this_pr_part"] = (
        str(this_pr_part_raw).strip() if isinstance(this_pr_part_raw, str) else ""
    )
    total_annotated_visuals = _validate_visuals_list(
        overview.get("visuals"), "overview", errors, search_dirs=search_dirs
    )

    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("`steps` must be a non-empty array of review steps.")
        steps = []

    seen_step_ids: set[str] = set()
    claimed_rows: dict[tuple[str, int], str] = {}
    row_step_order: dict[tuple[str, int], int] = {}

    def claim_spec(raw_spec: str, owner_label: str, timeline_idx: int) -> dict[str, Any] | None:
        try:
            hid, ranges = parse_hunk_spec(raw_spec)
        except ValueError as exc:
            errors.append(f"In `{owner_label}`: {exc}")
            return None
        if hid not in hunk_map:
            errors.append(f"`{owner_label}` references unknown hunk ID `{hid}`.")
            return None
        matched = match_slice_rows(hunk_anchors[hid], ranges)
        if not matched:
            avail = sorted(set(hunk_anchors[hid].values()))
            errors.append(
                f"Slice `{raw_spec}` in `{owner_label}` matched zero changed lines in `{hid}` "
                f"(changed lines in `{hid}`: {avail[0]}..{avail[-1] if avail else '?'})."
            )
            return None
        for r_idx in matched:
            key = (hid, r_idx)
            if key in claimed_rows:
                anchor_ln = hunk_anchors[hid][r_idx]
                errors.append(
                    f"Duplicate hunk/line assignment: `{hid}` line {anchor_ln} claimed in both "
                    f"`{claimed_rows[key]}` and `{owner_label}`."
                )
            else:
                claimed_rows[key] = owner_label
                row_step_order[key] = timeline_idx
        return {"raw": raw_spec, "hunk_id": hid, "ranges": ranges, "row_indices": matched}

    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            errors.append(f"Step #{idx} must be an object.")
            continue
        sid = str(step.get("id") or f"step-{idx}").strip()
        step["id"] = sid
        step["_timeline_order"] = idx
        if sid in seen_step_ids:
            errors.append(f"Duplicate step id `{sid}`.")
        seen_step_ids.add(sid)

        if not str(step.get("title", "")).strip():
            errors.append(f"Step `{sid}` is missing a `title`.")
        layer = str(step.get("layer", "")).strip().lower()
        if layer not in VALID_LAYERS:
            errors.append(
                f"Step `{sid}` has invalid `layer` `{layer}`. Expected one of: {', '.join(VALID_LAYERS)}."
            )
        step["layer"] = layer
        total_annotated_visuals += _validate_visuals_list(
            step.get("visuals"), f"steps[{sid}]", errors, search_dirs=search_dirs
        )

        raw_specs = step.get("hunk_ids")
        if not isinstance(raw_specs, list) or not raw_specs:
            errors.append(f"Step `{sid}` must have a non-empty `hunk_ids` array.")
            continue
        parsed_slices = []
        for spec in raw_specs:
            item = claim_spec(str(spec), sid, idx)
            if item:
                item["_highlights"] = []
                parsed_slices.append(item)
        step["_slices"] = parsed_slices

        # Validate optional interactive `diff_highlights` (max 5 per hunk card, non-overlapping)
        raw_hls = step.get("diff_highlights")
        if raw_hls is not None:
            if not isinstance(raw_hls, list):
                errors.append(f"`steps[{sid}].diff_highlights` must be an array if provided.")
            else:
                slices_by_hid: dict[str, list[dict[str, Any]]] = {}
                for sl_obj in parsed_slices:
                    slices_by_hid.setdefault(sl_obj["hunk_id"], []).append(sl_obj)
                seen_hl_lines: dict[str, set[int]] = {}
                hl_counts: dict[str, int] = {}
                for hl_idx, hl in enumerate(raw_hls):
                    if not isinstance(hl, dict):
                        errors.append(f"`steps[{sid}].diff_highlights[{hl_idx}]` must be an object.")
                        continue
                    hl_hid = str(hl.get("hunk_id") or "").strip()
                    if not hl_hid and len(parsed_slices) == 1:
                        hl_hid = parsed_slices[0]["hunk_id"]
                    if hl_hid not in slices_by_hid:
                        errors.append(
                            f"`steps[{sid}].diff_highlights[{hl_idx}]` references hunk `{hl_hid}` "
                            f"which is not in `{sid}.hunk_ids`."
                        )
                        continue
                    hl_counts[hl_hid] = hl_counts.get(hl_hid, 0) + 1
                    if hl_counts[hl_hid] > 5:
                        errors.append(
                            f"`steps[{sid}].diff_highlights` has >5 blocks on `{hl_hid}`. "
                            f"Cap highlights at <=5 key logical chunks per diff card so you don't overwhelm the reviewer."
                        )
                        continue
                    hl_title = str(hl.get("title") or "").strip()
                    hl_expl = str(hl.get("explanation") or "").strip()
                    if not hl_title or not hl_expl:
                        errors.append(
                            f"`steps[{sid}].diff_highlights[{hl_idx}]` requires non-empty `title` and `explanation`."
                        )
                        continue
                    raw_ln_spec = str(hl.get("lines") or "").strip()
                    try:
                        hl_ranges = parse_line_ranges(raw_ln_spec, f"steps[{sid}].diff_highlights[{hl_idx}]")
                    except ValueError as exc:
                        errors.append(str(exc))
                        continue
                    expanded_lns = {ln_no for lo, hi in hl_ranges for ln_no in range(lo, hi + 1)}
                    claimed_set = seen_hl_lines.setdefault(hl_hid, set())
                    overlap = sorted(claimed_set & expanded_lns)
                    if overlap:
                        errors.append(
                            f"`steps[{sid}].diff_highlights[{hl_idx}]` overlaps already-highlighted line(s) "
                            f"{overlap[:5]} on `{hl_hid}`."
                        )
                        continue
                    claimed_set.update(expanded_lns)
                    slices_by_hid[hl_hid][0]["_highlights"].append(
                        {
                            "title": hl_title,
                            "explanation": hl_expl,
                            "lines_raw": raw_ln_spec,
                            "ranges": hl_ranges,
                        }
                    )

    mechanical = plan.get("mechanical") or {}
    if not isinstance(mechanical, dict):
        errors.append("`mechanical` must be an object if provided.")
        mechanical = {}
    mech_groups = mechanical.get("groups") or []
    if not isinstance(mech_groups, list):
        errors.append("`mechanical.groups` must be an array if provided.")
        mech_groups = []

    mech_timeline = len(steps) + 1
    for g_idx, grp in enumerate(mech_groups, start=1):
        if not isinstance(grp, dict):
            errors.append(f"`mechanical.groups[{g_idx - 1}]` must be an object.")
            continue
        g_label = str(grp.get("title") or f"mechanical-{g_idx}").strip()
        grp["title"] = g_label
        g_specs = grp.get("hunk_ids") or []
        if not isinstance(g_specs, list) or not g_specs:
            errors.append(f"Mechanical group `{g_label}` must have a non-empty `hunk_ids` array.")
            continue
        parsed_slices = []
        for spec in g_specs:
            item = claim_spec(str(spec), f"mechanical:{g_label}", mech_timeline)
            if item:
                parsed_slices.append(item)
        grp["_slices"] = parsed_slices

    # Verify 100% changed-line coverage across every hunk
    unclaimed_msgs: list[str] = []
    for hid, anchors in hunk_anchors.items():
        missing_anchors = [
            anchor for r_idx, anchor in sorted(anchors.items()) if (hid, r_idx) not in claimed_rows
        ]
        if missing_anchors:
            h = hunk_map[hid]
            if len(missing_anchors) == len(anchors):
                unclaimed_msgs.append(
                    f"  - {hid}: entire hunk unclaimed ({h['file_path']} L{h['new_start']}, +{h['additions']}/-{h['deletions']})"
                )
            else:
                preview = ", ".join(str(x) for x in missing_anchors[:10])
                more = f" (+{len(missing_anchors) - 10} more)" if len(missing_anchors) > 10 else ""
                unclaimed_msgs.append(
                    f"  - {hid} ({h['file_path']}): unclaimed changed line(s) [{preview}{more}]"
                )
    if unclaimed_msgs:
        errors.append(
            "100% Diff Coverage Check FAILED — some hunks or sliced lines were omitted:\n"
            + "\n".join(unclaimed_msgs)
        )

    # Validate optional Chapters (Virtual Stacked PRs)
    chapters = plan.get("chapters")
    if chapters is not None:
        if not isinstance(chapters, list) or not chapters:
            errors.append("`chapters` must be a non-empty array when provided.")
        else:
            chapter_steps_seen: list[str] = []
            for c_idx, ch in enumerate(chapters, start=1):
                if not isinstance(ch, dict):
                    errors.append(f"`chapters[{c_idx - 1}]` must be an object.")
                    continue
                ch["id"] = str(ch.get("id") or f"chapter-{c_idx}").strip()
                if not str(ch.get("title") or "").strip():
                    errors.append(f"Chapter `{ch['id']}` requires a `title`.")
                sids = [str(s).strip() for s in (ch.get("step_ids") or [])]
                if not sids:
                    errors.append(f"Chapter `{ch['id']}` must list at least one step in `step_ids`.")
                ch["step_ids"] = sids
                for s in sids:
                    if s not in seen_step_ids:
                        errors.append(f"Chapter `{ch['id']}` references unknown step `{s}`.")
                    chapter_steps_seen.append(s)
            ordered_step_ids = [st["id"] for st in steps]
            if chapter_steps_seen != ordered_step_ids:
                errors.append(
                    "`chapters[*].step_ids` must partition `steps[*].id` in exact reading order without omissions or duplicates."
                )

    # Validate Concepts (both Task-Introduced & Existing Repo Concepts) + Pass-2 Semantic Sweep Gate
    concepts = plan.get("concepts") or []
    if not isinstance(concepts, list):
        errors.append("`concepts` must be an array if provided.")
        concepts = []
    seen_concepts: set[str] = set()
    for c_idx, c in enumerate(concepts):
        if not isinstance(c, dict):
            errors.append(f"`concepts[{c_idx}]` must be an object.")
            continue
        cid = re.sub(r"[^a-z0-9_-]+", "-", str(c.get("id") or c.get("term") or f"concept-{c_idx}").strip().lower()).strip("-")
        if not cid or cid in seen_concepts:
            errors.append(f"Duplicate or empty concept id `{cid}`.")
        seen_concepts.add(cid)
        c["id"] = cid
        raw_origin = str(c.get("origin") or "existing").strip().lower()
        c["origin"] = (
            "introduced"
            if raw_origin in {"introduced", "new", "this_pr", "task", "added"}
            else "existing"
        )
        if not str(c.get("term") or "").strip():
            errors.append(f"Concept `{cid}` requires `term`.")
        if not str(c.get("what_it_is") or "").strip():
            errors.append(f"Concept `{cid}` requires `what_it_is` (1-sentence plain-English definition).")
        for rs in c.get("related_steps") or []:
            if str(rs) not in seen_step_ids:
                errors.append(f"Concept `{cid}` references unknown `related_steps` entry `{rs}`.")

    no_concepts_just = str(plan.get("no_concepts_justification") or "").strip()
    if not concepts and len(no_concepts_just) < 15:
        errors.append(
            "Two-Sweep Concept Pipeline Check FAILED — `concepts[]` is empty without `\"no_concepts_justification\"`!\n"
            "  Run both sweeps after writing `plan.json`:\n"
            "    - Sweep 1 (Extract & filter candidates cold from written text):\n"
            "        `python3 <SKILL_DIR>/scripts/concept_sweep.py dump-prose --plan $WORK_DIR/plan.json`\n"
            "        `python3 <SKILL_DIR>/scripts/concept_sweep.py save-candidates --plan $WORK_DIR/plan.json --candidates $WORK_DIR/concept-candidates.json`\n"
            "    - Sweep 2 (Explain confirmed concepts from repo/diff + link mentions):\n"
            "        `python3 <SKILL_DIR>/scripts/concept_sweep.py apply --plan $WORK_DIR/plan.json --patch $WORK_DIR/concept-links.json`"
        )
    elif concepts and (
        not bool(plan.get("concept_candidates_extracted"))
        or not bool(plan.get("concept_sweep_completed"))
    ):
        errors.append(
            "Two-Sweep Concept Pipeline Check FAILED — both `\"concept_candidates_extracted\": true` (Sweep 1) "
            "and `\"concept_sweep_completed\": true` (Sweep 2) are required before compiling!\n"
            "  1. Sweep 1 (Extract & filter candidates cold from written text): `concept_sweep.py dump-prose` -> `save-candidates`\n"
            "  2. Sweep 2 (Explain confirmed concepts from repo/diff + link mentions): `concept_sweep.py apply`"
        )

    # Enforce Mandatory Visual Grounding (even on backend PRs with a frontend consumer!)
    no_vis_reason = str(plan.get("no_visuals_justification") or "").strip()
    if total_annotated_visuals == 0 and len(no_vis_reason) < 20:
        errors.append(
            "Visual Grounding Check FAILED — zero red-annotated screenshots attached across the review!\n"
            "  Even when a PR only changes backend/API/DB code, you MUST:\n"
            "    1. Trace which frontend screen, admin view, or user flow consumes or triggers this backend logic,\n"
            "    2. Capture/export a screenshot or Figma frame of that UI into `$WORK_DIR/visuals/`,\n"
            "    3. View it with `Read` and add red `circle` / `arrow` / `box` marks showing what this code is for.\n"
            "  ONLY if you verified no UI consumer exists anywhere (or no app/Figma/PR image is reachable), "
            "set top-level `\"no_visuals_justification\"` (>= 20 chars) stating which downstream UI you checked and why no screenshot could be captured."
        )

    if errors:
        raise PlanValidationError("\n".join(errors))

    # Normalize optional `file_scopes` (`{"path/to/file": {"surface": "...", "area": "..."}}`)
    raw_file_scopes = plan.get("file_scopes")
    if raw_file_scopes is not None and not isinstance(raw_file_scopes, dict):
        raise PlanValidationError("`file_scopes` must be an object mapping file paths to `{surface, area}`.")

    plan["_row_step_order"] = row_step_order
    plan["_total_annotated_visuals"] = total_annotated_visuals
    return plan


# ---------------------------------------------------------------------------
# 2-Level Scope Filter Tree (Role Surface -> App Area; never raw filenames)
# ---------------------------------------------------------------------------

CANONICAL_SURFACE_ORDER = [
    "Frontend",
    "Backend & API",
    "Database & Contracts",
    "Tests & QA",
    "Infra & Config",
]

ACRONYM_WORDS = {"api", "sql", "ui", "ux", "db", "di", "http", "https", "qa", "id", "ids", "url", "jwt", "sso", "cli", "pr", "css", "dom", "html", "rpc"}

GENERIC_PATH_PARTS = {
    "src", "app", "apps", "packages", "lib", "internal", "pkg", "cmd",
    "server", "backend", "frontend", "web", "client", "ui", "api", "routes",
    "controllers", "handlers", "services", "components", "pages", "views",
    "hooks", "migrations", "db", "__tests__", "tests", "test", "config",
    "utils", "helpers", "common", "shared", "modules", "features",
}

STRIP_FILE_EXTS_RE = re.compile(
    r"(\.(test|spec|stories|d))?\.(tsx?|jsx?|mjs|cjs|vue|svelte|css|scss|less|py|go|rs|java|kt|rb|php|sql|prisma|graphql|proto|ya?ml|json|toml|lock|md|sh)$",
    re.IGNORECASE,
)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-") or "general"


def _humanize_words(raw: str) -> str:
    s = re.sub(r"^\d{6,}[_-]?", "", raw.strip())
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    parts = [p for p in re.split(r"[\s_\-./]+", s) if p]
    if not parts:
        return "Core Module"
    out = []
    for w in parts:
        lw = w.lower()
        if lw in ACRONYM_WORDS:
            out.append(lw.upper() if lw != "ids" else "IDs")
        else:
            out.append(w[0].upper() + w[1:] if len(w) > 1 else w.upper())
    return " ".join(out)


def clean_area_label(raw_label: str) -> str:
    """Guarantee area labels represent semantic app parts and never exact filenames."""
    txt = str(raw_label or "").strip()
    if not txt:
        return "General"
    # If a path or filename with extension was passed, strip folder prefix and file extensions
    if "/" in txt or STRIP_FILE_EXTS_RE.search(txt):
        base = PurePosixPath(txt).name
        base = STRIP_FILE_EXTS_RE.sub("", base)
        return _humanize_words(base)
    return txt


def normalize_surface_label(raw_surface: str) -> str:
    low = str(raw_surface or "").strip().lower()
    if not low:
        return "Backend & API"
    if low in {"fe", "frontend", "ui", "web", "client"}:
        return "Frontend"
    if low in {"be", "backend", "api", "server", "service", "services", "backend & api"}:
        return "Backend & API"
    if low in {"db", "data", "database", "schema", "contracts", "database & contracts", "sql"}:
        return "Database & Contracts"
    if low in {"test", "tests", "qa", "verification", "testing", "tests & qa"}:
        return "Tests & QA"
    if low in {"infra", "config", "devops", "ci", "build", "boilerplate", "mechanical", "infra & config"}:
        return "Infra & Config"
    return str(raw_surface).strip()


def infer_file_scope(
    file_obj: dict[str, Any], *, is_mechanical: bool = False, step_layer: str = ""
) -> tuple[str, str]:
    """Infer `(surface_label, area_label)` without ever exposing raw filenames."""
    fpath = str(file_obj.get("path") or "")
    cat = str(file_obj.get("category") or "")
    p = PurePosixPath(fpath)
    parts_low = [seg.lower() for seg in p.parts[:-1]]
    name_low = p.name.lower()
    stem_clean = STRIP_FILE_EXTS_RE.sub("", p.name)

    if (
        is_mechanical
        or cat == "generated_or_lockfile"
        or name_low in {"index.ts", "index.js", "__init__.py", "package.json"}
        or name_low.endswith((".lock", "lock.yaml", "lock.json"))
        or (parts_low and parts_low[0] in {"config", ".github", "infra", "deploy", "scripts"})
    ):
        surface = "Infra & Config"
    elif (
        cat == "test"
        or any(seg in {"__tests__", "tests", "test", "e2e", "cypress"} for seg in parts_low)
        or ".test." in name_low
        or ".spec." in name_low
        or name_low.endswith("_test.go")
        or name_low.startswith("test_")
    ):
        surface = "Tests & QA"
    elif (
        name_low.endswith((".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".less"))
        or any(seg in {"frontend", "client", "web", "ui", "components", "pages", "views", "hooks", "drawers", "modals"} for seg in parts_low)
    ):
        surface = "Frontend"
    elif (
        name_low.endswith((".sql", ".prisma", ".graphql", ".proto"))
        or any(seg in {"migrations", "schema", "schemas", "db", "contracts"} for seg in parts_low)
        or stem_clean.lower() in {"types", "schema", "models", "contracts"}
        or step_layer == "contracts"
    ):
        surface = "Database & Contracts"
    else:
        surface = "Backend & API"

    # Pick semantic module/feature part (never a filename)
    specific_dirs = [seg for seg in p.parts[:-1] if seg.lower() not in GENERIC_PATH_PARTS]
    if specific_dirs:
        area = _humanize_words(specific_dirs[-1])
    elif surface == "Infra & Config":
        if "config" in parts_low or stem_clean.lower() in {"container", "di", "env"}:
            area = "App Wiring & Config"
        else:
            area = "Exports & Dependencies"
    elif surface == "Tests & QA":
        base_topic = re.sub(r"[_-]?(test|spec)s?$", "", stem_clean, flags=re.IGNORECASE)
        area = f"{_humanize_words(base_topic)} Tests" if base_topic else "Automated Tests"
    elif "routes" in parts_low or "controllers" in parts_low or "api" in parts_low:
        area = f"{_humanize_words(stem_clean)} API"
    elif "migrations" in parts_low or name_low.endswith(".sql"):
        area = f"{_humanize_words(stem_clean)} Schema"
    else:
        area = _humanize_words(stem_clean)

    return surface, area


def resolve_slice_scope(
    file_obj: dict[str, Any],
    *,
    plan_file_scopes: dict[str, Any],
    step_scope: dict[str, Any] | None,
    is_mechanical: bool = False,
    step_layer: str = "",
) -> tuple[str, str, str, str]:
    """Return `(surface_id, surface_label, area_id, area_label)`."""
    fpath = file_obj["path"]
    override = plan_file_scopes.get(fpath) if isinstance(plan_file_scopes, dict) else None
    if isinstance(override, dict) and (override.get("surface") or override.get("area")):
        surf_lbl = normalize_surface_label(override.get("surface") or "")
        area_lbl = clean_area_label(override.get("area") or surf_lbl)
    elif isinstance(step_scope, dict) and (step_scope.get("surface") or step_scope.get("area")):
        surf_lbl = normalize_surface_label(step_scope.get("surface") or "")
        area_lbl = clean_area_label(step_scope.get("area") or surf_lbl)
    else:
        surf_lbl, area_lbl = infer_file_scope(
            file_obj, is_mechanical=is_mechanical, step_layer=step_layer
        )
    surf_id = _slugify(surf_lbl)
    area_id = f"{surf_id}__{_slugify(area_lbl)}"
    return surf_id, surf_lbl, area_id, area_lbl


def _render_cb_box_html() -> str:
    return (
        '<span class="cb-box" aria-hidden="true">'
        '<svg class="cb-svg cb-svg-check" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>'
        '<svg class="cb-svg cb-svg-dash" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/></svg>'
        "</span>"
    )


def render_scope_popover_html(surfaces_tree: list[dict[str, Any]]) -> str:
    """Render the 2-level tree popover (`Role Surface -> App Area`, zero filenames)."""
    cb_box = _render_cb_box_html()
    groups_html: list[str] = []
    for surf in surfaces_tree:
        sid = surf["id"]
        slabel = surf["label"]
        s_steps = len(surf["step_indices"])
        s_count_txt = (
            f"{s_steps} step{'s' if s_steps != 1 else ''}"
            if s_steps > 0
            else "Boilerplate"
        )
        area_rows: list[str] = []
        for area in surf["areas"]:
            aid = area["id"]
            alabel = area["label"]
            a_steps = len(area["step_indices"])
            a_count_txt = (
                f"{a_steps} step{'s' if a_steps != 1 else ''}"
                if a_steps > 0
                else "Boilerplate"
            )
            area_rows.append(
                f'<div class="scope-row scope-area-row">'
                f'<label class="scope-label">'
                f'<input type="checkbox" class="cb-input scope-area-cb" '
                f'data-surface-id="{esc(sid)}" data-area-id="{esc(aid)}" data-area-label="{esc(alabel)}" checked>'
                f"{cb_box}"
                f'<span class="scope-area-name">{esc(alabel)}</span>'
                f"</label>"
                f'<span class="scope-meta-count">{esc(a_count_txt)}</span>'
                f'<button type="button" class="scope-only-btn" data-only-area="{esc(aid)}" '
                f'aria-label="Only {esc(alabel)}">Only</button>'
                f"</div>"
            )

        groups_html.append(
            f'<div class="scope-surface-group" data-surface-group="{esc(sid)}">'
            f'<div class="scope-row scope-surface-row">'
            f'<button type="button" class="scope-exp-btn" aria-expanded="true" '
            f'aria-label="Toggle {esc(slabel)} areas">▾</button>'
            f'<label class="scope-label">'
            f'<input type="checkbox" class="cb-input scope-surface-cb" '
            f'data-surface-id="{esc(sid)}" data-surface-label="{esc(slabel)}" checked>'
            f"{cb_box}"
            f'<span class="scope-surface-name">{esc(slabel)}</span>'
            f"</label>"
            f'<span class="scope-meta-count">{esc(s_count_txt)}</span>'
            f'<button type="button" class="scope-only-btn" data-only-surface="{esc(sid)}" '
            f'aria-label="Only {esc(slabel)}">Only</button>'
            f"</div>"
            f'<div class="scope-area-list">{"".join(area_rows)}</div>'
            f"</div>"
        )

    return f"""<div id="scope-filter-popover" popover="auto" class="scope-popover" role="region" aria-label="Filter review areas">
      <div class="scope-pop-head">
        <div>
          <strong class="scope-pop-title">Filter Review by Area</strong>
          <span class="scope-pop-sub">Unmatched chapters &amp; steps dim automatically</span>
        </div>
        <button type="button" id="scope-select-all" class="scope-all-btn">Select all</button>
      </div>
      <div class="scope-tree">
        {"".join(groups_html)}
      </div>
    </div>"""


# ---------------------------------------------------------------------------
# HTML Rendering (Timeline Cards, Formatted Blocks, Concept Modals, Arc Sidebar)
# ---------------------------------------------------------------------------

def compute_row_pr_url(
    raw_deep_url: str | None,
    *,
    pr_old_line: int | None,
    pr_new_line: int | None,
    prefer_side: str = "auto",
) -> str | None:
    """Build an exact single-line PR/MR/blob deep link (`#diff-<sha256>R<new>` or `L<old>`)."""
    deep_url = safe_http_url(raw_deep_url)
    if not deep_url:
        return None

    m_gh = re.match(r"^(https?://[^#]+#diff-[0-9a-f]{64})", deep_url)
    if m_gh:
        base = m_gh.group(1)
        if prefer_side == "left" and pr_old_line is not None:
            return f"{base}L{pr_old_line}"
        if pr_new_line is not None:
            return f"{base}R{pr_new_line}"
        if pr_old_line is not None:
            return f"{base}L{pr_old_line}"
        return None

    m_gl = re.match(r"^(https?://[^#]+/diffs#[0-9a-f]{40})", deep_url)
    if m_gl and (pr_new_line is not None or pr_old_line is not None):
        ol = pr_old_line if pr_old_line is not None else pr_new_line
        nl = pr_new_line if pr_new_line is not None else pr_old_line
        return f"{m_gl.group(1)}_{ol}_{nl}"

    if "/blob/" in deep_url and pr_new_line is not None:
        blob_base = deep_url.split("#", 1)[0]
        return f"{blob_base}#L{pr_new_line}"

    return None


def render_code_row_html(
    *,
    cls: str,
    old_line: int | None,
    new_line: int | None,
    pr_old_line: int | None = None,
    pr_new_line: int | None = None,
    raw_deep_url: str | None = None,
    sig: str,
    content: str,
    lang: str,
    extra_attrs: str = "",
    trailing_html: str = "",
) -> str:
    old_s = str(old_line) if old_line is not None else ""
    new_s = str(new_line) if new_line is not None else ""
    highlighted = highlight_code_line(content, lang)

    eff_pr_old = pr_old_line if pr_old_line is not None else old_line
    eff_pr_new = pr_new_line if pr_new_line is not None else new_line

    old_url = (
        compute_row_pr_url(raw_deep_url, pr_old_line=eff_pr_old, pr_new_line=None, prefer_side="left")
        if old_line is not None and eff_pr_old is not None
        else None
    )
    new_url = (
        compute_row_pr_url(raw_deep_url, pr_old_line=None, pr_new_line=eff_pr_new, prefer_side="right")
        if new_line is not None and eff_pr_new is not None
        else None
    )
    primary_url = new_url or old_url or compute_row_pr_url(
        raw_deep_url, pr_old_line=eff_pr_old, pr_new_line=eff_pr_new
    )
    target_ln_lbl = (
        f"line {eff_pr_new}"
        if eff_pr_new is not None
        else (f"deleted line {eff_pr_old}" if eff_pr_old is not None else "line")
    )

    old_cell = (
        f'<a class="ln ln-link" href="{esc(old_url)}" target="_blank" rel="noopener noreferrer" '
        f'title="Open base line {eff_pr_old} in PR ↗">{esc(old_s)}</a>'
        if old_url
        else f'<span class="ln" aria-hidden="true">{esc(old_s)}</span>'
    )
    new_cell = (
        f'<a class="ln ln-link" href="{esc(new_url)}" target="_blank" rel="noopener noreferrer" '
        f'title="Open line {eff_pr_new} in PR ↗">{esc(new_s)}</a>'
        if new_url
        else f'<span class="ln" aria-hidden="true">{esc(new_s)}</span>'
    )
    hover_jump_html = (
        f'<a class="row-pr-jump" href="{esc(primary_url)}" target="_blank" rel="noopener noreferrer" '
        f'title="Jump to {esc(target_ln_lbl)} in GitHub PR to comment ↗">PR ↗</a>'
        if primary_url
        else ""
    )

    return (
        f'<span class="dl {cls}"{extra_attrs}>'
        f"{old_cell}"
        f"{new_cell}"
        f'<span class="sig" aria-hidden="true">{sig}</span>'
        f'<span class="code-txt">{highlighted}</span>'
        f"{trailing_html}"
        f"{hover_jump_html}"
        f"</span>"
    )


def render_timeline_slice_card_html(
    *,
    card_uid: str,
    file_obj: dict[str, Any],
    hunk: dict[str, Any],
    slice_item: dict[str, Any],
    file_tokens: list[dict[str, Any]],
    step_order: int,
    total_step_order: int,
    note: str | None,
    concept_lookup: dict[str, str],
    collapsed: bool,
    area_id: str = "",
    area_label: str = "",
) -> str:
    fpath = file_obj["path"]
    lang = detect_lang_family(fpath)
    has_full_file = bool(file_obj.get("post_lines")) and not hunk["is_synthetic"]

    active_keys = {(hunk["id"], r_idx) for r_idx in slice_item["row_indices"]}
    eval_rows = evaluate_file_at_step(
        file_tokens,
        has_full_file=has_full_file,
        step_order=step_order,
        active_keys=active_keys,
    )

    active_indices = [i for i, r in enumerate(eval_rows) if r.get("active")]
    if not active_indices:
        active_indices = [0] if eval_rows else []

    first_act = active_indices[0] if active_indices else 0
    last_act = active_indices[-1] if active_indices else 0
    win_start = max(0, first_act - 3)
    win_end = min(len(eval_rows) - 1, last_act + 3) if eval_rows else -1

    # Compute effective 1-based step line for each row (attaching `del` rows to the replacement `new_line` below)
    eff_lines: list[int] = [1] * len(eval_rows)
    next_known_nl: int | None = None
    for r_i in range(len(eval_rows) - 1, -1, -1):
        r_nl = eval_rows[r_i].get("new_line")
        if r_nl is not None:
            next_known_nl = int(r_nl)
            eff_lines[r_i] = next_known_nl
        elif next_known_nl is not None:
            eff_lines[r_i] = next_known_nl
        else:
            eff_lines[r_i] = int(eval_rows[r_i].get("old_line") or 1)

    slice_highlights: list[dict[str, Any]] = slice_item.get("_highlights") or []
    if slice_highlights and eval_rows:
        for hl in slice_highlights:
            matched_eval_idxs = [
                i for i, eln in enumerate(eff_lines)
                if any(lo <= eln <= hi for lo, hi in hl["ranges"])
            ]
            if matched_eval_idxs:
                win_start = min(win_start, matched_eval_idxs[0])
                win_end = max(win_end, matched_eval_idxs[-1])

    ctx_before = eval_rows[max(0, win_start - 60) : win_start]
    main_window = eval_rows[win_start : win_end + 1] if win_end >= win_start else []
    main_eff_lines = eff_lines[win_start : win_end + 1] if win_end >= win_start else []
    ctx_after = eval_rows[win_end + 1 : min(len(eval_rows), win_end + 1 + 60)]

    # File-at-this-step view (`F^(t)`: every non-deleted line currently in the file at Step `t`)
    step_file_rows = [r for r in eval_rows if r["kind"] != "del"]
    can_show_step_file = has_full_file and len(step_file_rows) > 0

    # Check if future steps add/delete more lines in this file later in the timeline
    has_future_changes = any(tok["step_order"] > step_order for tok in file_tokens if tok["step_order"] > 0)
    has_past_changes = any(tok["step_order"] < step_order for tok in file_tokens if tok["step_order"] > 0)

    timeline_tag = ""
    if has_future_changes or has_past_changes:
        if has_future_changes and not has_past_changes:
            t_lbl = "Step 1 state of file"
        elif has_future_changes and has_past_changes:
            t_lbl = "Intermediate file state"
        else:
            t_lbl = "Final PR state of file"
        timeline_tag = f'<span class="timeline-badge">{esc(t_lbl)}</span>'

    add_cnt = sum(1 for r in main_window if r.get("active") and r["kind"] == "add")
    del_cnt = sum(1 for r in main_window if r.get("active") and r["kind"] == "del")
    stat_parts: list[str] = []
    if add_cnt:
        stat_parts.append(f'<span class="stat-add">+{add_cnt}</span>')
    if del_cnt:
        stat_parts.append(f'<span class="stat-del">−{del_cnt}</span>')
    if not stat_parts:
        stat_parts.append(f'<span class="stat-neutral">{esc(hunk["file_status"])}</span>')
    stats_html = " ".join(stat_parts)

    if hunk["file_status"] == "renamed" and hunk["old_path"] != hunk["new_path"]:
        path_display = f"{esc(hunk['old_path'])} → {esc(hunk['new_path'])}"
    else:
        path_display = esc(fpath)

    # Compute deep link (if sliced, adjust GitHub `#diff-...R<line>` to slice's starting new_line)
    deep_url = safe_http_url(hunk.get("deep_link"))
    if deep_url and slice_item.get("ranges") and "#diff-" in deep_url:
        base_anchor = deep_url.split("#diff-", 1)[0]
        digest_part = re.match(r"^([0-9a-f]{64})", deep_url.split("#diff-", 1)[1])
        if digest_part:
            slice_first_ln = slice_item["ranges"][0][0]
            deep_url = f"{base_anchor}#diff-{digest_part.group(1)}R{slice_first_ln}"

    link_html = (
        f'<a class="hunk-pr-btn" href="{esc(deep_url)}" target="_blank" rel="noopener noreferrer" '
        f'aria-label="Open in PR: {esc(fpath)}">'
        f'Open in PR <span aria-hidden="true">↗</span></a>'
        if deep_url
        else ""
    )

    area_tag_html = (
        f'<span class="area-badge">{esc(area_label)}</span>'
        if area_label
        else ""
    )

    step_file_btn_html = (
        f'<button type="button" class="snapshot-toggle-btn" aria-pressed="false">'
        f"File at this step ({len(step_file_rows)})</button>"
        if can_show_step_file and step_order <= total_step_order
        else ""
    )

    before_bar_html = ""
    before_rows_html = ""
    if ctx_before:
        initial_chunk = min(EXPAND_CHUNK, len(ctx_before))
        before_bar_html = (
            f'<div class="ctx-bar ctx-bar-before" data-total="{len(ctx_before)}" data-shown="0">'
            f'<button type="button" class="ctx-btn ctx-more-btn" data-dir="before">'
            f"↑ Show {initial_chunk} lines above</button>"
            f'<button type="button" class="ctx-btn ctx-less-btn" data-dir="before" hidden>'
            f"Show less</button></div>"
        )
        before_rows_html = "".join(
            render_code_row_html(
                cls="dl-exp",
                old_line=row.get("old_line"),
                new_line=row.get("new_line"),
                pr_old_line=row.get("pr_old_line"),
                pr_new_line=row.get("pr_new_line"),
                raw_deep_url=deep_url,
                sig=" ",
                content=row.get("content", ""),
                lang=lang,
                extra_attrs=f' data-before-idx="{idx}" hidden',
            )
            for idx, row in enumerate(ctx_before)
        )

    after_bar_html = ""
    after_rows_html = ""
    if ctx_after:
        initial_chunk = min(EXPAND_CHUNK, len(ctx_after))
        after_rows_html = "".join(
            render_code_row_html(
                cls="dl-exp",
                old_line=row.get("old_line"),
                new_line=row.get("new_line"),
                pr_old_line=row.get("pr_old_line"),
                pr_new_line=row.get("pr_new_line"),
                raw_deep_url=deep_url,
                sig=" ",
                content=row.get("content", ""),
                lang=lang,
                extra_attrs=f' data-after-idx="{idx}" hidden',
            )
            for idx, row in enumerate(ctx_after)
        )
        after_bar_html = (
            f'<div class="ctx-bar ctx-bar-after" data-total="{len(ctx_after)}" data-shown="0">'
            f'<button type="button" class="ctx-btn ctx-more-btn" data-dir="after">'
            f"↓ Show {initial_chunk} lines below</button>"
            f'<button type="button" class="ctx-btn ctx-less-btn" data-dir="after" hidden>'
            f"Show less</button></div>"
        )

    # Map interactive diff_highlights onto `main_window` rows
    row_hl_map: dict[int, dict[str, Any]] = {}
    resolved_highlights: list[dict[str, Any]] = []
    for raw_hl in slice_highlights:
        matched_m_idxs = [
            m_i for m_i, eln in enumerate(main_eff_lines)
            if any(lo <= eln <= hi for lo, hi in raw_hl["ranges"])
        ]
        if not matched_m_idxs:
            continue
        seq_idx = len(resolved_highlights)
        num_sym = CIRCLED_NUMERALS[seq_idx] if seq_idx < len(CIRCLED_NUMERALS) else f"#{seq_idx + 1}"
        hl_uid = f"{card_uid}-hl{seq_idx + 1}"
        start_row = main_window[matched_m_idxs[0]]
        hl_pr_url = compute_row_pr_url(
            deep_url,
            pr_old_line=start_row.get("pr_old_line"),
            pr_new_line=start_row.get("pr_new_line"),
        )
        hl_entry = {
            "uid": hl_uid,
            "num": num_sym,
            "title": raw_hl["title"],
            "explanation": raw_hl["explanation"],
            "lines_raw": raw_hl["lines_raw"],
            "pr_url": hl_pr_url,
            "start_m_idx": matched_m_idxs[0],
            "end_m_idx": matched_m_idxs[-1],
        }
        resolved_highlights.append(hl_entry)
        for m_i in range(matched_m_idxs[0], matched_m_idxs[-1] + 1):
            row_hl_map[m_i] = hl_entry

    main_rows_html: list[str] = []
    for m_idx, ln in enumerate(main_window):
        kind = ln["kind"]
        if kind == "add":
            sig, cls = "+", "dl-add"
        elif kind == "del":
            sig, cls = "-", "dl-del"
        elif kind == "ctx":
            sig = " "
            cls = "dl-ctx dl-past" if ln.get("from_earlier_step") else "dl-ctx"
        else:
            sig, cls = " ", "dl-meta"

        extra_attrs = ""
        hl_marker = ""
        hl_hit = row_hl_map.get(m_idx)
        if hl_hit:
            cls += " dl-has-hl"
            if m_idx == hl_hit["start_m_idx"]:
                cls += " dl-hl-start"
                hl_marker = f'<span class="dl-hl-marker" aria-hidden="true">{esc(hl_hit["num"])}</span>'
            if m_idx == hl_hit["end_m_idx"]:
                cls += " dl-hl-end"
            extra_attrs = f' data-hl-id="{esc(hl_hit["uid"])}"'

        main_rows_html.append(
            render_code_row_html(
                cls=cls,
                old_line=ln.get("old_line"),
                new_line=ln.get("new_line"),
                pr_old_line=ln.get("pr_old_line"),
                pr_new_line=ln.get("pr_new_line"),
                raw_deep_url=deep_url if kind != "meta" else None,
                sig=sig,
                content=ln.get("content", ""),
                lang=lang if kind != "meta" else "plain",
                extra_attrs=extra_attrs,
                trailing_html=hl_marker,
            )
        )

    snapshot_block_html = ""
    if can_show_step_file:
        snap_rows = "".join(
            render_code_row_html(
                cls="dl-add" if r.get("active") and r["kind"] == "add" else "dl-ctx",
                old_line=None,
                new_line=r.get("new_line"),
                pr_old_line=r.get("pr_old_line"),
                pr_new_line=r.get("pr_new_line"),
                raw_deep_url=deep_url if r["kind"] != "meta" else None,
                sig="+" if (r.get("active") and r["kind"] == "add") else " ",
                content=r.get("content", ""),
                lang=lang if r["kind"] != "meta" else "plain",
            )
            for r in step_file_rows
        )
        snapshot_block_html = (
            f'<div class="snapshot-view" hidden>'
            f'<div class="snapshot-banner">Showing exact file state after this step '
            f'({len(step_file_rows)} lines; later steps are not added yet).</div>'
            f'<pre class="diff-pre" role="region" aria-label="File state at this step" tabindex="0">'
            f'<code class="diff-code">{snap_rows}</code></pre>'
            f"</div>"
        )

    hl_hint_html = ""
    hl_strip_html = ""
    if resolved_highlights:
        strip_btns = "".join(
            f'<button type="button" class="dl-hl-pill" data-hl-trigger="{esc(h_e["uid"])}" '
            f'aria-expanded="false" aria-controls="hl-note-{esc(h_e["uid"])}">'
            f'<span class="dl-hl-num" aria-hidden="true">{esc(h_e["num"])}</span>'
            f'<span>{esc(h_e["title"])}</span>'
            f'<span class="dl-hl-ln">L{esc(h_e["lines_raw"])}</span></button>'
            for h_e in resolved_highlights
        )
        hl_strip_html = (
            f'<div class="hl-chip-strip">'
            f'<span class="hl-strip-lbl">Click block to explain:</span>'
            f"{strip_btns}</div>"
        )

    caption_id = f"cap-{esc(card_uid)}"
    note_html = f'<p class="hunk-note">{fmt_prose(note, concept_lookup)}</p>' if note else ""
    if resolved_highlights:
        hl_cards: list[str] = []
        total_hl = len(resolved_highlights)
        for h_pos, hl_entry in enumerate(resolved_highlights):
            prev_hl_btn = (
                f'<button type="button" class="ctx-btn" data-hl-switch="{esc(resolved_highlights[h_pos - 1]["uid"])}">← Prev part</button>'
                if h_pos > 0
                else "<span></span>"
            )
            next_hl_btn = (
                f'<button type="button" class="ctx-btn" data-hl-switch="{esc(resolved_highlights[h_pos + 1]["uid"])}">Next part →</button>'
                if h_pos + 1 < total_hl
                else "<span></span>"
            )
            nav_row = (
                f'<div class="hl-note-nav">{prev_hl_btn}{next_hl_btn}</div>'
                if total_hl > 1
                else ""
            )
            hl_lines_badge = (
                f'<a class="hl-note-lines hl-note-pr-link" href="{esc(hl_entry["pr_url"])}" '
                f'target="_blank" rel="noopener noreferrer" title="Open L{esc(hl_entry["lines_raw"])} in GitHub PR ↗">'
                f'L{esc(hl_entry["lines_raw"])} ↗</a>'
                if hl_entry.get("pr_url")
                else f'<code class="hl-note-lines">L{esc(hl_entry["lines_raw"])}</code>'
            )
            hl_cards.append(
                f'<div class="hl-note-card" id="hl-note-{esc(hl_entry["uid"])}" data-hl-note="{esc(hl_entry["uid"])}" hidden>'
                f'<div class="hl-note-head">'
                f'<div class="hl-note-heading">'
                f'<span class="hl-note-num" aria-hidden="true">{esc(hl_entry["num"])}</span>'
                f'<strong>{esc(hl_entry["title"])}</strong>'
                f"{hl_lines_badge}"
                f"</div>"
                f'<button type="button" class="hl-close-btn" data-hl-close="{esc(hl_entry["uid"])}" aria-label="Close block explanation">×</button>'
                f"</div>"
                f'<p class="hl-note-body">{fmt_prose(hl_entry["explanation"], concept_lookup)}</p>'
                f"{nav_row}"
                f"</div>"
            )
        diff_view_html = (
            f"{hl_strip_html}"
            f'<div class="diff-view diff-view-hl">'
            f'<div class="diff-code-col">'
            f"{before_bar_html}"
            f'<pre class="diff-pre" role="region" aria-labelledby="{caption_id}" tabindex="0">'
            f'<code class="diff-code">{before_rows_html}{"".join(main_rows_html)}{after_rows_html}</code>'
            f"</pre>"
            f"{after_bar_html}"
            f"</div>"
            f'<aside class="hl-side-rail" hidden aria-label="Highlighted code explanation">'
            f'{"".join(hl_cards)}'
            f"</aside>"
            f"</div>"
            f"{snapshot_block_html}"
        )
    else:
        diff_view_html = (
            f'<div class="diff-view">'
            f"{before_bar_html}"
            f'<pre class="diff-pre" role="region" aria-labelledby="{caption_id}" tabindex="0">'
            f'<code class="diff-code">{before_rows_html}{"".join(main_rows_html)}{after_rows_html}</code>'
            f"</pre>"
            f"{after_bar_html}"
            f"</div>"
            f"{snapshot_block_html}"
        )

    area_attr = f' data-area-id="{esc(area_id)}"' if area_id else ""
    if collapsed:
        return f"""<article class="hunk-card" id="card-{esc(card_uid)}"{area_attr}>
  <div class="hunk-head">
    <div class="hunk-title-wrap">
      <code class="hunk-filepath" id="{caption_id}">{path_display}</code>
      {area_tag_html}
      {timeline_tag}
      {hl_hint_html}
    </div>
    <div class="hunk-right">{step_file_btn_html}{stats_html}{link_html}</div>
  </div>
  {note_html}
  <details class="hunk-disclosure">
    <summary class="hunk-toggle-summary">Show diff</summary>
    {diff_view_html}
  </details>
</article>"""

    return f"""<article class="hunk-card" id="card-{esc(card_uid)}"{area_attr}>
  <div class="hunk-head">
    <div class="hunk-title-wrap">
      <code class="hunk-filepath" id="{caption_id}">{path_display}</code>
      {area_tag_html}
      {timeline_tag}
      {hl_hint_html}
    </div>
    <div class="hunk-right">{step_file_btn_html}{stats_html}{link_html}</div>
  </div>
  {note_html}
  {diff_view_html}
</article>"""


def render_rotated_structured_blocks_html(
    step: dict[str, Any], concept_lookup: dict[str, str]
) -> str:
    """Render Dynamic Visual Modulation blocks (anchored bullets and/or compact decision tables)."""
    blocks: list[str] = []

    # 1. Compact structured table (`headers` + `rows`, capped at 5 rows for low cognitive load)
    tbl = step.get("table")
    if isinstance(tbl, dict) and tbl.get("headers") and tbl.get("rows"):
        headers = [str(h) for h in tbl["headers"]]
        rows = tbl["rows"][:5]
        th_html = "".join(f'<th scope="col">{fmt_prose(h, concept_lookup)}</th>' for h in headers)
        tr_list: list[str] = []
        for r in rows:
            if isinstance(r, list):
                tds = "".join(f"<td>{fmt_prose(cell, concept_lookup)}</td>" for cell in r)
                tr_list.append(f"<tr>{tds}</tr>")
        blocks.append(
            f'<div class="table-wrap"><table class="mod-table">'
            f"<thead><tr>{th_html}</tr></thead><tbody>{''.join(tr_list)}</tbody></table></div>"
        )

    # 2. Bold-anchored 1-sentence bullet points (`points`, capped at 5 items)
    pts = step.get("points")
    if isinstance(pts, list) and pts:
        li_items: list[str] = []
        for pt in pts[:5]:
            if isinstance(pt, dict) and pt.get("text"):
                anchor = str(pt.get("anchor") or "").strip()
                lead = f"<strong>{fmt_prose(anchor, concept_lookup)}:</strong> " if anchor else ""
                li_items.append(f"<li>{lead}{fmt_prose(pt['text'], concept_lookup)}</li>")
            elif isinstance(pt, str) and pt.strip():
                li_items.append(f"<li>{fmt_prose(pt.strip(), concept_lookup)}</li>")
        if li_items:
            blocks.append(f'<ul class="anchored-list">{"".join(li_items)}</ul>')

    return "".join(blocks)


def render_concept_templates_html(
    concepts: list[dict[str, Any]],
    *,
    concept_lookup: dict[str, str],
    step_title_by_id: dict[str, str],
) -> str:
    """Emit `<template id="concept-tpl-...">` blocks cloned safely into the Concept Modal."""
    tpls: list[str] = []
    for c in concepts:
        cid = c["id"]
        term = str(c["term"]).strip()
        is_new = c.get("origin") == "introduced"
        file_ref = str(c.get("file") or "").strip()
        what_it_is = str(c["what_it_is"]).strip()
        how_list = [str(x).strip() for x in (c.get("how_it_works") or []) if str(x).strip()][:5]
        pr_impact = str(c.get("pr_impact") or "").strip()
        rel_steps = [str(s) for s in (c.get("related_steps") or []) if str(s) in step_title_by_id]

        # Avoid self-linking the concept's own term inside its own modal
        inner_lookup = {k: v for k, v in concept_lookup.items() if v != cid}

        origin_pill = (
            '<span class="concept-origin-pill is-new">Introduced in this PR</span>'
            if is_new
            else '<span class="concept-origin-pill">Existing in repo</span>'
        )
        file_badge = f'<code class="concept-file-pill">{esc(file_ref)}</code>' if file_ref else ""
        meta_row = f'<div class="concept-meta-row">{origin_pill}{file_badge}</div>'
        how_heading = "How we set it up here" if is_new else "How it works in this repo"
        how_html = ""
        if how_list:
            lis = []
            for item in how_list:
                # Support both `**Anchor:** rest` and `**Anchor**: rest`
                m_bold = re.match(r"^\*\*([^*]+?):?\*\*:?\s*(.+)$", item)
                if m_bold:
                    lis.append(
                        f"<li><strong>{esc(m_bold.group(1))}:</strong> {fmt_prose(m_bold.group(2), inner_lookup)}</li>"
                    )
                else:
                    lis.append(f"<li>{fmt_prose(item, inner_lookup)}</li>")
            how_html = (
                f'<div class="concept-sec"><h3>{how_heading}</h3>'
                f'<ul class="anchored-list">{"".join(lis)}</ul></div>'
            )

        impact_html = (
            f'<div class="callout callout-decision"><strong>In this PR:</strong> {fmt_prose(pr_impact, inner_lookup)}</div>'
            if pr_impact
            else ""
        )

        jumps_html = ""
        if rel_steps:
            btns = "".join(
                f'<button type="button" class="nav-btn concept-jump-btn" data-goto-step="{esc(sid)}">'
                f"{esc(step_title_by_id[sid])} →</button>"
                for sid in rel_steps
            )
            jumps_html = f'<div class="concept-jumps"><span class="ba-label">Where in this review:</span><div class="concept-jump-row">{btns}</div></div>'

        tpls.append(
            f"""<template id="concept-tpl-{esc(cid)}" data-term="{esc(term)}">
  <div class="concept-modal-content">
    {meta_row}
    <p class="lead-text">{fmt_prose(what_it_is, inner_lookup)}</p>
    {how_html}
    {impact_html}
    {jumps_html}
  </div>
</template>"""
        )
    return "\n".join(tpls)


def build_html_artifact(
    manifest: dict[str, Any],
    plan: dict[str, Any],
    *,
    search_dirs: list[Path] | None = None,
) -> str:
    asset_dirs = search_dirs or [Path.cwd()]
    meta = manifest["meta"]
    stats = manifest["stats"]
    hunk_map: dict[str, dict[str, Any]] = {h["id"]: h for h in manifest["hunks"]}
    file_map: dict[str, dict[str, Any]] = {f["path"]: f for f in manifest["files"]}
    row_step_order: dict[tuple[str, int], int] = plan["_row_step_order"]

    # Build concept lookup (`term` and `aliases` -> `concept_id`)
    concepts: list[dict[str, Any]] = plan.get("concepts") or []
    concept_lookup: dict[str, str] = {}
    for c in concepts:
        cid = c["id"]
        concept_lookup[cid.lower()] = cid
        t_low = str(c["term"]).strip().lower()
        concept_lookup[t_low] = cid
        concept_lookup[t_low.removesuffix("()")] = cid
        for alias in c.get("aliases") or []:
            a_low = str(alias).strip().lower()
            if a_low:
                concept_lookup[a_low] = cid
                concept_lookup[a_low.removesuffix("()")] = cid

    # Pre-build unified timeline token stream for every file
    file_tokens_map: dict[str, list[dict[str, Any]]] = {}
    for f in manifest["files"]:
        f_hunks = [hunk_map[hid] for hid in f["hunk_ids"]]
        file_tokens_map[f["path"]] = build_file_timeline_tokens(f, f_hunks, row_step_order)

    title = str(plan.get("title") or meta.get("title") or "PR Review Guide")
    pr_url = safe_http_url(meta.get("pr_url"))
    overview = plan["overview"]
    steps: list[dict[str, Any]] = plan["steps"]
    mech_groups: list[dict[str, Any]] = (plan.get("mechanical") or {}).get("groups") or []
    mech_slice_count = sum(len(g["_slices"]) for g in mech_groups)
    total_main_steps = len(steps)

    # Map chapters (`Chapter 1`, `Chapter 2`, ...)
    chapters: list[dict[str, Any]] = plan.get("chapters") or []
    step_to_chapter: dict[str, tuple[int, dict[str, Any]]] = {}
    for c_idx, ch in enumerate(chapters, start=1):
        for sid in ch["step_ids"]:
            step_to_chapter[sid] = (c_idx, ch)

    step_index_by_id = {st["id"]: idx for idx, st in enumerate(steps, start=1)}
    step_title_by_id = {
        st["id"]: f"Step {idx}: {strip_concept_markup(st['title'])}"
        for idx, st in enumerate(steps, start=1)
    }

    # --- Pre-compute 2-Level Scope Tree (`Role Surface -> App Area`, zero filenames) ---
    plan_file_scopes: dict[str, Any] = plan.get("file_scopes") or {}
    surface_registry: dict[str, dict[str, Any]] = {}

    def register_scope_usage(
        surf_id: str, surf_lbl: str, area_id: str, area_lbl: str, main_step_idx: int | None
    ) -> None:
        s_node = surface_registry.setdefault(
            surf_id,
            {"id": surf_id, "label": surf_lbl, "step_indices": set(), "areas_map": {}},
        )
        a_node = s_node["areas_map"].setdefault(
            area_id,
            {"id": area_id, "label": area_lbl, "step_indices": set()},
        )
        if main_step_idx is not None:
            s_node["step_indices"].add(main_step_idx)
            a_node["step_indices"].add(main_step_idx)

    # Assign scopes to every slice in Steps 1..N and Mechanical groups
    for idx, st in enumerate(steps, start=1):
        st_scope = st.get("scope") if isinstance(st.get("scope"), dict) else None
        st_area_ids: list[str] = []
        for sl in st["_slices"]:
            h_obj = hunk_map[sl["hunk_id"]]
            f_obj = file_map[h_obj["file_path"]]
            surf_id, surf_lbl, area_id, area_lbl = resolve_slice_scope(
                f_obj,
                plan_file_scopes=plan_file_scopes,
                step_scope=st_scope,
                is_mechanical=False,
                step_layer=st["layer"],
            )
            sl["_area_id"] = area_id
            sl["_area_label"] = area_lbl
            if area_id not in st_area_ids:
                st_area_ids.append(area_id)
            register_scope_usage(surf_id, surf_lbl, area_id, area_lbl, idx)
        st["_area_ids"] = st_area_ids

    mech_area_ids: list[str] = []
    for grp in mech_groups:
        grp_scope = grp.get("scope") if isinstance(grp.get("scope"), dict) else None
        for sl in grp["_slices"]:
            h_obj = hunk_map[sl["hunk_id"]]
            f_obj = file_map[h_obj["file_path"]]
            surf_id, surf_lbl, area_id, area_lbl = resolve_slice_scope(
                f_obj,
                plan_file_scopes=plan_file_scopes,
                step_scope=grp_scope,
                is_mechanical=True,
                step_layer="",
            )
            sl["_area_id"] = area_id
            sl["_area_label"] = area_lbl
            if area_id not in mech_area_ids:
                mech_area_ids.append(area_id)
            register_scope_usage(surf_id, surf_lbl, area_id, area_lbl, None)

    def _surface_sort_key(item: dict[str, Any]) -> tuple[int, str]:
        lbl = item["label"]
        if lbl in CANONICAL_SURFACE_ORDER:
            return (CANONICAL_SURFACE_ORDER.index(lbl), lbl)
        # Custom surfaces appear right after Database & Contracts, ahead of Tests & Infra
        return (2, lbl)

    surfaces_tree: list[dict[str, Any]] = []
    for s_node in sorted(surface_registry.values(), key=_surface_sort_key):
        areas_sorted = sorted(
            s_node["areas_map"].values(),
            key=lambda a: (min(a["step_indices"]) if a["step_indices"] else 9999, a["label"]),
        )
        surfaces_tree.append(
            {
                "id": s_node["id"],
                "label": s_node["label"],
                "step_indices": s_node["step_indices"],
                "areas": areas_sorted,
            }
        )

    scope_popover_html = render_scope_popover_html(surfaces_tree)

    # --- Build Arc-Style Left Vertical Sidebar ---
    arc_nav_items: list[str] = []
    if chapters:
        for c_idx, ch in enumerate(chapters, start=1):
            cid = ch["id"]
            first_sid = ch["step_ids"][0]
            first_panel_idx = step_index_by_id[first_sid]
            sub_btns: list[str] = []
            for s_pos, sid in enumerate(ch["step_ids"], start=1):
                p_idx = step_index_by_id[sid]
                st_obj = steps[p_idx - 1]
                lyr_short = VALID_LAYERS[st_obj["layer"]]["short"]
                sub_btns.append(
                    f'<button type="button" class="arc-subtab" data-panel-index="{p_idx}" data-chapter-id="{esc(cid)}">'
                    f'<span class="arc-sub-dot" aria-hidden="true"></span>'
                    f'<span class="arc-sub-txt">{s_pos}. {esc(strip_concept_markup(st_obj["title"]))}</span>'
                    f'<span class="arc-sub-tag">{esc(lyr_short)}</span>'
                    f"</button>"
                )
            arc_nav_items.append(
                f"""<div class="arc-chapter" data-chapter-box="{esc(cid)}">
          <button type="button" class="arc-tab arc-chapter-btn" data-panel-index="{first_panel_idx}" data-chapter-id="{esc(cid)}">
            <span class="arc-ch-num">Chapter {c_idx}</span>
            <span class="arc-tab-label">{esc(strip_concept_markup(ch['title']))}</span>
            <span class="arc-match-pill" hidden></span>
          </button>
          <div class="arc-sublist">
            {"".join(sub_btns)}
          </div>
        </div>"""
            )
    else:
        for idx, st in enumerate(steps, start=1):
            lyr_short = VALID_LAYERS[st["layer"]]["short"]
            arc_nav_items.append(
                f'<button type="button" class="arc-tab" data-panel-index="{idx}">'
                f'<span class="arc-ch-num">{idx}</span>'
                f'<span class="arc-tab-label">{esc(strip_concept_markup(st["title"]))}</span>'
                f'<span class="arc-sub-tag">{esc(lyr_short)}</span>'
                f"</button>"
            )

    mech_tab_html = ""
    if mech_groups:
        mech_idx = total_main_steps + 1
        mech_tab_html = (
            f'<button type="button" class="arc-tab arc-tab-muted" data-panel-index="{mech_idx}">'
            f'<span class="arc-ch-num">⚙</span>'
            f'<span class="arc-tab-label">Boilerplate ({mech_slice_count})</span>'
            f"</button>"
        )

    # --- Panel 0: Big Picture Overview ---
    panels_html: list[str] = []
    global_ctx = str(overview.get("global_context") or "").strip()
    global_ctx_p = f'<p class="lead-subtle">{fmt_prose(global_ctx, concept_lookup)}</p>' if global_ctx else ""
    overview_visuals_html = render_visuals_block_html(
        overview.get("visuals"),
        search_dirs=asset_dirs,
        concept_lookup=concept_lookup,
        eager=True,
    )
    this_pr_part = str(overview.get("this_pr_part") or "").strip()
    this_pr_part_html = (
        f'<div class="pr-part-card">'
        f'<span class="pr-part-badge">What we built in this PR</span>'
        f"<p>{fmt_prose(this_pr_part, concept_lookup)}</p>"
        f"</div>"
        if this_pr_part
        else ""
    )

    concepts_bar_html = ""
    if concepts:
        c_btns = "".join(
            f'<button type="button" class="concept-chip" data-concept="{esc(c["id"])}">'
            f'{esc(c["term"])}'
            f'{" <span class=\"concept-new-tag\">new</span>" if c.get("origin") == "introduced" else ""}'
            f' <span class="concept-dot" aria-hidden="true">?</span></button>'
            for c in concepts
        )
        concepts_bar_html = (
            f'<div class="concepts-Primer-box">'
            f'<span class="ba-label">Terms &amp; pieces in this review — click any chip (or dotted link in text) for a quick rundown:</span>'
            f'<div class="concept-chip-row">{c_btns}</div></div>'
        )

    chapters_preview_html = ""
    if chapters:
        ch_rows = []
        for c_idx, ch in enumerate(chapters, start=1):
            first_p = step_index_by_id[ch["step_ids"][0]]
            ch_sum = str(ch.get("summary") or f"{len(ch['step_ids'])} steps").strip()
            ch_rows.append(
                f'<tr data-overview-chapter="{esc(ch["id"])}">'
                f'<td><button type="button" class="inline-jump" data-goto-chapter="{esc(ch["id"])}" data-goto-index="{first_p}">Chapter {c_idx} · {esc(strip_concept_markup(ch["title"]))} →</button></td>'
                f"<td>{fmt_prose(ch_sum, concept_lookup)}</td>"
                f"</tr>"
            )
        chapters_preview_html = (
            f'<div class="table-wrap"><table class="mod-table">'
            f'<thead><tr><th scope="col">Chapter</th><th scope="col">What We Do Here</th></tr></thead>'
            f'<tbody>{"".join(ch_rows)}</tbody></table></div>'
        )

    first_btn_label = (
        f"Start Chapter 1: {esc(strip_concept_markup(chapters[0]['title']))} →"
        if chapters
        else f"Start Step 1: {esc(VALID_LAYERS[steps[0]['layer']]['short'])} →"
    )

    panels_html.append(
        f"""<section class="step-panel" id="step-overview" data-step-id="step-overview" data-panel-index="0" data-chapter-id="" data-area-ids="*" aria-labelledby="heading-overview">
      <div class="step-card">
        <p class="eyebrow">Overview · {stats['total_files']} files (+{stats['total_additions']} / −{stats['total_deletions']})</p>
        <h2 id="heading-overview" class="step-title">The Big Picture</h2>
        <p class="lead-text">{fmt_prose(overview['whole_task'], concept_lookup)}</p>
        {global_ctx_p}
        {overview_visuals_html}
        {this_pr_part_html}
        <div class="before-after">
          <div class="ba-item">
            <span class="ba-label">Before</span>
            <p>{fmt_prose(overview['before_state'], concept_lookup)}</p>
          </div>
          <div class="ba-item">
            <span class="ba-label">After</span>
            <p>{fmt_prose(overview['after_state'], concept_lookup)}</p>
          </div>
        </div>
        {concepts_bar_html}
        {chapters_preview_html}
        <div class="step-footer">
          <span class="coverage-note">Every changed line accounted across {total_main_steps} steps</span>
          <button type="button" id="overview-start-btn" class="nav-btn nav-btn-primary" data-nav-dir="next" data-default-label="{first_btn_label}" data-goto-index="1">
            {first_btn_label}
          </button>
        </div>
      </div>
    </section>"""
    )

    # --- Panels 1..N: Guided Timeline Steps ---
    slice_uid_counter = 0
    for idx, st in enumerate(steps, start=1):
        sid = st["id"]
        lyr = st["layer"]
        lyr_info = VALID_LAYERS[lyr]
        ch_entry = step_to_chapter.get(sid)
        ch_id = ch_entry[1]["id"] if ch_entry else ""
        if ch_entry:
            c_idx, ch_obj = ch_entry
            pos_in_ch = ch_obj["step_ids"].index(sid) + 1
            eyebrow_txt = (
                f"Chapter {c_idx}: {esc(ch_obj['title'])} · Step {pos_in_ch} of {len(ch_obj['step_ids'])} · "
                f"{esc(lyr_info['label'])}"
            )
        else:
            slice_prefix = f"{esc(st['slice'])} · " if st.get("slice") else ""
            eyebrow_txt = f"Step {idx} of {total_main_steps} · {slice_prefix}{esc(lyr_info['label'])}"

        lead = str(st.get("explanation") or st.get("why_now") or "").strip()
        lead_html = f'<p class="lead-text">{fmt_prose(lead, concept_lookup)}</p>' if lead else ""
        rotated_blocks_html = render_rotated_structured_blocks_html(st, concept_lookup)
        step_visuals_html = render_visuals_block_html(
            st.get("visuals"),
            search_dirs=asset_dirs,
            concept_lookup=concept_lookup,
            eager=False,
        )

        callouts_html: list[str] = []
        for co in st.get("callouts") or []:
            if not isinstance(co, dict) or not co.get("text"):
                continue
            ckind = str(co.get("kind") or "decision").lower()
            cinfo = VALID_CALLOUT_KINDS.get(ckind, VALID_CALLOUT_KINDS["decision"])
            callouts_html.append(
                f'<div class="callout callout-{esc(ckind)}">'
                f'<strong>{esc(cinfo["symbol"])} {esc(cinfo["label"])}:</strong> '
                f'{fmt_prose(co["text"], concept_lookup)}</div>'
            )

        gaps = [str(g).strip() for g in (st.get("untested_gaps") or []) if str(g).strip()][:5]
        if gaps:
            gap_items = "".join(f"<li>{fmt_prose(g, concept_lookup)}</li>" for g in gaps)
            callouts_html.append(
                f'<div class="callout callout-risk">'
                f"<strong>! Missing test coverage:</strong>"
                f'<ul class="compact-list">{gap_items}</ul></div>'
            )

        collapse_hunks = lyr == "verification"
        hunk_notes = st.get("hunk_notes") or {}
        only_multiple = len(st["_slices"]) > 1

        cards_rendered: list[str] = []
        for sl in st["_slices"]:
            slice_uid_counter += 1
            h_obj = hunk_map[sl["hunk_id"]]
            f_obj = file_map[h_obj["file_path"]]
            note_val = None
            if isinstance(hunk_notes, dict) and only_multiple:
                note_val = hunk_notes.get(sl["raw"]) or hunk_notes.get(sl["hunk_id"])
            cards_rendered.append(
                render_timeline_slice_card_html(
                    card_uid=f"s{slice_uid_counter}",
                    file_obj=f_obj,
                    hunk=h_obj,
                    slice_item=sl,
                    file_tokens=file_tokens_map[f_obj["path"]],
                    step_order=st["_timeline_order"],
                    total_step_order=total_main_steps,
                    note=note_val,
                    concept_lookup=concept_lookup,
                    collapsed=collapse_hunks,
                    area_id=sl.get("_area_id", ""),
                    area_label=sl.get("_area_label", ""),
                )
            )

        prev_btn = (
            f'<button type="button" class="nav-btn" data-nav-dir="prev" data-goto-index="{idx - 1}">'
            f'← {"Overview" if idx == 1 else "Previous"}</button>'
        )
        if idx < total_main_steps:
            nxt_st = steps[idx]
            nxt_ch = step_to_chapter.get(nxt_st["id"])
            if nxt_ch and (not ch_entry or nxt_ch[1]["id"] != ch_entry[1]["id"]):
                next_label = f"Next Chapter: {esc(nxt_ch[1]['title'])} →"
            else:
                next_label = f"Next: {esc(VALID_LAYERS[nxt_st['layer']]['short'])} →"
            next_btn = (
                f'<button type="button" class="nav-btn nav-btn-primary" data-nav-dir="next" '
                f'data-default-label="{next_label}" data-goto-index="{idx + 1}">'
                f"{next_label}</button>"
            )
        elif mech_groups:
            def_mech_lbl = f"Boilerplate ({mech_slice_count}) →"
            next_btn = (
                f'<button type="button" class="nav-btn" data-nav-dir="next" '
                f'data-default-label="{def_mech_lbl}" data-goto-index="{total_main_steps + 1}">'
                f"{def_mech_lbl}</button>"
            )
        else:
            next_btn = '<span class="done-badge" data-done-slot="true">✓ Review complete</span>'

        step_areas_csv = ",".join(st.get("_area_ids") or [])
        panels_html.append(
            f"""<section class="step-panel" id="{esc(sid)}" data-step-id="{esc(sid)}" data-panel-index="{idx}" data-chapter-id="{esc(ch_id)}" data-area-ids="{esc(step_areas_csv)}" aria-labelledby="heading-{esc(sid)}" hidden="until-found">
      <div class="step-card">
        <div class="outside-filter-banner" hidden>Showing step outside your active area filter.</div>
        <p class="eyebrow">{eyebrow_txt}</p>
        <h2 id="heading-{esc(sid)}" class="step-title">{fmt_prose(st['title'], concept_lookup)}</h2>
        {lead_html}
        {rotated_blocks_html}
        {step_visuals_html}
        {"".join(callouts_html)}
        <div class="hunk-stack">
          {"".join(cards_rendered)}
        </div>
        <div class="filtered-cards-bar" hidden>
          <span class="filtered-cards-msg"></span>
          <button type="button" class="ctx-btn filtered-cards-toggle">Show anyway</button>
        </div>
        <div class="step-footer">
          {prev_btn}
          <span class="step-next-wrap">{next_btn}</span>
        </div>
      </div>
    </section>"""
        )

    # --- Optional Last Panel: Mechanical / Boilerplate ---
    if mech_groups:
        mech_idx = total_main_steps + 1
        mech_timeline_order = total_main_steps + 1
        mech_blocks: list[str] = []
        for grp in mech_groups:
            reason = str(grp.get("reason") or "").strip()
            reason_p = f'<p class="lead-subtle">{fmt_prose(reason, concept_lookup)}</p>' if reason else ""
            cards: list[str] = []
            for sl in grp["_slices"]:
                slice_uid_counter += 1
                h_obj = hunk_map[sl["hunk_id"]]
                f_obj = file_map[h_obj["file_path"]]
                cards.append(
                    render_timeline_slice_card_html(
                        card_uid=f"s{slice_uid_counter}",
                        file_obj=f_obj,
                        hunk=h_obj,
                        slice_item=sl,
                        file_tokens=file_tokens_map[f_obj["path"]],
                        step_order=mech_timeline_order,
                        total_step_order=total_main_steps,
                        note=None,
                        concept_lookup=concept_lookup,
                        collapsed=True,
                        area_id=sl.get("_area_id", ""),
                        area_label=sl.get("_area_label", ""),
                    )
                )
            mech_blocks.append(
                f'<div class="mech-section"><h3 class="mech-heading">{esc(grp["title"])}</h3>'
                f'{reason_p}<div class="hunk-stack">{"".join(cards)}</div></div>'
            )

        mech_areas_csv = ",".join(mech_area_ids)
        panels_html.append(
            f"""<section class="step-panel" id="step-mechanical" data-step-id="step-mechanical" data-panel-index="{mech_idx}" data-chapter-id="" data-area-ids="{esc(mech_areas_csv)}" aria-labelledby="heading-mechanical" hidden="until-found">
      <div class="step-card">
        <div class="outside-filter-banner" hidden>Showing boilerplate outside your active area filter.</div>
        <p class="eyebrow">Appendix · Mechanical &amp; Boilerplate</p>
        <h2 id="heading-mechanical" class="step-title">Low-Signal Changes ({mech_slice_count} items)</h2>
        <p class="lead-subtle">Tucked at the end so boilerplate never blocks core review, while keeping 100% of the PR diff inspectable.</p>
        {"".join(mech_blocks)}
        <div class="filtered-cards-bar" hidden>
          <span class="filtered-cards-msg"></span>
          <button type="button" class="ctx-btn filtered-cards-toggle">Show anyway</button>
        </div>
        <div class="step-footer">
          <button type="button" class="nav-btn" data-nav-dir="prev" data-goto-index="{total_main_steps}">← Previous</button>
          <span class="done-badge">✓ 100% of PR diff accounted</span>
        </div>
      </div>
    </section>"""
        )

    concept_templates_html = render_concept_templates_html(
        concepts,
        concept_lookup=concept_lookup,
        step_title_by_id=step_title_by_id,
    )

    pr_topbar_cta = (
        f'<a class="pr-cta-btn" href="{esc(pr_url)}" target="_blank" rel="noopener noreferrer">'
        f'Open Pull Request <span aria-hidden="true">↗</span></a>'
        if pr_url
        else ""
    )

    section_heading = "CHAPTERS" if chapters else "STEPS"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light dark">
  <script>
    try {{
      const saved = localStorage.getItem("unmaze-color-scheme");
      if (saved === "light" || saved === "dark") {{
        document.querySelector('meta[name="color-scheme"]').content = saved;
      }}
    }} catch (_) {{}}
  </script>
  <title>{esc(title)} — unmaze</title>
  <style>
    :root {{
      --rail-light: #eef1f6;
      --rail-dark: #0b0f16;
      --bg-light: #f8f9fb;
      --bg-dark: #11161f;
      --surface-light: #ffffff;
      --surface-dark: #171d28;
      --subtle-light: #f2f4f8;
      --subtle-dark: #1e2634;
      --border-light: #dfe4ec;
      --border-dark: #2c3545;
      --text-light: #1e232a;
      --text-dark: #e6edf5;
      --muted-light: #5b6573;
      --muted-dark: #8c98a9;
      --accent-light: #0969da;
      --accent-dark: #58a6ff;
      --accent-soft-light: #e0effe;
      --accent-soft-dark: #192b44;

      --add-bg-light: #e6ffec;
      --add-bg-dark: #122920;
      --add-fg-light: #1a7f37;
      --add-fg-dark: #3fb950;
      --del-bg-light: #ffebe9;
      --del-bg-dark: #30181c;
      --del-fg-light: #cf222e;
      --del-fg-dark: #f85149;

      --warn-bg-light: #fff8c5;
      --warn-bg-dark: #2b2413;
      --warn-fg-light: #9a6700;
      --warn-fg-dark: #e3b341;

      --kw-light: #cf222e;
      --kw-dark: #ff7b72;
      --str-light: #0a3069;
      --str-dark: #a5d6ff;
      --cmt-light: #6e7781;
      --cmt-dark: #8b949e;
      --typ-light: #953800;
      --typ-dark: #ffa657;
      --fn-light: #8250df;
      --fn-dark: #d2a8ff;
      --num-light: #0550ae;
      --num-dark: #79c0ff;

      --rail: var(--rail-light);
      --bg: var(--bg-light);
      --surface: var(--surface-light);
      --subtle: var(--subtle-light);
      --border: var(--border-light);
      --text: var(--text-light);
      --muted: var(--muted-light);
      --accent: var(--accent-light);
      --accent-soft: var(--accent-soft-light);
      --add-bg: var(--add-bg-light);
      --add-fg: var(--add-fg-light);
      --del-bg: var(--del-bg-light);
      --del-fg: var(--del-fg-light);
      --warn-bg: var(--warn-bg-light);
      --warn-fg: var(--warn-fg-light);
      --tok-kw: var(--kw-light);
      --tok-str: var(--str-light);
      --tok-cmt: var(--cmt-light);
      --tok-typ: var(--typ-light);
      --tok-fn: var(--fn-light);
      --tok-num: var(--num-light);
      --scroll-thumb: #9ca3af;
      --scroll-track: transparent;

      @media (prefers-color-scheme: dark) {{
        --rail: var(--rail-dark);
        --bg: var(--bg-dark);
        --surface: var(--surface-dark);
        --subtle: var(--subtle-dark);
        --border: var(--border-dark);
        --text: var(--text-dark);
        --muted: var(--muted-dark);
        --accent: var(--accent-dark);
        --accent-soft: var(--accent-soft-dark);
        --add-bg: var(--add-bg-dark);
        --add-fg: var(--add-fg-dark);
        --del-bg: var(--del-bg-dark);
        --del-fg: var(--del-fg-dark);
        --warn-bg: var(--warn-bg-dark);
        --warn-fg: var(--warn-fg-dark);
        --tok-kw: var(--kw-dark);
        --tok-str: var(--str-dark);
        --tok-cmt: var(--cmt-dark);
        --tok-typ: var(--typ-dark);
        --tok-fn: var(--fn-dark);
        --tok-num: var(--num-dark);
        --scroll-thumb: #6e7681;
      }}

      color-scheme: light dark;

      &:has(> head > meta[name="color-scheme"][content="dark"]) {{
        color-scheme: dark;
        --rail: var(--rail-dark);
        --bg: var(--bg-dark);
        --surface: var(--surface-dark);
        --subtle: var(--subtle-dark);
        --border: var(--border-dark);
        --text: var(--text-dark);
        --muted: var(--muted-dark);
        --accent: var(--accent-dark);
        --accent-soft: var(--accent-soft-dark);
        --add-bg: var(--add-bg-dark);
        --add-fg: var(--add-fg-dark);
        --del-bg: var(--del-bg-dark);
        --del-fg: var(--del-fg-dark);
        --warn-bg: var(--warn-bg-dark);
        --warn-fg: var(--warn-fg-dark);
        --tok-kw: var(--kw-dark);
        --tok-str: var(--str-dark);
        --tok-cmt: var(--cmt-dark);
        --tok-typ: var(--typ-dark);
        --tok-fn: var(--fn-dark);
        --tok-num: var(--num-dark);
        --scroll-thumb: #6e7681;
      }}

      &:has(> head > meta[name="color-scheme"][content="light"]) {{
        color-scheme: light;
        --rail: var(--rail-light);
        --bg: var(--bg-light);
        --surface: var(--surface-light);
        --subtle: var(--subtle-light);
        --border: var(--border-light);
        --text: var(--text-light);
        --muted: var(--muted-light);
        --accent: var(--accent-light);
        --accent-soft: var(--accent-soft-light);
        --add-bg: var(--add-bg-light);
        --add-fg: var(--add-fg-light);
        --del-bg: var(--del-bg-light);
        --del-fg: var(--del-fg-light);
        --warn-bg: var(--warn-bg-light);
        --warn-fg: var(--warn-fg-light);
        --tok-kw: var(--kw-light);
        --tok-str: var(--str-light);
        --tok-cmt: var(--cmt-light);
        --tok-typ: var(--typ-light);
        --tok-fn: var(--fn-light);
        --tok-num: var(--num-light);
        --scroll-thumb: #9ca3af;
      }}

      @supports (color: light-dark(white, black)) {{
        --rail: light-dark(var(--rail-light), var(--rail-dark));
        --bg: light-dark(var(--bg-light), var(--bg-dark));
        --surface: light-dark(var(--surface-light), var(--surface-dark));
        --subtle: light-dark(var(--subtle-light), var(--subtle-dark));
        --border: light-dark(var(--border-light), var(--border-dark));
        --text: light-dark(var(--text-light), var(--text-dark));
        --muted: light-dark(var(--muted-light), var(--muted-dark));
        --accent: light-dark(var(--accent-light), var(--accent-dark));
        --accent-soft: light-dark(var(--accent-soft-light), var(--accent-soft-dark));
        --add-bg: light-dark(var(--add-bg-light), var(--add-bg-dark));
        --add-fg: light-dark(var(--add-fg-light), var(--add-fg-dark));
        --del-bg: light-dark(var(--del-bg-light), var(--del-bg-dark));
        --del-fg: light-dark(var(--del-fg-light), var(--del-fg-dark));
        --warn-bg: light-dark(var(--warn-bg-light), var(--warn-bg-dark));
        --warn-fg: light-dark(var(--warn-fg-light), var(--warn-fg-dark));
        --tok-kw: light-dark(var(--kw-light), var(--kw-dark));
        --tok-str: light-dark(var(--str-light), var(--str-dark));
        --tok-cmt: light-dark(var(--cmt-light), var(--cmt-dark));
        --tok-typ: light-dark(var(--typ-light), var(--typ-dark));
        --tok-fn: light-dark(var(--fn-light), var(--fn-dark));
        --tok-num: light-dark(var(--num-light), var(--num-dark));
        --scroll-thumb: light-dark(#9ca3af, #6e7681);
      }}

      scrollbar-color: var(--scroll-thumb) var(--scroll-track);
    }}

    @supports not (scrollbar-color: auto) {{
      ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
      ::-webkit-scrollbar-thumb {{ background: var(--scroll-thumb); border-radius: 4px; }}
    }}

    *, *::before, *::after {{ box-sizing: border-box; }}

    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--rail);
      color: var(--text);
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
    }}

    code, pre {{
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    }}

    .inline-code {{
      font-size: 0.85em;
      padding: 0.1rem 0.34rem;
      border-radius: 5px;
      background: var(--subtle);
      border: 1px solid var(--border);
    }}

    /* Clickable Project & Task Concept Links (subtle dotted underline in prose) */
    .concept-link {{
      appearance: none;
      border: 0;
      background: transparent;
      color: var(--accent);
      font: inherit;
      font-weight: 540;
      padding: 0 0.08rem;
      margin: 0;
      border-radius: 3px;
      text-decoration-line: underline;
      text-decoration-style: dotted;
      text-decoration-thickness: 1.5px;
      text-underline-offset: 3px;
      cursor: pointer;
    }}

    .concept-link:hover {{
      background: var(--accent-soft);
      text-decoration-style: solid;
    }}

    .concept-link-code {{
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.86em;
      font-weight: 600;
      padding: 0.06rem 0.32rem;
      border-radius: 5px;
      background: var(--accent-soft);
    }}

    .concept-new-tag {{
      font-size: 0.65rem;
      font-weight: 700;
      text-transform: uppercase;
      padding: 0.04rem 0.3rem;
      border-radius: 4px;
      color: var(--add-fg);
      background: var(--add-bg);
    }}

    .concept-dot {{
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      font-size: 0.68em;
      font-weight: 700;
      inline-size: 1.15em;
      block-size: 1.15em;
      border-radius: 999px;
      background: var(--accent);
      color: var(--surface);
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}

    html {{
      scroll-padding-top: 4.2rem;
    }}

    /* Global Sticky App Chrome Bar (outside content container, governs sidebar + canvas) */
    .app-topbar {{
      position: sticky;
      top: 0;
      z-index: 30;
      min-block-size: 3.35rem;
      padding: 0.45rem 0.85rem;
      background: var(--rail);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.6rem;
    }}

    .topbar-left, .topbar-right {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
    }}

    /* Arc-Browser Shell Layout (sits directly beneath sticky .app-topbar) */
    .arc-layout {{
      display: grid;
      grid-template-columns: 276px minmax(0, 1fr);
      min-height: calc(100vh - 3.35rem);
    }}

    .arc-layout.is-rail-collapsed {{
      grid-template-columns: 0px minmax(0, 1fr);
    }}

    .arc-layout.is-rail-collapsed .arc-sidebar {{
      overflow: hidden;
      padding: 0;
      border: 0;
    }}

    @media (max-width: 860px) {{
      .arc-layout {{ grid-template-columns: 1fr; }}
      .arc-sidebar {{
        position: static !important;
        block-size: auto !important;
        border-bottom: 1px solid var(--border);
      }}
    }}

    .arc-sidebar {{
      position: sticky;
      top: 3.35rem;
      block-size: calc(100vh - 3.35rem);
      padding: 0.25rem 0.7rem 0.85rem;
      display: flex;
      flex-direction: column;
      gap: 0.55rem;
      overflow-y: auto;
      background: var(--rail);
    }}

    .arc-brand {{
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.85rem;
      font-weight: 760;
      letter-spacing: 0.02em;
      color: var(--text);
      padding: 0 0.2rem;
    }}

    .icon-btn {{
      appearance: none;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--muted);
      border-radius: 7px;
      min-inline-size: 24px;
      min-block-size: 32px;
      padding: 0.28rem 0.6rem;
      font-size: 0.77rem;
      font-weight: 580;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.28rem;
    }}

    .icon-btn:hover {{
      color: var(--text);
      border-color: var(--text);
    }}

    .mode-toggle-btn[aria-pressed="true"] {{
      background: var(--accent-soft);
      border-color: var(--accent);
      color: var(--accent);
      font-weight: 660;
    }}

    /* Pronounced PR CTA Button in Global Top Bar */
    .pr-cta-btn {{
      background: var(--accent);
      color: #ffffff;
      border: 1.5px solid var(--accent);
      border-radius: 8px;
      padding: 0.42rem 0.88rem;
      font-size: 0.82rem;
      font-weight: 700;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 0.32rem;
      box-shadow: 0 2px 6px rgba(9, 105, 218, 0.28);
      white-space: nowrap;
    }}

    :root:has(> head > meta[name="color-scheme"][content="dark"]) .pr-cta-btn {{
      color: #0b0f16;
    }}

    @media (prefers-color-scheme: dark) {{
      :root:not(:has(> head > meta[name="color-scheme"][content="light"])) .pr-cta-btn {{
        color: #0b0f16;
      }}
    }}

    .pr-cta-btn:hover {{
      opacity: 0.92;
      transform: translateY(-0.5px);
    }}

    .arc-pr-title {{
      margin: 0;
      padding: 0 0.3rem 0.35rem;
      font-size: 0.82rem;
      font-weight: 600;
      color: var(--muted);
      line-height: 1.35;
      border-bottom: 1px solid var(--border);
    }}

    .arc-section-lbl {{
      font-size: 0.67rem;
      font-weight: 700;
      letter-spacing: 0.07em;
      color: var(--muted);
      padding: 0.3rem 0.4rem 0.1rem;
    }}

    .arc-tabs-scroll {{
      display: flex;
      flex-direction: column;
      gap: 0.22rem;
      flex: 1;
    }}

    .arc-tab {{
      width: 100%;
      appearance: none;
      border: 1px solid transparent;
      background: transparent;
      color: var(--text);
      border-radius: 9px;
      padding: 0.46rem 0.55rem;
      font: inherit;
      font-size: 0.83rem;
      font-weight: 560;
      text-align: left;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.48rem;
    }}

    .arc-tab:hover:not([aria-disabled="true"]) {{
      background: var(--subtle);
    }}

    .arc-tab.is-active {{
      background: var(--surface);
      border-color: var(--border);
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
      font-weight: 650;
    }}

    .arc-tab-muted {{
      color: var(--muted);
      margin-top: auto;
    }}

    .arc-ch-num {{
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.68rem;
      font-weight: 700;
      padding: 0.12rem 0.4rem;
      border-radius: 5px;
      background: var(--subtle);
      color: var(--muted);
      flex-shrink: 0;
      white-space: nowrap;
    }}

    .arc-tab.is-active .arc-ch-num {{
      background: var(--accent-soft);
      color: var(--accent);
    }}

    .arc-match-pill {{
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.66rem;
      font-weight: 700;
      padding: 0.06rem 0.34rem;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      flex-shrink: 0;
    }}

    .arc-match-pill[hidden] {{ display: none; }}

    .arc-tab-label {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 1;
    }}

    /* Indented sub-tabs inside active Arc Chapter */
    .arc-sublist {{
      display: none;
      flex-direction: column;
      gap: 0.15rem;
      margin: 0.18rem 0 0.35rem 0.85rem;
      padding-left: 0.55rem;
      border-left: 1.5px solid var(--border);
    }}

    .arc-chapter.is-open:not(.is-filter-disabled) .arc-sublist {{
      display: flex;
    }}

    .arc-subtab {{
      width: 100%;
      appearance: none;
      border: 0;
      background: transparent;
      color: var(--muted);
      border-radius: 7px;
      padding: 0.34rem 0.5rem;
      font: inherit;
      font-size: 0.78rem;
      text-align: left;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }}

    .arc-subtab:hover:not([aria-disabled="true"]) {{
      color: var(--text);
      background: var(--subtle);
    }}

    .arc-subtab.is-active {{
      background: var(--surface);
      color: var(--text);
      font-weight: 620;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
    }}

    /* Dimmed / Disabled Chapters & Steps when Filtered Out */
    .arc-chapter.is-filter-disabled .arc-chapter-btn,
    .arc-tab.is-filter-disabled,
    .arc-subtab.is-filter-disabled {{
      opacity: 0.38;
      cursor: not-allowed;
    }}

    .arc-chapter.is-filter-disabled .arc-match-pill {{
      background: var(--subtle);
      color: var(--muted);
    }}

    tr.is-filter-disabled {{
      opacity: 0.42;
    }}

    .arc-sub-dot {{
      inline-size: 5px;
      block-size: 5px;
      border-radius: 999px;
      background: currentColor;
      flex-shrink: 0;
      opacity: 0.65;
    }}

    .arc-subtab.is-active .arc-sub-dot {{
      background: var(--accent);
      opacity: 1;
    }}

    .arc-sub-txt {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 1;
    }}

    .arc-sub-tag {{
      font-size: 0.67rem;
      color: var(--muted);
      flex-shrink: 0;
    }}

    /* Main Rounded Content Surface (holds ONLY selected step/page content) */
    .canvas-wrap {{
      background: var(--bg);
      margin: 0 0.55rem 0.55rem 0;
      border-radius: 12px;
      border: 1px solid var(--border);
      min-height: calc(100vh - 3.9rem);
      padding: 1.35rem 1.5rem 3rem;
    }}

    .arc-layout.is-rail-collapsed .canvas-wrap {{
      margin-left: 0.55rem;
    }}

    .step-panel {{
      scroll-margin-top: 4.2rem;
    }}

    /* Pronounced Top-Bar Area Filter Trigger & 2-Level Tree Popover */
    .scope-filter-group {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      position: relative;
    }}

    .scope-trigger-btn {{
      appearance: none;
      border: 1.5px solid var(--border);
      background: var(--surface);
      color: var(--text);
      border-radius: 8px;
      min-block-size: 34px;
      padding: 0.36rem 0.72rem;
      font: inherit;
      font-size: 0.82rem;
      font-weight: 650;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.42rem;
    }}

    .scope-trigger-btn:hover {{
      border-color: var(--accent);
    }}

    .scope-trigger-btn.is-filtered {{
      background: var(--accent-soft);
      border-color: var(--accent);
      color: var(--accent);
    }}

    .scope-icon {{
      font-size: 0.9rem;
      color: var(--accent);
    }}

    .scope-trigger-badge {{
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.71rem;
      font-weight: 700;
      padding: 0.08rem 0.42rem;
      border-radius: 999px;
      background: var(--surface);
      color: var(--muted);
      border: 1px solid var(--border);
    }}

    .scope-trigger-btn.is-filtered .scope-trigger-badge {{
      background: var(--accent);
      color: #ffffff;
      border-color: var(--accent);
    }}

    :root:has(> head > meta[name="color-scheme"][content="dark"]) .scope-trigger-btn.is-filtered .scope-trigger-badge {{
      color: #0b0f16;
    }}

    @media (prefers-color-scheme: dark) {{
      :root:not(:has(> head > meta[name="color-scheme"][content="light"])) .scope-trigger-btn.is-filtered .scope-trigger-badge {{
        color: #0b0f16;
      }}
    }}

    .scope-reset-btn {{
      appearance: none;
      border: 1px solid var(--accent);
      background: transparent;
      color: var(--accent);
      border-radius: 7px;
      padding: 0.34rem 0.56rem;
      font-size: 0.75rem;
      font-weight: 650;
      cursor: pointer;
    }}

    .scope-reset-btn[hidden] {{ display: none; }}
    .scope-reset-btn:hover {{ background: var(--accent-soft); }}

    .scope-popover {{
      border: 1px solid var(--border);
      border-radius: 11px;
      padding: 0;
      width: min(92vw, 390px);
      max-height: min(82vh, 580px);
      overflow-y: auto;
      background: var(--surface);
      color: var(--text);
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.28);
    }}

    @supports (position-area: block-end) {{
      .scope-trigger-btn {{
        anchor-name: --scope-trigger;
      }}
      [popover].scope-popover {{
        position-anchor: --scope-trigger;
        inset: auto;
        margin: 0.35rem 0 0;
        position-area: block-end span-inline-end;
        position-try-fallbacks: flip-block, flip-inline, flip-block flip-inline;
      }}
    }}

    @supports not (position-area: block-end) {{
      [popover].scope-popover {{
        position: fixed;
        inset: auto;
        margin: 0;
      }}
    }}

    .scope-pop-head {{
      padding: 0.65rem 0.85rem;
      border-bottom: 1px solid var(--border);
      background: var(--subtle);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }}

    .scope-pop-title {{
      display: block;
      font-size: 0.83rem;
    }}

    .scope-pop-sub {{
      display: block;
      font-size: 0.72rem;
      color: var(--muted);
    }}

    .scope-all-btn {{
      appearance: none;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--accent);
      border-radius: 6px;
      padding: 0.22rem 0.52rem;
      font-size: 0.74rem;
      font-weight: 650;
      cursor: pointer;
      flex-shrink: 0;
    }}

    .scope-all-btn:hover {{ border-color: var(--accent); }}

    .scope-tree {{
      padding: 0.45rem 0.55rem 0.6rem;
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
    }}

    .scope-surface-group {{
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      background: var(--bg);
    }}

    .scope-row {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.34rem 0.55rem;
    }}

    .scope-surface-row {{
      background: var(--subtle);
      font-weight: 650;
    }}

    .scope-area-list {{
      display: flex;
      flex-direction: column;
      padding: 0.2rem 0 0.25rem 1.45rem;
      background: var(--surface);
      border-top: 1px solid var(--border);
    }}

    .scope-surface-group.is-collapsed .scope-area-list {{
      display: none;
    }}

    .scope-exp-btn {{
      appearance: none;
      border: 0;
      background: transparent;
      color: var(--muted);
      padding: 0.1rem 0.2rem;
      font-size: 0.78rem;
      cursor: pointer;
      line-height: 1;
    }}

    .scope-label {{
      position: relative;
      display: inline-flex;
      align-items: center;
      gap: 0.48rem;
      flex: 1;
      min-width: 0;
      min-block-size: 28px;
      cursor: pointer;
      font-size: 0.81rem;
    }}

    .scope-surface-name, .scope-area-name {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

    .scope-area-name {{
      font-weight: 500;
    }}

    .scope-meta-count {{
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.69rem;
      color: var(--muted);
      flex-shrink: 0;
    }}

    .scope-only-btn {{
      appearance: none;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--muted);
      border-radius: 5px;
      padding: 0.1rem 0.38rem;
      font-size: 0.69rem;
      font-weight: 650;
      cursor: pointer;
      flex-shrink: 0;
    }}

    .scope-only-btn:hover {{
      border-color: var(--accent);
      color: var(--accent);
      background: var(--accent-soft);
    }}

    /* Accessible Custom Checkboxes (Checked ✓ & Indeterminate −) */
    .cb-input {{
      position: absolute !important;
      clip-path: inset(50%) !important;
      overflow: hidden !important;
      width: 1px !important;
      height: 1px !important;
      margin: -1px !important;
      padding: 0 !important;
      border: 0 !important;
      white-space: nowrap !important;
    }}

    .cb-box {{
      inline-size: 1.05rem;
      block-size: 1.05rem;
      border: 1.75px solid var(--muted);
      border-radius: 4px;
      background: var(--surface);
      color: #ffffff;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }}

    :root:has(> head > meta[name="color-scheme"][content="dark"]) .cb-box {{
      color: #0b0f16;
    }}

    @media (prefers-color-scheme: dark) {{
      :root:not(:has(> head > meta[name="color-scheme"][content="light"])) .cb-box {{
        color: #0b0f16;
      }}
    }}

    .cb-svg {{ display: none; }}

    .cb-input:is(:checked, :indeterminate) + .cb-box {{
      background: var(--accent);
      border-color: var(--accent);
    }}

    .cb-input:checked + .cb-box .cb-svg-check {{ display: block; }}
    .cb-input:indeterminate + .cb-box .cb-svg-dash {{ display: block; }}

    .cb-input:focus-visible + .cb-box {{
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }}

    .steps-stack {{
      max-width: 1140px;
      margin: 0 auto;
    }}

    /* Note: `hidden="until-found"` uses `content-visibility: hidden` (NOT `display: none`),
       so `.steps-stack` must NOT use `display: flex` + `gap` (which adds N * gap above Step N). */
    .step-panel[hidden="until-found"] {{
      margin: 0 !important;
      padding: 0 !important;
      border: 0 !important;
      block-size: 0 !important;
      overflow: hidden !important;
    }}

    .step-panel:not([hidden]) ~ .step-panel:not([hidden]) {{
      margin-top: 1.25rem;
    }}

    .step-card {{
      container-type: inline-size;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.35rem 1.5rem;
    }}

    .eyebrow {{
      margin: 0 0 0.3rem;
      font-size: 0.77rem;
      color: var(--muted);
      font-weight: 560;
    }}

    .step-title {{
      margin: 0 0 0.55rem;
      font-size: 1.24rem;
      font-weight: 660;
      line-height: 1.3;
    }}

    .lead-text {{
      max-width: 80ch;
      margin: 0 0 0.95rem;
      font-size: 0.95rem;
    }}

    .lead-subtle {{
      margin: 0 0 0.85rem;
      font-size: 0.87rem;
      color: var(--muted);
    }}

    /* Rotated Formatting Containers: Tables, Anchored Lists, Concept Chips */
    .table-wrap {{
      margin: 0 0 1rem;
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
    }}

    .mod-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
    }}

    .mod-table th, .mod-table td {{
      padding: 0.5rem 0.75rem;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }}

    .mod-table th {{
      background: var(--subtle);
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
    }}

    .mod-table tr:last-child td {{ border-bottom: 0; }}

    .inline-jump {{
      appearance: none;
      border: 0;
      background: none;
      padding: 0;
      font: inherit;
      font-weight: 620;
      color: var(--accent);
      cursor: pointer;
    }}

    .inline-jump:hover {{ text-decoration: underline; }}

    .anchored-list {{
      margin: 0 0 1rem;
      padding-left: 1.2rem;
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      font-size: 0.89rem;
    }}

    .concepts-Primer-box {{
      margin: 0 0 1rem;
      padding: 0.65rem 0.85rem;
      border-radius: 8px;
      background: var(--subtle);
      border: 1px solid var(--border);
    }}

    .concept-chip-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
      margin-top: 0.4rem;
    }}

    .concept-chip {{
      appearance: none;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--accent);
      border-radius: 999px;
      padding: 0.22rem 0.6rem;
      font-size: 0.79rem;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
    }}

    .concept-chip:hover {{ border-color: var(--accent); }}

    /* Flow & Before/After */
    .flow-strip {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.4rem;
      margin: 0 0 1rem;
      padding: 0.65rem 0.8rem;
      background: var(--subtle);
      border-radius: 8px;
      font-size: 0.82rem;
    }}

    .flow-chip {{
      font: inherit;
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.78rem;
      padding: 0.22rem 0.55rem;
      border-radius: 6px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text);
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
    }}

    button.flow-chip {{ cursor: pointer; }}
    button.flow-chip:hover {{ border-color: var(--accent); color: var(--accent); }}
    .flow-unchanged {{ opacity: 0.78; border-style: dashed; }}

    .flow-tag {{
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      font-size: 0.65rem;
      font-weight: 650;
      text-transform: uppercase;
      padding: 0.04rem 0.3rem;
      border-radius: 4px;
    }}

    .flow-tag-new {{ color: var(--add-fg); background: var(--add-bg); }}
    .flow-tag-mod {{ color: var(--accent); }}
    .flow-arrow {{ color: var(--muted); }}

    .before-after {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 0.75rem;
      margin-bottom: 0.9rem;
    }}

    .pr-part-card {{
      margin: 0 0 0.9rem;
      padding: 0.72rem 0.9rem;
      border-radius: 8px;
      background: var(--accent-soft);
      border: 1px solid var(--border);
      border-inline-start: 3px solid var(--accent);
      font-size: 0.89rem;
    }}

    .pr-part-card p {{ margin: 0.22rem 0 0; }}

    .pr-part-badge {{
      display: inline-block;
      font-size: 0.71rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--accent);
    }}

    .ba-item {{
      padding: 0.75rem 0.9rem;
      border-radius: 8px;
      background: var(--subtle);
      font-size: 0.88rem;
    }}

    .ba-item p {{ margin: 0.2rem 0 0; }}

    .ba-label {{
      font-size: 0.71rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
    }}

    /* Visuals & Red Annotations */
    .visuals-stack {{
      display: flex;
      flex-direction: column;
      gap: 0.85rem;
      margin: 0 0 1rem;
    }}

    .visual-card {{
      margin: 0;
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      background: var(--subtle);
    }}

    .visual-head {{
      padding: 0.4rem 0.75rem;
      background: var(--subtle);
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
      font-size: 0.78rem;
    }}

    .vis-badge {{ font-weight: 650; color: var(--muted); }}
    .visual-actions {{ display: flex; align-items: center; gap: 0.65rem; }}
    .vis-ext-link {{ color: var(--accent); text-decoration: none; font-weight: 550; }}
    .vis-ext-link:hover {{ text-decoration: underline; }}

    .vis-zoom-btn, .snapshot-toggle-btn {{
      appearance: none;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--muted);
      border-radius: 5px;
      padding: 0.12rem 0.48rem;
      font-size: 0.73rem;
      font-weight: 550;
      cursor: pointer;
    }}

    .vis-zoom-btn:hover, .snapshot-toggle-btn:hover {{
      color: var(--text);
      border-color: var(--text);
    }}

    .snapshot-toggle-btn[aria-pressed="true"] {{
      background: var(--accent-soft);
      color: var(--accent);
      border-color: var(--accent);
    }}

    .visual-stage {{ position: relative; line-height: 0; background: var(--surface); }}
    .visual-stage img {{ display: block; inline-size: 100%; block-size: auto; }}
    .visual-overlay {{ position: absolute; inset: 0; inline-size: 100%; block-size: 100%; pointer-events: none; }}
    .visual-caption {{
      padding: 0.5rem 0.75rem;
      font-size: 0.82rem;
      color: var(--muted);
      border-top: 1px solid var(--border);
      background: var(--surface);
    }}

    .figma-only-card {{
      padding: 0.6rem 0.85rem;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--subtle);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      font-size: 0.85rem;
    }}

    /* Modals (<dialog> for Concept Primer & Image Zoom) */
    .modal-dialog {{
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 0;
      background: var(--surface);
      color: var(--text);
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.35);
    }}

    .modal-dialog::backdrop {{ background: rgba(0, 0, 0, 0.65); }}

    .concept-dialog {{
      width: min(92vw, 580px);
    }}

    .zoom-dialog {{
      max-width: min(94vw, 1360px);
      width: fit-content;
    }}

    .modal-bar {{
      padding: 0.7rem 1rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
    }}

    .modal-title {{
      margin: 0;
      font-size: 1.02rem;
      font-weight: 680;
    }}

    .concept-modal-body {{
      padding: 1rem 1.15rem 1.2rem;
    }}

    .concept-meta-row {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.45rem;
      margin-bottom: 0.65rem;
    }}

    .concept-origin-pill {{
      display: inline-block;
      font-size: 0.72rem;
      font-weight: 660;
      padding: 0.14rem 0.48rem;
      border-radius: 999px;
      background: var(--subtle);
      border: 1px solid var(--border);
      color: var(--muted);
    }}

    .concept-origin-pill.is-new {{
      color: var(--add-fg);
      background: var(--add-bg);
      border-color: transparent;
    }}

    .concept-file-pill {{
      display: inline-block;
      font-size: 0.76rem;
      padding: 0.15rem 0.45rem;
      border-radius: 5px;
      background: var(--subtle);
      color: var(--muted);
    }}

    .concept-sec h3 {{
      margin: 0.2rem 0 0.4rem;
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
    }}

    .concept-jumps {{
      margin-top: 0.9rem;
      padding-top: 0.75rem;
      border-top: 1px solid var(--border);
    }}

    .concept-jump-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
      margin-top: 0.4rem;
    }}

    .zoom-dialog-stage {{
      position: relative;
      line-height: 0;
      max-height: 84vh;
      overflow: auto;
    }}

    .zoom-dialog-stage img {{ display: block; max-width: 100%; height: auto; }}

    /* Callouts */
    .callout {{
      margin: 0 0 0.9rem;
      padding: 0.6rem 0.85rem;
      border-left: 3px solid var(--warn-fg);
      background: var(--warn-bg);
      border-radius: 0 6px 6px 0;
      font-size: 0.87rem;
    }}

    .callout-decision {{
      border-left-color: var(--accent);
      background: var(--subtle);
    }}

    .compact-list {{ margin: 0.3rem 0 0; padding-left: 1.15rem; }}

    /* Timeline Diff Cards */
    .hunk-stack {{ display: flex; flex-direction: column; gap: 0.85rem; }}

    .hunk-card {{
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      background: var(--surface);
    }}

    .hunk-head {{
      padding: 0.45rem 0.75rem;
      background: var(--subtle);
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
    }}

    .hunk-title-wrap {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      min-width: 0;
    }}

    .hunk-filepath {{ font-size: 0.81rem; font-weight: 600; word-break: break-all; }}

    .timeline-badge {{
      font-size: 0.68rem;
      font-weight: 620;
      padding: 0.08rem 0.4rem;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      white-space: nowrap;
    }}

    .hunk-right {{
      display: flex;
      align-items: center;
      gap: 0.6rem;
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.76rem;
      flex-shrink: 0;
    }}

    .stat-add {{ color: var(--add-fg); font-weight: 650; }}
    .stat-del {{ color: var(--del-fg); font-weight: 650; }}
    .stat-neutral {{ color: var(--muted); }}

    .hunk-pr-btn {{
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      border: 1px solid var(--accent);
      background: var(--accent-soft);
      color: var(--accent);
      border-radius: 6px;
      padding: 0.14rem 0.5rem;
      font-size: 0.74rem;
      font-weight: 660;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 0.2rem;
    }}

    .hunk-pr-btn:hover {{
      background: var(--accent);
      color: var(--surface);
    }}

    .area-badge {{
      font-size: 0.67rem;
      font-weight: 600;
      padding: 0.08rem 0.38rem;
      border-radius: 5px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--muted);
      white-space: nowrap;
    }}

    .hunk-card.is-card-filtered {{ display: none; }}

    .filtered-cards-bar {{
      margin-top: 0.65rem;
      padding: 0.42rem 0.75rem;
      border: 1px dashed var(--border);
      border-radius: 7px;
      background: var(--subtle);
      font-size: 0.79rem;
      color: var(--muted);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.6rem;
    }}

    .filtered-cards-bar[hidden], .outside-filter-banner[hidden] {{ display: none; }}

    .outside-filter-banner {{
      margin: 0 0 0.75rem;
      padding: 0.4rem 0.7rem;
      border-radius: 6px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 0.79rem;
      font-weight: 600;
    }}

    .hunk-note {{
      margin: 0;
      padding: 0.4rem 0.75rem;
      font-size: 0.82rem;
      color: var(--muted);
      border-bottom: 1px solid var(--border);
    }}

    .snapshot-banner {{
      padding: 0.32rem 0.75rem;
      font-size: 0.76rem;
      background: var(--accent-soft);
      color: var(--accent);
      border-bottom: 1px solid var(--border);
    }}

    summary {{
      list-style: none;
      display: flex;
      align-items: center;
      gap: 0.45ch;
      cursor: pointer;
    }}

    summary::-webkit-details-marker {{ display: none; }}

    summary::before {{
      content: "";
      inline-size: 0.9em;
      block-size: 0.9em;
      flex-shrink: 0;
      background-color: currentColor;
      mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath fill='black' d='M4.427 6.427l3.396 3.396a.25.25 0 00.354 0l3.396-3.396A.25.25 0 0011.396 6H4.604a.25.25 0 00-.177.427z'/%3E%3C/svg%3E") no-repeat center / contain;
      rotate: -90deg;
    }}

    details[open] > summary::before {{ rotate: 0deg; }}

    .hunk-toggle-summary {{
      padding: 0.45rem 0.75rem;
      font-size: 0.8rem;
      color: var(--muted);
      font-weight: 550;
    }}

    details[open] > .hunk-toggle-summary {{ border-bottom: 1px solid var(--border); }}

    .ctx-bar {{
      padding: 0.25rem 0.75rem;
      background: var(--subtle);
      display: flex;
      align-items: center;
      gap: 0.75rem;
      font-size: 0.76rem;
    }}

    .ctx-bar-before {{ border-bottom: 1px dashed var(--border); }}
    .ctx-bar-after {{ border-top: 1px dashed var(--border); }}

    .ctx-btn {{
      appearance: none;
      border: 0;
      background: none;
      padding: 0.1rem 0;
      font: inherit;
      color: var(--accent);
      cursor: pointer;
      font-weight: 550;
    }}

    .ctx-btn[hidden], .snapshot-view[hidden], .diff-view[hidden] {{ display: none; }}
    .ctx-btn:hover {{ text-decoration: underline; }}
    .ctx-less-btn {{ color: var(--muted); }}

    .diff-pre {{
      margin: 0;
      padding: 0.3rem 0;
      overflow-x: auto;
      font-size: 0.8rem;
      line-height: 1.45;
      white-space: normal;
      background: var(--surface);
    }}

    .diff-code {{ display: block; min-width: max-content; white-space: normal; }}
    .dl {{ display: flex; align-items: center; padding: 0 0.75rem 0 0.25rem; }}
    .dl[hidden] {{ display: none; }}
    .dl-ctx {{ background-color: var(--surface); }}
    .dl-add {{ background-color: var(--add-bg); }}
    .dl-del {{ background-color: var(--del-bg); }}
    .dl-exp {{ background-color: var(--subtle); opacity: 0.88; }}
    .dl-meta {{ color: var(--muted); font-style: italic; }}

    /* Interactive Diff-Block Highlights (Translucent Overlay Over Red/Green/Gray + Side Card) */
    .hl-hint-badge {{
      font-size: 0.68rem;
      font-weight: 640;
      padding: 0.08rem 0.44rem;
      border-radius: 999px;
      border: 1px solid var(--accent);
      background: var(--accent-soft);
      color: var(--accent);
      white-space: nowrap;
    }}

    .dl-has-hl {{
      cursor: pointer;
      box-shadow: inset 3px 0 0 rgba(88, 166, 255, 0.52);
    }}

    @media (forced-colors: active) {{
      .dl-has-hl {{ border-inline-start: 3px solid Highlight; }}
    }}

    .dl-has-hl.is-hl-hover {{
      box-shadow: inset 4px 0 0 var(--accent);
      background-image: linear-gradient(rgba(56, 139, 253, 0.16), rgba(56, 139, 253, 0.16));
    }}

    .dl-has-hl.is-hl-active {{
      box-shadow: inset 4px 0 0 var(--accent);
      background-image: linear-gradient(rgba(56, 139, 253, 0.26), rgba(56, 139, 253, 0.26));
    }}

    @supports (color: color-mix(in oklab, white, black)) {{
      .dl-has-hl {{
        box-shadow: inset 3px 0 0 color-mix(in oklab, var(--accent), transparent 42%);
      }}
      .dl-ctx.is-hl-hover {{
        background-image: none;
        background-color: color-mix(in oklab, var(--surface), var(--accent) 14%);
      }}
      .dl-add.is-hl-hover {{
        background-image: none;
        background-color: color-mix(in oklab, var(--add-bg), var(--accent) 20%);
      }}
      .dl-del.is-hl-hover {{
        background-image: none;
        background-color: color-mix(in oklab, var(--del-bg), var(--accent) 20%);
      }}
      .dl-ctx.is-hl-active {{
        background-image: none;
        background-color: color-mix(in oklab, var(--surface), var(--accent) 22%);
      }}
      .dl-add.is-hl-active {{
        background-image: none;
        background-color: color-mix(in oklab, var(--add-bg), var(--accent) 28%);
      }}
      .dl-del.is-hl-active {{
        background-image: none;
        background-color: color-mix(in oklab, var(--del-bg), var(--accent) 28%);
      }}
    }}

    .hl-chip-strip {{
      padding: 0.38rem 0.75rem;
      background: var(--subtle);
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.4rem;
    }}

    .hl-strip-lbl {{
      font-size: 0.72rem;
      font-weight: 620;
      color: var(--muted);
      margin-inline-end: 0.15rem;
    }}

    .dl-hl-pill {{
      appearance: none;
      border: 1px solid var(--accent);
      background: var(--surface);
      color: var(--accent);
      border-radius: 999px;
      padding: 0.12rem 0.54rem;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 0.72rem;
      font-weight: 650;
      line-height: 1.35;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      white-space: nowrap;
    }}

    .dl-hl-ln {{
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 0.66rem;
      opacity: 0.75;
    }}

    .dl-hl-pill.is-hl-hover,
    .dl-hl-pill[aria-expanded="true"] {{
      background: var(--accent);
      color: #ffffff;
    }}

    :root:has(> head > meta[name="color-scheme"][content="dark"]) .dl-hl-pill.is-hl-hover,
    :root:has(> head > meta[name="color-scheme"][content="dark"]) .dl-hl-pill[aria-expanded="true"] {{
      color: #0b0f16;
    }}

    @media (prefers-color-scheme: dark) {{
      :root:not(:has(> head > meta[name="color-scheme"][content="light"])) .dl-hl-pill.is-hl-hover,
      :root:not(:has(> head > meta[name="color-scheme"][content="light"])) .dl-hl-pill[aria-expanded="true"] {{
        color: #0b0f16;
      }}
    }}

    .dl-hl-marker {{
      margin-inline-start: 0.55rem;
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      font-size: 0.72rem;
      font-weight: 750;
      color: var(--accent);
      user-select: none;
    }}

    /* Side-Margin Explanation Rail (Never Covers Code) */
    .hl-side-rail[hidden], .hl-note-card[hidden] {{ display: none; }}

    .diff-view-hl.has-active-hl {{
      display: flex;
      flex-direction: column;
    }}

    .diff-view-hl.has-active-hl .hl-side-rail {{
      background: var(--subtle);
      border-top: 1px solid var(--border);
      padding: 0.55rem;
    }}

    @media (min-width: 960px) {{
      .diff-view-hl.has-active-hl {{
        display: grid;
        grid-template-columns: minmax(0, 1fr) 264px;
        align-items: stretch;
      }}
      .diff-view-hl.has-active-hl .diff-code-col {{
        min-width: 0;
        border-inline-end: 1px solid var(--border);
      }}
      .diff-view-hl.has-active-hl .hl-side-rail {{
        border-top: 0;
      }}
    }}

    @supports (container-type: inline-size) {{
      @media (min-width: 960px) {{
        .diff-view-hl.has-active-hl {{
          display: flex;
        }}
        .diff-view-hl.has-active-hl .diff-code-col {{
          border-inline-end: 0;
        }}
        .diff-view-hl.has-active-hl .hl-side-rail {{
          border-top: 1px solid var(--border);
        }}
      }}
      @container (min-width: 680px) {{
        .diff-view-hl.has-active-hl {{
          display: grid;
          grid-template-columns: minmax(0, 1fr) 264px;
          align-items: stretch;
        }}
        .diff-view-hl.has-active-hl .diff-code-col {{
          min-width: 0;
          border-inline-end: 1px solid var(--border);
        }}
        .diff-view-hl.has-active-hl .hl-side-rail {{
          border-top: 0;
        }}
      }}
    }}

    .hl-note-card {{
      background: var(--surface);
      border: 1.5px solid var(--accent);
      border-radius: 8px;
      padding: 0.65rem 0.75rem;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.14);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}

    .hl-note-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 0.4rem;
      margin-bottom: 0.4rem;
    }}

    .hl-note-heading {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.32rem;
      font-size: 0.81rem;
    }}

    .hl-note-num {{
      color: var(--accent);
      font-weight: 750;
    }}

    .hl-note-lines {{
      font-size: 0.69rem;
      font-family: ui-monospace, SFMono-Regular, monospace;
      padding: 0.05rem 0.34rem;
      border-radius: 4px;
      background: var(--subtle);
      color: var(--muted);
      text-decoration: none;
    }}

    .hl-note-pr-link {{
      color: var(--accent);
      background: var(--accent-soft);
      font-weight: 620;
    }}

    .hl-note-pr-link:hover {{
      text-decoration: underline;
    }}

    .hl-close-btn {{
      appearance: none;
      border: 0;
      background: transparent;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1;
      padding: 0.05rem 0.25rem;
      cursor: pointer;
      border-radius: 4px;
    }}

    .hl-close-btn:hover {{
      background: var(--subtle);
      color: var(--text);
    }}

    .hl-note-body {{
      margin: 0;
      font-size: 0.82rem;
      line-height: 1.48;
      color: var(--text);
    }}

    .hl-note-nav {{
      margin-top: 0.5rem;
      padding-top: 0.4rem;
      border-top: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 0.74rem;
    }}

    .ln {{
      inline-size: 2.75rem;
      text-align: right;
      padding-right: 0.5rem;
      color: var(--muted);
      user-select: none;
      flex-shrink: 0;
      font-size: 0.74rem;
      opacity: 0.72;
      text-decoration: none;
    }}

    .ln-link {{
      cursor: pointer;
    }}

    .dl:hover .ln-link {{
      color: var(--accent);
      opacity: 0.95;
    }}

    .ln-link:hover,
    .ln-link:focus-visible {{
      color: var(--accent);
      opacity: 1;
      font-weight: 650;
      text-decoration: underline;
    }}

    .row-pr-jump {{
      margin-left: auto;
      position: sticky;
      right: 0.45rem;
      flex-shrink: 0;
      padding: 0.04rem 0.38rem;
      border-radius: 4px;
      border: 1px solid var(--accent);
      background: var(--surface);
      color: var(--accent);
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      font-size: 0.66rem;
      font-weight: 650;
      line-height: 1.35;
      text-decoration: none;
      white-space: nowrap;
      opacity: 0;
      pointer-events: none;
    }}

    .dl:hover .row-pr-jump,
    .dl:focus-within .row-pr-jump {{
      opacity: 1;
      pointer-events: auto;
    }}

    .row-pr-jump:hover {{
      background: var(--accent);
      color: var(--surface);
    }}

    .sig {{ inline-size: 1.05rem; user-select: none; flex-shrink: 0; font-weight: 700; }}
    .dl-add .sig {{ color: var(--add-fg); }}
    .dl-del .sig {{ color: var(--del-fg); }}
    .code-txt {{ white-space: pre; }}

    .tok-kw {{ color: var(--tok-kw); font-weight: 550; }}
    .tok-str {{ color: var(--tok-str); }}
    .tok-cmt {{ color: var(--tok-cmt); font-style: italic; }}
    .tok-typ {{ color: var(--tok-typ); }}
    .tok-fn {{ color: var(--tok-fn); }}
    .tok-num {{ color: var(--tok-num); }}

    .mech-section + .mech-section {{ margin-top: 1.1rem; }}
    .mech-heading {{ margin: 0 0 0.25rem; font-size: 0.95rem; }}

    .step-footer {{
      margin-top: 1.25rem;
      padding-top: 0.9rem;
      border-top: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }}

    .nav-btn {{
      appearance: none;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      border-radius: 7px;
      padding: 0.42rem 0.85rem;
      font-size: 0.84rem;
      font-weight: 550;
      cursor: pointer;
    }}

    .nav-btn:hover {{ border-color: var(--text); }}

    .nav-btn-primary {{
      background: var(--text);
      color: var(--surface);
      border-color: var(--text);
    }}

    .nav-btn-primary:hover {{ opacity: 0.9; }}
    .coverage-note, .done-badge {{ font-size: 0.8rem; color: var(--muted); }}
    .done-badge {{ color: var(--add-fg); font-weight: 600; }}

    .foot-hint {{
      max-width: 1020px;
      margin: 1.5rem auto 0;
      text-align: center;
      font-size: 0.75rem;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <header class="app-topbar">
    <div class="topbar-left">
      <button type="button" id="rail-toggle" class="icon-btn" aria-expanded="true" aria-label="Toggle chapter sidebar">◧ Sidebar</button>
      <span class="arc-brand">unmaze</span>
      <div class="scope-filter-group">
        <button type="button" id="scope-filter-btn" class="scope-trigger-btn" popovertarget="scope-filter-popover" aria-controls="scope-filter-popover" aria-expanded="false">
          <svg class="scope-icon" width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="1.5 2.5 14.5 2.5 9.5 8.2 9.5 13 6.5 14.2 6.5 8.2 1.5 2.5"/></svg>
          <span id="scope-trigger-text">Filter by Area: All</span>
          <span id="scope-trigger-badge" class="scope-trigger-badge">{total_main_steps}/{total_main_steps} steps</span>
          <span aria-hidden="true">▾</span>
        </button>
        <button type="button" id="scope-reset-top" class="scope-reset-btn" hidden>Reset ×</button>
        {scope_popover_html}
      </div>
    </div>
    <div class="topbar-right">
      <button type="button" id="mode-toggle" class="icon-btn mode-toggle-btn" aria-pressed="false">All steps</button>
      <button type="button" id="theme-toggle" class="icon-btn" aria-label="Switch color theme">Theme</button>
      {pr_topbar_cta}
    </div>
  </header>

  <div id="arc-shell" class="arc-layout">
    <nav class="arc-sidebar" aria-label="Review chapters and steps">
      <h1 class="arc-pr-title">{esc(title)}</h1>

      <button type="button" class="arc-tab is-active" data-panel-index="0" aria-current="step">
        <span class="arc-tab-label">Overview</span>
      </button>

      <div class="arc-section-lbl">{section_heading}</div>
      <div class="arc-tabs-scroll">
        {"".join(arc_nav_items)}
        {mech_tab_html}
      </div>
    </nav>

    <div class="canvas-wrap">
      <main id="steps-main" class="steps-stack">
        {"".join(panels_html)}
      </main>

      <footer class="foot-hint">
        Shortcuts: <code>[</code> / <code>]</code> prev/next enabled step · Filter by area in the top bar · Click any <code>concept ?</code> for a repo primer
      </footer>
    </div>
  </div>

  <dialog id="concept-dialog" class="modal-dialog concept-dialog" closedby="any">
    <div class="modal-bar">
      <h2 id="concept-modal-title" class="modal-title">Concept</h2>
      <form method="dialog">
        <button class="icon-btn" value="close">Close</button>
      </form>
    </div>
    <div id="concept-modal-body" class="concept-modal-body"></div>
  </dialog>

  <dialog id="zoom-dialog" class="modal-dialog zoom-dialog" closedby="any">
    <div class="modal-bar">
      <span id="zoom-caption" class="modal-title">Visual Reference</span>
      <form method="dialog">
        <button class="icon-btn" value="close">Close</button>
      </form>
    </div>
    <div id="zoom-stage" class="zoom-dialog-stage"></div>
  </dialog>

  {concept_templates_html}

  <script>
    (() => {{
      const EXPAND_STEP = {EXPAND_CHUNK};
      const TOTAL_MAIN_STEPS = {total_main_steps};
      const supportsUntilFound = "onbeforematch" in HTMLElement.prototype;
      const supportsNativePopover = Object.hasOwn(HTMLElement.prototype, "popover");
      const shell = document.getElementById("arc-shell");
      const railToggle = document.getElementById("rail-toggle");
      const panels = Array.from(document.querySelectorAll(".step-panel"));
      const topTabs = Array.from(document.querySelectorAll(".arc-tab"));
      const subTabs = Array.from(document.querySelectorAll(".arc-subtab"));
      const chapterBoxes = Array.from(document.querySelectorAll(".arc-chapter"));
      const modeToggle = document.getElementById("mode-toggle");
      const themeToggle = document.getElementById("theme-toggle");
      const metaScheme = document.querySelector('meta[name="color-scheme"]');
      const conceptDialog = document.getElementById("concept-dialog");
      const conceptTitle = document.getElementById("concept-modal-title");
      const conceptBody = document.getElementById("concept-modal-body");
      const zoomDialog = document.getElementById("zoom-dialog");
      const zoomStage = document.getElementById("zoom-stage");
      const zoomCaption = document.getElementById("zoom-caption");

      // Scope Filter Controls
      const scopeBtn = document.getElementById("scope-filter-btn");
      const scopePopover = document.getElementById("scope-filter-popover");
      const scopeTriggerText = document.getElementById("scope-trigger-text");
      const scopeTriggerBadge = document.getElementById("scope-trigger-badge");
      const scopeResetTop = document.getElementById("scope-reset-top");
      const scopeSelectAll = document.getElementById("scope-select-all");
      const surfaceCbs = Array.from(document.querySelectorAll(".scope-surface-cb"));
      const areaCbs = Array.from(document.querySelectorAll(".scope-area-cb"));

      let guidedMode = supportsUntilFound;
      let activeIndex = 0;
      let forcedOutsideIndex = -1;
      let memoryScheme = null;
      const panelEnabled = panels.map(() => true);

      function findAdjacentEnabled(fromIdx, dir) {{
        for (let i = fromIdx + dir; i >= 0 && i < panels.length; i += dir) {{
          if (panelEnabled[i]) return i;
        }}
        return -1;
      }}

      function syncPanelVisibility() {{
        panels.forEach((panel, idx) => {{
          const banner = panel.querySelector(".outside-filter-banner");
          if (banner) banner.hidden = !(idx === forcedOutsideIndex && !panelEnabled[idx]);

          if (guidedMode) {{
            if (idx === activeIndex) panel.removeAttribute("hidden");
            else if (panelEnabled[idx]) panel.setAttribute("hidden", "until-found");
            else panel.setAttribute("hidden", "");
          }} else {{
            if (panelEnabled[idx] || idx === forcedOutsideIndex) panel.removeAttribute("hidden");
            else panel.setAttribute("hidden", "");
          }}
        }});
      }}

      function activatePanel(index, scroll = true, allowOutside = false) {{
        if (index < 0 || index >= panels.length) return;
        if (!panelEnabled[index] && !allowOutside) return;
        forcedOutsideIndex = (!panelEnabled[index] && allowOutside) ? index : -1;
        activeIndex = index;
        const activePanel = panels[index];
        const activeChapterId = activePanel.getAttribute("data-chapter-id") || "";

        syncPanelVisibility();

        // Sync Arc top-level tabs & chapters
        chapterBoxes.forEach((box) => {{
          const cid = box.getAttribute("data-chapter-box");
          box.classList.toggle("is-open", cid === activeChapterId);
        }});

        topTabs.forEach((tab) => {{
          const tabChapter = tab.getAttribute("data-chapter-id");
          const tabPanelIdx = Number(tab.getAttribute("data-panel-index"));
          const isCurrent = tabChapter ? tabChapter === activeChapterId : tabPanelIdx === index;
          tab.classList.toggle("is-active", isCurrent);
          if (tabPanelIdx === index && !tabChapter) tab.setAttribute("aria-current", "step");
          else tab.removeAttribute("aria-current");
        }});

        subTabs.forEach((sub) => {{
          const isCur = Number(sub.getAttribute("data-panel-index")) === index;
          sub.classList.toggle("is-active", isCur);
          if (isCur) sub.setAttribute("aria-current", "step");
          else sub.removeAttribute("aria-current");
        }});

        if (scroll) {{
          if (guidedMode) window.scrollTo({{ top: 0, behavior: "smooth" }});
          else activePanel.scrollIntoView({{ behavior: "smooth", block: "start" }});
        }}
      }}

      function updateNavButtons(isFiltered) {{
        panels.forEach((panel, idx) => {{
          const prevBtn = panel.querySelector('[data-nav-dir="prev"]');
          if (prevBtn) {{
            const prevIdx = findAdjacentEnabled(idx, -1);
            const targetPrev = prevIdx !== -1 ? prevIdx : 0;
            prevBtn.setAttribute("data-goto-index", String(targetPrev));
            prevBtn.textContent = targetPrev === 0 ? "← Overview" : "← Previous";
          }}

          if (idx === 0) {{
            const startBtn = document.getElementById("overview-start-btn");
            if (startBtn) {{
              const firstEn = findAdjacentEnabled(0, 1);
              if (firstEn === -1) {{
                startBtn.setAttribute("aria-disabled", "true");
                startBtn.classList.add("is-filter-disabled");
                startBtn.textContent = "No steps match filter";
              }} else {{
                startBtn.removeAttribute("aria-disabled");
                startBtn.classList.remove("is-filter-disabled");
                startBtn.setAttribute("data-goto-index", String(firstEn));
                startBtn.textContent = isFiltered
                  ? `Start Filtered Review (Step ${{firstEn}}) →`
                  : (startBtn.getAttribute("data-default-label") || "Start Review →");
              }}
            }}
            return;
          }}

          const nextWrap = panel.querySelector(".step-next-wrap");
          if (!nextWrap) return;
          const nextIdx = findAdjacentEnabled(idx, 1);
          if (nextIdx === -1) {{
            const done = document.createElement("span");
            done.className = "done-badge";
            done.textContent = isFiltered ? "✓ Filtered slice complete" : "✓ Review complete";
            nextWrap.replaceChildren(done);
          }} else {{
            const isMech = panels[nextIdx].id === "step-mechanical";
            const btnCls = isMech ? "nav-btn" : "nav-btn nav-btn-primary";
            let lbl = "";
            if (!isFiltered && nextIdx === idx + 1) {{
              const origBtn = nextWrap.querySelector("[data-default-label]");
              lbl = origBtn ? origBtn.getAttribute("data-default-label") : "";
            }}
            if (!lbl) {{
              if (isMech) {{
                lbl = "Boilerplate →";
              }} else {{
                const h2 = panels[nextIdx].querySelector(".step-title");
                lbl = `Next: Step ${{nextIdx}}${{h2 ? " · " + h2.textContent : ""}} →`;
              }}
            }}
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = btnCls;
            btn.setAttribute("data-nav-dir", "next");
            btn.setAttribute("data-goto-index", String(nextIdx));
            if (!isFiltered && nextIdx === idx + 1) btn.setAttribute("data-default-label", lbl);
            btn.textContent = lbl;
            nextWrap.replaceChildren(btn);
          }}
        }});
      }}

      function applyScopeFilter() {{
        const checkedAreas = new Set(
          areaCbs.filter((cb) => cb.checked).map((cb) => cb.getAttribute("data-area-id"))
        );
        const isFiltered = checkedAreas.size < areaCbs.length;

        // Sync parent surface tri-state checkboxes
        const fullyCheckedSurfaces = [];
        surfaceCbs.forEach((scb) => {{
          const sid = scb.getAttribute("data-surface-id");
          const children = areaCbs.filter((acb) => acb.getAttribute("data-surface-id") === sid);
          const checkedCount = children.filter((acb) => acb.checked).length;
          scb.checked = children.length > 0 && checkedCount === children.length;
          scb.indeterminate = checkedCount > 0 && checkedCount < children.length;
          if (scb.checked) fullyCheckedSurfaces.push(scb.getAttribute("data-surface-label") || sid);
        }});

        // Compute panel enablement & per-card visibility inside mixed panels
        panels.forEach((panel, idx) => {{
          if (idx === 0) {{
            panelEnabled[0] = true;
            return;
          }}
          const rawAreas = (panel.getAttribute("data-area-ids") || "").split(",").filter(Boolean);
          const enabled = rawAreas.some((aid) => checkedAreas.has(aid));
          panelEnabled[idx] = enabled;

          const cards = Array.from(panel.querySelectorAll(".hunk-card[data-area-id]"));
          const filterBar = panel.querySelector(".filtered-cards-bar");
          if (enabled && isFiltered && cards.length > 1) {{
            const unmatched = cards.filter((c) => !checkedAreas.has(c.getAttribute("data-area-id")));
            if (unmatched.length > 0 && unmatched.length < cards.length) {{
              const showAllInStep = panel.getAttribute("data-show-unmatched") === "true";
              cards.forEach((c) => {{
                const match = checkedAreas.has(c.getAttribute("data-area-id"));
                c.classList.toggle("is-card-filtered", !match && !showAllInStep);
              }});
              if (filterBar) {{
                const msg = filterBar.querySelector(".filtered-cards-msg");
                const tgl = filterBar.querySelector(".filtered-cards-toggle");
                const noun = unmatched.length === 1 ? "1 file" : `${{unmatched.length}} files`;
                if (msg) msg.textContent = `${{noun}} in this step outside active area filter`;
                if (tgl) tgl.textContent = showAllInStep ? "Hide filtered files" : "Show anyway";
                filterBar.hidden = false;
              }}
            }} else {{
              cards.forEach((c) => c.classList.remove("is-card-filtered"));
              if (filterBar) filterBar.hidden = true;
            }}
          }} else {{
            cards.forEach((c) => c.classList.remove("is-card-filtered"));
            if (filterBar) filterBar.hidden = true;
          }}
        }});

        // Sync Arc subtabs & chapterless top tabs
        subTabs.forEach((sub) => {{
          const pIdx = Number(sub.getAttribute("data-panel-index"));
          const en = panelEnabled[pIdx];
          sub.classList.toggle("is-filter-disabled", !en);
          if (!en) sub.setAttribute("aria-disabled", "true");
          else sub.removeAttribute("aria-disabled");
        }});

        topTabs.forEach((tab) => {{
          if (tab.classList.contains("arc-chapter-btn")) return;
          const pIdx = Number(tab.getAttribute("data-panel-index"));
          if (pIdx === 0) return;
          const en = panelEnabled[pIdx];
          tab.classList.toggle("is-filter-disabled", !en);
          if (!en) tab.setAttribute("aria-disabled", "true");
          else tab.removeAttribute("aria-disabled");
        }});

        // Sync Chapters (`Chapter 1`, `Chapter 2`, ...) & Overview chapter rows
        chapterBoxes.forEach((box) => {{
          const cid = box.getAttribute("data-chapter-box");
          const subs = Array.from(box.querySelectorAll(".arc-subtab"));
          const enabledSubs = subs.filter((s) => panelEnabled[Number(s.getAttribute("data-panel-index"))]);
          const chDisabled = enabledSubs.length === 0;
          box.classList.toggle("is-filter-disabled", chDisabled);

          const chBtn = box.querySelector(".arc-chapter-btn");
          if (chBtn) {{
            if (chDisabled) {{
              chBtn.setAttribute("aria-disabled", "true");
            }} else {{
              chBtn.removeAttribute("aria-disabled");
              chBtn.setAttribute("data-panel-index", enabledSubs[0].getAttribute("data-panel-index"));
            }}
          }}

          const pill = box.querySelector(".arc-match-pill");
          if (pill) {{
            pill.hidden = !isFiltered;
            pill.textContent = `${{enabledSubs.length}}/${{subs.length}}`;
          }}

          const ovRow = document.querySelector(`[data-overview-chapter="${{cid}}"]`);
          if (ovRow) {{
            ovRow.classList.toggle("is-filter-disabled", chDisabled);
            const jumpBtn = ovRow.querySelector("[data-goto-chapter]");
            if (jumpBtn) {{
              if (chDisabled) jumpBtn.setAttribute("aria-disabled", "true");
              else {{
                jumpBtn.removeAttribute("aria-disabled");
                jumpBtn.setAttribute("data-goto-index", enabledSubs[0].getAttribute("data-panel-index"));
              }}
            }}
          }}
        }});

        // Update Top-Bar Filter Trigger Pill & Reset Button
        let enabledMainSteps = 0;
        for (let i = 1; i <= TOTAL_MAIN_STEPS; i += 1) {{
          if (panelEnabled[i]) enabledMainSteps += 1;
        }}
        scopeBtn.classList.toggle("is-filtered", isFiltered);
        scopeResetTop.hidden = !isFiltered;
        scopeTriggerBadge.textContent = `${{enabledMainSteps}}/${{TOTAL_MAIN_STEPS}} steps`;

        if (!isFiltered) {{
          scopeTriggerText.textContent = "Filter by Area: All";
        }} else if (checkedAreas.size === 0) {{
          scopeTriggerText.textContent = "Filter: None selected";
        }} else if (checkedAreas.size === 1) {{
          const singleCb = areaCbs.find((cb) => cb.checked);
          scopeTriggerText.textContent = `Filter: ${{singleCb ? singleCb.getAttribute("data-area-label") : "1 area"}}`;
        }} else if (
          fullyCheckedSurfaces.length === 1 &&
          areaCbs.filter((cb) => cb.checked).every((cb) => {{
            const s = surfaceCbs.find((x) => x.getAttribute("data-surface-id") === cb.getAttribute("data-surface-id"));
            return s && s.checked;
          }})
        ) {{
          scopeTriggerText.textContent = `Filter: ${{fullyCheckedSurfaces[0]}}`;
        }} else {{
          scopeTriggerText.textContent = `Filter: ${{checkedAreas.size}} areas`;
        }}

        updateNavButtons(isFiltered);

        // If current active step was just filtered out, advance to nearest enabled step (or Overview)
        if (activeIndex > 0 && !panelEnabled[activeIndex]) {{
          forcedOutsideIndex = -1;
          const nextValid = findAdjacentEnabled(activeIndex, 1);
          const prevValid = findAdjacentEnabled(activeIndex, -1);
          activatePanel(nextValid !== -1 ? nextValid : (prevValid !== -1 ? prevValid : 0), false);
        }} else {{
          syncPanelVisibility();
        }}
      }}

      function setGuidedMode(enabled) {{
        guidedMode = enabled;
        modeToggle.setAttribute("aria-pressed", String(!guidedMode));
        activatePanel(activeIndex, false, forcedOutsideIndex === activeIndex);
      }}

      applyScopeFilter();
      setGuidedMode(supportsUntilFound);

      // Popover state & positioning fallback when CSS Anchor Positioning is unsupported
      if (scopePopover && scopeBtn) {{
        if (supportsNativePopover) {{
          scopePopover.addEventListener("toggle", (e) => {{
            const open = e.newState === "open";
            scopeBtn.setAttribute("aria-expanded", String(open));
            if (open && !CSS.supports("position-area", "block-end")) {{
              const t = scopeBtn.getBoundingClientRect();
              const m = scopePopover.getBoundingClientRect();
              const below = t.bottom + 6 + m.height <= window.innerHeight;
              scopePopover.style.top = `${{below ? t.bottom + 6 : Math.max(8, t.top - m.height - 6)}}px`;
              scopePopover.style.left = `${{Math.max(8, Math.min(t.left, window.innerWidth - m.width - 8))}}px`;
            }}
          }});
        }} else {{
          scopePopover.hidden = true;
          scopeBtn.addEventListener("click", (e) => {{
            e.stopPropagation();
            scopePopover.hidden = !scopePopover.hidden;
            scopeBtn.setAttribute("aria-expanded", String(!scopePopover.hidden));
          }});
          document.addEventListener("click", (e) => {{
            if (!scopePopover.hidden && !scopePopover.contains(e.target) && e.target !== scopeBtn) {{
              scopePopover.hidden = true;
              scopeBtn.setAttribute("aria-expanded", "false");
            }}
          }});
        }}
      }}

      surfaceCbs.forEach((scb) => {{
        scb.addEventListener("change", () => {{
          const sid = scb.getAttribute("data-surface-id");
          areaCbs.forEach((acb) => {{
            if (acb.getAttribute("data-surface-id") === sid) acb.checked = scb.checked;
          }});
          applyScopeFilter();
        }});
      }});

      areaCbs.forEach((acb) => {{
        acb.addEventListener("change", applyScopeFilter);
      }});

      function resetAllFilters() {{
        areaCbs.forEach((cb) => {{ cb.checked = true; }});
        applyScopeFilter();
      }}

      if (scopeSelectAll) scopeSelectAll.addEventListener("click", resetAllFilters);
      if (scopeResetTop) scopeResetTop.addEventListener("click", resetAllFilters);

      railToggle.addEventListener("click", () => {{
        const collapsed = shell.classList.toggle("is-rail-collapsed");
        railToggle.setAttribute("aria-expanded", String(!collapsed));
      }});

      document.getElementById("steps-main").addEventListener("beforematch", (event) => {{
        const matched = event.target.closest(".step-panel");
        if (!matched) return;
        const idx = Number(matched.getAttribute("data-panel-index"));
        if (!Number.isNaN(idx) && panelEnabled[idx]) activatePanel(idx, false);
      }});

      function openConceptModal(conceptId) {{
        const tpl = document.getElementById(`concept-tpl-${{conceptId}}`);
        if (!tpl || !conceptDialog || typeof conceptDialog.showModal !== "function") return;
        conceptTitle.textContent = tpl.getAttribute("data-term") || "Concept";
        conceptBody.replaceChildren(tpl.content.cloneNode(true));
        conceptDialog.showModal();
      }}

      function setHoveredHighlight(hlId) {{
        document.querySelectorAll(".is-hl-hover").forEach((el) => {{
          const eid = el.getAttribute("data-hl-id") || el.getAttribute("data-hl-trigger");
          if (eid !== hlId) el.classList.remove("is-hl-hover");
        }});
        if (hlId) {{
          document.querySelectorAll(`[data-hl-id="${{hlId}}"], [data-hl-trigger="${{hlId}}"]`).forEach((el) => {{
            el.classList.add("is-hl-hover");
          }});
        }}
      }}

      document.addEventListener("pointerover", (event) => {{
        const hlEl = event.target.closest("[data-hl-id], [data-hl-trigger]");
        const hlId = hlEl ? (hlEl.getAttribute("data-hl-id") || hlEl.getAttribute("data-hl-trigger")) : "";
        setHoveredHighlight(hlId);
      }});

      document.addEventListener("pointerout", (event) => {{
        const hlEl = event.target.closest("[data-hl-id], [data-hl-trigger]");
        if (!hlEl) return;
        const related = event.relatedTarget && event.relatedTarget.closest
          ? event.relatedTarget.closest("[data-hl-id], [data-hl-trigger]")
          : null;
        if (!related) setHoveredHighlight("");
      }});

      function selectDiffHighlight(card, hlId, forceOpen = false) {{
        if (!card) return;
        const diffView = card.querySelector(".diff-view-hl");
        const sideRail = card.querySelector(".hl-side-rail");
        if (!diffView || !sideRail) return;

        const curActive = diffView.getAttribute("data-active-hl") || "";
        const nextActive = (!forceOpen && curActive === hlId) ? "" : hlId;
        diffView.setAttribute("data-active-hl", nextActive);

        card.querySelectorAll(".dl-has-hl").forEach((row) => {{
          row.classList.toggle("is-hl-active", Boolean(nextActive) && row.getAttribute("data-hl-id") === nextActive);
        }});
        card.querySelectorAll("[data-hl-trigger]").forEach((btn) => {{
          btn.setAttribute("aria-expanded", String(Boolean(nextActive) && btn.getAttribute("data-hl-trigger") === nextActive));
        }});

        const notes = Array.from(sideRail.querySelectorAll(".hl-note-card"));
        notes.forEach((n) => {{
          n.hidden = n.getAttribute("data-hl-note") !== nextActive;
          n.style.marginTop = "0px";
        }});

        if (!nextActive) {{
          sideRail.hidden = true;
          diffView.classList.remove("has-active-hl");
          return;
        }}

        sideRail.hidden = false;
        diffView.classList.add("has-active-hl");

        // Align side card vertically next to the highlighted lines when in side-by-side grid mode
        const activeNote = sideRail.querySelector(`[data-hl-note="${{nextActive}}"]`);
        const startRow = card.querySelector(`.dl-hl-start[data-hl-id="${{nextActive}}"]`);
        if (activeNote && startRow && getComputedStyle(diffView).display === "grid") {{
          const railRect = sideRail.getBoundingClientRect();
          const rowRect = startRow.getBoundingClientRect();
          const noteRect = activeNote.getBoundingClientRect();
          const desired = Math.max(0, rowRect.top - railRect.top - 8);
          const maxTop = Math.max(0, railRect.height - noteRect.height - 12);
          activeNote.style.marginTop = `${{Math.round(Math.min(desired, maxTop))}}px`;
        }}
      }}

      document.addEventListener("click", (event) => {{
        // Interactive Diff-Block Highlight close / switch / toggle
        const closeHlBtn = event.target.closest("[data-hl-close]");
        if (closeHlBtn) {{
          selectDiffHighlight(closeHlBtn.closest(".hunk-card"), "", false);
          return;
        }}

        const switchHlBtn = event.target.closest("[data-hl-switch]");
        if (switchHlBtn) {{
          selectDiffHighlight(
            switchHlBtn.closest(".hunk-card"),
            switchHlBtn.getAttribute("data-hl-switch") || "",
            true
          );
          return;
        }}

        const pillHlBtn = event.target.closest("[data-hl-trigger]");
        if (pillHlBtn) {{
          selectDiffHighlight(
            pillHlBtn.closest(".hunk-card"),
            pillHlBtn.getAttribute("data-hl-trigger") || "",
            false
          );
          return;
        }}

        const rowHl = event.target.closest(".dl-has-hl[data-hl-id]");
        if (rowHl && !event.target.closest("button, a")) {{
          const sel = window.getSelection ? window.getSelection() : null;
          if (sel && !sel.isCollapsed) return;
          selectDiffHighlight(
            rowHl.closest(".hunk-card"),
            rowHl.getAttribute("data-hl-id") || "",
            false
          );
          return;
        }}

        // Scope tree expand/collapse & "Only" quick-solo buttons
        const expBtn = event.target.closest(".scope-exp-btn");
        if (expBtn) {{
          const grp = expBtn.closest(".scope-surface-group");
          if (grp) {{
            const collapsed = grp.classList.toggle("is-collapsed");
            expBtn.setAttribute("aria-expanded", String(!collapsed));
            expBtn.textContent = collapsed ? "▸" : "▾";
          }}
          return;
        }}

        const onlySurfBtn = event.target.closest("[data-only-surface]");
        if (onlySurfBtn) {{
          const targetSid = onlySurfBtn.getAttribute("data-only-surface");
          areaCbs.forEach((cb) => {{
            cb.checked = cb.getAttribute("data-surface-id") === targetSid;
          }});
          applyScopeFilter();
          return;
        }}

        const onlyAreaBtn = event.target.closest("[data-only-area]");
        if (onlyAreaBtn) {{
          const targetAid = onlyAreaBtn.getAttribute("data-only-area");
          areaCbs.forEach((cb) => {{
            cb.checked = cb.getAttribute("data-area-id") === targetAid;
          }});
          applyScopeFilter();
          return;
        }}

        const filterCardsTgl = event.target.closest(".filtered-cards-toggle");
        if (filterCardsTgl) {{
          const panel = filterCardsTgl.closest(".step-panel");
          if (panel) {{
            const cur = panel.getAttribute("data-show-unmatched") === "true";
            panel.setAttribute("data-show-unmatched", String(!cur));
            applyScopeFilter();
          }}
          return;
        }}

        // Concept Modal trigger
        const conceptBtn = event.target.closest("[data-concept]");
        if (conceptBtn) {{
          openConceptModal(conceptBtn.getAttribute("data-concept"));
          return;
        }}

        // Sidebar tabs & subtabs (respect aria-disabled="true")
        const navItem = event.target.closest("[data-panel-index]");
        if (navItem && (navItem.classList.contains("arc-tab") || navItem.classList.contains("arc-subtab"))) {{
          if (navItem.getAttribute("aria-disabled") === "true") return;
          activatePanel(Number(navItem.getAttribute("data-panel-index")), true);
          return;
        }}

        // Step jump buttons
        const idxBtn = event.target.closest("[data-goto-index]");
        if (idxBtn) {{
          if (idxBtn.getAttribute("aria-disabled") === "true") return;
          if (conceptDialog && conceptDialog.open) conceptDialog.close();
          activatePanel(Number(idxBtn.getAttribute("data-goto-index")), true);
          return;
        }}
        const stepBtn = event.target.closest("[data-goto-step]");
        if (stepBtn) {{
          const fromModal = Boolean(conceptDialog && conceptDialog.open && conceptDialog.contains(stepBtn));
          if (conceptDialog && conceptDialog.open) conceptDialog.close();
          const targetId = stepBtn.getAttribute("data-goto-step");
          const idx = panels.findIndex((p) => p.getAttribute("data-step-id") === targetId);
          if (idx !== -1) activatePanel(idx, true, fromModal);
          return;
        }}

        // Toggle "Diff" vs "File at this step" timeline snapshot
        const snapBtn = event.target.closest(".snapshot-toggle-btn");
        if (snapBtn) {{
          const card = snapBtn.closest(".hunk-card");
          const diffView = card && card.querySelector(".diff-view");
          const snapView = card && card.querySelector(".snapshot-view");
          if (diffView && snapView) {{
            const showingSnap = !snapView.hidden;
            snapView.hidden = showingSnap;
            diffView.hidden = !showingSnap;
            snapBtn.setAttribute("aria-pressed", String(!showingSnap));
          }}
          return;
        }}

        // Visual Zoom Modal
        const zoomBtn = event.target.closest(".vis-zoom-btn");
        if (zoomBtn && zoomDialog && typeof zoomDialog.showModal === "function") {{
          const card = zoomBtn.closest(".visual-card");
          const stage = card && card.querySelector(".visual-stage");
          const cap = card && card.querySelector(".visual-caption");
          if (stage) {{
            zoomStage.replaceChildren(...Array.from(stage.childNodes).map((n) => n.cloneNode(true)));
            zoomCaption.textContent = cap ? cap.textContent : "Visual Reference";
            zoomDialog.showModal();
          }}
          return;
        }}

        // Expandable before / after context lines
        const ctxBtn = event.target.closest(".ctx-btn");
        if (ctxBtn) {{
          const bar = ctxBtn.closest(".ctx-bar");
          const card = ctxBtn.closest(".hunk-card");
          if (!bar || !card) return;

          const dir = ctxBtn.getAttribute("data-dir");
          const total = Number(bar.getAttribute("data-total") || "0");
          let shown = Number(bar.getAttribute("data-shown") || "0");
          const isMore = ctxBtn.classList.contains("ctx-more-btn");

          shown = isMore ? Math.min(total, shown + EXPAND_STEP) : 0;
          bar.setAttribute("data-shown", String(shown));

          if (dir === "before") {{
            const rows = card.querySelectorAll("[data-before-idx]");
            const revealFrom = total - shown;
            rows.forEach((row) => {{
              row.hidden = Number(row.getAttribute("data-before-idx")) < revealFrom;
            }});
          }} else {{
            const rows = card.querySelectorAll("[data-after-idx]");
            rows.forEach((row) => {{
              row.hidden = Number(row.getAttribute("data-after-idx")) >= shown;
            }});
          }}

          const moreBtn = bar.querySelector(".ctx-more-btn");
          const lessBtn = bar.querySelector(".ctx-less-btn");
          const remaining = total - shown;
          if (moreBtn) {{
            if (remaining > 0) {{
              const nextChunk = Math.min(EXPAND_STEP, remaining);
              const arrow = dir === "before" ? "↑" : "↓";
              const where = dir === "before" ? "above" : "below";
              moreBtn.textContent = shown > 0
                ? `${{arrow}} Show ${{nextChunk}} more ${{where}}`
                : `${{arrow}} Show ${{nextChunk}} lines ${{where}}`;
              moreBtn.hidden = false;
            }} else {{
              moreBtn.hidden = true;
            }}
          }}
          if (lessBtn) lessBtn.hidden = shown === 0;
        }}
      }});

      for (const dlg of [conceptDialog, zoomDialog]) {{
        if (dlg) {{
          dlg.addEventListener("click", (e) => {{
            if (e.target === dlg) dlg.close();
          }});
        }}
      }}

      modeToggle.addEventListener("click", () => setGuidedMode(!guidedMode));

      function getStoredScheme() {{
        try {{ return localStorage.getItem("unmaze-color-scheme") || memoryScheme; }}
        catch (_) {{ return memoryScheme; }}
      }}

      function setStoredScheme(val) {{
        memoryScheme = val;
        try {{
          if (val === null) localStorage.removeItem("unmaze-color-scheme");
          else localStorage.setItem("unmaze-color-scheme", val);
        }} catch (_) {{}}
      }}

      function syncThemeLabel() {{
        const stored = getStoredScheme();
        const sysDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        const isDark = stored ? stored === "dark" : sysDark;
        themeToggle.textContent = isDark ? "☀" : "☾";
      }}

      themeToggle.addEventListener("click", () => {{
        const stored = getStoredScheme();
        const sysDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        const isDark = stored ? stored === "dark" : sysDark;
        const target = isDark ? "light" : "dark";
        if (target === (sysDark ? "dark" : "light")) {{
          setStoredScheme(null);
          metaScheme.content = "light dark";
        }} else {{
          setStoredScheme(target);
          metaScheme.content = target;
        }}
        syncThemeLabel();
      }});

      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncThemeLabel);
      window.addEventListener("storage", (e) => {{
        if (e.key === "unmaze-color-scheme") {{
          metaScheme.content = e.newValue || "light dark";
          syncThemeLabel();
        }}
      }});
      syncThemeLabel();

      document.addEventListener("keydown", (event) => {{
        if (event.metaKey || event.ctrlKey || event.altKey) return;
        if ((conceptDialog && conceptDialog.open) || (zoomDialog && zoomDialog.open)) return;
        const tag = (event.target && event.target.tagName) || "";
        if (tag === "INPUT" || tag === "TEXTAREA") return;
        if (event.key === "]") {{
          const nxt = findAdjacentEnabled(activeIndex, 1);
          if (nxt !== -1) activatePanel(nxt, true);
        }} else if (event.key === "[") {{
          const prv = findAdjacentEnabled(activeIndex, -1);
          if (prv !== -1) activatePanel(prv, true);
        }}
      }});
    }})();
  </script>
</body>
</html>
"""


def build_markdown_summary(manifest: dict[str, Any], plan: dict[str, Any]) -> str:
    meta = manifest["meta"]
    stats = manifest["stats"]
    hunk_map: dict[str, dict[str, Any]] = {h["id"]: h for h in manifest["hunks"]}
    title = str(plan.get("title") or meta.get("title") or "PR Code Review Guide")
    ov = plan["overview"]

    lines: list[str] = [
        f"# {title}",
        "",
        f"{ov.get('whole_task') or ov['tldr']}",
        "",
    ]
    for vis in ov.get("visuals") or []:
        if isinstance(vis, dict) and (vis.get("src") or vis.get("figma_url")):
            v_cap = str(vis.get("caption") or vis.get("source_label") or "Big-picture screenshot").strip()
            v_marks = [str(a.get("label")).strip() for a in (vis.get("annotations") or []) if isinstance(a, dict) and a.get("label")]
            mark_note = f" (red marks: {', '.join(v_marks)})" if v_marks else ""
            lines.append(f"- **Visual:** {v_cap}{mark_note}")
    if ov.get("this_pr_part"):
        lines.append(f"- **What we built in this PR:** {ov['this_pr_part']}")
    lines.extend(
        [
            f"- **Before:** {ov['before_state']}",
            f"- **After:** {ov['after_state']}",
            "",
        ]
    )

    for idx, st in enumerate(plan["steps"], start=1):
        lyr = VALID_LAYERS[st["layer"]]
        lines.append(f"## {idx}. {st['title']} ({lyr['short']})")
        lead = str(st.get("explanation") or st.get("why_now") or "").strip()
        if lead:
            lines.append(lead)
        for vis in st.get("visuals") or []:
            if isinstance(vis, dict) and (vis.get("src") or vis.get("figma_url")):
                v_cap = str(vis.get("caption") or vis.get("source_label") or "UI screenshot").strip()
                v_marks = [str(a.get("label")).strip() for a in (vis.get("annotations") or []) if isinstance(a, dict) and a.get("label")]
                mark_note = f" (red marks: {', '.join(v_marks)})" if v_marks else ""
                lines.append(f"- **Visual:** {v_cap}{mark_note}")
        for pt in st.get("points") or []:
            if isinstance(pt, dict) and pt.get("text"):
                anc = f"**{pt['anchor']}:** " if pt.get("anchor") else ""
                lines.append(f"- {anc}{pt['text']}")
        for co in st.get("callouts") or []:
            if isinstance(co, dict) and co.get("text"):
                ckind = VALID_CALLOUT_KINDS.get(str(co.get("kind", "")).lower(), VALID_CALLOUT_KINDS["decision"])
                lines.append(f"- **{ckind['label']}:** {co['text']}")
        for gap in st.get("untested_gaps") or []:
            lines.append(f"- **Missing test coverage:** {gap}")
        for sl in st["_slices"]:
            h = hunk_map[sl["hunk_id"]]
            loc = f"L{sl['ranges'][0][0]}" if sl.get("ranges") else (f"L{h['new_start']}" if not h["is_synthetic"] else "file")
            link = safe_http_url(h.get("deep_link"))
            ref = f"[`{h['file_path']}:{loc}`]({link})" if link else f"`{h['file_path']}:{loc}`"
            lines.append(f"- {ref}")
        lines.append("")

    mech_groups = (plan.get("mechanical") or {}).get("groups") or []
    if mech_groups:
        lines.append(f"## Boilerplate ({stats['total_hunks']}/{stats['total_hunks']} hunks accounted)")
        for grp in mech_groups:
            lines.append(f"- **{grp['title']}:** {grp.get('reason') or 'Mechanical changes'}")
        lines.append("")

    return "\n".join(strip_concept_markup(line) for line in lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate 100% diff coverage and compile the unmaze HTML/Markdown artifact."
    )
    parser.add_argument("--manifest", default=".unmaze/diff-manifest.json")
    parser.add_argument("--plan", default=".unmaze/plan.json")
    parser.add_argument("--assets-dir", help="Optional base directory to resolve relative visual screenshot paths")
    parser.add_argument("--out", default=".unmaze/unmaze-review.html")
    parser.add_argument("--markdown-out", default=None, help="Optional output path for the companion Markdown guide")
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

    search_dirs: list[Path] = []
    if args.assets_dir:
        assets_root = Path(args.assets_dir).resolve()
        search_dirs.append(assets_root)
        if (assets_root / "visuals").is_dir():
            search_dirs.append(assets_root / "visuals")
    plan_dir = plan_path.resolve().parent
    search_dirs.append(plan_dir)
    if (plan_dir / "visuals").is_dir():
        search_dirs.append(plan_dir / "visuals")
    search_dirs.append(manifest_path.resolve().parent)
    search_dirs.append(Path.cwd())

    try:
        validated_plan = validate_and_normalize_plan(manifest, plan, search_dirs=search_dirs)
    except PlanValidationError as exc:
        print(f"Plan validation error:\n{exc}", file=sys.stderr)
        return 2

    html_content = build_html_artifact(manifest, validated_plan, search_dirs=search_dirs)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_content, encoding="utf-8")

    vis_count = validated_plan.get("_total_annotated_visuals", 0)
    vis_msg = f", {vis_count} red-marked screenshot(s)" if vis_count else " (no-UI justification recorded)"
    print(
        f"✓ Verified 100% diff coverage ({manifest['stats']['total_hunks']}/"
        f"{manifest['stats']['total_hunks']} hunks accounted across {len(validated_plan['steps'])} steps{vis_msg})."
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
