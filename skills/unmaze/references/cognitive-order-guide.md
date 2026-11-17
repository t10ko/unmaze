# Cognitive Ordering, Timeline Slicing & Simple English Guide

Raw `git diff` sorts files alphabetically (`a.ts` -> `z.ts`), dumping every changed file in its final state at once. Use these rules so a reviewer can understand even a 2,500-line AI-generated PR without mental fatigue.

---

## 1. Split Multi-Concern Diffs Into Ordered Chapters (`chapters[]`)

When a PR has more than ~4 steps or touches multiple subsystems (e.g., DB storage + API routes + Admin UI):
- Split the review into **2–5 ordered chapters** (`Chapter 1`, `Chapter 2`, `Chapter 3`), ordered by dependency so `Chapter 1` makes complete sense on its own before `Chapter 2` builds on it.
- Give each chapter its own tight `contracts -> core -> wiring -> verification` loop (2–4 steps per chapter), and tag `file_scopes` with semantic 2–3 word `area` names so FE and BE reviewers can filter to their own areas in the top bar.

---

## 2. Evolve Files Along the Timeline (`H007:start-end`)

When an AI creates a large new file or big contiguous edit (`H007`, lines `1..180`) that spans multiple concepts:
- **Slice it across steps** instead of dumping 180 lines in one card!
- Include closing braces when slicing classes/blocks (e.g., `"H007:1-34,52"` in Step 2 for the class + primary method + closing `}`, then `"H007:35-51"` in Step 3 for secondary helper methods).
- Because `build_artifact.py` computes cumulative file state $F^{(t)}$, Step 2 will show a clean 35-line class without leaking Step 3's methods early, and Step 3 will show Step 2's code already in place above while adding lines `35..51`.
- When a single function or new file inside a step is still large or dense (`>= 20` lines of SQL, state logic, or branching) and belongs together in one view, add **2–5 `diff_highlights`** on the tricky line ranges (`"lines": "14-29"`). Do not highlight obvious imports, getters, or short diffs (`< 20` lines).

---

## 3. Explain Touched Repo Concepts From Scratch (`concepts[]`)

In the AI coding era, reviewers often did not write the surrounding modules and don't hold the global project map in memory.
- Whenever the PR calls, wraps, or modifies a non-obvious repo module, table, queue, or domain rule, add an entry in `concepts[]`.
- Write for **a capable engineer who joined this repo 5 minutes ago**:
  - **Do:** State which file owns it, what calls it, whether it runs sync or async, what state it mutates, and how it fails.
  - **Don't:** Use childish metaphors (*"think of it like a bouncer at a club"*).

---

## 4. Always Post Red-Marked Screenshots — Even on Backend PRs

Reviewers grasp backend, API, and database changes 5x faster when they can see **where in the product that data or behavior actually shows up**:
- **Trace backend -> frontend:** When a PR only edits SQL, services, or API routes, grep for which frontend page, admin table, form, or modal calls that endpoint or renders that state.
- **Capture & mark in red:** Screenshot that impacted UI (or export its Figma frame), inspect it with `Read`, and place red `circle`, `arrow`, or `box` marks showing *what this backend change is for*.
- **Never post unmarked screenshots:** Raw screenshots force the reviewer to play "Where's Waldo?". Every screenshot must have red marks pointing right at the impacted area.

---

## 5. Simple English & Style Formatting Rotation

Follow these rules on every sentence in `plan.json`:
- **Simple everyday English:** Short sentences (`<= 20` words), one idea per sentence. Prefer *saves, checks, skips, calls, fails, deletes* over *orchestrates, leverages, materializes, facilitates, subsequently*.
- **1-Sentence Invariant & 5-Item Cap:** `explanation` is 1 single sentence (BLUF). Every bullet, table cell, or callout is <= 1 sentence, with at most 5 items per list or table.
- **Rotate visual containers:** Never write consecutive prose paragraphs. Pair the 1-sentence `explanation` with a **table** (for input/state -> behavior rules), **bold-anchored bullets** (`points` for step mechanics), an **annotated screenshot** (`visuals` with red circles/arrows), or a **callout** (only when a real risk, assumption, or question exists).
