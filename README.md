# PCDesign

**P**roposer / **C**ritic adversarial **Design** iteration — a tiny CLI that
drives two kinds of AI agents around a single `design.md` file until the
design stops getting worse.

- **Proposer (P)** — long-lived session; owns and rewrites `design.md`.
- **Three specialist Critics** — ephemeral, parallel:
  - one for *User Requirement* (section 1)
  - one for *Solution* (section 2)
  - one for *Rationale* (section 3, first-principles derivation)
- **Judge (J)** — ephemeral; merges the three critics' issues into a
  single decision package with `must_fix / should_fix / reject / defer`.
- Only the Judge's package is shown to P. P decides what to accept.

All four roles run as [OpenAI codex][codex] subprocesses speaking JSON-RPC
over stdio (`codex app-server --listen stdio://`). Different models can
be assigned to P, the critics, and the Judge.

[codex]: https://github.com/openai/codex

## The design document

`design.md` always has exactly three sections:

```
# 1. User Requirement (用户需求整理)
# 2. Solution (方案)
# 3. Rationale (方案论述)
```

Section 3 is intentionally the hardest: it must derive the solution from
a small set of assumptions, step by step, in the style of a mathematical
derivation. The Rationale critic's job is to attack leaps and hidden
assumptions here.

## Install

Requires Python ≥ 3.10 and the `codex` CLI on `$PATH`.

```bash
pip install -e .
# or, if PEP 517 build isolation is slow on your network:
pip install -e . --no-build-isolation
```

That installs a `pcd` script.

## CLI

```
pcd init            <name> --prompt "…" [--proposer-model] [--critic-model] [--judge-model] [--reasoning medium]
pcd run-once        <name> [--proposer-reasoning] [--critic-reasoning] [--judge-reasoning]
pcd run-until-stop  <name> --max-iter K [--proposer-reasoning] [--critic-reasoning] [--judge-reasoning]
pcd status          <name>
```

- **`init`** creates `./<name>/` with `design.md` (v0) and `.pcd/`
  (metadata + logs).
- **`run-once`** does exactly one round: 3 parallel critics → Judge →
  (if not converged) Proposer revises `design.md`.
- **`run-until-stop`** loops `run-once` up to `--max-iter` times or until
  the Judge's package is "clean enough" (see below).
- **`status`** prints metadata and the last judgment's summary.

### Convergence rule (v1)

A round is considered converged when the Judge's summary reports:

- `must_fix_count == 0`, **and**
- `should_fix_count <= 2`, **and**
- no `high`-severity items among `must_fix`/`should_fix`.

`run-until-stop` exits as soon as a round converges.

### Example

```bash
# 1. Start a new project at ./webcrawler/ from a one-line requirement.
pcd init webcrawler --prompt "Design a polite distributed web crawler that respects robots.txt and can resume after crashes."

# 2. Run up to 5 iterations, stopping early on convergence.
pcd run-until-stop webcrawler --max-iter 5

# 3. Inspect state and the last judge summary.
pcd status webcrawler

# 4. Or step one round at a time.
pcd run-once webcrawler
```

The final design is at `webcrawler/design.md`; every round is logged at
`webcrawler/.pcd/judgments.jsonl` (critics' raw issues + the Judge's
package) and `webcrawler/.pcd/revisions.jsonl`.

### Per-role models

Set at `init` time and stored in `.pcd/meta.json`; re-used on subsequent
`run-once` / `run-until-stop` calls:

```bash
pcd init myproj --prompt "..." \
  --proposer-model gpt-5-codex \
  --critic-model  gpt-5-codex-mini \
  --judge-model   gpt-5-codex
```

Any of the three can be omitted to use codex's default.

### Reasoning effort

codex exposes a `model_reasoning_effort` knob (`low / medium / high`).
The `--reasoning` flag on `init` controls it for the v0 pass; the three
per-role flags on `run-once` / `run-until-stop` control it per role per
round.

## Project layout

```
<name>/
  design.md                 # the living design (rewritten each round)
  .pcd/
    meta.json               # thread id, models, iteration counter, convergence
    initial_prompt.txt      # the --prompt you passed to `init`
    judgments.jsonl         # append-only: critics' raw issues + Judge's package
    revisions.jsonl         # append-only: "revised" / "converged — no revise"
```

`p_thread_id` in `meta.json` is what lets the Proposer's session survive
across CLI invocations — codex's `thread/resume` reattaches to the same
conversation.

## How a round actually runs

1. **Three critics launch in parallel** (`ThreadPoolExecutor`, one codex
   subprocess each, `sandbox=read-only`). Each is scoped to a single
   section and returns a JSON array of issues.
2. **Judge** (another codex subprocess, `read-only`) receives all the
   critics' issues as JSON, re-reads `design.md`, merges duplicates,
   calibrates severity, and assigns a decision per cluster.
3. **Convergence check.** If the Judge's summary passes, the round ends
   without touching `design.md`.
4. **Otherwise Proposer revises** (`sandbox=workspace-write`): resumes
   its long-lived thread, reads the Judge's package (rendered as
   markdown, grouped by decision), decides what to accept, and
   rewrites `design.md`.

## Design choices worth naming

- **P is not shown raw critic output.** It sees only the Judge's
  package. This keeps three biases out of P's context and lets P treat
  each cluster as one calibrated judgment.
- **P is told the critics are colleagues, not authorities.** P may
  decline a `must_fix` as long as it justifies the decline in the
  Rationale section.
- **Critics are strictly scoped.** Each critic judges one section only;
  their prompts explicitly mark the others as out of scope. This
  produces less overlap to merge.
- **The Judge re-reads `design.md`** rather than judging solely on
  critics' summaries — issues can be calibrated against the source.
- **Everything is append-only on disk.** `design.md` is the only file
  that's rewritten; `judgments.jsonl` and `revisions.jsonl` grow.

## Troubleshooting

- **`pip install -e .` hangs at "Obtaining file:///…"** — PEP 517 build
  isolation is fetching setuptools over a slow network. Use
  `pip install -e . --no-build-isolation -v`.
- **A run seems stuck** — `py-spy dump --pid <pid>` will show whether
  it's the orchestrator waiting on codex or a codex process itself.
- **codex not found** — `pcd` requires the `codex` CLI on `$PATH`; see
  [openai/codex][codex] for install.
