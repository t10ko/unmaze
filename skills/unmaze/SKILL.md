---
name: unmaze
description: Transforms pull requests and git diffs into a shareable, step-by-step interactive code review guide ordered how humans understand code (global architecture map, data contracts, core logic, wiring, then verification and mechanical noise) with verified 100% diff coverage. Use when asked to review a PR, walk through a diff, explain branch changes, untangle a large AI-generated PR, or when invoked via /unmaze.
argument-hint: "[pr-number | pr-url | git-range | --staged | path.patch]"
license: MIT
compatibility: Requires Python 3.9+ and git (plus gh CLI when fetching GitHub PRs by number or URL).
metadata:
  author: t10ko
  version: "0.1.0"
---

# unmaze — Guided Code Review in Human Reading Order

GitHub and Git diffs sort files alphabetically (`a.ts` -> `z.ts`), forcing reviewers to read routes, tests, and boilerplate before they even know what data structures or core logic exist. `unmaze` untangles a diff into a **zero-forward-reference step-by-step walkthrough** with **100% deterministic hunk coverage**, and publishes it as an interactive HTML Artifact.

User arguments (if any): `$ARGUMENTS`

---

## Workflow

### Step 1: Parse the Diff Deterministically

Resolve `<SKILL_DIR>` as the directory containing this `SKILL.md` file. Always write intermediate files to a temporary directory (e.g., `WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/unmaze.XXXXXX")"`) so you never leave untracked files inside the repository being reviewed—unless the user explicitly asks to save files to a specific path.

Map `$ARGUMENTS` to `<SKILL_DIR>/scripts/parse_diff.py`:
- **PR number or PR URL** (e.g., `142`, `#142`, `https://github.com/org/repo/pull/142`):
  ```bash
  python3 "<SKILL_DIR>/scripts/parse_diff.py" --pr "<pr>" --out-dir "$WORK_DIR"
  ```
- **Git revision range** (e.g., `main...HEAD`, `HEAD~3..HEAD`, `origin/main..feature`):
  ```bash
  python3 "<SKILL_DIR>/scripts/parse_diff.py" --range "<range>" --out-dir "$WORK_DIR"
  ```
- **Staged or working-tree changes** (`--staged` or `--working`):
  ```bash
  python3 "<SKILL_DIR>/scripts/parse_diff.py" --staged --out-dir "$WORK_DIR"
  ```
- **Patch file** (`*.patch` or `*.diff`):
  ```bash
  python3 "<SKILL_DIR>/scripts/parse_diff.py" --patch "<file>" --out-dir "$WORK_DIR"
  ```
- **Empty `$ARGUMENTS`** (default):
  ```bash
  python3 "<SKILL_DIR>/scripts/parse_diff.py" --out-dir "$WORK_DIR"
  ```
  *(Auto-detects the open PR or `<default-branch>...HEAD` when on a feature branch, or uncommitted/last-commit changes when on the default branch.)*

---

### Step 2: Build the Global Mental Model (Inspect Diff + Surrounding Repo)

1. Read `$WORK_DIR/hunks-readable.txt` to inspect all hunk IDs (`H001`..`HNNN`), file paths, line numbers, and diff contents.
2. **Do not review in a vacuum:** Briefly read or grep surrounding callers, entrypoints, or neighboring modules in the repository (2–5 targeted lookups) so you can:
   - Explain **how this subsystem fits into the global architecture** for a reviewer who did not write this code.
   - Include **1–2 `"unchanged"` landmark components** in `architecture_map.nodes` so the reviewer sees *where* the changed modules plug into the existing system.
   - Spot codebase-specific risks (e.g., missing auth/tenant scoping compared to neighboring routes, duplicated utilities, broken invariants, or tautological tests).

---

### Step 3: Design the Human Reading Sequence (`plan.json`)

Read `<SKILL_DIR>/references/plan-schema.md` and `<SKILL_DIR>/references/cognitive-order-guide.md`, then write `$WORK_DIR/plan.json`.

Order steps by **zero forward references and decreasing cognitive criticality**:
1. **`contracts` (Shapes & Contracts):** Data structures, DB schemas/migrations, interfaces, API payloads—*introduce the nouns before the verbs*.
2. **`core` (Core Logic & Decisions):** Domain algorithms, state transitions, auth/security boundaries, and key tradeoffs—*spend the reviewer's freshest attention where real bugs live*. Keep steps bite-sized (1–5 hunks per step; split distinct mechanisms into separate steps).
3. **`wiring` (Entrypoints & Wiring):** Routes, controllers, CLI flags, UI bindings, DI registration, config plumbing connecting `core` to the app. *(If a PR contains 2+ independent features, sequence `contracts -> core -> wiring` per feature slice using the `"slice"` label.)*
4. **`verification` (Verification & Edge Cases):** What behaviors the tests prove, **plus explicit `untested_gaps`** calling out missing edge cases, race conditions, or over-mocked tests.
5. **`mechanical` (Appendix Drawer):** Pure boilerplate—lockfiles, generated artifacts, barrel exports, mechanical renames. Never bury behavioral changes inside `mechanical`.

Ensure **every single Hunk ID** from `diff-manifest.json` appears **once and only once** across `steps[*].hunk_ids` and `mechanical.groups[*].hunk_ids`.

---

### Step 4: Compile & Verify 100% Diff Coverage

Run `build_artifact.py`:

```bash
python3 "<SKILL_DIR>/scripts/build_artifact.py" \
  --manifest "$WORK_DIR/diff-manifest.json" \
  --plan "$WORK_DIR/plan.json" \
  --out "$WORK_DIR/unmaze-review.html" \
  --markdown-out "$WORK_DIR/unmaze-review.md"
```

If `build_artifact.py` exits with code `2` (missing, duplicate, or invalid hunk IDs), fix `$WORK_DIR/plan.json` immediately and re-run until it succeeds with `✓ Verified 100% diff coverage`.

---

### Step 5: Publish Artifact & Present Concise Summary

1. **Publish as a Shareable Claude Code Artifact (Primary):**
   - If the `Artifact` tool is available in your environment, read `$WORK_DIR/unmaze-review.html` and invoke the `Artifact` tool with that complete self-contained HTML so the user receives a live, shareable `claude.ai/code/artifact/...` link (remind them they can click **Share** in the artifact header to share with teammates or paste into the PR description).
2. **Local Browser Fallback:**
   - Always include the local path `$WORK_DIR/unmaze-review.html` (and `$WORK_DIR/unmaze-review.md`). If the `Artifact` tool is not available (e.g., API-key session or non-Claude harness) or the user passed `--open`, offer or open `$WORK_DIR/unmaze-review.html` locally.
3. **Keep Chat Output Short & High-Signal:**
   - Do **not** dump the entire walkthrough into chat—that's what the interactive Artifact is for.
   - In chat, give:
     1. The **TL;DR** + cognitive budget breakdown (e.g., *"19 files / +840 lines -> 4 core steps (185 lines) + wiring/tests + 480 lines of mechanical noise"*).
     2. The **numbered step list** (1 line per step).
     3. The top **risks / AI assumptions / untested gaps** that deserve human scrutiny.
     4. The **Artifact link** (and local HTML/Markdown file paths).
