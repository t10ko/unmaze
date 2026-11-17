#!/usr/bin/env python3
"""Automated test suite for unmaze diff parser, coverage validator, and artifact builder."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
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

    def test_parse_sample_patch_and_github_deep_links(self) -> None:
        pr_url = "https://github.com/acme/payments/pull/142"
        files, hunks = parse_diff.parse_unified_diff(
            self.sample_patch,
            repo_web_url="https://github.com/acme/payments",
            host_kind="github",
            pr_url=pr_url,
            head_ref="deadbeef",
        )
        self.assertEqual(len(files), 8)
        self.assertEqual(len(hunks), 8)
        self.assertEqual([h["id"] for h in hunks], [f"H{i:03d}" for i in range(1, 9)])

        # Verify deterministic GitHub PR diff SHA-256 deep link on H002 (api/routes/webhooks.ts)
        h2 = hunks[1]
        expected_sha = hashlib.sha256(b"api/routes/webhooks.ts").hexdigest()
        self.assertEqual(
            h2["deep_link"],
            f"{pr_url}/files#diff-{expected_sha}R1",
        )

        # Verify category hints
        hints = {f["path"]: f["category_hint"] for f in files}
        self.assertEqual(hints["__tests__/webhook-dedup.test.ts"], "test")
        self.assertEqual(hints["pnpm-lock.yaml"], "generated_or_lockfile")
        self.assertEqual(hints["migrations/20261117_webhook_idempotency.sql"], "contracts")
        self.assertEqual(hints["services/idempotency/types.ts"], "contracts")
        self.assertEqual(hints["services/idempotency/store.ts"], "core")
        self.assertEqual(hints["api/routes/webhooks.ts"], "wiring")

    def test_synthetic_hunks_for_pure_rename_and_binary(self) -> None:
        raw_diff = (
            "diff --git a/old_name.ts b/new_name.ts\n"
            "similarity index 100%\n"
            "rename from old_name.ts\n"
            "rename to new_name.ts\n"
            "diff --git a/logo.png b/logo.png\n"
            "index 1111111..2222222 100644\n"
            "Binary files a/logo.png and b/logo.png differ\n"
        )
        files, hunks = parse_diff.parse_unified_diff(raw_diff)
        self.assertEqual(len(files), 2)
        self.assertEqual(len(hunks), 2)
        self.assertTrue(hunks[0]["is_synthetic"])
        self.assertEqual(hunks[0]["file_status"], "renamed")
        self.assertTrue(hunks[1]["is_synthetic"])
        self.assertTrue(hunks[1]["is_binary"])

    def test_coverage_validator_catches_missing_and_duplicate_hunks(self) -> None:
        files, hunks = parse_diff.parse_unified_diff(self.sample_patch)
        manifest = {
            "meta": {"title": "Test PR"},
            "stats": {
                "total_files": len(files),
                "total_hunks": len(hunks),
                "total_additions": sum(f["additions"] for f in files),
                "total_deletions": sum(f["deletions"] for f in files),
            },
            "files": files,
            "hunks": hunks,
        }

        # Valid plan passes
        validated = build_artifact.validate_and_normalize_plan(
            manifest, copy.deepcopy(self.sample_plan)
        )
        self.assertEqual(len(validated["steps"]), 4)

        # Missing H006 fails loudly
        broken_missing = copy.deepcopy(self.sample_plan)
        broken_missing["mechanical"]["groups"][0]["hunk_ids"] = ["H004"]
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_missing:
            build_artifact.validate_and_normalize_plan(manifest, broken_missing)
        self.assertIn("H006", str(ctx_missing.exception))

        # Duplicate H007 fails loudly
        broken_dup = copy.deepcopy(self.sample_plan)
        broken_dup["steps"][2]["hunk_ids"].append("H007")
        with self.assertRaises(build_artifact.PlanValidationError) as ctx_dup:
            build_artifact.validate_and_normalize_plan(manifest, broken_dup)
        self.assertIn("Duplicate hunk ID `H007`", str(ctx_dup.exception))

    def test_xss_escaping_and_end_to_end_cli_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Inject an XSS payload into patch & plan to verify strict HTML escaping
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
                    "tldr": "<script>evil()</script>",
                    "before_state": "before <b>raw</b>",
                    "after_state": "after",
                },
                "steps": [
                    {
                        "id": "step-1",
                        "layer": "core",
                        "title": "Check <script> escaping",
                        "hunk_ids": ["H001"],
                    }
                ],
            }
            plan_file = out_dir / "plan.json"
            plan_file.write_text(json.dumps(evil_plan), encoding="utf-8")

            html_out = out_dir / "review.html"
            md_out = out_dir / "review.md"
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
                    "--markdown-out",
                    str(md_out),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_build.returncode, 0, res_build.stderr)
            rendered = html_out.read_text(encoding="utf-8")

            self.assertNotIn('<script>alert("pwned")</script>', rendered)
            self.assertNotIn("<script>evil()</script>", rendered)
            self.assertNotIn("<img src=x onerror=alert(1)>", rendered)
            self.assertNotIn("javascript:alert(1)", rendered)
            self.assertIn("&lt;script&gt;alert(&quot;pwned&quot;)&lt;/script&gt;", rendered)


if __name__ == "__main__":
    unittest.main()
