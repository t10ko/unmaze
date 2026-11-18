# `plan.json` Schema Reference (v0.8)

`build_artifact.py` merges `diff-manifest.json` with `plan.json` to compile the Arc-sidebar review guide, cumulative timeline file states, semantic concept modals, and annotated UI/Figma screenshots.

**Hard invariants & core writing rules:**
1. **Super Casual Teammate Tone Across the Whole Board:** Every text field in `plan.json` (`overview`, `chapters`, `steps`, `points`, `table`, `diff_highlights`, `visuals[*].caption`, `concepts`, `callouts`, `mechanical`) MUST be written **super casually** in short, simple everyday English—like a teammate walking you through the PR on Slack (`we`, `you`, contractions like `we're`, `don't`, `it's`, zero stiff spec prose or AI jargon).
2. **Overview = Whole Task in One Breath, Then Everything About THIS PR** (render order: `links` → `whole_task` → "This PR" block → hero screenshot → before/after → terms → chapters):
   - `overview.links` (optional): `[{"kind": "ticket" | "design" | "spec" | "docs" | "other", "label", "url"}]`, ≤ 6, absolute http(s) URLs only. Figma URLs on screenshots are added as design links automatically.
   - `overview.whole_task` (required; `tldr` accepted as alias): 1–2 short, super casual sentences explaining **the whole task** (the big picture, even if it spans multiple PRs).
   - `overview.this_pr_part` (optional): Include **only** when this PR builds one slice of a bigger task, casually stating what part we built here. Omit completely when this PR implements the whole task!
   - `overview.footprint` (optional, recommended): `[{"path", "label", "change", "entry_points"}]`—one row per deployable unit (service, app, package, main module). `path` is a folder/file prefix that must match changed files; uncovered files are grouped automatically (`apps/<x>`, `packages/<x>`, `services/<x>`, ... else top-level folder, else `(repo root)`).
   - `overview.diagram` (optional, gated): `{"nodes": [{"id", "label", "note", "status"}], "edges": [{"from", "to", "label", "status", "evidence", "async"}]}`. Node status `new | changed | unchanged | planned`; edge status `new | changed | removed | unchanged | planned`. Allowed only with 3+ real parts and 2+ new/changed/removed calls, or a new/changed `async` link; ≤ 6 parts and ≤ 8 calls; every new/changed/removed call needs `evidence` (`changed/file.py:42`). All-new parts render as a plain diagram, anything else as a change-marked diagram.
   - `overview.no_diagram_reason` (required when the PR changes code in 3+ parts and has no diagram): one sentence on why a picture wouldn't help; never rendered.
   - `overview.ship_facts` (**required**, exit code `2` when missing): every key answered with a short sentence, `"none"`, or `"unknown"`—`schema_migrations`, `data_backfills`, `config_and_secrets`, `feature_flags`, `dependencies`, `api_contracts`, `deploy_order`.
   - `overview.visuals`: **1 hero** red-marked screenshot of the whole feature (max 3, exit code `2` above that). Never repeat a screenshot a step already shows.
3. **Mandatory Two-Sweep Concept Pipeline (`concept_candidates_extracted: true` & `concept_sweep_completed: true`, exit code `2` on violation):**
   - **Sweep 1 (`concept_sweep.py dump-prose` -> `save-candidates`):** A cold prose-only reader extracts candidate terms from the written text and marks `"keep": true` (real task-introduced or repo concepts) vs. `"keep": false` (generic tech noise).
   - **Sweep 2 (`concept_sweep.py apply`):** A repo-grounded pass looks up the actual code/diff for **only** the `keep: true` concepts, writes short casual explanations (`"origin": "introduced" | "existing"`), and semantically wraps `[[concept-id|natural phrase]]` across `prose_slots`.
