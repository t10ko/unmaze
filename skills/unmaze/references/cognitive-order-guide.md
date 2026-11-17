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

## 2. Start Overview With the Whole Task + Screenshots (Then Optional PR Part)

A PR is often just one piece of a bigger task or multi-PR project (though sometimes one PR ships the whole thing). Either way, a reviewer gets lost if you jump straight into local code details without showing the big picture first:
- **Explain the whole task (`overview.whole_task`):** In 1–2 short, super casual sentences in simple English, explain the **overall product or engineering goal** we're solving.
- **Show big-picture red-marked screenshots right on Overview (`overview.visuals`):** Whenever UI visuals apply, put at least one red-marked screenshot right in `overview.visuals` so the reviewer sees what the whole thing looks like immediately on the first screen.
- **Say what part this PR built—only when it's a partial slice (`overview.this_pr_part`):** If this PR is part of a bigger task, add 1 casual sentence saying which piece we built in this PR (and what's separate). **If this PR builds the whole task end-to-end, omit `this_pr_part` completely!**

---

## 3. Split Multi-Concern Diffs Into Ordered Chapters (`chapters[]`)

When a PR has more than ~4 steps or touches multiple subsystems (e.g., DB storage + API routes + Admin UI):
- Split the review into **2–5 ordered chapters** (`Chapter 1`, `Chapter 2`, `Chapter 3`), ordered by dependency so `Chapter 1` makes complete sense on its own before `Chapter 2` builds on it.
- Give each chapter its own tight `contracts -> core -> wiring -> verification` loop (2–4 steps per chapter), and tag `file_scopes` with semantic 2–3 word `area` names so FE and BE reviewers can filter to their own areas in the top bar.

---

## 4. Evolve Files Along the Timeline (`H007:start-end`) & Highlight Tricky Blocks

When an AI creates a large new file or big contiguous edit (`H007`, lines `1..180`) that spans multiple concepts:
- **Slice it across steps** instead of dumping 180 lines in one card! Include closing braces when slicing classes/blocks (e.g., `"H007:1-34,52"` in Step 2, `"H007:35-51"` in Step 3) so Step 2 never leaks Step 3's methods early.
- When a single function or new file inside a step is still large or dense (`>= 20` lines of SQL, state logic, or branching), add **2–5 casual `diff_highlights`** on the tricky line ranges (`"lines": "14-29"`). Skip obvious imports, getters, or short diffs (`< 20` lines).

---

## 5. Explain Repo Concepts From Scratch & Always Post Red-Marked Screenshots

- **Repo Concept Modals (`concepts[]`):** Whenever the PR touches a non-obvious service, table, queue, or domain rule, explain it casually from zero for an engineer who joined the repo 5 minutes ago (owning file, callers, sync vs. async, and failure behavior).
- **Red-Marked Screenshots — Even on Backend PRs (`visuals[]`):** Trace backend, API, and DB changes forward to the frontend page, admin table, badge, or modal they power. Capture it (or export Figma), inspect it with `Read`, and draw red `circle`, `arrow`, or `box` marks showing what this code is for.
