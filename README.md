# unmaze

> Stop getting lost in 40-file AI diffs. Turn sprawling PRs into a shareable, step-by-step code review artifact ordered how humans actually understand code—not `a.ts` to `z.ts`.

GitHub and Git sort pull request diffs **alphabetically by file path**. That worked when a human carefully wrote 120 lines of code in a subsystem everyone already knew. In the AI coding era—where agents generate 1,500-line diffs across 25 files in minutes and nobody holds the entire codebase in their head—alphabetical diffs force your brain to read `__tests__/` and `api/routes.ts` before you've even seen the data structures or domain logic they depend on.

**`unmaze`** is a [Claude Code](https://docs.anthropic.com/en/docs/claude-code) skill (compatible with the open `SKILL.md` standard) that untangles any PR or Git diff into a **guided, zero-forward-reference walkthrough** with **mathematically verified 100% diff coverage**, and publishes it as a shareable interactive HTML Artifact.

---

## How `unmaze` Orders a Code Review

Instead of alphabetical files, `unmaze` sequences hunks in decreasing cognitive criticality with zero forward references:

```text
Step 0 · Global System Map       Where this change lives in the wider codebase (before vs. after)
  ↓
Step 1 · Shapes & Contracts      Schemas, SQL migrations, types, API payloads ("the nouns first")
  ↓
Step 2 · Core Logic & Decisions  Algorithms, state transitions, auth/security boundaries ("the brain")
  ↓
Step 3 · Entrypoints & Wiring    Routes, DI bindings, CLI flags, UI prop plumbing ("the nerves")
  ↓
Step 4 · Verification & Gaps     What the tests actually prove + explicit UNTESTED edge cases
  ↓
Appendix · Mechanical Noise      Lockfiles, barrel exports, renames (collapsed, still 100% accounted)
```

*(When a PR bundles multiple independent vertical features, `unmaze` sequences `Contracts → Core → Wiring → Verification` per feature slice rather than jumbling unrelated files together.)*

---

## Key Guarantees

- **100% Deterministic Diff Accounting (Zero Blind Spots):** `parse_diff.py` assigns a deterministic ID (`H001`..`HNNN`) to every changed hunk—including pure file renames and binary files—and `build_artifact.py` fails loudly if the agent omits or duplicates even a single hunk. Verbatim diff lines are injected directly from `git diff`, never re-typed by the LLM.
- **Shareable Interactive HTML Artifacts:** In Claude Code, `/unmaze` invokes the built-in `Artifact` tool to publish a self-contained interactive walkthrough (`claude.ai/code/artifact/...`) you can share with teammates or drop straight into a PR description—while also saving local `.html` and `.md` copies.
- **One-Click Deep Links Back to the PR:** Every hunk card computes the exact GitHub/GitLab/Bitbucket diff anchor (`#diff-<sha256>R<line>`) so when you spot an issue, one click opens the exact line in your PR to leave an inline review comment.
- **Built for Reviewer Ergonomics:**
  - Interactive **Global Architecture Map** that highlights active modules as you step through `Step 1 → Step N` (including unchanged surrounding landmarks so unfamiliar reviewers stay oriented).
  - Explicit callouts for **AI Assumptions**, **Key Decisions**, **Subtle Risks**, and **Untested Gaps**.
  - **Guided Step-by-Step** mode (`[` / `]` shortcuts) and **All-Steps Linear** mode (`m`), per-hunk **Reviewed** checkboxes with live progress, light/dark theme support, and native browser `⌘F` / `Ctrl+F` search that automatically reveals matches inside collapsed/inactive steps (`hidden="until-found"`).
  - Clean repository hygiene: writes intermediate files to a temporary directory by default so running `/unmaze` never dirties `git status`.

---

## Installation

**Prerequisites:** Python 3.9+, `git`, and optionally GitHub CLI (`gh`) when reviewing GitHub PRs by number or URL. Zero third-party Python or Node packages required.

### Option 1: Claude Code Plugin Marketplace
Inside an interactive Claude Code session:
```text
/plugin marketplace add t10ko/unmaze
/plugin install unmaze
```

### Option 2: `npx skills` CLI
```bash
npx skills add t10ko/unmaze --skill unmaze --agent claude-code
```

### Option 3: Manual Global or Project Install
**Global (available in all your repositories):**
```bash
git clone https://github.com/t10ko/unmaze.git
mkdir -p ~/.claude/skills
ln -s "$(pwd)/unmaze/skills/unmaze" ~/.claude/skills/unmaze
```

**Per-repository (checked into `.claude/skills/` for your team):**
```bash
mkdir -p .claude/skills
cp -R /path/to/unmaze/skills/unmaze .claude/skills/unmaze
```

---

## Usage

Inside Claude Code (in any git repository):

```text
/unmaze
```
*(Auto-detects your current branch's open PR or diffs `<default-branch>...HEAD`.)*

Or pass an explicit target:

```text
/unmaze 142
/unmaze https://github.com/org/repo/pull/142
/unmaze main...HEAD
/unmaze --staged
/unmaze path/to/changes.patch
```

Once generated:
1. Open the published **`claude.ai/code/artifact/...`** URL (or local `unmaze-review.html`).
2. Click **Share** in the Claude Artifact header if you want to post the guided walkthrough link directly in your GitHub PR description for reviewers.

---

## Try the Included Demo Locally (5 Seconds)

`examples/sample-hairball.patch` contains a realistic 8-file AI-generated webhook idempotency PR where alphabetical sorting puts mocked unit tests first, lockfiles in the middle, and the SQL schema + core state machine last—hiding both a fail-open billing hazard and a broken mocked test.

Run the deterministic pipeline directly to inspect the compiled HTML artifact:

```bash
python3 skills/unmaze/scripts/parse_diff.py \
  --patch examples/sample-hairball.patch \
  --pr-url https://github.com/acme/payments/pull/142 \
  --out-dir .unmaze/demo

python3 skills/unmaze/scripts/build_artifact.py \
  --manifest .unmaze/demo/diff-manifest.json \
  --plan examples/sample-plan.json \
  --out .unmaze/demo/unmaze-review.html \
  --markdown-out .unmaze/demo/unmaze-review.md

open .unmaze/demo/unmaze-review.html   # macOS (or xdg-open on Linux)
```

---

## Repository Structure

```text
unmaze/
├── .claude-plugin/
│   ├── plugin.json                      # Claude Code plugin manifest
│   └── marketplace.json                 # Self-hosted Claude Code marketplace catalog
├── skills/
│   └── unmaze/
│       ├── SKILL.md                     # Core skill workflow & instructions
│       ├── scripts/
│       │   ├── parse_diff.py            # Unified diff parser, hunk ID & deep-link builder
│       │   └── build_artifact.py        # 100% hunk coverage validator & HTML/MD compiler
│       └── references/
│           ├── plan-schema.md           # Schema for cognitive review plans
│           └── cognitive-order-guide.md # Zero-forward-reference & AI-code review heuristics
├── examples/
│   ├── sample-hairball.patch            # Example scrambled multi-file AI PR diff
│   └── sample-plan.json                 # Example human-ordered review plan
└── tests/
    └── test_unmaze.py                   # Unit & end-to-end test suite
```

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
