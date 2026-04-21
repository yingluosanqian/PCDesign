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

Each role can run on either of two backend CLIs, picked per role at
`init` time:

- [OpenAI codex][codex] — a long-lived `codex app-server --listen
  stdio://` subprocess speaking JSON-RPC.
- [Anthropic Claude Code][claude] — one-shot `claude -p
  --output-format stream-json` invocations. PCDesign always starts
  Claude with `IS_SANDBOX=1` in the environment and
  `--dangerously-skip-permissions` on the command line so the agent
  has read/write access to the project directory.

Different models and different agents can be assigned to P, the
critics, and the Judge independently.

[codex]: https://github.com/openai/codex
[claude]: https://docs.claude.com/en/docs/claude-code/overview

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

Requires Python ≥ 3.10 and whichever agent CLIs you plan to use on
`$PATH` (`codex`, `claude`, or both).

```bash
pip install -e .
# or, if PEP 517 build isolation is slow on your network:
pip install -e . --no-build-isolation
```

That installs a `pcd` script.

## CLI

```
pcd init            <name> (--prompt "…" | --prompt-file <path>) \
                           [--proposer-model] [--critic-model] [--judge-model] \
                           [--agent codex|claude] [--proposer-agent] [--critic-agent] [--judge-agent] \
                           [--reasoning medium]
pcd run-once        <name> [--proposer-reasoning] [--critic-reasoning] [--judge-reasoning] [--manual-judge]
pcd run-until-stop  <name> --max-iter K [--proposer-reasoning] [--critic-reasoning] [--judge-reasoning]
pcd status          <name>
pcd check           <name> --result <confirm_stop|reopen|advisory_only> [--scope "…"] [--note "…"]
```

- **`init`** creates `./<name>/` with `design.md` (v0) and `.pcd/`
  (metadata + logs). Pass the initial requirement inline with
  `--prompt "…"` (fine for one-liners) or from a file with
  `--prompt-file <path>` (use `-` for stdin). The resolved prompt is
  copied verbatim to `.pcd/initial_prompt.txt`.
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

A round is considered converged when ALL of the following hold:

