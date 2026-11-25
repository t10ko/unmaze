---
name: unmaze
description: Resurrects human code review in the AI era by turning sprawling pull requests and git diffs into a shareable, step-by-step interactive review guide—with Arc-style stacked PR chapters, cumulative file evolution across the timeline, clickable repo concept modals, annotated Figma/UI screenshots, and verified 100% diff coverage. Use when asked to review a PR, walk through a diff, explain branch changes, untangle a large AI-generated PR, or when invoked via /unmaze.
argument-hint: "[pr-number | pr-url | git-range | --staged | path.patch | --refresh]"
license: MIT
compatibility: Requires Python 3.9+ and git (plus gh CLI when fetching GitHub PRs by number or URL).
metadata:
  author: t10ko
  version: "0.11.0"
---

# unmaze — Resurrecting Human Code Review in the AI Era

GitHub and Git sort diffs alphabetically (`a.ts` -> `z.ts`), dumping 30 files at once on reviewers who didn't write the code by hand and may not know how that subsystem works globally. `unmaze` turns any PR or Git diff into a **guided, low-cognitive-load interactive Artifact** with:

- **Big-Picture Overview First, Then "This PR"** (one casual sentence on the **whole task**, then a "This PR" block right under it: what this PR builds, every deployable part it touches, and its ship facts—migrations, flags, config, dependencies, contract changes and deploy order, with "none" said out loud—before one hero screenshot),
- **Ticket, Design & Spec Links Up Top** (the main ticket, the Figma file and the spec sit as link pills under the Overview title whenever they exist),
- **Smart Screenshot Grouping** (3+ screenshots in one block become a gallery—one large image plus a captioned thumbnail strip; a before/after pair sits side by side; screenshots never grow taller than ~70% of the window),
- **Boards That Stay Fresh** (a published board is remembered per branch, and after every later `git push` Claude refreshes it at the same URL without anyone asking for `/unmaze` again),
- **Super Casual Teammate Voice Everywhere** (written like a teammate walking you through the PR over Slack—contractions, `we`/`you`, zero stiff docs or AI jargon),
- **Arc-style Chapters** (splitting large PRs into ordered chapters—`Chapter 1`, `Chapter 2`—in a clean sidebar),
- **Top-Bar Role & Area Tree Filter** (2-level `Role Surface -> App Area` filter that dims unrelated chapters and steps so FE, BE, or domain engineers can review just their slice),
- **Cumulative Timeline File Evolution** (showing each file *as it exists at that step*, slicing large hunks with `H007:1-34,52` so future steps don't leak early),
- **Inline Code Notes** (a short note sits right above each block a newcomer would have to stop and decode, inside the diff, with its lines tinted and linked to the PR; one "Hide notes" button per file),
- **Semantic Concept Modals Everywhere You Encounter Them** (a dedicated 2nd-pass AI sweep explains **both** brand-new terms/things introduced by this task **and** existing repo concepts, semantically linking casual phrases like `[[claimed-lease|frees the lock]]` wherever they're talked about across every step, bullet, table, caption, and diff note),
- **Mandatory Red-Marked Screenshots — Even on Backend PRs** (traces backend/API/DB changes to the frontend screen they impact and places red circles, boxes, and arrows showing what the code is for), and
- **100% deterministic changed-line accounting**.

User arguments (if any): `$ARGUMENTS`

---

## Workflow

### Step 1: Parse the Diff & Full Post-Change File Context

Resolve `<SKILL_DIR>` as the directory containing this `SKILL.md` file.

**If `$ARGUMENTS` contains `--refresh`** (or a hook message told you a published board is out of date), skip to **Refresh a Published Board** at the end of this file.

Pick a **durable work dir inside the git directory** so the plan survives for later refreshes and never shows up as untracked files (use `pr-<number>` as the key for PRs, the range or branch name otherwise):
```bash
WORK_DIR="$(python3 "<SKILL_DIR>/scripts/board_registry.py" work-dir --key "<pr-142 | branch-or-range>")" \
  || WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/unmaze.XXXXXX")"   # fallback outside a git repo
```

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

#### A. Overview (`overview`): Whole Task in One Breath, Then Everything About THIS PR
A reviewer must be able to answer **"what does this PR touch, and what could break when it ships?"** from the first screen, without reading code. The Overview renders in this order: links → whole task → "This PR" block → hero screenshot → before/after → terms → chapters.
- **`overview.links` (Ticket, Design, Spec):** `[{"kind": "ticket" | "design" | "spec" | "docs" | "other", "label": "PAY-412", "url": "https://..."}]`, at most 6. Take them from the PR body, the ticket and commit messages (`hunks-readable.txt` lists detected ticket and Figma links). Only add links you actually found; Figma URLs on screenshots are added automatically.
- **`overview.whole_task` (Required):** **1–2 short, super casual sentences** on the whole task—the big picture of what we're building or fixing overall (even if it spans multiple PRs). Don't spend more words on it than on this PR.
- **`overview.this_pr_part` (Only when this PR is one slice of a bigger task):** 1 short sentence on **what we built in this PR** and what comes separately. Omit it when this PR ships the whole task.
- **`overview.footprint` (Recommended):** `[{"path": "apps/analytics", "label": "Analytics service", "change": "1 sentence on what changed there", "entry_points": ["GET /api/v1/.../resilience"]}]`—one entry per **deployable unit** (service, app, package, or main module folder in a single-app repo). `path` is a folder or file prefix; files it doesn't cover are grouped automatically by `apps/<x>`, `packages/<x>`, `services/<x>`, ... or their top-level folder, so **every changed file always shows up**. List new or changed routes, jobs, events, commands or public functions in `entry_points` (≤ 5).
- **`overview.diagram` — "How the parts connect" (only when it earns its place, compiler-enforced):** A picture helps only when the hard part of the PR is **how parts call each other**; on most PRs it just repeats the footprint table. Decide like this:
  1. **Draw it** when **3+ parts** are in play and **2+ calls between them are new, changed or removed**, or when a **new or changed asynchronous link** (job, queue, event, webhook) connects two parts (`"async": true`).
  2. **Skip it** when changes stay inside one part, when only one call is new (a sentence in the footprint says it better), or when it would need more than 6 parts or 8 calls. When the PR changes code in 3+ parts, say why in one sentence with `overview.no_diagram_reason` (not shown to readers); the build fails until you either draw it or give the reason.
  3. **Format:** `{"nodes": [{"id", "label", "note", "status": "new" | "changed" | "unchanged" | "planned"}], "edges": [{"from", "to", "label", "status": "new" | "changed" | "removed" | "unchanged" | "planned", "evidence": "path/to/file.py:42", "async": false}]}`. Show only the parts this PR touches plus what they directly call (never the whole system); `label` names the real call (route, event, job, function); `note` is ≤ 4 words.
  4. **Grounding:** every `new`, `changed` or `removed` call needs `evidence`—a file this PR changes plus the line that makes (or used to make) the call. Unverifiable arrows don't go in.
  5. **Look:** one picture with changes marked on it, like a unified diff—new parts and calls green, changed amber, unchanged gray, removed red and dashed, not-built-yet dashed—plus a list of every call linking to its exact line. When every part is new, it renders as a plain diagram with no change colors. The Markdown summary gets the same picture as a Mermaid block, which GitHub renders.
- **`overview.ship_facts` (Required, compiler-enforced):** answer **every** key with one short sentence, `"none"`, or `"unknown"`: `schema_migrations`, `data_backfills`, `config_and_secrets`, `feature_flags`, `dependencies`, `api_contracts`, `deploy_order`. "None" is useful information—say it out loud. Real answers render first; "none" answers collapse into one muted line. Pull these from the diff (migration files, flag checks, env reads, lockfiles, route/schema changes) and from the PR body's rollout or blast-radius notes.
- **`overview.visuals` (One Hero Screenshot, max 3):** One red-marked screenshot of the whole feature, with marks on the parts **this PR** feeds. Step-specific crops belong in their steps—**never repeat in the Overview a screenshot a step already shows**. `build_artifact.py` rejects more than 3.
- **`overview.before_state` & `overview.after_state`:** 1 short, casual sentence each showing life before vs. after.

#### B. Chapters (`chapters[]`), 2-Level Area Scopes (`file_scopes`), & Zero-Forward-Reference Order
- For any PR with **4+ steps or 2+ logical concerns**, group `steps` into ordered **`chapters`** (displayed as Arc-style tabs in the left sidebar: `Chapter 1`, `Chapter 2`, `Chapter 3`...), ordered so Chapter 2 builds cleanly on Chapter 1. When a PR spans several services or packages, one chapter per service usually reads best.
- Inside each chapter, order steps from **`contracts` -> `core` -> `wiring` -> `verification`**, tucking lockfiles/exports into `mechanical` at the end.
- **Every step must be worth a reviewer's stop (Step Budget, compiler-enforced):** about **one step per ~150 lines of production code**, between 5 and 12 steps in total, with 2–5 steps per chapter. Tests, docs and lockfiles don't count toward the budget. To stay inside it:
  - **Fold tests into the step whose code they prove** (the step's diff cards just include the test file). Keep a standalone tests step only when the tests are the point, or one final tests step for a small PR.
  - **Merge a small `contracts` step** (request/response shapes, types) into the `core` step that uses it.
  - **Merge tiny steps** (under ~12 changed lines, e.g. mounting one route or adding one client) into the neighbour they serve.
  - **Put plans, docs, decision logs and evidence files in `mechanical` groups**, which render in the **Appendix**, with a one-line `reason`; link the important ones from `overview.links` instead of walking reviewers through them.
- Define top-level **`file_scopes`** mapping each changed file path to a 2-level `{ "surface": "...", "area": "..." }` pair (`surface` in `"Frontend"`, `"Backend & API"`, `"Database & Contracts"`, `"Tests & QA"`, `"Infra & Config"`; `area` as a clean 2–3 word feature/module part like `"Explore Metric Drawer"` or `"Rule Counts Route"`—**never** an exact filename). This powers the pronounced Top-Bar Area Filter so FE or BE reviewers can isolate their areas in one click.

#### C. Cumulative Timeline File Evolution & Sub-Hunk Slicing (`H007:1-34,52`)
- `build_artifact.py` reconstructs each file **as it exists at Step $N$**: earlier steps are already merged into the file, Step $N$ glows `+`/`−`, and future steps **do not exist in that file yet**.
- Whenever a single large hunk or new file mixes multiple stages (e.g., types + core method + later lifecycle helpers, or imports + logic), **slice it by line range** (`"H007:1-34,52"` in Step 2, `"H007:35-51"` in Step 3) so the file evolves cleanly step by step along the timeline!
- **Code notes (`diff_highlights`): explain what a newcomer would have to stop and decode.** A note sits right above the lines it explains, inside the diff. Decide note by note, never by size or quota:
  - **Add a note** on any block a reviewer who doesn't know this code would have to stop and work out, however short: a non-obvious condition or guard, an order that matters, a query, regex or data-shape transform, a magic number or threshold, retries, caching or concurrency, an error that's swallowed or turned into a fallback, a clever one-liner, or test setup that encodes a business rule.
  - **Skip** imports, plain wiring, renames, simple types, and code that already reads like English.
  - **One idea per note:** usually 3–12 lines, and a single line is fine. Split a long function at the points where it starts doing something different; never stretch one note over a whole function (`build_artifact.py` rejects blocks over 40 lines).
  - **What a note says:** one sentence, two at most (12–60 words), in plain words: what the block does **and** the part that isn't obvious from reading it, such as why it's written this way, which case it handles, or what would break without it. Name concrete values and cases; never just restate identifiers.
  - **Title:** name the idea in a few words (≤ 8), like a heading a reviewer could skim.
  - Format: `{"hunk_id": "H007", "lines": "14-29", "title": "...", "explanation": "..."}`; notes on the same lines can't overlap.

#### D. Keep Everything SUPER Casual + Simple English + Formatting Rotation (VERY IMPORTANT RULE)
- **Keep Everything Super Casual Across the Whole Board:** Every single piece of writing—`overview`, chapter summaries, step titles, `explanation`, `points`, `table`, `diff_highlights`, screenshot `caption`s, `concepts`, `callouts`, and `mechanical`—**MUST sound like a friendly engineer walking a teammate through the code at their desk or on Slack**.
  - **Use natural conversational phrasing:** Use `we`, `you`, and natural contractions (`we're`, `don't`, `it's`, `won't`, `isn't`, `"Right now..."`, `"Heads up:"`, `"If Postgres is down..."`).
  - **Zero stiff or robotic documentation tone:** Never write dry third-person spec prose (*"Defines X before any query logic uses them"*, *"Extends X with Y"*, *"Wires X into the container"*, *"This service is responsible for..."*) or heavy AI words (*orchestrates, leverages, materializes, facilitates, subsequently, aforementioned*). Say *"Sets up the lock table first so..."*, *"Hooks the lock check into `/webhooks`..."*, *"Skips duplicates right away"*.
- **Short & Simple (`<= 20` words):** One clear idea per sentence using everyday words (*saves, checks, skips, calls, fails, deletes*). Code notes are the exception: they may run up to ~60 words so they can say why, not just what.
- **1-Sentence Invariant & 5-Item Cap:** Step `explanation` MUST be **1 single casual sentence** (BLUF). Cap `points`, `table.rows`, `callouts`, and `untested_gaps` at **<= 5 items**, with <= 1 sentence per item.
- **Dynamic Visual Modulation (Rotate Containers):** Never write walls of prose. Pair the 1-sentence `explanation` with a compact `table` for state/input->outcome rules, bold-anchored `points` (`{"anchor": "...", "text": "..."}`) for mechanics, `visuals` for UI/Figma screens, and `callouts` only for real risks/assumptions.

#### E. Compiler-Enforced Red-Marked Visuals (`visuals[]`)
- Put one big-picture hero screenshot in `overview.visuals` and step-specific screenshots in `steps[*].visuals` wherever code connects to a user-visible screen, admin table, badge, form, or flow—including backend steps whose output feeds a frontend UI.
- **Grouping is automatic:** 3+ screenshots in one block render as a gallery (one large image + thumbnail strip), so give each a 2–4 word `"label"` for its thumbnail. Mark a changed screen with `"role": "before"` and `"role": "after"` to show the pair side by side instead.
- Every screenshot item with `src` **MUST** exist on disk and **MUST** contain at least one `circle`, `arrow`, or `box` in `annotations[]`.
- `build_artifact.py` **fails with exit code `2`** if zero red-marked screenshots are attached without checking first. When **no UI consumer exists anywhere** (e.g., pure CLI, SDK, infra, or internal job) or no app/Figma image is reachable, set top-level `"no_visuals_justification"` in `plan.json` explaining what you checked—the build then succeeds cleanly (`exit 0`) and drops the screenshot section without clutter.

---

### Step 4: Sweep 1 — Cold-Reader Candidate Extraction & Filtering From the Written Text

Do **NOT** guess `concepts[]` during drafting (`Step 3` can leave `"concepts": []` empty)! Instead, extract candidates **from the finished casual text** with cold eyes, then filter down to only real concepts worth explaining:

1. **Dump ONLY the written prose slots** (no code diffs, no explanations):
   ```bash
   python3 "<SKILL_DIR>/scripts/concept_sweep.py" dump-prose --plan "$WORK_DIR/plan.json" > "$WORK_DIR/prose-dump.json"
   ```
2. **Run a Cold-Reader Subagent (Prose-Only, Zero Code Bias):** Pass **ONLY** `$WORK_DIR/prose-dump.json` (never the raw diff or repo files!) to a lightweight `Agent`/`Task` subagent (or cold-reader pass) to write `$WORK_DIR/concept-candidates.json` with `"candidates": [...]`:
   - Pull out every unfamiliar or domain-specific term/phrase in the text—both **brand-new things introduced by this task** (`"origin": "introduced"`) and **existing repo/product concepts** (`"origin": "existing"`).
   - Explicitly judge every candidate with boolean `"keep": true` or `"keep": false` + `"reason"`: keep **only real task/product/repo concepts (`4..10` items)** a reviewer genuinely benefits from clicking, and mark generic tech words (`Express`, `Postgres`, `HTTP 400`, `Promise`) or self-explanatory words `"keep": false`.
3. **Validate & save the filtered shortlist:**
   ```bash
   python3 "<SKILL_DIR>/scripts/concept_sweep.py" save-candidates \
     --plan "$WORK_DIR/plan.json" \
     --candidates "$WORK_DIR/concept-candidates.json" > "$WORK_DIR/sweep2-input.json"
   ```

---

### Step 5: Sweep 2 — Repo-Grounded Concept Explanations & Semantic Linking

Now switch back to **full repository + diff context** (`Read` / `Grep` + `$WORK_DIR/hunks-readable.txt`) so zero concept explanations are guessed from prose:

1. Read `$WORK_DIR/sweep2-input.json`. For **every** confirmed concept in `confirmed_concepts_to_explain_in_sweep_2`:
   - Verify the real owning file, callers, sync vs. async execution, edge cases, and PR impact in the codebase/diff.
   - Write `"concepts": [...]` **super casually** (`id`, `term`, `origin`, `file`, `what_it_is`, `how_it_works` bullets, `pr_impact`, `related_steps`)—accurate engineering facts, zero fairy-tale metaphors.
2. Walk `prose_slots` top-to-bottom and write `"rewrites": { ... }` wrapping `[[concept-id|exact phrase in text]]` **every time any confirmed concept is talked about** across `overview`, `chapters`, `steps`, `points`, `table`, `visuals[*].caption`, `diff_highlights`, `callouts`, and `untested_gaps`—including indirect casual phrasing (`[[claimed-lease|its 60s timer ran out]]`, `[[idempotency-store|frees the lock]]`), while skipping generic English coincidences (*"as a rule"*, *"not an issue"*). Link each concept **once per step** (its first mention there); the builder renders later mentions in the same step as plain text anyway. **Never link a vague phrase** like *"the report"*, *"the key"* or *"it"*—link a phrase that names the thing (*"the Impact Report tab"*), or leave it unlinked; `apply` rejects vague links.
3. **Apply Sweep 2** (verifies Sweep 1 ran, verifies every confirmed concept has a grounded explanation, validates all slot paths & `[[...]]` links, and marks `"concept_sweep_completed": true`):
   ```bash
   python3 "<SKILL_DIR>/scripts/concept_sweep.py" apply \
     --plan "$WORK_DIR/plan.json" \
     --patch "$WORK_DIR/concept-links.json"
   ```

---

### Step 6: Compile & Verify 100% Diff Coverage + Visual Grounding + Two-Sweep Pipeline

```bash
python3 "<SKILL_DIR>/scripts/build_artifact.py" \
  --manifest "$WORK_DIR/diff-manifest.json" \
  --plan "$WORK_DIR/plan.json" \
  --assets-dir "$WORK_DIR" \
  --out "$WORK_DIR/unmaze-review.html" \
  --markdown-out "$WORK_DIR/unmaze-review.md"
```

If `build_artifact.py` exits with code `2` (unclaimed/overlapping hunk lines, too many steps for the step budget, missing ship facts, a missing diagram decision or an ungrounded/unneeded diagram, missing red-marked screenshots, skipped Sweep 1/Sweep 2, or schema error), fix the reported issue and re-run until `✓ Verified 100% diff coverage` succeeds.

---

### Step 7: Publish Artifact & Present Concise Summary

1. **Publish via Claude Code `Artifact` tool (Primary):** Read `$WORK_DIR/unmaze-review.html` and invoke the `Artifact` tool so the user receives a live shareable `claude.ai/code/artifact/...` URL.
2. **Remember the board for automatic refreshes:** right after publishing, record it so later pushes keep it fresh at the same URL:
   ```bash
   python3 "<SKILL_DIR>/scripts/board_registry.py" record --work-dir "$WORK_DIR" --url "<artifact-url>"
   ```
   The board is keyed by the PR's head branch. Tell the user in one line that the board now refreshes itself after each push, and that `board_registry.py pause` turns that off.
3. **Local Fallback:** Include `$WORK_DIR/unmaze-review.html` (and `$WORK_DIR/unmaze-review.md`), opening locally only if `Artifact` is unavailable or `--open` was passed.
4. **Low-Load Chat Summary:** Give a 1-sentence BLUF, a compact chapter/step table, top risks/gaps, and the Artifact link.

---

## Refresh a Published Board (`--refresh`, or when a hook says the board is out of date)

The plugin's hook watches `git push` (and session start). When the branch moved past the commit a published board was built from, it tells you to refresh—**do it right away, without waiting for the user to ask for `/unmaze`**, then carry on with what you were doing.

1. **Find the board:** `python3 "<SKILL_DIR>/scripts/board_registry.py" status` prints `url`, `work_dir`, `built_sha`, `current_sha` and `refresh_args`. Set `WORK_DIR` to its `work_dir`. If no board is recorded, run the normal workflow instead.
2. **Re-parse the same PR or range into the same work dir**, keeping the old manifest for the carry-over:
   ```bash
   cp "$WORK_DIR/diff-manifest.json" "$WORK_DIR/diff-manifest.prev.json"
   python3 "<SKILL_DIR>/scripts/parse_diff.py" <refresh_args from status> --out-dir "$WORK_DIR"
   ```
3. **Carry the plan over to the new diff** (renumbered hunks, shifted lines, moved highlights):
   ```bash
   python3 "<SKILL_DIR>/scripts/refresh_plan.py" \
     --old-manifest "$WORK_DIR/diff-manifest.prev.json" --new-manifest "$WORK_DIR/diff-manifest.json" \
     --plan "$WORK_DIR/plan.json" --out "$WORK_DIR/plan.json"
   ```
   Its report lists what you still own: **unclaimed new hunks** (place each in the right step, a new step, or `mechanical`), **steps to re-read** (update their sentence, points, tables, highlights and screenshots to match the new code), **empty steps** (delete them or give them lines) and dropped highlights. Also re-check `overview.footprint`, `overview.ship_facts` and `this_pr_part`. Link any new prose to existing concepts with `[[concept-id|phrase]]`; write a new concept only for a genuinely new term.
4. **Build** exactly as in Step 6 until `✓ Verified 100% diff coverage`.
5. **Republish to the same URL:** read the board with the `Artifact` tool (`action: "read"`, `url`), then publish the new HTML with that `url` so every link to it (PR description, Slack) keeps working.
6. **Record the new commit:** `python3 "<SKILL_DIR>/scripts/board_registry.py" record --work-dir "$WORK_DIR" --url "<same-url>"`.
7. Tell the user in one line that the board was refreshed, what changed in it, and its link.
