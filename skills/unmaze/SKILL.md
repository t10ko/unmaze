---
name: unmaze
description: Resurrects human code review in the AI era by turning sprawling pull requests and git diffs into a shareable, step-by-step interactive review guide—with Arc-style stacked PR chapters, cumulative file evolution across the timeline, clickable repo concept modals, annotated Figma/UI screenshots, and verified 100% diff coverage. Use when asked to review a PR, walk through a diff, explain branch changes, untangle a large AI-generated PR, or when invoked via /unmaze.
argument-hint: "[pr-number | pr-url | git-range | --staged | path.patch]"
license: MIT
compatibility: Requires Python 3.9+ and git (plus gh CLI when fetching GitHub PRs by number or URL).
metadata:
  author: t10ko
  version: "0.3.0"
---

# unmaze — Resurrecting Human Code Review in the AI Era

GitHub and Git sort diffs alphabetically (`a.ts` -> `z.ts`), dumping 30 files at once on reviewers who didn't write the code by hand and may not know how that subsystem works globally. `unmaze` turns any PR or Git diff into a **guided, low-cognitive-load interactive Artifact** with:

- **Arc-style Stacked PR Chapters** (splitting large PRs into small, logical virtual PRs),
- **Cumulative Timeline File Evolution** (showing each file *as it exists at that step*, slicing large hunks with `H007:1-34,52` so future steps don't leak early),
- **Clickable Repo Concept Modals** (explaining any touched module or domain term from scratch for an engineer who is new to the repo),
- **Dynamic Visual Formatting Rotation** in **very simple English**,
- **Annotated Figma & Real-App Screenshots** (red circles, boxes, and directional arrows), and
- **100% deterministic changed-line accounting**.

User arguments (if any): `$ARGUMENTS`

---

## Workflow

### Step 1: Parse the Diff & Full Post-Change File Context

Resolve `<SKILL_DIR>` as the directory containing this `SKILL.md` file. Write working files to a temporary directory (`WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/unmaze.XXXXXX")"`) so you never leave untracked files in the repository being reviewed.

Map `$ARGUMENTS` to `<SKILL_DIR>/scripts/parse_diff.py`:
- **PR number or URL** (`142`, `#142`, `https://github.com/org/repo/pull/142`):
  ```bash
  python3 "<SKILL_DIR>/scripts/parse_diff.py" --pr "<pr>" --out-dir "$WORK_DIR"
  ```
- **Git revision range** (`main...HEAD`, `HEAD~3..HEAD`):
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
- **Empty `$ARGUMENTS`** (auto-detects current branch PR or `<default-branch>...HEAD`):
  ```bash
  python3 "<SKILL_DIR>/scripts/parse_diff.py" --out-dir "$WORK_DIR"
  ```

---

### Step 2: Gather Global Repo Context, Concepts & Visual References

1. Read `$WORK_DIR/hunks-readable.txt` to inspect all hunk IDs (`H001`..`HNNN`), line numbers, and any Figma/screenshot URLs extracted from the PR description.
2. Perform 2–6 targeted lookups in surrounding repo files so you understand **how the touched subsystem actually works from scratch** (who calls it, whether it runs sync or async, what DB tables/services it owns, and what happens on error). You will use these concrete facts to write **Concept Modals (`concepts[]`)** for reviewers who don't know this codebase!
3. **Collect & Inspect Visual References (Figma / App Screenshots):**
   - If Figma URLs exist and Figma MCP tools (`get_screenshot` / node export) are available, export the relevant frame PNG into `$WORK_DIR/visuals/` and attach `figma_url`.
   - If UI screenshots exist in the PR, `$ARGUMENTS`, or can be captured from a running local app via Playwright/DevTools MCP, save them into `$WORK_DIR/visuals/`.
   - **MANDATORY before adding annotations:** View every screenshot with your `Read` tool first, then place **red circles (`circle`), directional arrows (`arrow`), or boxes (`box`)** in `0..100` percentage coordinates pointing directly at what that step controls.

---

### Step 3: Design `plan.json` (Chapters, Timeline Evolution, Concepts & Simple English)

Read `<SKILL_DIR>/references/plan-schema.md` and `<SKILL_DIR>/references/cognitive-order-guide.md`, then write `$WORK_DIR/plan.json`.

#### A. Virtual Stacked PRs (`chapters[]`) & Zero-Forward-Reference Order
- For any PR with **4+ steps or 2+ logical concerns**, group `steps` into ordered **`chapters`** (displayed as Arc-style tabs in the left sidebar: `PR 1`, `PR 2`, `PR 3`...), ordered so Chapter 2 builds cleanly on Chapter 1.
- Inside each chapter, order steps from **`contracts` -> `core` -> `wiring` -> `verification`**, tucking lockfiles/exports into `mechanical` at the end.

#### B. Cumulative Timeline File Evolution & Sub-Hunk Slicing (`H007:1-34,52`)
- `build_artifact.py` reconstructs each file **as it exists at Step $N$**: earlier steps are already merged into the file, Step $N$ glows `+`/`−`, and future steps **do not exist in that file yet**.
- Whenever a single large hunk or new file mixes multiple stages (e.g., types + core method + later lifecycle helpers, or imports + logic), **slice it by line range** (`"H007:1-34,52"` in Step 2, `"H007:35-51"` in Step 3) so the file evolves cleanly step by step along the timeline!

#### C. Project Concept Modals (`concepts[]`)
- Define 3–8 `concepts` for key services, tables, queues, middlewares, or domain terms touched by this PR.
- Write each concept **from scratch for a capable engineer who has never seen this project before**—plain, concrete engineering facts (owning file, callers, sync vs. async, failure behavior, and how this PR changes or relies on it). **Never** use childish metaphors or fairy-tale analogies.
- Wrap concept names in backticks (`` `WebhookDispatcher` ``) or `[[concept-id|Label]]` in step text so they automatically become clickable modal links!

#### D. Simple English & Style Formatting Rotation (Mandatory)
- **Very Simple English:** Keep every sentence short (<= 20 words), one idea per sentence. Use plain everyday verbs (*saves, checks, skips, calls, fails, deletes*)—ban heavy AI words (*orchestrates, leverages, materializes, facilitates, subsequently, aforementioned*).
- **1-Sentence Invariant & 5-Item Cap:** `explanation` MUST be **1 single plain-English sentence** (BLUF). Cap `points`, `table.rows`, `callouts`, and `untested_gaps` at **<= 5 items**, with <= 1 sentence per item.
- **Dynamic Visual Modulation (Rotate Containers):** Never write walls of prose. After the 1-sentence `explanation`, pair the diff with the container that fits the step's shape—a compact `table` for state/input->outcome rules, bold-anchored `points` (`{"anchor": "...", "text": "..."}`) for mechanics, `visuals` for UI/Figma screens, and `callouts` only for real risks/assumptions.

---

### Step 4: Compile & Verify 100% Changed-Line Coverage

```bash
python3 "<SKILL_DIR>/scripts/build_artifact.py" \
  --manifest "$WORK_DIR/diff-manifest.json" \
  --plan "$WORK_DIR/plan.json" \
  --assets-dir "$WORK_DIR" \
  --out "$WORK_DIR/unmaze-review.html" \
  --markdown-out "$WORK_DIR/unmaze-review.md"
```

If `build_artifact.py` exits with code `2` (unclaimed/overlapping hunk lines, invalid chapter ordering, or schema error), fix `$WORK_DIR/plan.json` and re-run until `✓ Verified 100% diff coverage` succeeds.

---

### Step 5: Publish Artifact & Present Concise Summary

1. **Publish via Claude Code `Artifact` tool (Primary):** Read `$WORK_DIR/unmaze-review.html` and invoke the `Artifact` tool so the user receives a live shareable `claude.ai/code/artifact/...` URL.
2. **Local Fallback:** Include `$WORK_DIR/unmaze-review.html` (and `$WORK_DIR/unmaze-review.md`), opening locally only if `Artifact` is unavailable or `--open` was passed.
3. **Low-Load Chat Summary:** Give a 1-sentence BLUF, a compact chapter/step table, top risks/gaps, and the Artifact link.
