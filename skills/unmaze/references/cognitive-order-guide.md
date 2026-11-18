# Cognitive Ordering, Timeline Slicing & Super Casual Voice Guide

Raw `git diff` sorts files alphabetically (`a.ts` -> `z.ts`), dumping every changed file in its final state at once. Use these rules so a reviewer can understand even a 2,500-line AI-generated PR without mental fatigue.

---

## 1. Keep Everything SUPER Casual Across the Entire Board (Critical Rule)

Every piece of text—`overview`, chapter summaries, step titles, `explanation`, `points`, `table`, `diff_highlights`, screenshot `caption`s, `concepts`, `callouts`, and `mechanical`—**must read like a friendly teammate walking you through the PR over Slack or at your desk**.

| Stiff / Formal Docs Voice (Never Write This) | Super Casual Teammate Voice (Always Write This) |
| :--- | :--- |
| `"Defines webhook_idempotency_keys and IdempotencyStore types before any query logic uses them."` | `"First we set up the Postgres lock table and types that everything else uses."` |
| `"Extends PostgresIdempotencyStore with completion and release lifecycle methods."` | `"Adds markCompleted() and releaseClaim() so the route can finish or free a lock."` |
| `"Wires PostgresIdempotencyStore into the dependency container and wraps WebhookDispatcher."` | `"Hooks our new lock store into /webhooks/:provider so duplicate retries stop right at the door."` |
| `"The catch block returns PROCESS on database exceptions."` | `"Heads up: if Postgres hiccups, catch lets the webhook run anyway—check if billing should return 500 instead."` |

- **Talk like a human teammate:** Use `we`, `you`, and natural contractions (`we're`, `don't`, `it's`, `won't`, `isn't`, `"Right now..."`, `"Heads up:"`, `"Here's the catch:"`).
- **Keep it short & simple (`<= 20` words):** Use plain words (*saves, checks, skips, calls, fails, deletes*)—ban stiff AI words (*orchestrates, leverages, materializes, facilitates, subsequently, aforementioned*).
- **Casual != sloppy or childish:** Keep engineering facts 100% accurate and skip fairy-tale metaphors (*"like a bouncer at a club"*).
- **1-Sentence Invariant & 5-Item Cap + Rotation:** `explanation` is 1 single casual sentence. Cap `points`, `table.rows`, `callouts`, and `untested_gaps` at `<= 5` items (`<= 1` sentence each), and rotate tables, bold-anchored bullets, screenshots, and callouts instead of writing prose paragraphs.

---

## 2. Overview: Whole Task in One Breath, Then Everything About THIS PR

A PR is often one piece of a bigger task. Reviewers need the big picture, but they approve **this PR**—so the Overview spends most of its space on what this PR touches and what ships with it:
- **Links first (`overview.links`):** the main ticket, the design file and the spec as pills under the title, whenever they exist.
- **Whole task in 1–2 sentences (`overview.whole_task`):** the overall product or engineering goal, in simple casual English. Never longer than the "This PR" part.
- **"This PR" block right under it:** `this_pr_part` (only for a partial slice), the **change footprint** (`overview.footprint`: every deployable unit touched, with new or changed entry points) and the **ship facts** (`overview.ship_facts`: migrations, backfills, config, flags, dependencies, contract changes, deploy order—"none" said out loud).
- **One hero screenshot (`overview.visuals`):** the whole feature with red marks on what this PR feeds. Step-specific crops stay in their steps; never show the same screenshot twice.

## 3. Split Multi-Concern Diffs Into Ordered Chapters (`chapters[]`)

When a PR has more than ~4 steps or touches multiple subsystems (e.g., DB storage + API routes + Admin UI):
- Split the review into **2–5 ordered chapters** (`Chapter 1`, `Chapter 2`, `Chapter 3`), ordered by dependency so `Chapter 1` makes complete sense on its own before `Chapter 2` builds on it.
- Give each chapter its own tight `contracts -> core -> wiring -> verification` loop (**2–5 steps per chapter**), and tag `file_scopes` with semantic 2–3 word `area` names so FE and BE reviewers can filter to their own areas in the top bar.
- **Spend steps where review attention matters (Step Budget):** about one step per ~150 lines of production code, 5–12 steps total (tests, docs and lockfiles don't count). Fold tests into the step they prove, merge tiny steps (< ~12 changed lines) and small `contracts` steps into their neighbours, and send plans, docs and evidence to the Appendix (`mechanical`). `build_artifact.py` rejects over-split reviews and names the steps to merge.

---

## 4. Evolve Files Along the Timeline (`H007:start-end`) & Highlight Tricky Blocks

When an AI creates a large new file or big contiguous edit (`H007`, lines `1..180`) that spans multiple concepts:
- **Slice it across steps** instead of dumping 180 lines in one card! Include closing braces when slicing classes/blocks (e.g., `"H007:1-34,52"` in Step 2, `"H007:35-51"` in Step 3) so Step 2 never leaks Step 3's methods early.
- When a single function or new file inside a step is still large or dense (`>= 20` lines of SQL, state logic, or branching), add **2–5 casual `diff_highlights`** on the tricky line ranges (`"lines": "14-29"`). Skip obvious imports, getters, or short diffs (`< 20` lines).

---

## 5. Two-Sweep Concept Extraction, Grounded Explanations & Red-Marked Screenshots

- **Sweep 1 — Extract & Filter Candidates Cold From the Written Text (`dump-prose` -> `save-candidates`):** Read the finished casual text with zero code bias, pull out candidate terms (both `"introduced"` by this task and `"existing"` in the repo), and filter out generic tech noise (`keep: false`) so only concepts worth explaining (`keep: true`) survive.
- **Sweep 2 — Repo-Grounded Casual Explanations & Semantic Linking (`apply`):** Look up the actual owning files and callers in the repo/diff for **only** the confirmed `keep: true` concepts (never guess from prose!), write casual explanations, and wrap `[[concept-id|natural phrase]]` wherever those concepts are talked about—skipping everyday English coincidences (*"as a rule"*, *"not an issue"*). Link each concept **once per step** (first mention), and never on a vague phrase like *"the report"* or *"it"*.
- **Red-Marked Screenshots — Even on Backend PRs (`visuals[]`):** Trace backend, API, and DB changes forward to the frontend page, admin table, badge, or modal they power. Capture it (or export Figma), inspect it with `Read`, and draw red `circle`, `arrow`, or `box` marks showing what this code is for.