4. **Step Budget (exit code `2` on violation):** about one step per ~150 lines of production code, clamped to 5–12 steps (tests, docs and lockfiles don't count). Fold tests into the steps they prove, merge tiny or `contracts`-only steps into their neighbours, and put plans/docs/evidence in `mechanical` groups (rendered as the **Appendix**). The error names the steps to merge.
5. **100% Changed-Line Coverage (exit code `2` on violation):** Every changed (`+`/`−`) line in every hunk (`H001`..`HNNN`) MUST be claimed **exactly once** across `steps[*].hunk_ids` and `mechanical.groups[*].hunk_ids` (using whole-hunk `"H007"` or line-slice `"H007:1-34,52"` syntax).
6. **Mandatory Red-Marked Screenshots (Frontend AND Backend PRs, exit code `2` on violation):**
   - Every visual card with `src` MUST exist on disk (or be a valid `data:`/`https:` URL) and MUST include at least one red mark (`circle`, `arrow`, or `box`) inside `annotations[]`.
   - Every `plan.json` MUST attach at least 1 red-marked screenshot across `overview.visuals` or `steps[*].visuals`—even on backend-only PRs whenever a frontend screen consumes or reflects that backend logic.
   - When zero UI consumers exist (e.g., pure CLI/SDK/infra) or no UI/Figma/screenshot is reachable after checking downstream callers, pass top-level `"no_visuals_justification"` (`>= 20` chars) so the build succeeds cleanly (`exit 0`) and omits the screenshot block.

---

## Complete Example

```json
{
  "title": "PR #142: Idempotent Webhook Ingestion",
  "overview": {
    "whole_task": "We're making Stripe and GitHub webhooks safe against retries and showing live dedup status in the Payments Admin Console so support can spot skipped duplicates.",
    "links": [
      { "kind": "ticket", "label": "PAY-412", "url": "https://linear.app/acme/issue/PAY-412" },
      { "kind": "spec", "label": "Webhook dedup spec", "url": "https://github.com/acme/payments/blob/main/docs/webhook-dedup.md" }
    ],
    "this_pr_part": "This PR ships the backend part: the Postgres lock table and `/webhooks/:provider` guard—the Admin Console retry button comes in the next PR.",
    "footprint": [
      {
        "path": "services/idempotency",
        "label": "Idempotency store",
        "change": "New Postgres lock store that claims, finishes and frees one webhook event at a time.",
        "entry_points": ["claimOrSkip()", "markCompleted()", "releaseClaim()"]
      },
      {
        "path": "api/routes/webhooks.ts",
        "label": "Webhook route",
        "change": "`/webhooks/:provider` checks the lock before it calls `WebhookDispatcher`.",
        "entry_points": ["POST /webhooks/:provider"]
      }
    ],
    "diagram": {
      "nodes": [
        { "id": "providers", "label": "Stripe and GitHub", "note": "send webhooks", "status": "unchanged" },
        { "id": "route", "label": "Webhook route", "note": "checks the lock first", "status": "changed" },
        { "id": "store", "label": "Idempotency store", "note": "claims each event once", "status": "new" },
        { "id": "dispatcher", "label": "WebhookDispatcher", "note": "billing and repo sync", "status": "unchanged" }
      ],
      "edges": [
        { "from": "providers", "to": "route", "status": "unchanged", "label": "POST /webhooks/:provider" },
        { "from": "route", "to": "store", "status": "new", "label": "claimOrSkip()", "evidence": "api/routes/webhooks.ts:24" },
        { "from": "route", "to": "dispatcher", "status": "changed", "label": "dispatch() only after a won claim", "evidence": "api/routes/webhooks.ts:30" }
      ]
    },
    "ship_facts": {
      "schema_migrations": "Adds the `webhook_idempotency_keys` table, so run the migration before the new API rolls out.",
      "data_backfills": "none",
      "config_and_secrets": "none",
      "feature_flags": "none",
      "dependencies": "none",
      "api_contracts": "`/webhooks/:provider` now answers duplicates with `200 deduplicated` instead of running them again.",
      "deploy_order": "Migration first, then the API; old pods ignore the new table, so rolling back is safe."
    },
    "before_state": "If Stripe retried a webhook, we just ran `WebhookDispatcher` again and could double-charge a customer.",
    "after_state": "Now `webhookRouter` locks `(provider, event_id)` in `PostgresIdempotencyStore` first and replies `200 OK` right away on duplicates.",
    "visuals": [
      {
        "src": "sample-webhook-ui.png",
        "source_label": "Payments Admin Console (Big Picture)",
        "figma_url": "https://www.figma.com/design/demo/Spec?node-id=18-204",
        "caption": "Here's where this shows up in the Admin Console: duplicate retries get a quick `200 deduplicated` badge while active runs hold a 60s lock.",
        "annotations": [
          { "kind": "circle", "x": 86.2, "y": 42.7, "rx": 8.2, "ry": 5.8, "label": "200 deduplicated" },
          { "kind": "arrow", "from_x": 60.0, "from_y": 29.0, "to_x": 77.2, "to_y": 41.0, "label": "decision != PROCESS" }
        ]
      }
    ]
  },
  "concept_candidates_extracted": true,
  "concept_sweep_completed": true,
  "concepts": [
    {
      "id": "claimed-lease",
      "term": "60s Lease",
      "origin": "introduced",
      "file": "services/idempotency/store.ts",
      "what_it_is": "Short 60-second lock timer we put on a webhook ID while a worker is handling it.",
      "how_it_works": [
        "**Crash recovery:** If a worker dies mid-request, `lease_expires_at` passes and the next retry takes over."
      ],
      "pr_impact": "New in this PR so crashed pods never block retries forever.",
      "related_steps": ["step-1", "step-2", "step-4"]
    },
    {
      "id": "webhook-dispatcher",
      "term": "WebhookDispatcher",
      "origin": "existing",
      "aliases": ["dispatchProviderWebhook", "webhookDispatcher"],
      "file": "services/webhooks/dispatcher.ts",
      "what_it_is": "Hands verified webhooks off to our billing and repo-sync handlers.",
      "how_it_works": [
        "**Runs inline:** Executes right inside the `POST /webhooks/:provider` request with no background queue.",
        "**On error:** Throws when a downstream handler fails so Express returns `500` and the sender retries later."
      ],
      "pr_impact": "We didn't touch its code—`webhookRouter` just skips calling it whenever `PostgresIdempotencyStore` spots a duplicate.",
      "related_steps": ["step-4"]
    }
  ],
  "file_scopes": {
    "migrations/20261117_webhook_idempotency.sql": { "surface": "Database & Contracts", "area": "SQL Lease Table" },
    "services/idempotency/types.ts": { "surface": "Database & Contracts", "area": "Idempotency Types" },
    "services/idempotency/store.ts": { "surface": "Backend & API", "area": "Idempotency Store" },
    "api/routes/webhooks.ts": { "surface": "Backend & API", "area": "Webhook Ingestion Route" },
    "config/container.ts": { "surface": "Infra & Config", "area": "DI Container" },
    "__tests__/webhook-dedup.test.ts": { "surface": "Tests & QA", "area": "Webhook Dedup Tests" },
    "index.ts": { "surface": "Infra & Config", "area": "Exports & Dependencies" },
    "pnpm-lock.yaml": { "surface": "Infra & Config", "area": "Exports & Dependencies" }
  },
  "chapters": [
    {
      "id": "ch-storage",
      "title": "Lease Storage",
      "summary": "Sets up the SQL lock table and the single-query claim check.",
      "step_ids": ["step-1", "step-2"]
    },
    {
      "id": "ch-guard",
      "title": "Webhook Guard & UI",
      "summary": "Adds finish/undo helpers, hooks them into `/webhooks/:provider`, and checks the tests.",
      "step_ids": ["step-3", "step-4", "step-5"]
    }
  ],
  "steps": [
    {
      "id": "step-1",
      "layer": "contracts",
      "title": "Lock table and TypeScript types",
      "explanation": "First we set up the `webhook_idempotency_keys` table and `IdempotencyStore` types that everything else uses.",
      "table": {
        "headers": ["Status", "What It Means"],
        "rows": [
          ["`CLAIMED`", "A worker is running this event right now until `lease_expires_at`."],
          ["`COMPLETED`", "We already finished this event, so any retry gets skipped."]
        ]
      },
      "hunk_ids": ["H005", "H008"]
    },
    {
      "id": "step-2",
      "layer": "core",
      "title": "Single-query lock with 60s crash recovery",
      "explanation": "Next we add `claimOrSkip()` on `PostgresIdempotencyStore` so only one worker can grab a new or expired event.",
      "points": [
        { "anchor": "Stuck lock takeover", "text": "`ON CONFLICT DO UPDATE` only steals a row if it's still `CLAIMED` and its 60s timer ran out." },
        { "anchor": "Duplicate skip", "text": "If Postgres updates zero rows, someone else is handling or finished it, so we bail out." }
      ],
      "diff_highlights": [
        {
          "hunk_id": "H007",
          "lines": "14-29",
          "title": "Single-query lease steal",
          "explanation": "Grabs a 60s `CLAIMED` lock—or steals an expired one—in one SQL trip so two workers never race."
        },
        {
          "hunk_id": "H007",
          "lines": "30-33",
          "title": "Fail-open DB catch",
          "explanation": "If Postgres hiccups, we return `PROCESS` anyway so a quick DB blip doesn't drop live webhooks."
        }
      ],
      "callouts": [
        {
          "kind": "assumption",
          "text": "Heads up: `catch` lets the webhook run when Postgres errors (`store.ts` L29)—double-check if billing webhooks should return `500` instead."
        }
      ],
      "hunk_ids": ["H007:1-34,52"]
    },
    {
      "id": "step-3",
      "layer": "core",
      "title": "Finish and undo helpers on the store",
      "explanation": "Now we add `markCompleted()` and `releaseClaim()` onto `PostgresIdempotencyStore` so the route can close or free a lock.",
      "hunk_ids": ["H007:35-51"]
    },
    {
      "id": "step-4",
      "layer": "wiring",
      "title": "Hook the lock guard into /webhooks/:provider",
      "explanation": "Here we plug `PostgresIdempotencyStore` into `webhookRouter` so `WebhookDispatcher` only runs once per event.",
      "visuals": [
        {
          "src": "sample-webhook-ui.png",
          "source_label": "Figma / App UI",
          "figma_url": "https://www.figma.com/design/demo/Spec?node-id=18-204",
          "caption": "Duplicate retries get a `200 deduplicated` badge on the top right while running events hold a 60s lock.",
          "annotations": [
            { "kind": "circle", "x": 86.2, "y": 42.7, "rx": 8.2, "ry": 5.8, "label": "200 deduplicated" },
            { "kind": "arrow", "from_x": 60.0, "from_y": 29.0, "to_x": 77.2, "to_y": 41.0, "label": "decision != PROCESS" }
          ]
        }
      ],
      "hunk_ids": ["H003", "H002"]
    },
    {
      "id": "step-5",
      "layer": "verification",
      "title": "Check the dedup unit tests",
      "explanation": "Finally we check the unit tests covering duplicate skips and expired-lock recovery.",
      "untested_gaps": [
        "Tests mock `db.query`, so our real `ON CONFLICT` SQL never runs against Postgres."
      ],
      "hunk_ids": ["H001"]
    }
  ],
  "mechanical": {
    "groups": [
      {
        "title": "Barrel exports & lockfile",
        "reason": "Just re-exports the new files in `index.ts` and updates `pnpm-lock.yaml`.",
        "hunk_ids": ["H004", "H006"]
      }
    ]
  }
}
```

---

## Key Schema Capabilities

1. **Sub-Hunk Slicing (`"H007:1-34,52"`) & Timeline File Evolution:**
   - Any entry in `hunk_ids` can be a whole hunk (`"H007"`) or comma-separated `new_line` ranges (`"H007:1-34,52"`).
   - At Step $N$, `build_artifact.py` renders each file as of Step $N$: slices from Steps $< N$ are already merged into the file, Step $N$'s slice shows as `+`/`−`, and slices from Steps $> N$ do not exist in the file yet (even inside `Show lines above/below` and `File at this step`).
2. **Chapters (`chapters[]`) & 2-Level Area Filter (`file_scopes`):**
   - `chapters[*].step_ids` must partition `steps[*].id` in exact reading order (rendered as `Chapter 1`, `Chapter 2`, ...).
   - `file_scopes` maps each file path to `{ "surface": "Frontend | Backend & API | Database & Contracts | Tests & QA | Infra & Config", "area": "Human Feature/Module Part" }` (never raw filenames) so reviewers can filter chapters and steps by role or subsystem in the top bar.
3. **Clickable Concept Modals (`concepts[]`):**
   - Matching backticked terms (`` `WebhookDispatcher` ``) or explicit `[[concept-id|Text]]` markers anywhere in `overview` or `steps` automatically turn into clickable modal triggers.
4. **Rotated Visual Containers (`table`, `points`, `visuals`, `callouts`):**
   - Keep `explanation` to **1 simple sentence**. Rotate `table` (`<= 5` rows) and `points` (`<= 5` bold-anchored 1-sentence bullets) so no step is a wall of prose.
5. **Smart Screenshot Grouping (automatic):**
   - 3+ screenshots in one `visuals[]` block render as a **gallery**: one large image, a captioned thumbnail strip, previous/next buttons and arrow keys. Give each a 2–4 word `"label"` for its thumbnail (falls back to the first red-mark label).
   - `"role": "before"` + `"role": "after"` on two screenshots renders them **side by side**, outside any gallery.
   - Every screenshot is capped at ~70% of the window height; **Zoom** opens the full size.
6. **Red-Marked Visual Annotation Coordinates (`0..100` percentages of image width & height):**
   - `{"kind": "circle", "x": 86.2, "y": 42.7, "r": 7.0, "label": "Short label"}` (or `"rx"` & `"ry"` for ellipses)
   - `{"kind": "arrow", "from_x": 60.0, "from_y": 29.0, "to_x": 77.2, "to_y": 41.0, "label": "Short label"}`
   - `{"kind": "box", "x": 12.0, "y": 30.0, "w": 40.0, "h": 18.0, "label": "Short label"}`
   - Always inspect the screenshot with `Read` before writing coordinates so every red mark lands accurately on the target UI element.
7. **Interactive Diff-Block Highlights (`diff_highlights[]`, optional on dense/large diffs):**
   - Use ONLY on large new files or dense functions (`>= 20` lines or tricky branching/SQL). Skip trivial/short hunks!
   - Capped at **<= 5 non-overlapping blocks per hunk card** (`build_artifact.py` fails with exit code `2` if `> 5` or overlapping).
   - Format: `{"hunk_id": "H007", "lines": "14-29", "title": "2-4 word label", "explanation": "1-2 simple sentences"}`. Hovering tints those lines across green/red/gray rows; clicking opens a side-margin explanation card without covering code.
