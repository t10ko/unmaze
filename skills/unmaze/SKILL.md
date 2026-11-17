---
name: unmaze
description: Resurrects human code review in the AI era by turning sprawling pull requests and git diffs into a shareable, step-by-step interactive review guide—with Arc-style stacked PR chapters, cumulative file evolution across the timeline, clickable repo concept modals, annotated Figma/UI screenshots, and verified 100% diff coverage. Use when asked to review a PR, walk through a diff, explain branch changes, untangle a large AI-generated PR, or when invoked via /unmaze.
argument-hint: "[pr-number | pr-url | git-range | --staged | path.patch]"
license: MIT
compatibility: Requires Python 3.9+ and git (plus gh CLI when fetching GitHub PRs by number or URL).
metadata:
  author: t10ko
  version: "0.6.0"
---

# unmaze — Resurrecting Human Code Review in the AI Era

GitHub and Git sort diffs alphabetically (`a.ts` -> `z.ts`), dumping 30 files at once on reviewers who didn't write the code by hand and may not know how that subsystem works globally. `unmaze` turns any PR or Git diff into a **guided, low-cognitive-load interactive Artifact** with:

- **Big-Picture Overview First** (explains the **whole task** casually in simple, short English with red-marked screenshots up front—and if this PR is only one slice of a bigger effort, clearly says which part we built here),
- **Super Casual Teammate Voice Everywhere** (written like a teammate walking you through the PR over Slack—contractions, `we`/`you`, zero stiff docs or AI jargon),
- **Arc-style Chapters** (splitting large PRs into ordered chapters—`Chapter 1`, `Chapter 2`—in a clean sidebar),
- **Top-Bar Role & Area Tree Filter** (2-level `Role Surface -> App Area` filter that dims unrelated chapters and steps so FE, BE, or domain engineers can review just their slice),
- **Cumulative Timeline File Evolution** (showing each file *as it exists at that step*, slicing large hunks with `H007:1-34,52` so future steps don't leak early),
- **Interactive Diff-Block Highlights** (hover-tinted code chunks inside large diffs or new files that open a side-margin explanation on click without covering any code),
- **Semantic Concept Modals Everywhere You Encounter Them** (a dedicated 2nd-pass AI sweep explains **both** brand-new terms/things introduced by this task **and** existing repo concepts, semantically linking casual phrases like `[[claimed-lease|frees the lock]]` wherever they're talked about across every step, bullet, table, caption, and diff note),
- **Mandatory Red-Marked Screenshots — Even on Backend PRs** (traces backend/API/DB changes to the frontend screen they impact and places red circles, boxes, and arrows showing what the code is for), and
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

### Step 2: Gather Whole-Task Context, Concepts & Red-Marked Screenshots (Mandatory)

1. Read `$WORK_DIR/hunks-readable.txt` (plus PR body, commit messages, or linked ticket hints) to understand **the whole task / bigger picture**—what user or engineering problem the team is solving overall, and whether this PR finishes the whole thing or builds one part of a larger effort.
2. Perform 2–6 targeted lookups in surrounding repo files so you understand **how the touched subsystem actually works from scratch** (who calls it, whether it runs sync or async, what DB tables/services it owns, and what happens on error). You will use these concrete facts to write **Concept Modals (`concepts[]`)** for reviewers who don't know this codebase!
3. **Hunt & Capture Impacted UI Screenshots — EVEN FOR 100% BACKEND PRs (Hard Requirement):**
   - **Never skip screenshots just because zero frontend files appear in `git diff`!** Whenever a PR changes an API route, GraphQL/tRPC resolver, DB column, webhook handler, queue job, permission rule, or config flag, **trace its callers/consumers forward across the repo or monorepo** (`Grep` endpoint paths, client SDK methods, DTO fields, or status enums -> UI pages/tables/modals/forms) to find **which frontend screen or user flow eventually uses or displays this work**.
   - Obtain real visual images (`$WORK_DIR/visuals/`) in this priority order:
     1. **Figma designs:** Export the relevant frame PNG via Figma MCP (`get_screenshot` / node export) from any Figma URL in the PR, ticket, or code comments, and attach `figma_url`.
     2. **Running local app / Storybook / browser:** Open the **impacted frontend screen** (even if its `.tsx`/`.vue` file wasn't edited in this PR!) via Playwright, Chrome DevTools MCP, or local preview URL and save a clean screenshot into `$WORK_DIR/visuals/`.
     3. **PR/repo visual assets:** Download PR/issue image attachments from `$WORK_DIR/hunks-readable.txt` or copy matching E2E/Playwright/docs screenshots (`__screenshots__`, `e2e`, `docs`, `assets`) into `$WORK_DIR/visuals/`.
   - **Inspect with `Read` -> Add Red Marks (`annotations[]`):** View every image file with your `Read` tool first to verify exact visual coordinates, then draw **red circles (`circle`), directional arrows (`arrow`), or boxes (`box`)** in `0..100` percentage coordinates with short labels explaining *what this code is for on that screen*. Unmarked raw screenshots are rejected by `build_artifact.py`!

---

### Step 3: Design `plan.json` (Big-Picture Overview, Chapters, Timeline & Super Casual Voice)

Read `<SKILL_DIR>/references/plan-schema.md` and `<SKILL_DIR>/references/cognitive-order-guide.md`, then write `$WORK_DIR/plan.json`.

#### A. Big-Picture Overview (`overview`): Explain the Whole Task First + Screenshots
- **`overview.whole_task` (Required):** Explain **the whole task**—the big picture of what we're building or fixing overall (even if it spans multiple PRs). Keep it **short (1–2 sentences)**, **super casual**, and in **simple everyday English**.
- **`overview.visuals` (Big-Picture Screenshots Up Front):** Place at least one **red-marked screenshot** right inside `overview.visuals` whenever UI visuals apply so the reviewer sees what the whole feature looks like on the very first screen—before reading any code!
- **`overview.this_pr_part` (Optional — only when this PR is part of a bigger task):** If this PR only builds one piece of a larger task/epic, casually state in 1 short sentence **what part we built in this PR** (and what comes separately). **If this PR implements the whole task end-to-end, omit `this_pr_part` completely!**
- **`overview.before_state` & `overview.after_state`:** 1 short, casual sentence each showing life before vs. after.

#### B. Chapters (`chapters[]`), 2-Level Area Scopes (`file_scopes`), & Zero-Forward-Reference Order
- For any PR with **4+ steps or 2+ logical concerns**, group `steps` into ordered **`chapters`** (displayed as Arc-style tabs in the left sidebar: `Chapter 1`, `Chapter 2`, `Chapter 3`...), ordered so Chapter 2 builds cleanly on Chapter 1.
- Inside each chapter, order steps from **`contracts` -> `core` -> `wiring` -> `verification`**, tucking lockfiles/exports into `mechanical` at the end.
- Define top-level **`file_scopes`** mapping each changed file path to a 2-level `{ "surface": "...", "area": "..." }` pair (`surface` in `"Frontend"`, `"Backend & API"`, `"Database & Contracts"`, `"Tests & QA"`, `"Infra & Config"`; `area` as a clean 2–3 word feature/module part like `"Explore Metric Drawer"` or `"Rule Counts Route"`—**never** an exact filename). This powers the pronounced Top-Bar Area Filter so FE or BE reviewers can isolate their areas in one click.

#### C. Cumulative Timeline File Evolution & Sub-Hunk Slicing (`H007:1-34,52`)
- `build_artifact.py` reconstructs each file **as it exists at Step $N$**: earlier steps are already merged into the file, Step $N$ glows `+`/`−`, and future steps **do not exist in that file yet**.
- Whenever a single large hunk or new file mixes multiple stages (e.g., types + core method + later lifecycle helpers, or imports + logic), **slice it by line range** (`"H007:1-34,52"` in Step 2, `"H007:35-51"` in Step 3) so the file evolves cleanly step by step along the timeline!
- When a single step still contains a **large or dense diff (`>= 20` lines, complex function, tricky SQL, or multi-branch handler)** that belongs together in one view, attach **2–5 `diff_highlights`** (`{"hunk_id": "H007", "lines": "14-29", "title": "...", "explanation": "..."}`) on the non-obvious blocks. Hovering tints that block across green/red/gray lines, and clicking opens a side-margin explanation without covering code. **Do NOT overdo it:** skip short/obvious diffs (`< 20` lines, imports, simple types), and cap at `<= 5` blocks per diff card (`build_artifact.py` rejects `> 5`).

#### D. Task-Introduced & Existing Repo Concept Modals (`concepts[]`)
- Define 4–10 `concepts` covering **BOTH**:
  1. **New words, domain concepts, states, tables, or helpers introduced by the task at hand** (`"origin": "introduced"`), AND
  2. **Existing repo services, routes, queues, or domain terms** touched or relied on by this PR (`"origin": "existing"`).
- Write each concept **casually from scratch for a capable engineer who has never seen this task or repo before**—plain, concrete facts (owning file, callers, sync vs. async, failure behavior, and how this PR sets it up or uses it). **Never** use childish metaphors.

#### E. Keep Everything SUPER Casual + Simple English + Formatting Rotation (VERY IMPORTANT RULE)
- **Keep Everything Super Casual Across the Whole Board:** Every single piece of writing—`overview`, chapter summaries, step titles, `explanation`, `points`, `table`, `diff_highlights`, screenshot `caption`s, `concepts`, `callouts`, and `mechanical`—**MUST sound like a friendly engineer walking a teammate through the code at their desk or on Slack**.
  - **Use natural conversational phrasing:** Use `we`, `you`, and natural contractions (`we're`, `don't`, `it's`, `won't`, `isn't`, `"Right now..."`, `"Heads up:"`, `"If Postgres is down..."`).
  - **Zero stiff or robotic documentation tone:** Never write dry third-person spec prose (*"Defines X before any query logic uses them"*, *"Extends X with Y"*, *"Wires X into the container"*, *"This service is responsible for..."*) or heavy AI words (*orchestrates, leverages, materializes, facilitates, subsequently, aforementioned*). Say *"Sets up the lock table first so..."*, *"Hooks the lock check into `/webhooks`..."*, *"Skips duplicates right away"*.
- **Short & Simple (`<= 20` words):** One clear idea per sentence using everyday words (*saves, checks, skips, calls, fails, deletes*).
- **1-Sentence Invariant & 5-Item Cap:** Step `explanation` MUST be **1 single casual sentence** (BLUF). Cap `points`, `table.rows`, `callouts`, and `untested_gaps` at **<= 5 items**, with <= 1 sentence per item.
- **Dynamic Visual Modulation (Rotate Containers):** Never write walls of prose. Pair the 1-sentence `explanation` with a compact `table` for state/input->outcome rules, bold-anchored `points` (`{"anchor": "...", "text": "..."}`) for mechanics, `visuals` for UI/Figma screens, and `callouts` only for real risks/assumptions.

#### F. Compiler-Enforced Red-Marked Visuals (`visuals[]`)
- Put big-picture red-marked screenshot(s) in `overview.visuals` and step-specific screenshots in `steps[*].visuals` wherever code connects to a user-visible screen, admin table, badge, form, or flow—including backend steps whose output feeds a frontend UI.
- Every screenshot item with `src` **MUST** exist on disk and **MUST** contain at least one `circle`, `arrow`, or `box` in `annotations[]`.
- `build_artifact.py` **fails with exit code `2`** if zero red-marked screenshots are attached without checking first. When **no UI consumer exists anywhere** (e.g., pure CLI, SDK, infra, or internal job) or no app/Figma image is reachable, set top-level `"no_visuals_justification"` in `plan.json` explaining what you checked—the build then succeeds cleanly (`exit 0`) and drops the screenshot section without clutter.

---

### Step 3.5: Fresh-Eyes Semantic Concept & Link Sweep (Mandatory Second Pass)

`build_artifact.py` **rejects `plan.json` (`exit 2`)** until this dedicated second sweep runs! Reviewers often filter straight into Chapter 2 or 3, so every task-introduced concept and repo term must be clickable **whenever they encounter it**—including indirect casual phrases (*"frees the lock"*, *"when a feed goes quiet"*), not just exact code identifiers.

1. **Dump ONLY `concepts[]` and prose slots** (tiny ~60-line JSON, zero raw diffs or coordinates):
   ```bash
   python3 "<SKILL_DIR>/scripts/concept_sweep.py" dump --plan "$WORK_DIR/plan.json" > "$WORK_DIR/prose-dump.json"
   ```
2. **Run a Fresh-Eyes Semantic Linker Subagent** (invoke the `Agent`/`Task` tool—or perform a dedicated cold-reader pass if subagents are unavailable—passing **ONLY** `$WORK_DIR/prose-dump.json`, **NOT** the raw git diff, so it reads with genuine fresh reviewer eyes) to write `$WORK_DIR/concept-links.json`:
   - **Spot missing concepts:** Add any unexplained task-introduced term, new status/rule, or domain concept into `"new_concepts"` (`"origin": "introduced"` or `"existing"`, written super casually).
   - **Semantically link every mention across all slots:** In `"rewrites"`, wrap `[[concept-id|exact phrase in text]]` **every time a slot talks about a concept** across `overview`, `chapters`, `steps`, `points`, `table`, `visuals[*].caption`, `diff_highlights`, `callouts`, and `untested_gaps`—including natural paraphrases (`[[claimed-lease|its 60s timer ran out]]`, `[[webhook-dispatcher|billing and repo-sync handlers]]`), while **skipping** generic English coincidences (*"as a rule"*, *"not an issue"*). Link a concept at most once per single sentence, while re-linking it in every new sentence, bullet, cell, caption, and step!
3. **Apply the semantic patch** (validates every slot key + concept ID and sets `"concept_sweep_completed": true`):
   ```bash
   python3 "<SKILL_DIR>/scripts/concept_sweep.py" apply --plan "$WORK_DIR/plan.json" --patch "$WORK_DIR/concept-links.json"
   ```

---

### Step 4: Compile & Verify 100% Diff Coverage + Visual Grounding + Semantic Sweep

```bash
python3 "<SKILL_DIR>/scripts/build_artifact.py" \
  --manifest "$WORK_DIR/diff-manifest.json" \
  --plan "$WORK_DIR/plan.json" \
  --assets-dir "$WORK_DIR" \
  --out "$WORK_DIR/unmaze-review.html" \
  --markdown-out "$WORK_DIR/unmaze-review.md"
```

If `build_artifact.py` exits with code `2` (unclaimed/overlapping hunk lines, missing red-marked screenshots, unannotated image, or schema error), fix `$WORK_DIR/plan.json` (and capture/annotate the impacted UI screenshot if missing) and re-run until `✓ Verified 100% diff coverage` succeeds.

---

### Step 5: Publish Artifact & Present Concise Summary

1. **Publish via Claude Code `Artifact` tool (Primary):** Read `$WORK_DIR/unmaze-review.html` and invoke the `Artifact` tool so the user receives a live shareable `claude.ai/code/artifact/...` URL.
2. **Local Fallback:** Include `$WORK_DIR/unmaze-review.html` (and `$WORK_DIR/unmaze-review.md`), opening locally only if `Artifact` is unavailable or `--open` was passed.
3. **Low-Load Chat Summary:** Give a 1-sentence BLUF, a compact chapter/step table, top risks/gaps, and the Artifact link.
