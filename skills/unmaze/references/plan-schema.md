# `plan.json` Schema Reference (v0.3)

`build_artifact.py` merges `diff-manifest.json` with `plan.json` to compile the Arc-sidebar review guide, cumulative timeline file states, clickable concept modals, and annotated UI/Figma screenshots.

**Hard invariants enforced by `build_artifact.py` (exit code `2` on violation):**
1. **100% Changed-Line Coverage:** Every changed (`+`/`−`) line in every hunk (`H001`..`HNNN`) MUST be claimed **exactly once** across `steps[*].hunk_ids` and `mechanical.groups[*].hunk_ids` (using either whole-hunk `"H007"` or line-slice `"H007:1-34,52"` syntax).
2. **Mandatory Red-Marked Screenshots (Frontend AND Backend PRs):**
   - Every visual card with `src` MUST exist on disk (or be a valid `data:`/`https:` URL) and MUST include at least one red mark (`circle`, `arrow`, or `box`) inside `annotations[]`.
   - Every `plan.json` MUST attach at least 1 red-marked screenshot across `overview.visuals` or `steps[*].visuals`—even on backend-only PRs whenever a frontend screen consumes or reflects that backend logic.
   - Only when zero UI consumers exist (or no UI/Figma/screenshot is reachable after checking downstream callers) may you pass top-level `"no_visuals_justification"` (`>= 20` chars), which is rendered visibly on `0. Overview`.

---

## Complete Example

