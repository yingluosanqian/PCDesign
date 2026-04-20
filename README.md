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
pcd run-once        <name> [--proposer-reasoning] [--critic-reasoning] [--judge-reasoning] [--manual-judge]
pcd run-until-stop  <name> --max-iter K [--proposer-reasoning] [--critic-reasoning] [--judge-reasoning]
pcd status          <name>
pcd check           <name> --result <confirm_stop|reopen|advisory_only> [--scope "…"] [--note "…"]
```

- **`init`** creates `./<name>/` with `design.md` (v0) and `.pcd/`
  (metadata + logs).
- **`run-once`** does exactly one round: 3 parallel critics → Judge →
  (if not converged) Proposer revises `design.md`. With
  `--manual-judge`, the Judge's package is opened in `$EDITOR` before
  the Proposer consumes it, so you can flip decisions, adjust
  severities, or drop spurious clusters.
- **`run-until-stop`** loops `run-once` up to `--max-iter` times. It
  exits early on convergence, and also halts (recommending a human
  check) when `must_fix` has failed to decrease for two consecutive
  rounds — to avoid burning cycles on a stuck run.
- **`status`** prints metadata, the last judgment's summary, and the
  `must_fix` trend across iterations.
- **`check`** records a human spot-check into `.pcd/human_checks.jsonl`.
  `--result confirm_stop` promotes the current state to a confirmed
  stop (sets `converged=true`); `--result reopen` flips it back to
  not-converged; `--result advisory_only` just logs the note without
  changing meta.

### Convergence rule

A round is considered converged when BOTH of the following hold:

1. **Quality suppression** (from the Judge's summary):
   - `must_fix_count == 0`, **and**
   - `should_fix_count <= 2`, **and**
   - no `high`-severity items among `must_fix`/`should_fix`.
2. **Stability**: the previous round's `must_fix_count` is greater
   than or equal to this round's (no rebound). Round 1 is therefore
   never automatically converged — the comparison needs two data
   points. Clean round-1 judgments skip the Proposer revise and wait
   one more round for the stability signal.

`run-until-stop` exits on convergence, on `no progress` (two consecutive
rounds without `must_fix` decreasing), or on `--max-iter`. If it exits
via no-progress, use `pcd check` to record a human decision.

A single round can also be `confirm_stop`'d via `pcd check` at any
time, which is how you turn a provisional stop into a confirmed one.

### Example

```bash
# 1. Start a new project at ./webcrawler/ from a one-line requirement.
pcd init webcrawler --prompt "Design a polite distributed web crawler that respects robots.txt and can resume after crashes."

# 2. Run up to 5 iterations, stopping early on convergence or no-progress.
pcd run-until-stop webcrawler --max-iter 5

# 3. Inspect state and the must_fix trend.
pcd status webcrawler

# 4. Step one round at a time, with manual intervention on the Judge's package.
pcd run-once webcrawler --manual-judge

# 5. After eyeballing design.md, confirm the stop (or reopen it).
pcd check webcrawler --result confirm_stop --scope "full review" \
  --note "spec is coherent and matches prompt"
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
    judgments.jsonl         # append-only: critics' raw issues + Judge's package (with `manually_edited` flag)
    revisions.jsonl         # append-only: "revised" / "converged — no revise" / "quality ok, awaiting stability — no revise"
    human_checks.jsonl      # append-only: records from `pcd check`
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
   assigns a *primary responsibility* when multiple critics hit the
   same root problem (primary-failure-consequence rule), calibrates
   severity, and emits a decision per cluster.
3. **Optional manual gate.** With `--manual-judge`, the Judge's
   package is dumped to `.pcd/tmp_judgment_iter<N>.json` and opened
   in `$EDITOR`. The edited version is what gets logged and fed to
   the Proposer; the log record is tagged `manually_edited: true`.
4. **Convergence check** = quality suppression × stability. If quality
   passes but stability doesn't (round 1 case), the round ends without
   a revise and waits one more round.
5. **Otherwise Proposer revises** (`sandbox=workspace-write`): resumes
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
- **Judge primary responsibility.** When the same root problem is
  reported from multiple critics, the Judge picks one *primary*
  section by primary-failure-consequence and folds the others into
  evidence — never spawns a second cluster.
- **The Judge re-reads `design.md`** rather than judging solely on
  critics' summaries — issues can be calibrated against the source.
- **Stability is a convergence requirement.** A single good round is
  not enough — we need the previous round to bound it (no must_fix
  rebound). This prevents "one-shot lucky" stops.
- **Human is in the loop at two small surfaces.** `--manual-judge`
  lets a human edit the decision package without touching agents or
  prompts; `pcd check` lets a human promote or reopen a stop without
  having to run another codex round.
- **Everything is append-only on disk.** `design.md` is the only file
  that's rewritten; `judgments.jsonl`, `revisions.jsonl`, and
  `human_checks.jsonl` grow.

## Troubleshooting

- **`pip install -e .` hangs at "Obtaining file:///…"** — PEP 517 build
  isolation is fetching setuptools over a slow network. Use
  `pip install -e . --no-build-isolation -v`.
- **A run seems stuck** — `py-spy dump --pid <pid>` will show whether
  it's the orchestrator waiting on codex or a codex process itself.
- **codex not found** — `pcd` requires the `codex` CLI on `$PATH`; see
  [openai/codex][codex] for install.
