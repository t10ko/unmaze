# `plan.json` Schema Reference

`build_artifact.py` merges `diff-manifest.json` (verbatim diff lines & deep links) with `plan.json` (your cognitive reading order, architectural mental model, and reviewer callouts).

**Hard invariant:** Every hunk ID (`H001`..`HNNN`) in `diff-manifest.json` MUST appear **exactly once** across `steps[*].hunk_ids` and `mechanical.groups[*].hunk_ids`. `build_artifact.py` will exit with code `2` and list any missing, duplicate, or unknown hunk IDs if coverage is not 100%.

## Top-Level Structure

```json
{
  "title": "PR #142: Idempotent Webhook Ingestion & Dead-Letter Retry",
  "overview": {
    "tldr": "One or two crisp sentences stating the problem solved and the core mechanism introduced.",
    "global_context": "How the surrounding codebase works globally so a reviewer unfamiliar with this subsystem immediately knows where these files sit in the request/data lifecycle.",
    "before_state": "How the system behaved prior to this diff (and what broke or was missing).",
    "after_state": "How the system behaves now, including key guarantees or boundaries."
  },
  "architecture_map": {
    "nodes": [
      {
        "id": "gateway",
        "label": "API Webhook Gateway",
        "role": "Receives Stripe/GitHub POST callbacks",
        "status": "unchanged",
        "steps": []
      },
      {
        "id": "dedup-store",
        "label": "IdempotencyStore (Redis/PG)",
        "role": "Atomic claim + TTL state transitions",
        "status": "added",
        "steps": ["step-1", "step-2"]
      },
      {
        "id": "handler",
        "label": "WebhookController",
        "role": "Wires dedup guard around domain dispatch",
        "status": "modified",
        "steps": ["step-3"]
      }
    ],
    "edges": [
      { "from": "gateway", "to": "handler", "label": "POST /webhooks/:provider" },
      { "from": "handler", "to": "dedup-store", "label": "claimOrSkip(eventId)" }
    ]
  },
  "steps": [
    {
      "id": "step-1",
      "layer": "contracts",
      "slice": "Optional vertical slice label when PR has 2+ distinct features",
      "title": "Define IdempotencyRecord states and storage interface",
      "why_now": "Establish the three lifecycle states (CLAIMED, COMPLETED, FAILED) before reading the locking and retry logic that mutates them.",
      "explanation": "Concise explanation of how these types/schemas shape the rest of the PR.",
      "callouts": [
        {
          "kind": "decision",
          "text": "Uses explicit status enum instead of boolean flag so crashed workers can expire and retry after `leaseTtlMs`."
        }
      ],
      "hunk_ids": ["H004", "H005"],
      "hunk_notes": {
        "H004": "New DB migration adding composite unique index on (provider, event_id).",
        "H005": "TypeScript domain contract consumed by both store and middleware."
      }
    },
    {
      "id": "step-2",
      "layer": "core",
      "title": "Implement atomic claim, lease expiration, and failure release",
      "why_now": "With `IdempotencyRecord` defined, inspect the state machine where concurrency bugs or data loss could occur.",
      "explanation": "Walks through atomic `INSERT ... ON CONFLICT` lease acquisition.",
      "callouts": [
        {
          "kind": "assumption",
          "text": "AI assumed a 60s default lease (`H002` L48). Verify no downstream webhook handler can legitimately run longer than 60s."
        },
        {
          "kind": "risk",
          "text": "Clock skew check relies on app-server `Date.now()` rather than DB `NOW()`."
        }
      ],
      "hunk_ids": ["H002", "H003"],
      "hunk_notes": {}
    },
    {
      "id": "step-3",
      "layer": "wiring",
      "title": "Wire dedup middleware into webhook routes and DI container",
      "why_now": "Now that the store guarantees atomic claims, verify how HTTP routes and container bindings plug it into incoming requests.",
      "explanation": "Registers `IdempotencyStore` singleton and wraps `/webhooks/:provider`.",
      "callouts": [],
      "hunk_ids": ["H001", "H006"]
    },
    {
      "id": "step-4",
      "layer": "verification",
      "title": "Verify duplicate suppression and crash-recovery tests",
      "why_now": "Check which concurrency and failure paths are proven by tests—and what remains untested.",
      "explanation": "Covers duplicate 200 OK short-circuiting and expired lease takeover.",
      "untested_gaps": [
        "No test covers simultaneous parallel requests arriving within the same millisecond (race test uses sequential calls).",
        "DB connection failure mid-lease-update is mocked away rather than tested."
      ],
      "hunk_ids": ["H007"]
    }
  ],
  "mechanical": {
    "groups": [
      {
        "title": "Barrel exports & lockfile updates",
        "reason": "Pure re-exports in index.ts and pnpm-lock.yaml metadata; verified zero behavioral logic.",
        "hunk_ids": ["H008", "H009"]
      }
    ]
  }
}
```

## Field Enumerations

- `steps[*].layer` *(required)*:
  - `"contracts"` — Types, interfaces, DB migrations, protobuf/OpenAPI schemas, state definitions, constants.
  - `"core"` — Domain logic, algorithms, state transitions, auth/permission checks, subtle bugfixes.
  - `"wiring"` — Routes, controllers, UI prop threading, CLI flag registration, DI/bootstrap, config plumbing.
  - `"verification"` — Unit/integration/e2e tests and assertions.
- `steps[*].callouts[*].kind`:
  - `"decision"` — Deliberate architectural or design tradeoff.
  - `"assumption"` — Implicit assumption made by the author/AI that a human reviewer MUST validate against domain reality.
  - `"risk"` — Concurrency, security, performance, backwards-compatibility, or edge-case hazard.
  - `"question"` — Concrete question worth asking on the PR before approving.
- `architecture_map.nodes[*].status`:
  - `"unchanged"` — Existing surrounding module included as a landmark so the reviewer sees *where* the diff plugs into the global system.
  - `"added"` — Newly introduced component/file.
  - `"modified"` — Existing component with changed behavior.
  - `"removed"` — Deleted/retired component.
