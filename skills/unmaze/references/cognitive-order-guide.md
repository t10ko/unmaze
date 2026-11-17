# Cognitive Ordering & AI-Era Code Review Heuristics

Raw `git diff` sorts files alphabetically (`a.ts` -> `z.ts`). That forces a human reviewer to read tests, route tables, and glue code full of unfamiliar types and functions before seeing what those types or functions even are.

Use these rules when constructing `plan.json` so a human can review a 2,000-line AI-generated PR with minimal working-memory load.

---

## 1. The Zero-Forward-Reference Rule

Before placing Hunk $B$ before Hunk $A$, ask:
> *"To understand Hunk $B$ without guessing, does the reader already need a type, state concept, or invariant defined in Hunk $A$?"*

If yes, **Hunk $A$ comes first**—even if Hunk $B$ is in `api/app.ts` and Hunk $A$ is deep in `packages/domain/types.ts`.

### Standard Sequence for a Cohesive Change
1. **Step 0 — Global Anchor (`overview` + `architecture_map`):**
   - Always include **1–2 `"unchanged"` landmark nodes** in `architecture_map.nodes` (such as the existing API gateway, job runner, or database) by quickly checking imports/callers in the repo. In the AI era, reviewers often haven't touched that subsystem manually—anchoring the diff to unchanged landmarks eliminates disorientation.
2. **Layer 1 — `contracts` (The Nouns):**
   - Schemas, SQL migrations, TypeScript interfaces, Rust structs/enums, Python dataclasses/Pydantic models, API request/response shapes.
   - Keep this tight: once the reviewer sees the data shapes, reading functions that transform them becomes effortless.
3. **Layer 2 — `core` (The Brain):**
   - Put the real algorithmic, business-rule, security, or state-machine hunks **while the reviewer's mental energy is at 100%**.
   - Split distinct mechanisms into separate `core` steps (aim for **1–5 hunks per step**, ~30–150 lines per step) rather than dumping 18 hunks into one giant `core` step.
4. **Layer 3 — `wiring` (The Nerves & Plumbing):**
   - HTTP routes, GraphQL resolvers, CLI argument wiring, React prop-passing, dependency injection, config files.
   - At this point, checking wiring is a fast sanity check: *"Does it pass the right inputs to the core function we just reviewed, and handle errors/auth properly?"*
5. **Layer 4 — `verification` (Proof & Blind Spots):**
   - Group tests by behavior verified, and **always populate `untested_gaps`** when you notice happy-path-only AI tests, over-mocked tests that only test the mock, or missing error/race/boundary cases.
6. **Appendix — `mechanical` (Zero-Decision Noise):**
   - Lockfiles, generated files, pure barrel re-exports, mechanical symbol renames, formatting-only hunks.
   - **Warning:** Inspect non-lockfile hunks before marking them `mechanical`! Never hide a subtle default-value change, auth bypass, or feature-flag flip inside `mechanical`.

---

## 2. Multi-Slice PRs (When One PR Does Two or Three Things)

AI-generated PRs frequently bundle multiple sub-features or a refactor + a feature.
- **Refactor + Feature:** Put pure structural extraction/refactoring only *after* core review if it's mechanical rename noise, OR if the new feature builds directly on a new abstraction, introduce the new interface (`contracts`) -> new behavior (`core`) -> migration of existing callers (`wiring`).
- **Two Independent Vertical Slices (e.g., "Backend Rate Limiter" + "Admin Dashboard UI"):**
  - Order **slice by slice**, NOT all contracts across unrelated features jumbled together:
    - Slice 1 (`"slice": "Rate Limiter"`): `contracts` -> `core` -> `wiring` -> `verification`
    - Slice 2 (`"slice": "Admin UI"`): `contracts` -> `core` -> `wiring` -> `verification`

---

## 3. High-Value AI-Code Callouts to Surface

AI coding harnesses produce clean-looking syntax that hides subtle structural flaws. Actively look for and flag these via `callouts`:
- **Silent fallback / swallowed errors (`risk`):** Catch blocks that return `null`/`[]` or log-and-continue when they should fail loudly.
- **Invented defaults & magic timeouts (`assumption`):** Hardcoded TTLs, batch sizes, retry counts, or fail-open vs. fail-closed behavior the user never specified.
- **Missing auth / tenant scoping (`risk`):** New endpoints or queries querying by `id` without `org_id` / `user_id` scoping—check neighboring endpoints in the repo to verify convention!
- **Tautological AI tests (`risk` / `untested_gaps`):** Tests that mock the exact unit under test and only assert that the mock was called.
- **Redundant parallel abstractions (`question`):** AI creating a brand-new helper/client when the codebase already has an established utility for the same job.
