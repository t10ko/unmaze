#!/usr/bin/env python3
"""Automated test suite for unmaze diff parser, timeline file evolution, sub-hunk slicing,
Arc chapters, concept modals, visual annotation overlays, and XSS safety.
"""

from __future__ import annotations

import copy
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "unmaze" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import board_registry  # noqa: E402
import build_artifact  # noqa: E402
import github_panel  # noqa: E402
import parse_diff  # noqa: E402
import pr_activity  # noqa: E402
import refresh_plan  # noqa: E402


class UnmazeTestSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.sample_patch = (REPO_ROOT / "examples" / "sample-hairball.patch").read_text(
            encoding="utf-8"
        )
        self.sample_plan = json.loads(
            (REPO_ROOT / "examples" / "sample-plan.json").read_text(encoding="utf-8")
        )

    def _build_sample_manifest(self, patch_text: str | None = None) -> dict:
        pr_url = "https://github.com/acme/payments/pull/142"
        files, hunks = parse_diff.parse_unified_diff(
            patch_text if patch_text is not None else self.sample_patch,
            repo_web_url="https://github.com/acme/payments",
            host_kind="github",
            pr_url=pr_url,
            head_ref="deadbeef",
        )
        hunk_by_id = {h["id"]: h for h in hunks}
        ctx_file = (
            REPO_ROOT / "examples" / "sample-context" / "api" / "routes" / "webhooks.ts"
        ).read_text(encoding="utf-8").splitlines()
        lookup = {"api/routes/webhooks.ts": ctx_file}
        for f in files:
            if f["status"] == "added":
                f["post_lines"] = [
                    ln["content"]
                    for hid in f["hunk_ids"]
                    for ln in hunk_by_id[hid]["lines"]
                    if ln["kind"] in {"add", "ctx"}
                ]
            elif f["path"] in lookup:
                f["post_lines"] = lookup[f["path"]]
        parse_diff.attach_surrounding_context(files, hunks, file_lines_lookup=lookup)
        return {
            "meta": {"title": "Test PR", "pr_url": pr_url},
            "stats": {
                "total_files": len(files),
                "total_hunks": len(hunks),
                "total_additions": sum(f["additions"] for f in files),
                "total_deletions": sum(f["deletions"] for f in files),
            },
            "files": files,
            "hunks": hunks,
        }

    def test_parse_sample_patch_and_github_deep_links(self) -> None:
        manifest = self._build_sample_manifest()
        self.assertEqual(len(manifest["files"]), 8)
        self.assertEqual(len(manifest["hunks"]), 8)
        h2 = manifest["hunks"][1]
        expected_sha = hashlib.sha256(b"api/routes/webhooks.ts").hexdigest()
        self.assertEqual(
            h2["deep_link"],
            f"https://github.com/acme/payments/pull/142/files#diff-{expected_sha}R12",
        )
        # Contiguous post-hunk context starts right at line 38 (no skipped lines)
        self.assertEqual(h2["context_after"][0]["new_line"], 38)

    def test_sub_hunk_slicing_and_cumulative_timeline_evolution(self) -> None:
        manifest = self._build_sample_manifest()
        validated = build_artifact.validate_and_normalize_plan(
            manifest, copy.deepcopy(self.sample_plan)
        )
        self.assertEqual(len(validated["steps"]), 5)
        self.assertEqual(len(validated["chapters"]), 2)

        # Inspect cumulative evolution of `services/idempotency/store.ts` across Step 2 -> Step 3
        store_file = next(f for f in manifest["files"] if f["path"] == "services/idempotency/store.ts")
        store_hunk = next(h for h in manifest["hunks"] if h["id"] == "H007")
        tokens = build_artifact.build_file_timeline_tokens(
            store_file, [store_hunk], validated["_row_step_order"]
        )

        # At Step 2 (`H007:1-34,52`), `markCompleted` and `releaseClaim` do NOT exist in the file yet!
        step2_slice = validated["steps"][1]["_slices"][0]
        rows_at_step2 = build_artifact.evaluate_file_at_step(
            tokens,
            has_full_file=True,
            step_order=2,
            active_keys={(store_hunk["id"], r) for r in step2_slice["row_indices"]},
        )
        step2_text = "\n".join(r["content"] for r in rows_at_step2)
        self.assertEqual(len(rows_at_step2), 35)  # lines 1..34 + closing `}` on line 52!
        self.assertIn("async claimOrSkip", step2_text)
        self.assertNotIn("markCompleted", step2_text)
        self.assertNotIn("releaseClaim", step2_text)

        # At Step 3 (`H007:35-51`), `claimOrSkip` is already merged (`ctx`), and `markCompleted` is active (`add`)!
        step3_slice = validated["steps"][2]["_slices"][0]
        rows_at_step3 = build_artifact.evaluate_file_at_step(
            tokens,
            has_full_file=True,
            step_order=3,
            active_keys={(store_hunk["id"], r) for r in step3_slice["row_indices"]},
        )
        self.assertEqual(len(rows_at_step3), 52)  # full 52-line final file!
        claim_row = next(r for r in rows_at_step3 if "async claimOrSkip" in r["content"])
        mark_row = next(r for r in rows_at_step3 if "async markCompleted" in r["content"])
        self.assertEqual(claim_row["kind"], "ctx")
        self.assertFalse(claim_row["active"])
        self.assertEqual(mark_row["kind"], "add")
        self.assertTrue(mark_row["active"])

    def test_coverage_validator_catches_unclaimed_single_sliced_line_and_overlaps(self) -> None:
        manifest = self._build_sample_manifest()

        # Omit line 35 of H007 (`H007:36-51` instead of `H007:35-51`) -> MUST fail!
        gap_plan = copy.deepcopy(self.sample_plan)
        gap_plan["steps"][2]["hunk_ids"] = ["H007:36-51"]
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_gap:
            build_artifact.validate_and_normalize_plan(manifest, gap_plan)
        self.assertIn("H007", str(ctx_gap.exception))
        self.assertIn("35", str(ctx_gap.exception))

        # Overlap line 34 in both Step 2 and Step 3 -> MUST fail!
        dup_plan = copy.deepcopy(self.sample_plan)
        dup_plan["steps"][2]["hunk_ids"] = ["H007:34-51"]
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_dup:
            build_artifact.validate_and_normalize_plan(manifest, dup_plan)
        self.assertIn("Duplicate hunk/line assignment", str(ctx_dup.exception))

    def test_extract_visual_refs_and_synthetic_hunks(self) -> None:
        body = "Figma: https://www.figma.com/design/abcXYZ/Checkout?node-id=10-20 ![UI](https://example.com/a.png)"
        refs = parse_diff.extract_visual_refs_from_text(body)
        self.assertEqual(len(refs["figma_urls"]), 1)
        self.assertEqual(len(refs["image_urls"]), 1)

        raw_diff = (
            "diff --git a/old.ts b/new.ts\n"
            "similarity index 100%\n"
            "rename from old.ts\n"
            "rename to new.ts\n"
        )
        files, hunks = parse_diff.parse_unified_diff(raw_diff)
        self.assertEqual(len(files), 1)
        self.assertTrue(hunks[0]["is_synthetic"])

    def test_arc_sidebar_concept_modals_visuals_and_xss_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            evil_patch = (
                "diff --git a/xss.ts b/xss.ts\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/xss.ts\n"
                "@@ -0,0 +1,2 @@\n"
                "+const payload = '<script>alert(\"pwned\")</script>';\n"
                "+export default payload;\n"
            )
            patch_file = tmp_path / "evil.patch"
            patch_file.write_text(evil_patch, encoding="utf-8")

            out_dir = tmp_path / "out"
            res_parse = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "parse_diff.py"),
                    "--patch",
                    str(patch_file),
                    "--pr-url",
                    "javascript:alert(1)",
                    "--out-dir",
                    str(out_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_parse.returncode, 0, res_parse.stderr)

            evil_plan = {
                "title": "<img src=x onerror=alert(1)>",
                "concept_candidates_extracted": True,
                "concept_sweep_completed": True,
                "overview": {
                    "tldr": "Touches `AuthCore` and <script>evil()</script>.",
                    "before_state": "before",
                    "after_state": "after",
                    "links": [
                        {"kind": "ticket", "label": "<script>tix()</script>", "url": "https://example.com/browse/X-1"}
                    ],
                    "footprint": [
                        {
                            "path": "xss.ts",
                            "label": "<script>fp()</script>",
                            "change": "Adds <script>chg()</script>.",
                            "entry_points": ["<img src=y onerror=alert(2)>"],
                        }
                    ],
                    "ship_facts": {
                        "schema_migrations": "none",
                        "data_backfills": "none",
                        "config_and_secrets": "Adds <script>ship()</script>.",
                        "feature_flags": "none",
                        "dependencies": "none",
                        "api_contracts": "unknown",
                        "deploy_order": "none",
                    },
                },
                "concepts": [
                    {
                        "id": "auth-core",
                        "term": "AuthCore",
                        "file": "src/auth.ts",
                        "what_it_is": "Handles tokens <script>bad()</script>.",
                        "how_it_works": ["**Sync:** Checks headers."],
                        "related_steps": ["step-1"],
                    }
                ],
                "chapters": [
                    {"id": "ch-1", "title": "Core Chapter", "step_ids": ["step-1"]}
                ],
                "steps": [
                    {
                        "id": "step-1",
                        "layer": "core",
                        "title": "Check `AuthCore` & XSS",
                        "explanation": "Updates `AuthCore` safely.",
                        "table": {
                            "headers": ["Case", "Result"],
                            "rows": [["Valid", "Calls `AuthCore`"]],
                        },
                        "visuals": [
                            {
                                "src": str(REPO_ROOT / "examples" / "sample-webhook-ui.png"),
                                "figma_url": "https://www.figma.com/design/abc/Spec",
                                "caption": "UI caption",
                                "annotations": [
                                    {"kind": "circle", "x": 86, "y": 42, "r": 7, "label": "Badge <xss>"},
                                    {"kind": "arrow", "from_x": 50, "from_y": 25, "to_x": 75, "to_y": 40},
                                ],
                            }
                        ],
                        "hunk_ids": ["H001"],
                    }
                ],
            }
            plan_file = out_dir / "plan.json"
            plan_file.write_text(json.dumps(evil_plan), encoding="utf-8")

            html_out = out_dir / "review.html"
            res_build = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_artifact.py"),
                    "--manifest",
                    str(out_dir / "diff-manifest.json"),
                    "--plan",
                    str(plan_file),
                    "--out",
                    str(html_out),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_build.returncode, 0, res_build.stderr)
            rendered = html_out.read_text(encoding="utf-8")

            # XSS safety
            self.assertNotIn('<script>alert("pwned")</script>', rendered)
            self.assertNotIn("<script>evil()</script>", rendered)
            self.assertNotIn("<script>bad()</script>", rendered)
            self.assertNotIn("<img src=x onerror=alert(1)>", rendered)
            self.assertNotIn("javascript:alert(1)", rendered)
            for payload in ("<script>tix()</script>", "<script>fp()</script>", "<script>chg()</script>",
                            "<img src=y onerror=alert(2)>", "<script>ship()</script>"):
                self.assertNotIn(payload, rendered)

            # Arc sidebar + Concept Modal + Single-spaced diff + SVG overlay
            self.assertIn('class="arc-sidebar"', rendered)
            self.assertIn('data-concept="auth-core"', rendered)
            self.assertIn('id="concept-tpl-auth-core"', rendered)
            self.assertIn('<span class="tok-kw">const</span>', rendered)
            code_match = re.search(r'<code class="diff-code">(.*?)</code>', rendered, re.DOTALL)
            self.assertIsNotNone(code_match)
            self.assertNotIn('</span>\n<span class="dl', code_match.group(1))

    def test_mandatory_red_marked_visual_grounding_gates(self) -> None:
        manifest = self._build_sample_manifest()

        # 1. Zero visuals and no justification -> MUST fail!
        no_vis_plan = copy.deepcopy(self.sample_plan)
        no_vis_plan["overview"].pop("visuals", None)
        for st in no_vis_plan["steps"]:
            st.pop("visuals", None)
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_none:
            build_artifact.validate_and_normalize_plan(manifest, no_vis_plan)
        self.assertIn("Visual Grounding Check FAILED", str(ctx_none.exception))

        # 2. Screenshot attached WITHOUT red marks (`annotations: []`) -> MUST fail!
        raw_shot_plan = copy.deepcopy(self.sample_plan)
        raw_shot_plan["steps"][3]["visuals"][0]["annotations"] = []
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_unmarked:
            build_artifact.validate_and_normalize_plan(manifest, raw_shot_plan)
        self.assertIn("zero red marks", str(ctx_unmarked.exception))

        # 3. Missing screenshot file on disk when search_dirs is enforced -> MUST fail!
        ghost_plan = copy.deepcopy(self.sample_plan)
        ghost_plan["steps"][3]["visuals"][0]["src"] = "does-not-exist-anywhere.png"
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_ghost:
            build_artifact.validate_and_normalize_plan(
                manifest, ghost_plan, search_dirs=[REPO_ROOT / "examples"]
            )
        self.assertIn("was not found on disk", str(ctx_ghost.exception))

        # 4. Headless CLI/SDK escape hatch (`no_visuals_justification`) -> succeeds cleanly and drops visual cards!
        headless_plan = copy.deepcopy(no_vis_plan)
        headless_plan["no_visuals_justification"] = (
            "Checked all callers of `webhooks.ts`; this repo is a headless CLI with zero UI consumers."
        )
        validated = build_artifact.validate_and_normalize_plan(manifest, headless_plan)
        html_out = build_artifact.build_html_artifact(manifest, validated)
        self.assertNotIn('<figure class="visual-card">', html_out)
        self.assertNotIn("headless CLI with zero UI consumers", html_out)

    def test_chapters_naming_overview_no_zero_pronounced_pr_and_two_level_scope_filter(self) -> None:
        manifest = self._build_sample_manifest()
        plan = copy.deepcopy(self.sample_plan)
        # Even if an agent accidentally writes a raw filename in `area`, build_artifact strips extensions!
        plan["file_scopes"]["api/routes/webhooks.ts"] = {
            "surface": "Backend & API",
            "area": "api/routes/webhooks.ts",
        }
        validated = build_artifact.validate_and_normalize_plan(manifest, plan)
        rendered = build_artifact.build_html_artifact(
            manifest, validated, search_dirs=[REPO_ROOT / "examples"]
        )

        # 1. Overview has NO `0` badge
        self.assertNotIn('<span class="arc-ch-num">0</span>', rendered)

        # 2. Chapters are labeled `Chapter 1`, `Chapter 2` (NOT `PR 1`, `PR 2`)
        self.assertIn('<span class="arc-ch-num">Chapter 1</span>', rendered)
        self.assertIn('<span class="arc-ch-num">Chapter 2</span>', rendered)
        self.assertNotIn('<span class="arc-ch-num">PR 1</span>', rendered)
        self.assertIn("Start Chapter 1:", rendered)

        # 3. Pronounced Pull Request CTA in global sticky header (and NO duplicate PR button in sidebar)
        self.assertIn('class="app-topbar"', rendered)
        self.assertLess(rendered.index('class="app-topbar"'), rendered.index('class="canvas-wrap"'))
        self.assertIn('class="pr-cta-btn"', rendered)
        self.assertNotIn('class="pr-rail-pill"', rendered)
        self.assertIn("Open Pull Request", rendered)
        self.assertIn('class="hunk-pr-btn"', rendered)

        # 4. Pronounced 2-level Scope Filter in top bar with zero raw filenames
        self.assertIn('id="scope-filter-btn"', rendered)
        self.assertIn('id="scope-filter-popover"', rendered)
        self.assertIn('data-surface-label="Backend &amp; API"', rendered)
        self.assertIn('data-area-label="Idempotency Store"', rendered)
        # Raw filename `api/routes/webhooks.ts` was cleaned to `"Webhooks"` (no `.ts` in area labels!)
        area_labels = re.findall(r'data-area-label="([^"]+)"', rendered)
        self.assertGreaterEqual(len(area_labels), 4)
        for lbl in area_labels:
            self.assertNotIn(".ts", lbl)
            self.assertNotIn(".sql", lbl)
            self.assertNotIn("/", lbl)

    def test_top_bar_dark_theme_and_click_to_zoom(self) -> None:
        manifest = self._build_sample_manifest()
        validated = build_artifact.validate_and_normalize_plan(
            manifest, copy.deepcopy(self.sample_plan)
        )
        rendered = build_artifact.build_html_artifact(
            manifest, validated, search_dirs=[REPO_ROOT / "examples"]
        )
        topbar = rendered.split('<header class="app-topbar">', 1)[1].split("</header>", 1)[0]
        # The ticket opens from the top bar, next to the PR button; other links stay in the overview.
        self.assertIn('class="ticket-cta-btn" href="https://linear.app/acme/issue/PAY-412"', topbar)
        self.assertLess(topbar.index("ticket-cta-btn"), topbar.index("pr-cta-btn"))
        overview = rendered.split('class="source-links"', 1)[1].split("</nav>", 1)[0]
        self.assertNotIn("PAY-412", overview)
        self.assertIn("Webhook dedup spec", overview)
        # No "All steps" or theme buttons: the guide is dark only.
        self.assertNotIn('id="mode-toggle"', rendered)
        self.assertNotIn('id="theme-toggle"', rendered)
        self.assertNotIn("unmaze-color-scheme", rendered)
        self.assertIn('<meta name="color-scheme" content="dark">', rendered)
        # Screenshots zoom when clicked; there is no separate Zoom button.
        self.assertNotIn('class="vis-zoom-btn"', rendered)
        self.assertIn('class="visual-stage" style="--vis-ar:', rendered)
        self.assertIn('role="button" tabindex="0" aria-label="Zoom', rendered)

    def test_inline_code_notes_and_note_quality_checks(self) -> None:
        manifest = self._build_sample_manifest()
        validated = build_artifact.validate_and_normalize_plan(
            manifest, copy.deepcopy(self.sample_plan)
        )
        rendered = build_artifact.build_html_artifact(
            manifest, validated, search_dirs=[REPO_ROOT / "examples"]
        )
        step2 = rendered.split('id="step-2"', 1)[1].split("</section>", 1)[0]

        # Notes sit inside the diff, right above the first line they explain; no side panel, no pill strip.
        self.assertNotIn('class="hl-side-rail"', rendered)
        self.assertNotIn('class="dl-hl-pill"', rendered)
        self.assertNotIn('class="hl-chip-strip"', rendered)
        self.assertEqual(step2.count('class="hl-note"'), 4)
        first_note = step2.index('class="hl-note"')
        uid = re.search(r'data-hl-trigger="([^"]+)"', step2[first_note:]).group(1)
        first_row = step2.rfind('<span class="dl ', 0, step2.index(f'data-hl-id="{uid}"'))
        self.assertLess(first_note, first_row)
        self.assertNotIn('<span class="dl ', step2[first_note:first_row])
        self.assertIn('<strong class="hl-note-title">Expiry uses this server&#x27;s clock</strong>', step2)
        self.assertIn('<span class="hl-num" aria-hidden="true">4</span>', step2)
        # A note shows its number and title; the explanation opens when the title is clicked.
        self.assertIn('class="hl-note-head" aria-expanded="false"', step2)
        self.assertEqual(step2.count('class="hl-note-text" id="hl-body-'), 4)
        # Each card with notes gets a "Hide notes" toggle.
        self.assertIn('data-hide-label="Hide 4 notes" data-show-label="Show 4 notes"', step2)
        md_out = build_artifact.build_markdown_summary(manifest, validated)
        self.assertIn("- **Note on L16-25 — Claim it, or take over a dead claim:**", md_out)

        # No per-file cap: notes follow the code, one idea each.
        many = copy.deepcopy(self.sample_plan)
        many["steps"][1]["diff_highlights"] = [
            {
                "hunk_id": "H007",
                "lines": str(ln),
                "title": f"Line {ln}",
                "explanation": "Explains one line of the claim query so a reviewer knows exactly why it is there and what breaks without it.",
            }
            for ln in range(16, 23)
        ]
        build_artifact.validate_and_normalize_plan(manifest, many)

        def note_errors(**overrides: str) -> str:
            plan = copy.deepcopy(self.sample_plan)
            plan["steps"][1]["diff_highlights"][1].update(overrides)
            with self.assertRaises(build_artifact.PlanValidationError) as ctx:
                build_artifact.validate_and_normalize_plan(manifest, plan)
            return str(ctx.exception)

        # Too thin: restating the code in a few words says nothing.
        self.assertIn("says too little", note_errors(explanation="Merges the rows and logs it."))
        # Too many sentences, or too long: the block holds more than one idea.
        self.assertIn(
            "has 3 sentences",
            note_errors(explanation="It inserts a claim row. It may take over an expired one. Then it returns the status column so we know."),
        )
        self.assertIn("runs ", note_errors(explanation=" ".join(["word"] * 61) + "."))
        # One idea per note: a block longer than 40 lines must be split.
        self.assertIn("spans 41 lines", note_errors(lines="1-41"))
        # Backticked code with dots doesn't count as extra sentences.
        plan_ok = copy.deepcopy(self.sample_plan)
        plan_ok["steps"][1]["diff_highlights"][1]["explanation"] = (
            "Calls `this.db.query()` once with `ON CONFLICT ... DO UPDATE`, so the claim and the takeover of an expired claim happen in one round trip."
        )
        build_artifact.validate_and_normalize_plan(manifest, plan_ok)

        # Overlapping notes on the same lines are still rejected.
        overlap_plan = copy.deepcopy(self.sample_plan)
        overlap_plan["steps"][1]["diff_highlights"][2]["lines"] = "24-29"
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_ov:
            build_artifact.validate_and_normalize_plan(manifest, overlap_plan)
        self.assertIn("overlaps already-highlighted", str(ctx_ov.exception))

    def test_overview_whole_task_screenshots_and_optional_this_pr_part(self) -> None:
        manifest = self._build_sample_manifest()

        # 1. Partial-PR case: Overview shows whole_task, screenshot, and What we built in this PR
        validated_partial = build_artifact.validate_and_normalize_plan(
            manifest, copy.deepcopy(self.sample_plan), search_dirs=[REPO_ROOT / "examples"]
        )
        rendered_partial = build_artifact.build_html_artifact(
            manifest, validated_partial, search_dirs=[REPO_ROOT / "examples"]
        )
        md_partial = build_artifact.build_markdown_summary(manifest, validated_partial)
        ov_section = rendered_partial.split('id="step-overview"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Big Picture · Payments Admin Console", ov_section)
        self.assertIn('class="this-pr-block"', ov_section)
        self.assertIn("What we built in this PR", ov_section)
        # The PR-specific block now comes right after the whole-task sentence, BEFORE any screenshot.
        self.assertLess(ov_section.index('class="lead-text"'), ov_section.index('class="this-pr-block"'))
        self.assertLess(ov_section.index('class="this-pr-block"'), ov_section.index('class="visual-card"'))
        # Links row sits right under the title.
        self.assertLess(ov_section.index('class="source-links"'), ov_section.index('class="lead-text"'))
        self.assertIn("- **What we built in this PR:**", md_partial)

        # 2. Whole-task PR case: `this_pr_part` omitted -> block stays (footprint + ship facts), no partial-PR lead.
        full_task_plan = copy.deepcopy(self.sample_plan)
        full_task_plan["overview"].pop("this_pr_part", None)
        validated_full = build_artifact.validate_and_normalize_plan(
            manifest, full_task_plan, search_dirs=[REPO_ROOT / "examples"]
        )
        rendered_full = build_artifact.build_html_artifact(
            manifest, validated_full, search_dirs=[REPO_ROOT / "examples"]
        )
        md_full = build_artifact.build_markdown_summary(manifest, validated_full)
        self.assertNotIn('class="this-pr-lead"', rendered_full)
        self.assertIn("What this PR changes", rendered_full)
        self.assertIn('footprint-table', rendered_full)
        self.assertNotIn("What we built in this PR", md_full)

    def test_ship_facts_are_required_and_none_answers_collapse(self) -> None:
        manifest = self._build_sample_manifest()

        missing = copy.deepcopy(self.sample_plan)
        missing["overview"].pop("ship_facts")
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_missing:
            build_artifact.validate_and_normalize_plan(manifest, missing)
        self.assertIn("Ship Facts Check FAILED", str(ctx_missing.exception))

        partial = copy.deepcopy(self.sample_plan)
        partial["overview"]["ship_facts"].pop("deploy_order")
        partial["overview"]["ship_facts"]["coffee"] = "none"
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_partial:
            build_artifact.validate_and_normalize_plan(manifest, partial)
        self.assertIn("`deploy_order`", str(ctx_partial.exception))
        self.assertIn("`overview.ship_facts.coffee` is not a known ship fact", str(ctx_partial.exception))

        plan = copy.deepcopy(self.sample_plan)
        plan["overview"]["ship_facts"]["feature_flags"] = "Unknown"
        validated = build_artifact.validate_and_normalize_plan(manifest, plan)
        rendered = build_artifact.build_html_artifact(manifest, validated, search_dirs=[REPO_ROOT / "examples"])
        md_out = build_artifact.build_markdown_summary(manifest, validated)
        ship = rendered.split('<h3 class="block-heading">Ship facts</h3>', 1)[1].split("</div>", 1)[0]
        # Answers that matter are listed first, one row each; "none" answers collapse into one muted line.
        self.assertEqual(ship.count('class="ship-item is-present"'), 3)
        self.assertEqual(ship.count('class="ship-item is-unknown"'), 1)
        self.assertIn('<p class="ship-none">', ship)
        self.assertIn("Data backfills or one-off scripts · Config, env vars or secrets", ship)
        self.assertLess(ship.index("is-present"), ship.index("is-unknown"))
        self.assertIn("- **Schema migrations:** Adds the", md_out)
        self.assertIn("- **Feature flags:** not checked yet", md_out)
        self.assertIn("- **None:** Data backfills or one-off scripts", md_out)

    def test_source_links_and_change_footprint(self) -> None:
        manifest = self._build_sample_manifest()

        # Links: declared ticket + spec, plus the Figma file a screenshot points at (added once).
        validated = build_artifact.validate_and_normalize_plan(manifest, copy.deepcopy(self.sample_plan))
        links = build_artifact.collect_overview_links(validated)
        self.assertEqual([lk["kind"] for lk in links], ["ticket", "spec", "design"])
        self.assertEqual(sum(1 for lk in links if "figma.com" in lk["url"]), 1)

        bad_link = copy.deepcopy(self.sample_plan)
        bad_link["overview"]["links"][0]["url"] = "javascript:alert(1)"
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_link:
            build_artifact.validate_and_normalize_plan(manifest, bad_link)
        self.assertIn("must be an absolute http(s) URL", str(ctx_link.exception))

        # Footprint: declared prefixes first (in plan order), then auto-detected units by size.
        rows = build_artifact.build_change_footprint(manifest, validated)
        self.assertEqual(
            [r["path"] for r in rows][:3], ["services/idempotency", "api/routes/webhooks.ts", "migrations"]
        )
        self.assertEqual(sum(len(r["files"]) for r in rows), len(manifest["files"]))
        self.assertIn("(repo root)", [r["path"] for r in rows])
        self.assertEqual(build_artifact.detect_deployable_unit("apps/analytics/src/api/v1/x.py"), "apps/analytics")
        self.assertEqual(build_artifact.detect_deployable_unit("docs/plan.md"), "docs")
        self.assertEqual(build_artifact.detect_deployable_unit("README.md"), "(repo root)")

        ghost = copy.deepcopy(self.sample_plan)
        ghost["overview"]["footprint"].append({"path": "apps/nowhere", "change": "Nothing."})
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_ghost:
            build_artifact.validate_and_normalize_plan(manifest, ghost)
        self.assertIn("matches zero changed files", str(ctx_ghost.exception))

    def test_overview_screenshot_cap_gallery_and_before_after_pair(self) -> None:
        manifest = self._build_sample_manifest()
        shot = copy.deepcopy(self.sample_plan["overview"]["visuals"][0])
        shot.pop("figma_url", None)

        # Overview: more than 3 screenshots is rejected (one hero + crops belong in their steps).
        crowded = copy.deepcopy(self.sample_plan)
        crowded["overview"]["visuals"] = [copy.deepcopy(shot) for _ in range(4)]
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_cap:
            build_artifact.validate_and_normalize_plan(manifest, crowded)
        self.assertIn("Overview Focus Check FAILED", str(ctx_cap.exception))

        # 3 screenshots in one step -> gallery with 3 tabs, only the first slide visible.
        gallery_plan = copy.deepcopy(self.sample_plan)
        gallery_plan["steps"][3]["visuals"] = [
            {**copy.deepcopy(shot), "label": f"View {i}"} for i in range(1, 4)
        ]
        validated = build_artifact.validate_and_normalize_plan(
            manifest, gallery_plan, search_dirs=[REPO_ROOT / "examples"]
        )
        rendered = build_artifact.build_html_artifact(manifest, validated, search_dirs=[REPO_ROOT / "examples"])
        step4 = rendered.split('id="step-4"', 1)[1].split("</section>", 1)[0]
        self.assertIn('class="vis-gallery"', step4)
        self.assertEqual(step4.count('class="vis-gallery-tab"'), 3)
        self.assertEqual(step4.count('role="tabpanel"'), 3)
        self.assertEqual(step4.count('role="tabpanel" aria-labelledby="gal-step-4-0-tab">'), 1)
        self.assertEqual(len(re.findall(r'aria-labelledby="gal-step-4-[12]-tab" hidden>', step4)), 2)
        self.assertIn("View 2", step4)
        self.assertIn('<span class="vis-gallery-count" aria-live="polite">1 / 3</span>', step4)
        self.assertNotIn("\x00", rendered)

        # 2 screenshots -> plain stack, no gallery, nothing hidden.
        pair_plan = copy.deepcopy(self.sample_plan)
        pair_plan["steps"][3]["visuals"] = [copy.deepcopy(shot), copy.deepcopy(shot)]
        validated_two = build_artifact.validate_and_normalize_plan(manifest, pair_plan)
        rendered_two = build_artifact.build_html_artifact(manifest, validated_two, search_dirs=[REPO_ROOT / "examples"])
        step4_two = rendered_two.split('id="step-4"', 1)[1].split("</section>", 1)[0]
        self.assertNotIn('class="vis-gallery"', step4_two)
        self.assertEqual(step4_two.count('<figure class="visual-card">'), 2)

        # before/after roles -> side-by-side pair that never joins a gallery.
        compare_plan = copy.deepcopy(self.sample_plan)
        compare_plan["steps"][3]["visuals"] = [
            {**copy.deepcopy(shot), "role": "before"},
            {**copy.deepcopy(shot), "role": "after"},
            copy.deepcopy(shot),
            copy.deepcopy(shot),
        ]
        validated_cmp = build_artifact.validate_and_normalize_plan(manifest, compare_plan)
        rendered_cmp = build_artifact.build_html_artifact(manifest, validated_cmp, search_dirs=[REPO_ROOT / "examples"])
        step4_cmp = rendered_cmp.split('id="step-4"', 1)[1].split("</section>", 1)[0]
        self.assertIn('class="vis-compare"', step4_cmp)
        self.assertIn('vis-role-before', step4_cmp)
        self.assertIn('vis-role-after', step4_cmp)
        self.assertNotIn('class="vis-gallery"', step4_cmp)

        bad_role = copy.deepcopy(self.sample_plan)
        bad_role["steps"][3]["visuals"][0]["role"] = "sideways"
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_role:
            build_artifact.validate_and_normalize_plan(manifest, bad_role)
        self.assertIn(".role` must be one of", str(ctx_role.exception))

    def test_semantic_concept_sweep_cli_gate_new_task_concepts_and_no_flow_strip(self) -> None:
        manifest = self._build_sample_manifest()

        # 1a. Skipping Sweep 1 (`concept_candidates_extracted` missing) MUST fail validation!
        no_s1 = copy.deepcopy(self.sample_plan)
        no_s1.pop("concept_candidates_extracted", None)
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_s1:
            build_artifact.validate_and_normalize_plan(manifest, no_s1)
        self.assertIn("Two-Sweep Concept Pipeline Check FAILED", str(ctx_s1.exception))

        # 1b. Skipping Sweep 2 (`concept_sweep_completed` missing) MUST fail validation!
        un_swept = copy.deepcopy(self.sample_plan)
        un_swept.pop("concept_sweep_completed", None)
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_gate:
            build_artifact.validate_and_normalize_plan(manifest, un_swept)
        self.assertIn("Two-Sweep Concept Pipeline Check FAILED", str(ctx_gate.exception))

        # 1c. Leaving `concepts: []` empty without `no_concepts_justification` MUST fail validation!
        empty_c = copy.deepcopy(self.sample_plan)
        empty_c["concepts"] = []
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_empty:
            build_artifact.validate_and_normalize_plan(manifest, empty_c)
        self.assertIn("Two-Sweep Concept Pipeline Check FAILED", str(ctx_empty.exception))

        # 2. Rendered HTML drops `.flow-strip`, renders `Introduced in this PR`, semantic links, and clean sidebar/md!
        validated = build_artifact.validate_and_normalize_plan(
            manifest, copy.deepcopy(self.sample_plan), search_dirs=[REPO_ROOT / "examples"]
        )
        rendered = build_artifact.build_html_artifact(
            manifest, validated, search_dirs=[REPO_ROOT / "examples"]
        )
        md_out = build_artifact.build_markdown_summary(manifest, validated)

        self.assertNotIn('class="flow-strip"', rendered)
        self.assertIn("Introduced in this PR", rendered)
        self.assertIn("Existing in repo", rendered)
        # Each concept is clickable only at its FIRST mention per step/overview panel; later mentions
        # in the same panel read as plain text so steps don't turn every other phrase into a link.
        self.assertIn("its 60s timer ran out", rendered)
        panels = re.findall(r'<section class="step-panel".*?</section>', rendered, re.DOTALL)
        self.assertGreater(len(panels), 5)
        for panel in panels:
            linked = re.findall(r'class="concept-link[^"]*" data-concept="([^"]+)"', panel)
            self.assertEqual(len(linked), len(set(linked)), f"duplicate concept links in a panel: {linked}")
        self.assertIn('data-concept="claimed-lease"', rendered.split('id="step-2"', 1)[1].split("</section>", 1)[0])
        self.assertNotIn("[[", rendered)
        self.assertNotIn("[[", md_out)

        # 3. Within a single sentence, duplicate concept mentions dedup; across two sentences, both link!
        lookup = {"claimed-lease": "claimed-lease"}
        two_mentions_one_sent = build_artifact.fmt_prose(
            "Grabs [[claimed-lease|a 60s lock]] and frees [[claimed-lease|that lock]]. Next sentence checks [[claimed-lease|expired locks]].",
            lookup,
        )
        self.assertEqual(two_mentions_one_sent.count('data-concept="claimed-lease"'), 2)

        # 4. End-to-end CLI test of separated Sweep 1 (`dump-prose` -> `save-candidates`) -> Sweep 2 (`apply`)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            draft_plan = copy.deepcopy(self.sample_plan)
            draft_plan["concepts"] = []
            draft_plan.pop("concept_candidates_extracted", None)
            draft_plan.pop("concept_sweep_completed", None)
            plan_file = tmp_path / "plan.json"
            plan_file.write_text(json.dumps(draft_plan), encoding="utf-8")

            # Calling `apply` BEFORE Sweep 1 (`save-candidates`) MUST fail with exit code 2!
            early_patch = tmp_path / "early.json"
            early_patch.write_text(json.dumps({"concepts": [], "rewrites": {}}), encoding="utf-8")
            res_early = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "concept_sweep.py"), "apply", "--plan", str(plan_file), "--patch", str(early_patch)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_early.returncode, 2)
            self.assertIn("Sweep 1 has not run yet", res_early.stderr)

            # Sweep 1A: `dump-prose`
            res_dump = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "concept_sweep.py"), "dump-prose", "--plan", str(plan_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_dump.returncode, 0, res_dump.stderr)
            dump_obj = json.loads(res_dump.stdout)
            self.assertIn("overview.whole_task", dump_obj["prose_slots"])

            # Sweep 1B: `save-candidates` filters `keep: true` vs `keep: false`
            cands_file = tmp_path / "candidates.json"
            cands_file.write_text(
                json.dumps(
                    {
                        "candidates": [
                            {"id": "dedup-badge", "term": "200 deduplicated", "origin": "introduced", "keep": True, "reason": "New task status."},
                            {"id": "express", "term": "Express", "origin": "existing", "keep": False, "reason": "Generic framework."},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            res_cands = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "concept_sweep.py"), "save-candidates", "--plan", str(plan_file), "--candidates", str(cands_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_cands.returncode, 0, res_cands.stderr)
            s1_out = json.loads(res_cands.stdout)
            self.assertEqual(s1_out["kept_count"], 1)
            self.assertEqual(s1_out["dropped_noise"], ["Express"])

            # Sweep 2 missing explanation for confirmed `dedup-badge` -> rejected with exit code 2!
            incomplete_patch = tmp_path / "incomplete.json"
            incomplete_patch.write_text(json.dumps({"concepts": [], "rewrites": {}}), encoding="utf-8")
            res_inc = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "concept_sweep.py"), "apply", "--plan", str(plan_file), "--patch", str(incomplete_patch)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_inc.returncode, 2)
            self.assertIn("missing its explanation in Sweep 2", res_inc.stderr)

            # Sweep 2 linking a vague phrase ("the reply") -> rejected; dotted links must name the thing.
            vague_patch = tmp_path / "vague.json"
            vague_patch.write_text(
                json.dumps(
                    {
                        "concepts": [
                            {
                                "id": "dedup-badge",
                                "term": "200 deduplicated",
                                "origin": "introduced",
                                "file": "api/routes/webhooks.ts",
                                "what_it_is": "Short HTTP 200 reply telling the sender we already saw this event.",
                                "how_it_works": ["**Early return:** Sent when the event was already seen."],
                            }
                        ],
                        "rewrites": {"overview.after_state": "Now we send [[dedup-badge|the reply]] right away."},
                    }
                ),
                encoding="utf-8",
            )
            res_vague = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "concept_sweep.py"), "apply", "--plan", str(plan_file), "--patch", str(vague_patch)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_vague.returncode, 2)
            self.assertIn("links the vague phrase `the reply`", res_vague.stderr)

            # Sweep 2 with complete repo-grounded explanation + semantic link -> succeeds!
            good_patch = tmp_path / "good.json"
            good_patch.write_text(
                json.dumps(
                    {
                        "concepts": [
                            {
                                "id": "dedup-badge",
                                "term": "200 deduplicated",
                                "origin": "introduced",
                                "file": "api/routes/webhooks.ts",
                                "what_it_is": "Short HTTP 200 reply telling the sender we already saw this event.",
                                "how_it_works": ["**Early return:** Sent on `webhooks.ts` L26 whenever `decision.action !== 'PROCESS'`."],
                            }
                        ],
                        "rewrites": {
                            "overview.after_state": "Now `webhookRouter` locks the ID first and replies with [[dedup-badge|an instant 200 skip]]."
                        },
                    }
                ),
                encoding="utf-8",
            )
            res_good = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "concept_sweep.py"), "apply", "--plan", str(plan_file), "--patch", str(good_patch)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_good.returncode, 0, res_good.stderr)
            updated_plan = json.loads(plan_file.read_text(encoding="utf-8"))
            self.assertTrue(updated_plan["concept_candidates_extracted"])
            self.assertTrue(updated_plan["concept_sweep_completed"])
            self.assertEqual(len(updated_plan["concepts"]), 1)

    def test_per_line_github_pr_deep_links_across_timeline_slices_and_deletions(self) -> None:
        manifest = self._build_sample_manifest()
        validated = build_artifact.validate_and_normalize_plan(
            manifest, copy.deepcopy(self.sample_plan), search_dirs=[REPO_ROOT / "examples"]
        )
        rendered = build_artifact.build_html_artifact(
            manifest, validated, search_dirs=[REPO_ROOT / "examples"]
        )

        store_sha = hashlib.sha256(b"services/idempotency/store.ts").hexdigest()
        webhooks_sha = hashlib.sha256(b"api/routes/webhooks.ts").hexdigest()

        # 1. In Step 2 (`H007:1-34,52`), closing `}` displays step line `35`, yet links to TRUE GitHub PR line `R52`!
        step2_sec = rendered.split('id="step-2"', 1)[1].split("</section>", 1)[0]
        self.assertIn(
            f'href="https://github.com/acme/payments/pull/142/files#diff-{store_sha}R52" '
            f'target="_blank" rel="noopener noreferrer" title="Open line 52 in PR ↗">35</a>',
            step2_sec,
        )
        # Each code note's line badge (`L14 ↗`) also links straight to its first line (`R14`)
        self.assertIn(
            f'class="hl-note-lines" href="https://github.com/acme/payments/pull/142/files#diff-{store_sha}R14"',
            step2_sec,
        )

        # 2. In Step 4 (`api/routes/webhooks.ts`), deleted old line 21 links to `L21`, and added new line 30 links to `R30`!
        step4_sec = rendered.split('id="step-4"', 1)[1].split("</section>", 1)[0]
        self.assertIn(
            f'class="row-pr-jump" href="https://github.com/acme/payments/pull/142/files#diff-{webhooks_sha}L21"',
            step4_sec,
        )
        self.assertIn(
            f'class="row-pr-jump" href="https://github.com/acme/payments/pull/142/files#diff-{webhooks_sha}R30"',
            step4_sec,
        )

        # 3. Regression guard: `.steps-stack` must NOT use flex `gap` with `hidden="until-found"` (prevents N * gap top drift!)
        steps_stack_css = re.search(r"\.steps-stack\s*\{([^}]*)\}", rendered)
        self.assertIsNotNone(steps_stack_css)
        self.assertNotIn("gap:", steps_stack_css.group(1))
        self.assertIn(".step-panel:not([hidden]) ~ .step-panel:not([hidden])", rendered)


    def _edited_sample_patch(self) -> str:
        """Sample PR after a follow-up push: one line inserted in the store, one brand-new docs file."""
        patch = self.sample_patch
        patch = patch.replace(
            "@@ -0,0 +1,52 @@\n+import type { ClaimDecision",
            "@@ -0,0 +1,53 @@\n+import type { ClaimDecision",
        )
        patch = patch.replace(
            "+    this.leaseTtlMs = options.leaseTtlMs ?? 60_000;\n",
            "+    this.leaseTtlMs = options.leaseTtlMs ?? 60_000;\n"
            "+    this.leaseTtlMs = Math.max(this.leaseTtlMs, 1_000);\n",
        )
        docs = (
            "diff --git a/docs/dedup.md b/docs/dedup.md\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/docs/dedup.md\n"
            "@@ -0,0 +1,2 @@\n"
            "+# Webhook dedup\n"
            "+Retries within 60s are skipped.\n"
        )
        return patch.replace("diff --git a/index.ts b/index.ts", docs + "diff --git a/index.ts b/index.ts", 1)

    def test_refresh_plan_carries_steps_over_to_a_newer_diff(self) -> None:
        old_manifest = self._build_sample_manifest()
        new_manifest = self._build_sample_manifest(self._edited_sample_patch())
        new_ids = {h["file_path"]: h["id"] for h in new_manifest["hunks"]}
        self.assertEqual(new_ids["docs/dedup.md"], "H004")
        self.assertEqual(new_ids["services/idempotency/store.ts"], "H008")

        refreshed, report = refresh_plan.refresh_plan(old_manifest, new_manifest, self.sample_plan)
        by_id = {st["id"]: st for st in refreshed["steps"]}

        # Renumbered hunks follow their lines; the inserted line shifts later slices by one.
        self.assertEqual(by_id["step-1"]["hunk_ids"], ["H006", "H009"])
        self.assertEqual(by_id["step-2"]["hunk_ids"], ["H008:1-35,53"])
        self.assertEqual(by_id["step-3"]["hunk_ids"], ["H008:36-52"])
        self.assertEqual(by_id["step-4"]["hunk_ids"], ["H003", "H002"])
        self.assertEqual(refreshed["mechanical"]["groups"][0]["hunk_ids"], ["H005", "H007"])
        self.assertEqual(
            [(h["hunk_id"], h["lines"]) for h in by_id["step-2"]["diff_highlights"]],
            [("H008", "15"), ("H008", "17-26"), ("H008", "27-30"), ("H008", "31-34")],
        )
        self.assertEqual(report["auto_assigned"], {"step-2": 1})
        self.assertEqual(report["dropped_lines"], 0)
        self.assertEqual([u["hunk_id"] for u in report["unclaimed_hunks"]], ["H004"])
        self.assertIn("step-2", report["steps_to_reread"])
        self.assertNotIn("step-4", report["steps_to_reread"])
        # The original plan object is untouched.
        self.assertEqual(self.sample_plan["steps"][1]["hunk_ids"], ["H007:1-34,52"])

        # The builder then flags only the brand-new docs hunk...
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_new:
            build_artifact.validate_and_normalize_plan(new_manifest, copy.deepcopy(refreshed))
        self.assertIn("H004", str(ctx_new.exception))
        self.assertNotIn("H008", str(ctx_new.exception))

        # ...and passes once the agent places it.
        refreshed["mechanical"]["groups"][0]["hunk_ids"].append("H004")
        validated = build_artifact.validate_and_normalize_plan(new_manifest, refreshed)
        self.assertEqual(len(validated["steps"]), 5)

    def test_board_registry_records_boards_and_hook_nudges_once_per_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args], cwd=repo, check=True, capture_output=True, text=True
                ).stdout.strip()

            git("init", "-q", "-b", "main")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "T")
            (repo / "a.txt").write_text("one\n")
            git("add", ".")
            git("commit", "-q", "-m", "one")
            git("checkout", "-q", "-b", "feat/board")
            built_sha = git("rev-parse", "HEAD")

            out = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "board_registry.py"), "work-dir", "--repo", str(repo), "--key", "pr-7"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            work_dir = Path(out)
            self.assertTrue(work_dir.is_dir())
            self.assertIn(str(Path(".git") / "unmaze" / "boards" / "pr-7"), out)
            (work_dir / "diff-manifest.json").write_text(json.dumps({
                "meta": {"head_branch": "feat/board", "head_sha": built_sha,
                         "refresh_args": ["--pr", "https://github.com/o/r/pull/7"],
                         "pr_url": "https://github.com/o/r/pull/7"}
            }))
            subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "board_registry.py"), "record", "--repo", str(repo),
                 "--work-dir", str(work_dir), "--url", "https://claude.ai/artifact/abc"],
                capture_output=True, text=True, check=True,
            )

            def run_hook(payload: dict) -> str:
                return subprocess.run(
                    [sys.executable, str(SCRIPTS_DIR / "board_registry.py"), "hook"],
                    input=json.dumps({"cwd": str(repo), **payload}),
                    capture_output=True, text=True, check=True,
                ).stdout.strip()

            push = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "git push -u origin feat/board"}}
            # Board is fresh -> no nudge.
            self.assertEqual(run_hook(push), "")

            (repo / "a.txt").write_text("two\n")
            git("commit", "-q", "-am", "two")
            # Non-push commands never nudge.
            self.assertEqual(run_hook({**push, "tool_input": {"command": "git status"}}), "")
            nudge = json.loads(run_hook(push))
            ctx = nudge["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(nudge["hookSpecificOutput"]["hookEventName"], "PostToolUse")
            self.assertIn("https://claude.ai/artifact/abc", ctx)
            self.assertIn("--refresh", ctx)
            self.assertIn("1 new commit", ctx)
            # Pushing the same commit again doesn't nudge twice...
            self.assertEqual(run_hook(push), "")
            # ...but a new session (or a compacted one) that lost the nudge is reminded again.
            self.assertIn("1 new commit", run_hook({"hook_event_name": "SessionStart"}))

            # Pausing stops nudges for later commits.
            subprocess.run([sys.executable, str(SCRIPTS_DIR / "board_registry.py"), "pause", "--repo", str(repo)],
                           capture_output=True, text=True, check=True)
            (repo / "a.txt").write_text("three\n")
            git("commit", "-q", "-am", "three")
            self.assertEqual(run_hook(push), "")
            subprocess.run([sys.executable, str(SCRIPTS_DIR / "board_registry.py"), "resume", "--repo", str(repo)],
                           capture_output=True, text=True, check=True)
            self.assertIn("2 new commits", run_hook({"hook_event_name": "SessionStart"}))

            # Outside any git repo the hook stays silent.
            self.assertEqual(
                subprocess.run(
                    [sys.executable, str(SCRIPTS_DIR / "board_registry.py"), "hook"],
                    input=json.dumps({"cwd": tmp, **push}), capture_output=True, text=True, check=True,
                ).stdout.strip(),
                "",
            )

    def test_parse_diff_detects_ticket_links(self) -> None:
        body = (
            "Fixes https://acme.atlassian.net/browse/PAY-412, see https://linear.app/acme/issue/PAY-9 "
            "and https://github.com/acme/payments/issues/12. Not https://github.com/acme/payments/pull/9."
        )
        self.assertEqual(
            parse_diff.extract_ticket_links_from_text(body),
            [
                "https://acme.atlassian.net/browse/PAY-412",
                "https://linear.app/acme/issue/PAY-9",
                "https://github.com/acme/payments/issues/12",
            ],
        )
        self.assertTrue(board_registry.PUSH_CMD_RE.search("cd x && git push --force-with-lease"))
        self.assertFalse(board_registry.PUSH_CMD_RE.search('git commit -m "push it"'))

    def test_push_detection_handles_quoted_git_options(self) -> None:
        pushes = [
            'git -c credential.helper= -c "credential.helper=!f() { echo username=x-access-token; '
            'echo \\"password=$(gh auth token)\\"; }; f" -c lfs.url=https://github.com/o/r.git/info/lfs '
            "push --force-with-lease=feat:abc1234 https://github.com/o/r.git HEAD:feat",
            "git -c user.name='T A' push",
            'git -c http.extraHeader="Authorization: Bearer x" push origin feat',
            'GIT_SSH_COMMAND="ssh -i ~/.ssh/k -o IdentitiesOnly=yes" git push -u origin feat',
        ]
        for command in pushes:
            self.assertTrue(board_registry.PUSH_CMD_RE.search(command), command)
        for command in ['git -c "alias.x=push y" status', 'git log --grep "push it"']:
            self.assertFalse(board_registry.PUSH_CMD_RE.search(command), command)

    def test_board_registry_follows_pushes_to_a_url(self) -> None:
        # `git push <url> HEAD:<branch>` updates the remote but not the remote-tracking ref, so
        # `@{upstream}` keeps pointing at the commit the board was first built from.
        with tempfile.TemporaryDirectory() as tmp:
            remote = Path(tmp) / "remote.git"
            repo = Path(tmp) / "repo"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            repo.mkdir()

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args], cwd=repo, check=True, capture_output=True, text=True
                ).stdout.strip()

            def registry(*args: str) -> str:
                return subprocess.run(
                    [sys.executable, str(SCRIPTS_DIR / "board_registry.py"), *args, "--repo", str(repo)],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()

            def record(head_sha: str) -> None:
                (work_dir / "diff-manifest.json").write_text(json.dumps({
                    "meta": {"head_branch": "feat", "head_sha": head_sha,
                             "refresh_args": ["--pr", "https://github.com/o/r/pull/7"]}
                }))
                registry("record", "--work-dir", str(work_dir), "--url", "https://claude.ai/artifact/abc")

            def run_hook(payload: dict) -> str:
                return subprocess.run(
                    [sys.executable, str(SCRIPTS_DIR / "board_registry.py"), "hook"],
                    input=json.dumps({"cwd": str(repo), **payload}),
                    capture_output=True, text=True, check=True,
                ).stdout.strip()

            git("init", "-q", "-b", "feat")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "T")
            (repo / "a.txt").write_text("one\n")
            git("add", ".")
            git("commit", "-q", "-m", "one")
            git("remote", "add", "origin", str(remote))
            git("push", "-q", "-u", "origin", "feat")
            work_dir = Path(registry("work-dir", "--key", "pr-7"))
            record(git("rev-parse", "HEAD"))

            # The same repository under a URL that differs from origin's, like an HTTPS push to a
            # remote configured over SSH: git then leaves origin's tracking ref alone.
            other_url = Path(tmp) / "same-remote.git"
            other_url.symlink_to(remote)

            def push_to_url() -> dict:
                command = (
                    'git -c credential.helper= -c "credential.helper=!f() { echo username=x; '
                    f'echo \\"password=y\\"; }}; f" push -q {other_url} HEAD:feat'
                )
                subprocess.run(command, shell=True, cwd=repo, check=True, capture_output=True)
                return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": command}}

            (repo / "a.txt").write_text("two\n")
            git("commit", "-q", "-am", "two")
            push = push_to_url()
            self.assertNotEqual(git("rev-parse", "@{upstream}"), git("rev-parse", "HEAD"))
            self.assertIn("1 new commit", run_hook(push))

            # The board is refreshed and recorded at the pushed commit: nothing is stale anymore,
            # even though the remote-tracking ref still points at the first commit.
            record(git("rev-parse", "HEAD"))
            self.assertFalse(json.loads(registry("status"))["stale"])
            self.assertEqual(run_hook({"hook_event_name": "SessionStart"}), "")

            # A nudge the session never acted on is repeated at the next session start.
            (repo / "a.txt").write_text("three\n")
            git("commit", "-q", "-am", "three")
            self.assertIn("1 new commit", run_hook(push_to_url()))
            self.assertIn("1 new commit", run_hook({"hook_event_name": "SessionStart"}))

    def test_step_budget_rejects_over_split_reviews_with_concrete_hints(self) -> None:
        manifest = self._build_sample_manifest()
        budget, prod_lines = build_artifact.compute_step_budget(manifest)
        self.assertEqual(budget, build_artifact.STEP_BUDGET_MIN)
        self.assertLess(prod_lines, 150)
        # Tests and lockfiles don't count toward the budget.
        self.assertEqual(build_artifact.hunk_review_kind({"file_path": "a/b.test.ts", "category_hint": "test"}), "test")
        self.assertEqual(build_artifact.hunk_review_kind({"file_path": "docs/plan.md", "category_hint": "core"}), "docs")
        self.assertEqual(build_artifact.hunk_review_kind({"file_path": "docs/decisions.tsv", "category_hint": "core"}), "docs")
        self.assertEqual(build_artifact.hunk_review_kind({"file_path": "src/app.ts", "category_hint": "core"}), "code")

        # Split the sample into 6 steps (one over the budget of 5) -> rejected with merge hints.
        over = copy.deepcopy(self.sample_plan)
        step2 = next(st for st in over["steps"] if st["id"] == "step-2")
        step2["hunk_ids"] = ["H007:1-20"]
        extra = {
            "id": "step-2b",
            "layer": "core",
            "title": "Rest of the claim check",
            "explanation": "The rest of the claim check.",
            "hunk_ids": ["H007:21-34,52"],
        }
        step2.pop("diff_highlights", None)
        over["steps"].insert(2, extra)
        over["chapters"][0]["step_ids"].append("step-2b")
        with self.assertRaises(build_artifact.PlanValidationError) as ctx:
            build_artifact.validate_and_normalize_plan(manifest, over)
        msg = str(ctx.exception)
        self.assertIn("Step Budget Check FAILED — 6 steps", msg)
        self.assertIn("(budget: 5;", msg)
        self.assertIn("tests-only steps: step-5", msg)

        # A big PR gets a bigger budget, capped at 12.
        big = {"hunks": [{"file_path": "src/x.ts", "category_hint": "core", "additions": 5000, "deletions": 0}]}
        self.assertEqual(build_artifact.compute_step_budget(big)[0], build_artifact.STEP_BUDGET_MAX)
        mid = {"hunks": [{"file_path": "src/x.ts", "category_hint": "core", "additions": 600, "deletions": 0}]}
        self.assertEqual(build_artifact.compute_step_budget(mid)[0], 7)

    def test_docs_hint_appendix_label_and_spacing(self) -> None:
        self.assertEqual(parse_diff.classify_file_hint("docs/analytics/plan.md"), "docs")
        self.assertEqual(parse_diff.classify_file_hint("docs/decisions.tsv"), "docs")
        self.assertEqual(parse_diff.classify_file_hint("README.md"), "docs")
        self.assertEqual(parse_diff.classify_file_hint("src/routes/users.ts"), "wiring")

        manifest = self._build_sample_manifest()
        validated = build_artifact.validate_and_normalize_plan(manifest, copy.deepcopy(self.sample_plan))
        rendered = build_artifact.build_html_artifact(manifest, validated, search_dirs=[REPO_ROOT / "examples"])
        md_out = build_artifact.build_markdown_summary(manifest, validated)
        self.assertIn('<span class="arc-tab-label">Appendix (', rendered)
        self.assertIn("Appendix · Docs, Records &amp; Boilerplate", rendered)
        self.assertNotIn(">Boilerplate (", rendered)
        self.assertIn("## Appendix: docs, records & boilerplate", md_out)
        # Readable line length and spacing for prose blocks.
        self.assertRegex(rendered, r"\.anchored-list \{[^}]*max-width: 64ch;[^}]*gap: 0\.75rem;")
        self.assertRegex(rendered, r"\.lead-text \{[^}]*max-width: 62ch;")

    def test_diagram_decision_worth_it_and_grounding_gates(self) -> None:
        manifest = self._build_sample_manifest()

        # 1. Several code parts changed -> the plan must decide: draw it, or say why not.
        undecided = copy.deepcopy(self.sample_plan)
        undecided["overview"].pop("diagram")
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_dec:
            build_artifact.validate_and_normalize_plan(manifest, undecided)
        self.assertIn("Diagram Decision Check FAILED", str(ctx_dec.exception))
        undecided["overview"]["no_diagram_reason"] = "Each part changes on its own; no calls between them changed."
        validated = build_artifact.validate_and_normalize_plan(manifest, undecided)
        rendered = build_artifact.build_html_artifact(manifest, validated, search_dirs=[REPO_ROOT / "examples"])
        self.assertNotIn('class="this-pr-section dg-block"', rendered)
        self.assertNotIn("no calls between them changed", rendered)

        # 2. Not worth a picture: too few parts and changed calls -> rejected.
        tiny = copy.deepcopy(self.sample_plan)
        tiny["overview"]["diagram"] = {
            "nodes": [
                {"id": "route", "label": "Webhook route", "status": "changed"},
                {"id": "store", "label": "Idempotency store", "status": "new"},
            ],
            "edges": [
                {"from": "route", "to": "store", "status": "new", "label": "claimOrSkip()", "evidence": "api/routes/webhooks.ts:24"}
            ],
        }
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_tiny:
            build_artifact.validate_and_normalize_plan(manifest, tiny)
        self.assertIn("Diagram Worth-It Check FAILED", str(ctx_tiny.exception))
        # ...unless the one new link is asynchronous (job, queue, event, webhook).
        tiny["overview"]["diagram"]["edges"][0]["async"] = True
        build_artifact.validate_and_normalize_plan(manifest, tiny)

        # 3. Every new/changed/removed call must point at a changed file.
        ungrounded = copy.deepcopy(self.sample_plan)
        ungrounded["overview"]["diagram"]["edges"][1].pop("evidence")
        ungrounded["overview"]["diagram"]["edges"][2]["evidence"] = "somewhere/else.ts:3"
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_ev:
            build_artifact.validate_and_normalize_plan(manifest, ungrounded)
        self.assertIn("has no `evidence`", str(ctx_ev.exception))
        self.assertIn("is not a file this PR changes", str(ctx_ev.exception))

        # 4. Crowded pictures are rejected (max 6 parts).
        crowded = copy.deepcopy(self.sample_plan)
        crowded["overview"]["diagram"]["nodes"] += [
            {"id": f"x{i}", "label": f"Extra {i}", "status": "unchanged"} for i in range(2)
        ]
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_big:
            build_artifact.validate_and_normalize_plan(manifest, crowded)
        self.assertIn("Diagram Size Check FAILED", str(ctx_big.exception))

    def test_diagram_renders_as_diff_or_plain_with_grounded_calls(self) -> None:
        manifest = self._build_sample_manifest()
        validated = build_artifact.validate_and_normalize_plan(manifest, copy.deepcopy(self.sample_plan))
        rendered = build_artifact.build_html_artifact(manifest, validated, search_dirs=[REPO_ROOT / "examples"])
        md_out = build_artifact.build_markdown_summary(manifest, validated)
        ov = rendered.split('id="step-overview"', 1)[1].split("</section>", 1)[0]

        # Diff mode: existing parts are gray, new green, changed amber; legend shows only what's used.
        self.assertEqual(validated["overview"]["_diagram"]["mode"], "diff")
        self.assertIn("How the parts connect", ov)
        self.assertLess(ov.index("footprint-table"), ov.index("How the parts connect"))
        self.assertLess(ov.index("How the parts connect"), ov.index("Ship facts"))
        self.assertIn('class="dg-node dg-node-changed"', ov)
        self.assertIn('class="dg-node dg-node-new"', ov)
        self.assertIn('class="dg-node dg-node-unchanged"', ov)
        self.assertIn('dg-key-new', ov)
        self.assertNotIn('dg-key-removed', ov)
        # Each drawn call is listed with a link to the exact changed line.
        store_sha = hashlib.sha256(b"api/routes/webhooks.ts").hexdigest()
        self.assertIn(f"https://github.com/acme/payments/pull/142/files#diff-{store_sha}R24", ov)
        self.assertIn('role="img"', ov)
        # GitHub-renderable Mermaid in the Markdown summary.
        self.assertIn("```mermaid", md_out)
        self.assertIn('route -->|"new: claimOrSkip()"| store', md_out)
        self.assertIn("class store,table new", md_out)

        # Removed calls are red, dashed and struck through.
        removed = copy.deepcopy(self.sample_plan)
        removed["overview"]["diagram"]["edges"].append(
            {"from": "providers", "to": "dispatcher", "status": "removed", "label": "direct dispatch", "evidence": "api/routes/webhooks.ts:30"}
        )
        v_removed = build_artifact.validate_and_normalize_plan(manifest, removed)
        r_removed = build_artifact.build_html_artifact(manifest, v_removed, search_dirs=[REPO_ROOT / "examples"])
        self.assertIn('class="dg-edge dg-edge-removed"', r_removed)
        self.assertIn('class="dg-call-label is-removed"', r_removed)
        self.assertIn("dg-key-removed", r_removed)

        # All parts new -> plain diagram: no change colors, no legend, no status badges.
        plain = copy.deepcopy(self.sample_plan)
        for node in plain["overview"]["diagram"]["nodes"]:
            node["status"] = "new"
        for edge in plain["overview"]["diagram"]["edges"]:
            edge["status"] = "new"
            edge.setdefault("evidence", "api/routes/webhooks.ts:17")
        v_plain = build_artifact.validate_and_normalize_plan(manifest, plain)
        self.assertEqual(v_plain["overview"]["_diagram"]["mode"], "plain")
        r_plain = build_artifact.build_html_artifact(manifest, v_plain, search_dirs=[REPO_ROOT / "examples"])
        ov_plain = r_plain.split('id="step-overview"', 1)[1].split("</section>", 1)[0]
        self.assertIn('class="dg-node dg-node-plain"', ov_plain)
        self.assertNotIn("dg-node-new", ov_plain)
        self.assertNotIn('class="dg-legend"', ov_plain)
        self.assertNotIn("dg-badge-new", ov_plain)

    def test_diagram_layout_never_overlaps_boxes_or_crosses_them(self) -> None:
        diagram = {
            "mode": "diff",
            "nodes": [
                {"id": "tab", "label": "Report tab", "note": "not built yet", "status": "planned"},
                {"id": "api", "label": "analytics", "note": "+ 2 report routes", "status": "changed"},
                {"id": "a", "label": "issues", "note": "+ issue-rows route", "status": "changed"},
                {"id": "b", "label": "entities", "note": "+ rule-counts route", "status": "changed"},
                {"id": "c", "label": "integrations", "note": "unchanged", "status": "unchanged"},
                {"id": "db", "label": "metrics table", "note": "daily counts", "status": "unchanged"},
            ],
            "edges": [
                {"from": "tab", "to": "api", "status": "planned", "label": "GET"},
                {"from": "api", "to": "a", "status": "new", "label": "GET"},
                {"from": "api", "to": "b", "status": "new", "label": "POST"},
                {"from": "api", "to": "c", "status": "new", "label": "GET"},
                {"from": "b", "to": "db", "status": "new", "label": "SELECT"},
                {"from": "tab", "to": "db", "status": "removed", "label": "old direct read"},
                {"from": "db", "to": "api", "status": "unchanged", "label": "back edge"},
            ],
        }
        lay = build_artifact.layout_diagram(diagram)
        rects = lay["pos"]
        ids = list(rects)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                ax, ay, aw, ah = rects[ids[i]]
                bx, by, bw, bh = rects[ids[j]]
                self.assertFalse(ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah, (ids[i], ids[j]))
        for item in lay["paths"]:
            edge = item["edge"]
            pts = [tuple(map(float, m)) for m in re.findall(r"([\d.]+) ([\d.]+)", item["d"])]
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                for nid, (rx, ry, rw, rh) in rects.items():
                    if nid in (edge["from"], edge["to"]):
                        continue
                    lo_x, hi_x = sorted((x1, x2))
                    lo_y, hi_y = sorted((y1, y2))
                    crosses = lo_x < rx + rw and hi_x > rx and lo_y < ry + rh and hi_y > ry
                    self.assertFalse(crosses, f"{edge['from']}->{edge['to']} crosses {nid}")
            self.assertTrue(all(0 <= x <= lay["width"] and 0 <= y <= lay["height"] for x, y in pts))

    def test_pr_activity_snapshot_normalizes_reviews_checks_and_threads(self) -> None:
        graphql = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 142,
                        "state": "OPEN",
                        "isDraft": False,
                        "reviewDecision": "CHANGES_REQUESTED",
                        "headRefOid": "b" * 40,
                        "latestReviews": {"nodes": [
                            {"author": {"login": "maya"}, "state": "CHANGES_REQUESTED", "submittedAt": "2026-01-02T00:00:00Z", "url": "https://github.com/a/b/pull/142#r1"},
                            {"author": {"login": "devon"}, "state": "APPROVED", "submittedAt": "2026-01-01T00:00:00Z", "url": "javascript:alert(1)"},
                        ]},
                        "reviewRequests": {"nodes": [
                            {"requestedReviewer": {"__typename": "Team", "slug": "payments"}},
                            {"requestedReviewer": {"__typename": "User", "login": "sam"}},
                        ]},
                        "reviews": {"nodes": [
                            {"author": {"login": "maya"}, "state": "COMMENTED", "body": "", "submittedAt": "2026-01-01T01:00:00Z", "url": None},
                            {"author": {"login": "maya"}, "state": "CHANGES_REQUESTED", "body": "Two asks", "submittedAt": "2026-01-02T00:00:00Z", "url": None},
                        ]},
                        "comments": {"nodes": [{"author": None, "body": "hi", "createdAt": "2026-01-01T02:00:00Z", "url": None}]},
                        "reviewThreads": {"pageInfo": {"hasNextPage": True}, "nodes": [
                            {"id": "T1", "path": "services/idempotency/store.ts", "line": 22, "startLine": 19, "originalLine": 22,
                             "diffSide": "RIGHT", "isResolved": False, "isOutdated": False, "subjectType": "LINE",
                             "comments": {"nodes": [{"author": {"login": "maya"}, "body": "x" * 5000, "createdAt": "2026-01-02T00:00:00Z", "url": None}]}},
                            {"id": "T2", "path": "a.ts", "line": None, "diffSide": "LEFT", "isResolved": True, "isOutdated": True,
                             "comments": {"nodes": []}},
                        ]},
                        "commits": {"nodes": [{"commit": {"oid": "c" * 40, "statusCheckRollup": {"state": "FAILURE", "contexts": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [
                                {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "https://ci/1",
                                 "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}}},
                                {"__typename": "CheckRun", "name": "e2e", "status": "IN_PROGRESS", "conclusion": None, "detailsUrl": None},
                                {"__typename": "CheckRun", "name": "deploy", "status": "COMPLETED", "conclusion": "SKIPPED", "detailsUrl": None},
                                {"__typename": "StatusContext", "context": "legacy/ci", "state": "ERROR", "targetUrl": "https://ci/2"},
                            ],
                        }}}}]},
                    }
                }
            }
        }
        snap = pr_activity.normalize_graphql(graphql, fetched_at="2026-01-03T00:00:00Z")
        self.assertEqual(snap["review_decision"], "CHANGES_REQUESTED")
        self.assertEqual([r["author"] for r in snap["reviews"]], ["maya", "devon"])
        self.assertIsNone(snap["reviews"][1]["url"], "non-https links are dropped")
        self.assertEqual(snap["requested_reviewers"], ["team:payments", "sam"])
        self.assertEqual(snap["checks"]["summary"], {"passed": 1, "failed": 1, "pending": 1, "skipped": 1, "total": 4})
        self.assertEqual(snap["checks"]["items"][0]["state"], "failed", "failed checks sort first")
        self.assertEqual(snap["head_sha"], "c" * 40)
        self.assertEqual(len(snap["threads"]), 1, "threads without comments are dropped")
        self.assertTrue(snap["threads_truncated"])
        self.assertLessEqual(len(snap["threads"][0]["comments"][0]["body"]), pr_activity.MAX_BODY_CHARS)
        kinds = [(c["kind"], c["author"]) for c in snap["conversation"]]
        self.assertEqual(kinds, [("comment", "ghost"), ("review", "maya")], "empty COMMENTED review wrappers are skipped")

        self.assertEqual(
            pr_activity.parse_pr_url("https://github.com/acme/payments/pull/142/files"),
            {"host": "github.com", "owner": "acme", "repo": "payments", "number": 142,
             "url": "https://github.com/acme/payments/pull/142"},
        )
        self.assertIsNone(pr_activity.parse_pr_url("https://gitlab.com/a/b/-/merge_requests/1"))

        def failing_runner(*_args, **_kwargs):
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="auth")

        self.assertIsNone(pr_activity.fetch_pr_activity("https://github.com/a/b/pull/1", runner=failing_runner))
        self.assertIsNone(pr_activity.fetch_pr_activity("not a url", runner=failing_runner))

    def test_github_review_status_renders_header_threads_and_markdown(self) -> None:
        manifest = self._build_sample_manifest()
        validated = build_artifact.validate_and_normalize_plan(
            manifest, copy.deepcopy(self.sample_plan), search_dirs=[REPO_ROOT / "examples"]
        )
        activity = json.loads((REPO_ROOT / "examples" / "sample-activity.json").read_text(encoding="utf-8"))
        activity["threads"][0]["comments"][0]["body"] = "</script><img src=x onerror=alert(1)>"
        activity["checks"]["items"].append({"name": "bad", "state": "exploded", "url": None, "group": ""})
        cfg = github_panel.build_github_config(manifest["meta"], activity)
        self.assertEqual(cfg["pr"], {"owner": "acme", "repo": "payments", "number": 142,
                                     "url": "https://github.com/acme/payments/pull/142", "host": "github.com", "head_sha": None})
        self.assertEqual(cfg["snapshot"]["checks"]["summary"]["total"], 8, "unknown check states are dropped")
        self.assertTrue(cfg["live_supported"])

        html_out = build_artifact.build_html_artifact(manifest, validated, github_cfg=cfg)
        self.assertIn('id="gh-status"', html_out)
        self.assertIn('id="gh-dialog"', html_out)
        self.assertIn('<script type="application/json" id="gh-data">', html_out)
        data = re.search(r'<script type="application/json" id="gh-data">(.*?)</script>', html_out, re.S).group(1)
        self.assertNotIn("</script", data.lower())
        self.assertEqual(json.loads(data)["snapshot"]["threads"][0]["comments"][0]["body"],
                         "</script><img src=x onerror=alert(1)>")
        self.assertIn("Not connected to GitHub", html_out)
        self.assertIn("Copy & open line on GitHub", html_out)
        # Cards and rows carry what the page needs to place threads on the right lines.
        self.assertIn('data-path="services/idempotency/store.ts"', html_out)
        self.assertRegex(html_out, r'<span class="dl dl-add[^"]*" data-pn="22"')
        self.assertRegex(html_out, r'<span class="dl dl-del[^"]*" data-po="21"')

        md = build_artifact.build_markdown_summary(manifest, validated, cfg)
        self.assertIn("## Review status", md)
        self.assertIn("- **Status:** Changes requested", md)
        self.assertIn("- **Checks:** 5 passed, 1 failed, 1 running, 1 skipped", md)
        self.assertIn("- **Open comment threads:** 2 (resolved: 1)", md)

        caps = github_panel.github_capabilities()
        self.assertEqual(caps["mcp"]["servers"][0]["server"], "GitHub")
        self.assertIn("add_comment_to_pending_review", caps["mcp"]["servers"][0]["tools"])

        # A board that isn't about a GitHub PR gets none of it; a PR without a snapshot still gets the header.
        self.assertIsNone(github_panel.build_github_config({"pr_url": None}, activity))
        plain = build_artifact.build_html_artifact(manifest, validated)
        self.assertNotIn('id="gh-status"', plain)
        self.assertNotIn("gh-thread", plain)
        bare = github_panel.build_github_config(manifest["meta"], None)
        self.assertIsNone(bare["snapshot"])
        self.assertIn("No GitHub snapshot was saved", "\n".join(github_panel.render_github_markdown(bare)))

    def test_board_and_summary_link_back_to_unmaze(self) -> None:
        manifest = self._build_sample_manifest()
        validated = build_artifact.validate_and_normalize_plan(
            manifest, copy.deepcopy(self.sample_plan), search_dirs=[REPO_ROOT / "examples"]
        )
        home = "https://github.com/t10ko/unmaze"
        html_out = build_artifact.build_html_artifact(manifest, validated)
        self.assertIn(
            f'<a class="arc-brand" href="{home}" target="_blank" rel="noopener noreferrer"', html_out,
            "the top-bar name opens the project",
        )
        self.assertRegex(
            html_out,
            r'(?s)<footer class="foot-hint">.*<p class="foot-credit">Made with <a href="'
            + re.escape(home) + r'" target="_blank" rel="noopener noreferrer">unmaze</a>, .*</p>\s*</footer>',
        )

        activity = json.loads((REPO_ROOT / "examples" / "sample-activity.json").read_text(encoding="utf-8"))
        cfg = github_panel.build_github_config(manifest["meta"], activity)
        for md in (build_artifact.build_markdown_summary(manifest, validated),
                   build_artifact.build_markdown_summary(manifest, validated, cfg)):
            tail = md.rstrip("\n").split("\n")[-4:]
            self.assertEqual(tail[:3], ["", "---", ""], "a blank line keeps the divider from turning text into a heading")
            self.assertTrue(tail[3].startswith(f"<sub>Made with [unmaze]({home}), "), tail[3])

    # --- "Tested end to end" section -------------------------------------------------------

    TINY_GIF = (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
        b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    )
    TESTED_SHA = "0123456789abcdef0123456789abcdef01234567"

    def _e2e_results(self) -> dict:
        return {
            "tested_sha": self.TESTED_SHA,
            "tested_at": "2026-11-27T12:00:00Z",
            "environment": "Local app, Chrome",
            "summary": "We checked duplicate webhooks end to end; one API check failed.",
            "scenarios": [
                {
                    "id": "S1",
                    "title": "A retried webhook shows the deduplicated badge",
                    "why": "Support would see the same payment twice if this broke.",
                    "kind": "browser",
                    "status": "pass",
                    "files": ["services/idempotency/store.ts"],
                    "steps": [
                        {"action": "Open the payments page", "expected": "The page loads", "status": "pass",
                         "gif": "media/S1-step1.gif"},
                        {"action": "Send the same webhook twice", "expected": "One row with a badge", "status": "pass"},
                    ],
                },
                {
                    "id": "S2",
                    "title": "The webhook route answers duplicates with 200",
                    "why": "Stripe would retry forever on a 500.",
                    "kind": "api",
                    "status": "fail",
                    "note": "The second call returned 500.",
                    "steps": [
                        {"action": "Call POST /webhooks/stripe twice", "expected": "HTTP 200 both times", "status": "fail",
                         "request": "POST /webhooks/stripe\n{\"id\": \"evt_1\"}", "response": "HTTP 500\n{\"error\": \"boom\"}"},
                    ],
                },
                {
                    "id": "S3",
                    "title": "Expired claims get picked up again",
                    "why": "A crashed worker would block the event forever.",
                    "kind": "job",
                    "status": "skipped",
                    "note": "No worker runs locally.",
                    "steps": [{"action": "Wait 60 seconds", "expected": "The job retries", "status": "skipped"}],
                },
            ],
        }

    def _e2e_build(self, tmp_path: Path, results: dict | None, *, asset_urls: dict | None = None,
                   with_pr: bool = True, head_sha: str | None = None, gif: bool = True,
                   inline_gifs: bool = False) -> tuple[subprocess.CompletedProcess, Path]:
        work = tmp_path / "work"
        if not (work / "diff-manifest.json").exists():
            cmd = [sys.executable, str(SCRIPTS_DIR / "parse_diff.py"), "--patch", str(REPO_ROOT / "examples" / "sample-hairball.patch"),
                   "--context-dir", str(REPO_ROOT / "examples" / "sample-context"), "--out-dir", str(work)]
            if with_pr:
                cmd += ["--pr-url", "https://github.com/acme/payments/pull/142"]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 0, res.stderr)
        manifest_path = work / "diff-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["meta"]["head_sha"] = head_sha
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        cmd = [sys.executable, str(SCRIPTS_DIR / "build_artifact.py"), "--manifest", str(manifest_path),
               "--plan", str(REPO_ROOT / "examples" / "sample-plan.json"), "--assets-dir", str(REPO_ROOT / "examples"),
               "--out", str(work / "review.html"), "--markdown-out", str(work / "review.md")]
        if with_pr:
            cmd += ["--activity", str(REPO_ROOT / "examples" / "sample-activity.json")]
        if results is not None:
            res_dir = tmp_path / "e2e"
            (res_dir / "media").mkdir(parents=True, exist_ok=True)
            if gif:
                (res_dir / "media" / "S1-step1.gif").write_bytes(self.TINY_GIF)
            (res_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
            cmd += ["--e2e-results", str(res_dir / "results.json")]
        if asset_urls is not None:
            (tmp_path / "asset-urls.json").write_text(json.dumps(asset_urls), encoding="utf-8")
            cmd += ["--asset-urls", str(tmp_path / "asset-urls.json")]
        if inline_gifs:
            cmd += ["--inline-gifs"]
        return subprocess.run(cmd, capture_output=True, text=True, check=False), work

    @staticmethod
    def _stdout_capabilities(stdout: str) -> dict | None:
        lines = [ln for ln in stdout.splitlines() if "capabilities=" in ln]
        if not lines:
            return None
        return json.loads(re.search(r"capabilities=(\{.*\})", lines[0]).group(1))

    def test_e2e_results_render_section_sidebar_pill_and_uploaded_gif(self) -> None:
        gif_sha = hashlib.sha256(self.TINY_GIF).hexdigest()
        gif_url = "https://claude.ai/_blob/s1-step1.gif"
        with tempfile.TemporaryDirectory() as tmp:
            res, work = self._e2e_build(Path(tmp), self._e2e_results(), asset_urls={gif_sha: gif_url})
            self.assertEqual(res.returncode, 0, res.stderr)
            html_out = (work / "review.html").read_text(encoding="utf-8")
            md = (work / "review.md").read_text(encoding="utf-8")

        # Sidebar: after the chapters, before the Appendix, with a pass count badge.
        e2e_tab = re.search(r'<button type="button" class="arc-tab arc-tab-e2e" data-panel-index="(\d+)">(.*?)</button>', html_out, re.S)
        self.assertIsNotNone(e2e_tab)
        self.assertEqual(e2e_tab.group(1), "6", "5 steps, then the checks, then the Appendix")
        self.assertIn("Tested end to end", e2e_tab.group(2))
        self.assertIn(">1/3<", e2e_tab.group(2))
        self.assertLess(html_out.index("arc-tab-e2e"), html_out.index('<span class="arc-tab-label">Appendix'))
        self.assertIn('id="step-e2e" data-step-id="step-e2e" data-panel-index="6"', html_out)
        self.assertIn('id="step-mechanical" data-step-id="step-mechanical" data-panel-index="7"', html_out)
        self.assertLess(html_out.index('id="step-e2e"'), html_out.index('id="step-mechanical"'))
        # The last step leads into the checks, the checks lead into the Appendix.
        self.assertIn('data-default-label="Tested end to end →" data-goto-index="6"', html_out)
        self.assertIn('data-default-label="Appendix (2) →" data-goto-index="7"', html_out)

        # Overview pill links to the section; failures red, skipped gray.
        pill = re.search(r'<button type="button" class="e2e-pill[^"]*" data-goto-index="6">(.*?)</button>', html_out, re.S)
        self.assertIsNotNone(pill)
        self.assertIn("Tested end to end: 1 of 3 passed", re.sub(r"<[^>]+>", "", pill.group(1)))
        self.assertIn('class="e2e-pill-fail">1 failed', pill.group(1))
        self.assertIn('class="e2e-pill-skip">1 skipped', pill.group(1))
        self.assertLess(html_out.index("e2e-pill"), html_out.index('class="this-pr-block"'))

        # Header, cards (failures first, then skipped, then passed), kinds in plain words.
        self.assertIn("We checked duplicate webhooks end to end; one API check failed.", html_out)
        self.assertIn("Local app, Chrome", html_out)
        self.assertIn("Tested on commit <code>0123456</code> · Nov 27, 2026 12:00 UTC", html_out)
        self.assertNotIn("older commit", html_out, "no head commit known, so no warning")
        order = [html_out.index(f'id="e2e-{sid}"') for sid in ("S2", "S3", "S1")]
        self.assertEqual(order, sorted(order))
        for kind in ("In the browser", "Through the API", "Background job"):
            self.assertIn(kind, html_out)
        self.assertIn("The second call returned 500.", html_out)

        # Step GIF from --asset-urls, opening in the existing zoom; API step request/response collapsed.
        self.assertRegex(html_out, r'<img src="' + re.escape(gif_url) + r'" loading="lazy" alt="[^"]+" width="1" height="1">')
        gif_card = html_out[html_out.index('class="visual-card e2e-gif"'):]
        self.assertIn('class="vis-zoom-btn e2e-gif-open"', gif_card[:400])
        self.assertNotIn("GIF appears after upload", html_out)
        self.assertRegex(html_out, r'<details class="e2e-io">\s*<summary>Request and response</summary>')
        self.assertIn("HTTP 500", html_out)

        # "Proves the code in" links to the steps whose diff covers the listed files.
        proves = re.search(r'<p class="e2e-proves">(.*?)</p>', html_out, re.S).group(1)
        self.assertIn('data-goto-step="step-2"', proves)
        self.assertIn('data-goto-step="step-3"', proves)
        self.assertNotIn('data-goto-step="step-1"', proves)
        self.assertEqual(html_out.count('class="e2e-proves"'), 1, "omitted for scenarios without matching files")

        # stdout: nothing left to upload; capabilities keeps GitHub and adds assets.
        self.assertNotIn("e2e-uploads=", res.stdout)
        caps = self._stdout_capabilities(res.stdout)
        self.assertEqual(caps["assets"], {})
        self.assertEqual(caps["mcp"]["servers"][0]["server"], "GitHub")

        # Markdown summary
        self.assertIn("## Tested end to end", md)
        self.assertIn("We checked duplicate webhooks end to end; one API check failed.", md)
        self.assertIn("Tested on commit `0123456`", md)
        self.assertIn("| Scenario | How | Result |", md)
        self.assertIn(f"[step 1]({gif_url})", md)
        self.assertIn("| The webhook route answers duplicates with 200 | Through the API | Failed: The second call returned 500. |", md)
        self.assertLess(md.index("## Tested end to end"), md.index("## Appendix"))

    def test_e2e_gif_without_url_gets_placeholder_and_upload_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gif_sha = hashlib.sha256(self.TINY_GIF).hexdigest()
            res, work = self._e2e_build(Path(tmp), self._e2e_results(), with_pr=False,
                                        asset_urls={gif_sha: "javascript:alert(1)", "f" * 64: "https://claude.ai/_blob/x"})
            self.assertEqual(res.returncode, 0, res.stderr)
            html_out = (work / "review.html").read_text(encoding="utf-8")
            gif_path = str((Path(tmp) / "e2e" / "media" / "S1-step1.gif").resolve())

        self.assertIn("GIF appears after upload", html_out)
        self.assertNotIn("javascript:alert(1)", html_out, "only https:// and /_blob/ URLs are used")
        upload_lines = [ln for ln in res.stdout.splitlines() if ln.startswith("e2e-uploads=")]
        self.assertEqual(len(upload_lines), 1)
        self.assertEqual(json.loads(upload_lines[0].removeprefix("e2e-uploads=")), [gif_path])
        self.assertEqual(self._stdout_capabilities(res.stdout), {"assets": {}}, "printed even without a GitHub PR")

        # A /_blob/ path counts as uploaded.
        with tempfile.TemporaryDirectory() as tmp:
            res2, work2 = self._e2e_build(Path(tmp), self._e2e_results(), with_pr=False,
                                          asset_urls={gif_sha: "/_blob/abc"})
            self.assertEqual(res2.returncode, 0, res2.stderr)
            self.assertIn('<img src="/_blob/abc" loading="lazy"', (work2 / "review.html").read_text(encoding="utf-8"))
            self.assertNotIn("e2e-uploads=", res2.stdout)

        # No GIF at all: no upload line and no assets capability.
        results = self._e2e_results()
        del results["scenarios"][0]["steps"][0]["gif"]
        with tempfile.TemporaryDirectory() as tmp:
            res3, _ = self._e2e_build(Path(tmp), results, with_pr=False, gif=False)
            self.assertEqual(res3.returncode, 0, res3.stderr)
            self.assertNotIn("e2e-uploads=", res3.stdout)
            self.assertNotIn("capabilities=", res3.stdout)

    def test_e2e_inline_gifs_embed_for_a_board_opened_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res, work = self._e2e_build(Path(tmp), self._e2e_results(), with_pr=False, inline_gifs=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            html_out = (work / "review.html").read_text(encoding="utf-8")

        data_uri = "data:image/gif;base64," + base64.b64encode(self.TINY_GIF).decode("ascii")
        self.assertIn(f'<img src="{data_uri}" loading="lazy"', html_out)
        self.assertNotIn("GIF appears after upload", html_out)
        self.assertNotIn("e2e-uploads=", res.stdout, "nothing to upload")
        self.assertNotIn("capabilities=", res.stdout, "an embedded GIF needs no asset store")

    def test_e2e_record_urls_merges_by_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            gif = tmp_path / "a.gif"
            gif.write_bytes(self.TINY_GIF)
            urls = tmp_path / "asset-urls.json"
            urls.write_text(json.dumps({"f" * 64: "https://claude.ai/_blob/old"}), encoding="utf-8")
            script = str(SCRIPTS_DIR / "e2e_results.py")
            res = subprocess.run([sys.executable, script, "record-urls", "--asset-urls", str(urls), f"{gif}=/_blob/new"],
                                 capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(json.loads(urls.read_text(encoding="utf-8")),
                             {"f" * 64: "https://claude.ai/_blob/old", hashlib.sha256(self.TINY_GIF).hexdigest(): "/_blob/new"})
            bad = subprocess.run([sys.executable, script, "record-urls", "--asset-urls", str(urls), f"{gif}=http://x/y"],
                                 capture_output=True, text=True, check=False)
            self.assertEqual(bad.returncode, 2)

    def test_e2e_stale_commit_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res, work = self._e2e_build(Path(tmp), self._e2e_results(), head_sha="fedcba9876543210fedcba9876543210fedcba98")
            self.assertEqual(res.returncode, 0, res.stderr)
            html_out = (work / "review.html").read_text(encoding="utf-8")
            md = (work / "review.md").read_text(encoding="utf-8")
        self.assertIn("These checks ran on an older commit (0123456), not the latest one (fedcba9).", html_out)
        self.assertIn("These checks ran on an older commit (0123456), not the latest one (fedcba9).", md)

        with tempfile.TemporaryDirectory() as tmp:
            res, work = self._e2e_build(Path(tmp), self._e2e_results(), head_sha=self.TESTED_SHA)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertNotIn("older commit", (work / "review.html").read_text(encoding="utf-8"))

    def test_e2e_results_validation_failures_exit_2(self) -> None:
        def broken(mutate) -> dict:
            results = self._e2e_results()
            mutate(results)
            return results

        def dup_ids(r: dict) -> None:
            r["scenarios"][1]["id"] = "S1"

        def pop(path: list, key: str):
            def _m(r: dict) -> None:
                target = r
                for part in path:
                    target = target[part]
                del target[key]
            return _m

        def setv(path: list, key: str, value):
            def _m(r: dict) -> None:
                target = r
                for part in path:
                    target = target[part]
                target[key] = value
            return _m

        cases = [
            (pop([], "tested_sha"), "tested_sha"),
            (pop([], "summary"), "summary"),
            (pop([], "scenarios"), "scenarios"),
            (setv([], "scenarios", []), "scenarios"),
            (pop(["scenarios", 0], "id"), "`id`"),
            (pop(["scenarios", 0], "title"), "`title`"),
            (pop(["scenarios", 0], "why"), "`why`"),
            (pop(["scenarios", 0], "kind"), "`kind`"),
            (pop(["scenarios", 0], "status"), "`status`"),
            (setv(["scenarios", 0], "steps", []), "at least one step"),
            (pop(["scenarios", 0, "steps", 1], "action"), "`action`"),
            (pop(["scenarios", 0, "steps", 1], "expected"), "`expected`"),
            (pop(["scenarios", 0, "steps", 1], "status"), "`status`"),
            (setv(["scenarios", 0], "kind", "desktop"), "browser, api, job"),
            (setv(["scenarios", 0], "status", "passed"), "pass, fail, skipped"),
            (setv(["scenarios", 0, "steps", 0], "status", "ok"), "pass, fail, skipped"),
            (dup_ids, "duplicate scenario id `S1`"),
            (setv(["scenarios", 1], "note", ""), "needs a `note`"),
            (pop(["scenarios", 2], "note"), "needs a `note`"),
            (setv(["scenarios", 0, "steps", 0], "gif", "media/missing.gif"), "media/missing.gif"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for mutate, expected in cases:
                res, _ = self._e2e_build(Path(tmp), broken(mutate))
                self.assertEqual(res.returncode, 2, f"{expected}: {res.stdout}{res.stderr}")
                self.assertIn("E2E results error", res.stderr)
                self.assertIn(expected, res.stderr)
            (Path(tmp) / "e2e" / "results.json").write_text("{not json", encoding="utf-8")
            res = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "build_artifact.py"), "--manifest", str(Path(tmp) / "work" / "diff-manifest.json"),
                 "--plan", str(REPO_ROOT / "examples" / "sample-plan.json"), "--assets-dir", str(REPO_ROOT / "examples"),
                 "--out", str(Path(tmp) / "x.html"), "--e2e-results", str(Path(tmp) / "e2e" / "results.json")],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(res.returncode, 2)
            self.assertIn("E2E results error", res.stderr)

    def test_e2e_results_strings_are_escaped(self) -> None:
        evil = "<script>e2e()</script>"
        results = self._e2e_results()
        results["summary"] = f"Summary {evil}"
        results["environment"] = f"Env <img src=x onerror=alert(1)>"
        sc = results["scenarios"][1]
        sc.update({"id": "S2\"><b>", "title": f"T {evil}", "why": f"W {evil}", "note": f"N {evil}", "files": [f"x{evil}.ts"]})
        sc["steps"][0].update({"action": f"A {evil}", "expected": f"E {evil}", "request": f"R {evil}", "response": f"P {evil}"})
        with tempfile.TemporaryDirectory() as tmp:
            res, work = self._e2e_build(Path(tmp), results)
            self.assertEqual(res.returncode, 0, res.stderr)
            html_out = (work / "review.html").read_text(encoding="utf-8")
        self.assertNotIn(evil, html_out)
        self.assertNotIn("<img src=x onerror=alert(1)>", html_out)
        self.assertNotIn('S2"><b>', html_out)
        self.assertIn("T &lt;script&gt;e2e()&lt;/script&gt;", html_out)
        self.assertIn("P &lt;script&gt;e2e()&lt;/script&gt;", html_out)

    def test_e2e_uncommitted_changes_are_shown_and_validated(self) -> None:
        results = self._e2e_results()
        results["uncommitted_changes"] = True
        with tempfile.TemporaryDirectory() as tmp:
            res, work = self._e2e_build(Path(tmp), results, with_pr=False, inline_gifs=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("Tested on commit <code>0123456</code> plus uncommitted changes", (work / "review.html").read_text(encoding="utf-8"))
        results["uncommitted_changes"] = "yes"
        with tempfile.TemporaryDirectory() as tmp:
            res, _ = self._e2e_build(Path(tmp), results, with_pr=False)
            self.assertEqual(res.returncode, 2)
            self.assertIn("`uncommitted_changes` must be true or false", res.stderr)

    def test_e2e_skill_keeps_checks_per_branch_and_moves_gifs(self) -> None:
        script = REPO_ROOT / "skills" / "e2e" / "scripts" / "e2e_runs.py"

        def runs(repo: Path, *args: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, str(script), *args, "--repo", str(repo)],
                                  capture_output=True, text=True, check=False)

        def git(repo: Path, *args: str) -> str:
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                                  capture_output=True, text=True, check=True).stdout.strip()

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "shop"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            (repo / "cart.ts").write_text("export const MIN = 5000;\n")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "cart")
            git(repo, "checkout", "-q", "-b", "feat/cart-banner")
            head = git(repo, "rev-parse", "HEAD")

            # The folder sits in the git directory, keyed by branch.
            res = runs(repo, "dir")
            self.assertEqual(res.returncode, 0, res.stderr)
            folder = Path(res.stdout.strip())
            self.assertEqual(folder, (repo / ".git" / "unmaze" / "e2e" / "feat-cart-banner").resolve())
            self.assertTrue((folder / "media").is_dir())
            status = json.loads(runs(repo, "status").stdout)
            self.assertIsNone(status["results"])
            self.assertFalse(status["stale"])

            # A valid run on HEAD is current; its passing browser step has its GIF.
            (folder / "media" / "S1-step1.gif").write_bytes(self.TINY_GIF)
            results = {
                "tested_sha": head, "summary": "Clicked through the cart.",
                "scenarios": [{"id": "S1", "title": "Banner", "why": "Shoppers see the goal.", "kind": "browser",
                               "status": "pass", "steps": [{"action": "Open the cart", "expected": "Banner",
                                                            "status": "pass", "gif": "media/S1-step1.gif"}]}],
            }
            (folder / "results.json").write_text(json.dumps(results))
            res = runs(repo, "check")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("1 passed, 0 failed, 0 skipped", res.stdout)
            status = json.loads(runs(repo, "status").stdout)
            self.assertEqual(status["results"], str(folder / "results.json"))
            self.assertEqual(status["tested_sha"], head)
            self.assertFalse(status["stale"])
            self.assertEqual(status["counts"], {"pass": 1, "fail": 0, "skipped": 0})

            # check holds the run to the plan and to its own claims.
            (folder / "scenarios.md").write_text(
                "## Scenarios\n### S1 — Banner\n- Kind: browser\n- Steps:\n  1. Open → Expected: banner\n"
                "### S2 — Quote API\n- Kind: api\n\n## Variants the code branches on\n- none\n")
            res = runs(repo, "check")
            self.assertEqual(res.returncode, 2)
            self.assertIn("S2 is in scenarios.md but not in results.json", res.stderr)
            api = {"id": "S2", "title": "Quote", "why": "Checkout uses it.", "kind": "browser", "status": "pass",
                   "steps": [{"action": "Call the quote API", "expected": "200", "status": "pass"}]}
            results["scenarios"].append(api)
            (folder / "results.json").write_text(json.dumps(results))
            self.assertIn("S2 is a api scenario in scenarios.md but `browser` in results.json", runs(repo, "check").stderr)
            api["kind"] = "api"
            (folder / "results.json").write_text(json.dumps(results))
            self.assertIn("S2 step 1: a passing api step needs the `response` it got", runs(repo, "check").stderr)
            api["steps"][0]["response"] = "HTTP 200"
            results["scenarios"][0]["steps"].append({"action": "Reload", "expected": "Same banner", "status": "skipped"})
            (folder / "results.json").write_text(json.dumps(results))
            self.assertIn("S1 is marked pass, but not every step passed", runs(repo, "check").stderr)
            results["scenarios"][0]["steps"].pop()
            del results["scenarios"][0]["steps"][0]["gif"]
            (folder / "results.json").write_text(json.dumps(results))
            res = runs(repo, "check")
            self.assertEqual(res.returncode, 2)
            self.assertIn("S1 step 1: a passing browser step needs its GIF", res.stderr)
            results["scenarios"][0]["steps"][0]["gif"] = "media/S1-step1.gif"

            # Uncommitted changes are reported but never make a run stale (a rerun on a dirty
            # tree would only be dirty again); a newer commit does.
            results["uncommitted_changes"] = True
            (folder / "results.json").write_text(json.dumps(results))
            self.assertEqual(runs(repo, "check").returncode, 0)
            status = json.loads(runs(repo, "status").stdout)
            self.assertFalse(status["stale"])
            self.assertTrue(status["uncommitted_changes"])
            results["uncommitted_changes"] = False
            (folder / "results.json").write_text(json.dumps(results))
            (repo / "cart.ts").write_text("export const MIN = 4000;\n")
            git(repo, "commit", "-q", "-am", "lower the bar")
            status = json.loads(runs(repo, "status").stdout)
            self.assertTrue(status["stale"])
            self.assertEqual(status["stale_reason"], "older_commit")
            self.assertFalse(json.loads(runs(repo, "status", "--sha", head).stdout)["stale"], "--sha picks the commit to compare with")
            self.assertIn("record the commit you tested", runs(repo, "check").stderr)

            # take-gif moves the newest export (browsers rename repeats to "name (1).gif").
            downloads = Path(tmp) / "Downloads"
            downloads.mkdir()
            old = downloads / "S1-step2.gif"
            old.write_bytes(b"old")
            stale_time = old.stat().st_mtime - 3600
            import os
            os.utime(old, (stale_time, stale_time))
            (downloads / "S1-step2 (1).gif").write_bytes(self.TINY_GIF)
            (downloads / "S1-step20.gif").write_bytes(b"other step")
            res = runs(repo, "take-gif", "S1-step2.gif", "--downloads", str(downloads))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual((folder / "media" / "S1-step2.gif").read_bytes(), self.TINY_GIF)
            self.assertTrue(old.exists(), "an export older than --max-age is left alone")
            self.assertTrue((downloads / "S1-step20.gif").exists(), "a different step's file is never taken")
            res = runs(repo, "take-gif", "S1-step3.gif", "--downloads", str(downloads))
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("export the GIF again", res.stderr)

            # A detached HEAD needs the branch named.
            git(repo, "checkout", "-q", "--detach")
            res = runs(repo, "dir")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("--branch", res.stderr)
            res = runs(repo, "dir", "--branch", "feat/cart-banner")
            self.assertEqual(Path(res.stdout.strip()), folder)

    def test_board_without_e2e_results_is_unchanged(self) -> None:
        # The sample snapshot has a check named "e2e-webhooks", so match the section's own markup.
        markers = ("Tested end to end", ".e2e-", 'class="e2e', "arc-tab-e2e", "step-e2e")
        for with_pr in (True, False):
            with tempfile.TemporaryDirectory() as tmp:
                res, work = self._e2e_build(Path(tmp), None, with_pr=with_pr)
                self.assertEqual(res.returncode, 0, res.stderr)
                html_out = (work / "review.html").read_text(encoding="utf-8")
                md = (work / "review.md").read_text(encoding="utf-8")
            for marker in markers:
                self.assertNotIn(marker, html_out)
                self.assertNotIn(marker, md)
            self.assertNotIn("e2e-uploads=", res.stdout)
            stdout_lines = [ln.split(" ")[1] if ln.startswith("✓") else ln for ln in res.stdout.splitlines()]
            self.assertEqual(stdout_lines, ["Verified", "Interactive"] + (["GitHub"] if with_pr else []) + ["Companion"])
            if with_pr:
                self.assertNotIn("assets", self._stdout_capabilities(res.stdout))
            else:
                self.assertIsNone(self._stdout_capabilities(res.stdout))

        manifest = self._build_sample_manifest()
        validated = build_artifact.validate_and_normalize_plan(
            manifest, copy.deepcopy(self.sample_plan), search_dirs=[REPO_ROOT / "examples"]
        )
        self.assertEqual(build_artifact.build_html_artifact(manifest, validated),
                         build_artifact.build_html_artifact(manifest, validated, e2e=None))


if __name__ == "__main__":
    unittest.main()