```json
{
  "title": "PR #142: Idempotent Webhook Ingestion",
  "overview": {
    "tldr": "Saves each webhook ID in Postgres before running it so retried Stripe or GitHub callbacks never run twice.",
    "global_context": "External providers POST signed payloads to `webhookRouter`, which calls `WebhookDispatcher` synchronously.",
    "before_state": "Every retry ran `WebhookDispatcher` again and could double-charge customers.",
    "after_state": "`webhookRouter` claims `(provider, event_id)` in `PostgresIdempotencyStore` first and skips duplicates with `200 OK`."
  },
  "architecture_map": {
    "nodes": [
      { "id": "senders", "label": "Stripe / GitHub", "status": "unchanged", "steps": [] },
      { "id": "router", "label": "api/routes/webhooks.ts", "status": "modified", "steps": ["step-4"] },
      { "id": "store", "label": "PostgresIdempotencyStore", "status": "added", "steps": ["step-2", "step-3"] },
      { "id": "db", "label": "webhook_idempotency_keys", "status": "added", "steps": ["step-1"] },
      { "id": "dispatcher", "label": "WebhookDispatcher", "status": "unchanged", "steps": [] }
    ]
  },
  "concepts": [
    {
      "id": "webhook-dispatcher",
      "term": "WebhookDispatcher",
      "aliases": ["dispatchProviderWebhook", "webhookDispatcher"],
      "file": "services/webhooks/dispatcher.ts",
      "what_it_is": "Routes verified provider webhooks to domain handlers like billing and repository sync.",
      "how_it_works": [
        "**Execution model:** Runs synchronously inside the `POST /webhooks/:provider` HTTP handler with no background queue.",
        "**Error handling:** Throws when a downstream handler fails so Express returns `500` and the provider retries later."
      ],
      "pr_impact": "Its own code is untouched, but `webhookRouter` now skips calling it whenever `PostgresIdempotencyStore` reports a duplicate event.",
      "related_steps": ["step-4"]
    }
  ],
  "chapters": [
    {
      "id": "ch-storage",
      "title": "Lease Storage",
      "summary": "Creates the SQL lease table and atomic claim query.",
      "step_ids": ["step-1", "step-2"]
    },
    {
      "id": "ch-guard",
      "title": "Webhook Guard & UI",
      "summary": "Adds completion/release helpers, wires the HTTP route, and audits tests.",
      "step_ids": ["step-3", "step-4", "step-5"]
    }
  ],
  "steps": [
    {
      "id": "step-1",
      "layer": "contracts",
      "title": "SQL lease table and store contract",
      "explanation": "Defines `webhook_idempotency_keys` and the `IdempotencyStore` types before any query logic uses them.",
      "table": {
        "headers": ["Status", "Meaning"],
        "rows": [
          ["`CLAIMED`", "A worker is currently running this event until `lease_expires_at`."],
          ["`COMPLETED`", "The event finished and all future retries must skip it."]
        ]
      },
      "hunk_ids": ["H005", "H008"]
    },
    {
      "id": "step-2",
      "layer": "core",
      "title": "Atomic claim query with 60s crash recovery",
      "explanation": "Builds the initial `PostgresIdempotencyStore` with `claimOrSkip()` so only one worker wins a new or expired event.",
      "points": [
        { "anchor": "Conflict rule", "text": "`ON CONFLICT DO UPDATE` only steals an existing row if `status = 'CLAIMED'` and its lease expired." },
        { "anchor": "Duplicate path", "text": "If Postgres updates zero rows, another worker owns or finished the event, so we skip it." }
      ],
      "callouts": [
        {
          "kind": "assumption",
          "text": "The `catch` block returns `PROCESS` on DB errors (`store.ts` L29)—check whether billing webhooks must fail with `500` instead."
        }
      ],
      "hunk_ids": ["H007:1-34,52"]
    },
    {
      "id": "step-3",
      "layer": "core",
      "title": "Completion and rollback helpers on the store",
      "explanation": "Adds `markCompleted()` and `releaseClaim()` onto `PostgresIdempotencyStore` so the HTTP route can finalize or free a lease.",
      "hunk_ids": ["H007:35-51"]
    },
    {
      "id": "step-4",
      "layer": "wiring",
      "title": "Protect /webhooks/:provider with the lease guard",
      "explanation": "Wires `PostgresIdempotencyStore` into the container and wraps `WebhookDispatcher` inside `POST /webhooks/:provider`.",
      "visuals": [
        {
          "src": "sample-webhook-ui.png",
          "source_label": "Figma / App UI",
          "figma_url": "https://www.figma.com/design/demo/Spec?node-id=18-204",
          "caption": "Duplicate retries return `200 deduplicated` on the top right while in-flight events hold a 60s lease.",
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
      "title": "Unit tests for duplicate skip and expired lease",
      "explanation": "Checks duplicate skipping and stalled-lease takeover with a mocked database client.",
      "untested_gaps": [
        "Mocks `db.query`, so the real Postgres `ON CONFLICT` SQL never runs."
      ],
      "hunk_ids": ["H001"]
    }
  ],
  "mechanical": {
    "groups": [
      {
        "title": "Barrel exports & lockfile",
        "reason": "Re-exports new files in `index.ts` and updates `pnpm-lock.yaml`.",
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
2. **Chapters / Virtual Stacked PRs (`chapters[]`):**
   - `chapters[*].step_ids` must partition `steps[*].id` in exact reading order.
3. **Clickable Concept Modals (`concepts[]`):**
   - Matching backticked terms (`` `WebhookDispatcher` ``) or explicit `[[concept-id|Text]]` markers anywhere in `overview` or `steps` automatically turn into clickable modal triggers.
4. **Rotated Visual Containers (`table`, `points`, `visuals`, `callouts`):**
   - Keep `explanation` to **1 simple sentence**. Rotate `table` (`<= 5` rows) and `points` (`<= 5` bold-anchored 1-sentence bullets) so no step is a wall of prose.
5. **Red-Marked Visual Annotation Coordinates (`0..100` percentages of image width & height):**
   - `{"kind": "circle", "x": 86.2, "y": 42.7, "r": 7.0, "label": "Short label"}` (or `"rx"` & `"ry"` for ellipses)
   - `{"kind": "arrow", "from_x": 60.0, "from_y": 29.0, "to_x": 77.2, "to_y": 41.0, "label": "Short label"}`
   - `{"kind": "box", "x": 12.0, "y": 30.0, "w": 40.0, "h": 18.0, "label": "Short label"}`
   - Always inspect the screenshot with `Read` before writing coordinates so every red mark lands accurately on the target UI element.