1. **Quality suppression** (from the Judge's summary):
   - `must_fix_count == 0`, **and**
   - `should_fix_count <= 2`, **and**
   - no `high`-severity items among `must_fix`/`should_fix`.
2. **Stability**: the **previous non-degraded round** also satisfied
   quality suppression. Convergence therefore requires two consecutive
   clean rounds — a single "lucky" round cannot declare convergence,
   and the round before the current one must not be a no-op / failure
   (see *Degraded rounds* below).
3. **Not degraded**: the current round itself is non-degraded.

Round 1 is therefore never automatically converged — there's no prior
non-degraded round to compare against. Clean round-1 judgments skip
the Proposer revise and wait one more round for the stability signal.

`run-until-stop` exits on convergence, on `no progress` (three
consecutive non-degraded rounds with non-decreasing `must_fix`), or on
`--max-iter`. If it exits via no-progress, use `pcd check` to record a
human decision.

A single round can also be `confirm_stop`'d via `pcd check` at any
time, which is how you turn a provisional stop into a confirmed one.

### Degraded rounds

A round is marked **degraded** when its quality signal is not trustworthy.
Triggers:

- **Critic failure**: one of the three critic subprocesses crashed or
  produced unparseable output.
- **Critic / Judge contamination**: under the claude backend, critics
  and the Judge have filesystem write tools available (claude has no
  read-only sandbox). If one of them modifies `design.md` during its
  read-only run, PCDesign rolls `design.md` back from an in-memory
  snapshot and marks the round degraded.
- **Proposer no-op**: the Proposer's revise call finished but
  `design.md` bytes were unchanged (the Proposer rejected every
  `must_fix` and didn't edit anything). Without this check, the next
  round's critics would run against the same text, so two consecutive
  `quality_ok` rounds could sit on the same document — stability would
  lose its meaning.

Degraded rounds:

- cannot declare convergence themselves, and
- are skipped when a later round looks back for "previous round was
  quality_ok", and
- are skipped from the `must_fix`-trend sliding window used by
  no-progress.

They still appear in `.pcd/judgments.jsonl`, `.pcd/rounds/iter_NNN/`,
and `pcd status` — tagged with `degraded: true` plus a `degraded_reasons`
list explaining why.

### Example

```bash
# 1a. Start a new project at ./webcrawler/ from a one-line requirement.
pcd init webcrawler --prompt "Design a polite distributed web crawler that respects robots.txt and can resume after crashes."

# 1b. Or, for longer briefs, load the requirement from a file.
pcd init webcrawler --prompt-file ./requirements.md

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

Any of the three can be omitted to use the backend agent's default.

### Per-role agents

`--agent` picks a single backend (`codex` or `claude`) for all three
roles; `--proposer-agent` / `--critic-agent` / `--judge-agent` override
per role. The choice is stored in `.pcd/meta.json` alongside the
models, and reused on subsequent `run-once` / `run-until-stop` calls.

```bash
# All three roles run on Claude.
pcd init myproj --prompt "..." --agent claude

# Mix: codex for the long-lived Proposer, claude for the ephemeral
# critics and judge.
pcd init myproj --prompt "..." --agent codex --critic-agent claude --judge-agent claude
```

Existing projects whose `meta.json` predates this feature fall through
to `codex` for every role.

### Reasoning effort

The `--reasoning` flag on `init` and the three per-role flags on
`run-once` / `run-until-stop` control reasoning effort per role per
round. The knob maps to each backend's native control:

- codex: `model_reasoning_effort` (`low | medium | high`)
- claude: `--effort` (`low | medium | high | xhigh | max`)

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
    rounds/                 # per-iteration breakout, written fresh each round
      iter_001/
        critic_requirement.json
        critic_design.json
        critic_rationale.json
        judgment.json
        summary.md          # one-page markdown rollup: all critics + judge package + counts
      iter_002/
        ...
```

The `rounds/` tree is redundant with `judgments.jsonl` — same data,
exploded per round and pretty-printed. It exists so you can `cat
.pcd/rounds/iter_003/summary.md` (or open it in your editor) without
having to dig a specific record out of the jsonl. `judgments.jsonl`
remains the authoritative append-only log.

`p_thread_id` in `meta.json` is what lets the Proposer's session survive
across CLI invocations — codex's `thread/resume` or claude's
`--resume <uuid>` reattaches to the same conversation. The
`proposer_agent` / `critic_agent` / `judge_agent` fields record which
backend CLI drives each role.

## How a round actually runs

1. **Three critics launch in parallel** (`ThreadPoolExecutor`, one
   agent subprocess each — codex uses `sandbox=read-only`; claude
   always has full filesystem access). Each is scoped to a single
   section and returns a JSON array of issues.
2. **Judge** (another agent subprocess, read-only under codex)
   receives all the critics' issues as JSON, re-reads `design.md`,
   merges duplicates, assigns a *primary responsibility* when multiple
   critics hit the same root problem (primary-failure-consequence
   rule), calibrates severity, and emits a decision per cluster.
3. **Optional manual gate.** With `--manual-judge`, the Judge's
   package is dumped to `.pcd/tmp_judgment_iter<N>.json` and opened
   in `$EDITOR`. The edited version is what gets logged and fed to
   the Proposer; the log record is tagged `manually_edited: true`.
4. **Convergence check** = quality suppression × stability. If quality
   passes but stability doesn't (round 1 case), the round ends without
   a revise and waits one more round.
5. **Otherwise Proposer revises** (codex uses
   `sandbox=workspace-write`; claude uses its standard
   skip-permissions mode): resumes its long-lived thread, reads the
   Judge's package (rendered as markdown, grouped by decision),
   decides what to accept, and rewrites `design.md`. The thread is
   identified by a UUID (`p_thread_id` in `meta.json`) that codex
   reattaches via `thread/resume` and claude via `--resume <uuid>`.

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
- **codex not found** — if any role is using the codex backend,
  `pcd` requires the `codex` CLI on `$PATH`; see
  [openai/codex][codex] for install.
- **claude not found** — if any role is using the claude backend,
  `pcd` requires the `claude` CLI on `$PATH`; see
  [Claude Code install][claude] for install.
