# unmaze — Bringing Human Code Review Back to Life in the AI Era

AI agents can now generate a 1,500-line pull request across 25 files in two minutes. Nobody wrote that code by hand, half the team no longer knows how that subsystem fits together globally, and **real code review is quietly dying**—turning into either an hour of mental exhaustion or a blind *"LGTM"*.

`unmaze` is a Claude Code skill that turns sprawling PR diffs into a **calm, step-by-step interactive review guide**—published as a shareable Artifact you can review yourself or drop straight into a PR description for your team.

![Arc-style chapters, top-bar area filter, clickable diff-block explanations, and timeline file evolution](assets/demo-walkthrough.gif)

---

## Why GitHub Diffs Break Your Brain in the AI Era

GitHub still dumps changed files in **alphabetical order (`A → Z`)**:

| What You Open | Reading Order | What Happens to Your Brain |
| :--- | :--- | :--- |
| **Raw GitHub Diff (`A → Z`)** | `__tests__/` → `api/routes.ts` → `pnpm-lock.yaml` → `store.ts` → `types.ts` | You read mocked tests and route glue *before* seeing the data types or business rules they use. |
| **`unmaze` Guide** | `Overview` → `Chapter 1: Storage` → `Chapter 2: Route & UI` → `Boilerplate` | Every concept is introduced before anything depends on it—zero jumping across 15 browser tabs. |

---

## How `unmaze` Makes Large PRs Easy to Understand

1. **Arc-Style Chapters & Role/Area Tree Filter:** Splits multi-part PRs into ordered chapters (`Chapter 1`, `Chapter 2`, ...) in a collapsible Arc sidebar, with a **pronounced top-bar tree filter** (`Frontend`, `Backend & API`, `Database & Contracts` → specific app areas) that dims unrelated chapters and steps so each engineer can review their own slice.
2. **True Timeline File Evolution & Clickable Diff Blocks:** Files grow step by step along the review timeline so future changes never leak early—and inside large functions or new files, hovering tricky code blocks tints them smoothly over red/green/gray lines and clicking opens a side-margin explanation without covering a single character of code.
3. **Click Any Task or Repo Concept Whenever You Encounter It:** A dedicated second-pass AI sweep explains **both** new terms/things introduced by the task and existing repo services—semantically linking natural phrases (like *frees the lock* or *60s timer*) with clean dotted underlines across every step, bullet, table, and diff note.
4. **Whole Task in One Breath, Then Everything About This PR:** The Overview opens with the ticket, design and spec links, says the whole task in one casual sentence, and then spends its space on **this PR**: which parts of the system it touches (every service, app or package, with new routes and entry points) and its **ship facts**—migrations, backfills, config, feature flags, dependencies, contract changes and deploy order, with "none" said out loud.
5. **Fewer, Fuller Steps & Calmer Text:** A step budget (about one step per 150 lines of production code, 5–12 total) folds tests into the steps they prove, merges tiny steps and sends plans and docs to an Appendix, while wider spacing, ~75-character lines and one dotted link per term per step keep each page easy to read.
6. **A Diagram Only When It Helps:** When the hard part of a PR is how several parts call each other, the Overview adds a small "How the parts connect" picture with changes marked like a code diff (new green, changed amber, removed red and dashed) and every arrow linked to the line that makes the call; on PRs where a picture would only repeat the list of changed parts, it's skipped on purpose.
7. **Boards That Stay Fresh:** Once a board is published, the plugin remembers it for that branch. After every later `git push`, Claude refreshes it at the **same link**—carrying the old walkthrough over to the new diff—without anyone typing `/unmaze` again (`board_registry.py pause` turns it off).

---

## Visual Context & Expandable Code

![Annotated Figma/UI screenshots, mixed red/green diff-block explanations, and expandable context](assets/demo-visuals-context.gif)

- **Smart screenshot grouping:** 3+ screenshots in one place become a gallery (one large image plus a captioned thumbnail strip), before/after pairs sit side by side, and no screenshot grows taller than ~70% of the window.
- **Red-marked screenshots—even on backend PRs:** Traces backend, API, and DB changes to the frontend screen they impact and posts screenshots or Figma frames with **red circles, boxes, and arrows** explaining what the code is actually for (enforced at build time).
- **Expand context & one-click exact-line PR jump:** Click `↑ Show lines above` / `↓ Show lines below` (`Show less`) or `File at this step` inline—and click **any line number or row `PR ↗` hover badge** to open GitHub scrolled straight to that exact line (`#diff-...R<line>` or `L<line>`) to leave a comment.
- **100% of the diff accounted for:** A deterministic Python check verifies every single changed line from `git diff` is included—nothing can be silently skipped or altered by the AI.
- **One-click team sharing:** Publishes directly via Claude Code's `Artifact` tool (`claude.ai/code/artifact/...`) and also saves local `.html` and `.md` copies.

---

## Install

Requires **Python 3.9+** and **git** (plus `gh` CLI when reviewing GitHub PRs by number or URL). Zero external packages needed.

**Option 1 — Inside Claude Code (Plugin Marketplace):**
```text
/plugin marketplace add t10ko/unmaze
/plugin install unmaze
```

**Option 2 — Via `npx skills`:**
```bash
npx skills add t10ko/unmaze --skill unmaze --agent claude-code
```

**Option 3 — Manual symlink (global or per-repo):**
```bash
git clone https://github.com/t10ko/unmaze.git
mkdir -p ~/.claude/skills
ln -s "$(pwd)/unmaze/skills/unmaze" ~/.claude/skills/unmaze
```

---

## Usage

Run `/unmaze` inside any repository in Claude Code:

```text
/unmaze                     # Reviews current branch PR or diff against main
/unmaze 142                 # Reviews GitHub PR #142
/unmaze <github-pr-url>     # Reviews any PR by URL
/unmaze main...HEAD         # Reviews a specific git range
/unmaze --staged            # Reviews staged changes before committing
```

---

## Try the Included Demo Locally

```bash
python3 skills/unmaze/scripts/parse_diff.py \
  --patch examples/sample-hairball.patch \
  --context-dir examples/sample-context \
  --pr-url https://github.com/acme/payments/pull/142 \
  --out-dir .unmaze/demo

python3 skills/unmaze/scripts/build_artifact.py \
  --manifest .unmaze/demo/diff-manifest.json \
  --plan examples/sample-plan.json \
  --out .unmaze/demo/unmaze-review.html

open .unmaze/demo/unmaze-review.html
```

## License

[MIT](LICENSE)
