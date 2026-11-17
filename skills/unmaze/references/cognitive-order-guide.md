# Cognitive Ordering, Timeline Slicing & Simple English Guide

Raw `git diff` sorts files alphabetically (`a.ts` -> `z.ts`), dumping every changed file in its final state at once. Use these rules so a reviewer can understand even a 2,500-line AI-generated PR without mental fatigue.

---

## 1. Emulate Stacked PRs (`chapters[]`) on Multi-Concern Diffs

When a PR has more than ~4 steps or touches multiple subsystems (e.g., DB storage + API routes + Admin UI):
- Split the review into **2–5 Virtual Stacked PR chapters** (`PR 1`, `PR 2`, `PR 3`), ordered by dependency so `PR 1` makes complete sense on its own before `PR 2` builds on it.
- Give each chapter its own tight `contracts -> core -> wiring -> verification` loop (2–4 steps per chapter).

---

## 2. Evolve Files Along the Timeline (`H007:start-end`)

When an AI creates a large new file or big contiguous edit (`H007`, lines `1..180`) that spans multiple concepts:
- **Slice it across steps** instead of dumping 180 lines in one card!
- Include closing braces when slicing classes/blocks (e.g., `"H007:1-34,52"` in Step 2 for the class + primary method + closing `}`, then `"H007:35-51"` in Step 3 for secondary helper methods).
- Because `build_artifact.py` computes cumulative file state $F^{(t)}$, Step 2 will show a clean 35-line class without leaking Step 3's methods early, and Step 3 will show Step 2's code already in place above while adding lines `35..51`.

---

## 3. Explain Touched Repo Concepts From Scratch (`concepts[]`)

In the AI coding era, reviewers often did not write the surrounding modules and don't hold the global project map in memory.
- Whenever the PR calls, wraps, or modifies a non-obvious repo module, table, queue, or domain rule, add an entry in `concepts[]`.
- Write for **a capable engineer who joined this repo 5 minutes ago**:
  - **Do:** State which file owns it, what calls it, whether it runs sync or async, what state it mutates, and how it fails.
  - **Don't:** Use childish metaphors (*"think of it like a bouncer at a club"*).

---

## 4. Simple English & Style Formatting Rotation

Follow these rules on every sentence in `plan.json`:
- **Simple everyday English:** Short sentences (`<= 20` words), one idea per sentence. Prefer *saves, checks, skips, calls, fails, deletes* over *orchestrates, leverages, materializes, facilitates, subsequently*.
- **1-Sentence Invariant & 5-Item Cap:** `explanation` is 1 single sentence (BLUF). Every bullet, table cell, or callout is <= 1 sentence, with at most 5 items per list or table.
- **Rotate visual containers:** Never write consecutive prose paragraphs. Pair the 1-sentence `explanation` with a **table** (for input/state -> behavior rules), **bold-anchored bullets** (`points` for step mechanics), an **annotated screenshot** (`visuals` with red circles/arrows), or a **callout** (only when a real risk, assumption, or question exists).
