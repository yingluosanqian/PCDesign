"""Exploration Critic: audits Reframer's alternative sketches.

Reframer is a generator — it produces alternative design skeletons.
Without a discriminator the alternatives can hand-wave mechanisms,
drop hard constraints silently, or claim aesthetic-only dominances
over baseline. Exploration Critic is that discriminator, scoped
specifically to Reframer's output.

It reads the Reframer package alongside the original initial_prompt
and the current design.md, and emits `section="alternatives"` issues
when an alternative fails one of four tests: requirement fit,
internal coherence, dominance claim, falsifiable failure mode. It
also raises if Reframer's own `hard_constraints` enumeration missed
a constraint from initial_prompt.

Exploration Critic issues flow into the Judge alongside all other
critic issues. The Judge merges / calibrates / decides — an alt that
Reframer proposed and Exploration judged coherent becomes a
`must_fix` or `should_fix` cluster the Proposer must engage in
Rationale; an alt that Exploration demolished as incoherent becomes
a `reject` cluster.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from pcd.agents import make_agent_client
from pcd.issues import parse_critic_issues
from pcd.roles._guard import readonly_guarded
from pcd.roles.prompts import exploration_critic_prompt


def run_exploration_critic(
    *,
    project_root: Path,
    reframer_package: dict,
    model: Optional[str],
    reasoning_effort: str = "medium",
    agent: str = "codex",
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict], bool]:
    """Run one fresh Exploration Critic session.

    `reframer_package` is the raw `{hard_constraints, alternatives}`
    dict from `run_reframer`. Returns `(issues, contaminated)` with
    issues having `section="alternatives"` and `critic_role="exploration"`.
    """
    def _body():
        with make_agent_client(
            agent,
            cwd=str(project_root),
            reasoning_effort=reasoning_effort,
        ) as client:
            thread_id = client.start_thread(
                cwd=str(project_root),
                model=model,
                sandbox="read-only",
            )
            return client.run_turn(
                thread_id=thread_id,
                cwd=str(project_root),
                model=model,
                prompt=exploration_critic_prompt(reframer_package),
                on_progress=on_progress,
            )

    result, contaminated = readonly_guarded(project_root, _body)
    return parse_critic_issues("exploration", result.final_text), contaminated
