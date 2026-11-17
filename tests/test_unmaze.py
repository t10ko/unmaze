#!/usr/bin/env python3
"""Automated test suite for unmaze diff parser, timeline file evolution, sub-hunk slicing,
Arc chapters, concept modals, visual annotation overlays, and XSS safety.
"""

from __future__ import annotations

import copy
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

import build_artifact  # noqa: E402
import parse_diff  # noqa: E402


class UnmazeTestSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.sample_patch = (REPO_ROOT / "examples" / "sample-hairball.patch").read_text(
            encoding="utf-8"
        )
        self.sample_plan = json.loads(
            (REPO_ROOT / "examples" / "sample-plan.json").read_text(encoding="utf-8")
        )

    def _build_sample_manifest(self) -> dict:
        pr_url = "https://github.com/acme/payments/pull/142"
        files, hunks = parse_diff.parse_unified_diff(
            self.sample_patch,
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
                "overview": {
                    "tldr": "Touches `AuthCore` and <script>evil()</script>.",
                    "before_state": "before",
                    "after_state": "after",
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


if __name__ == "__main__":
    unittest.main()
