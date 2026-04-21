"""Critic agents: ephemeral agent threads that review one section each."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from pcd.agents import make_agent_client
from pcd.issues import parse_critic_issues
from pcd.roles._guard import private_staging
from pcd.roles.prompts import (
    design_critic_prompt,
    rationale_critic_prompt,
    requirement_critic_prompt,
)


CRITIC_ROLES = ("requirement", "design", "rationale")


_PROMPT_BY_ROLE = {
    "requirement": requirement_critic_prompt,
    "design": design_critic_prompt,
    "rationale": rationale_critic_prompt,
}

# role -> the `section` value we expect on issues from that critic
SECTION_BY_ROLE = {
    "requirement": "requirement",
    "design": "solution",
    "rationale": "rationale",
}


def run_critic(
    *,
    role: str,
    project_root: Path,
    iteration: int,
    model: Optional[str],
    reasoning_effort: str = "medium",
    agent: str = "codex",
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict], bool]:
    """Run one fresh critic session for the given role.

    Runs inside a per-critic private staging dir so the shared
    design.md is not even in the critic's cwd. Returns
    `(issues, contaminated)` where `contaminated=True` means the
    critic wrote to its private design.md (C11 violation) — its
    output should be discarded by the caller.
    """
    if role not in _PROMPT_BY_ROLE:
        raise ValueError(f"unknown critic role: {role!r}")

    def _body(staging_dir: Path):
        with make_agent_client(
            agent,
            cwd=str(staging_dir),
            reasoning_effort=reasoning_effort,
        ) as client:
            thread_id = client.start_thread(
                cwd=str(staging_dir),
                model=model,
                sandbox="read-only",
            )
            return client.run_turn(
                thread_id=thread_id,
                cwd=str(staging_dir),
                model=model,
                prompt=_PROMPT_BY_ROLE[role](),
                on_progress=on_progress,
            )

    result, contaminated = private_staging(
        project_root=project_root,
        iteration=iteration,
        role_name=f"critic_{role}",
        body=_body,
    )
    return parse_critic_issues(role, result.final_text), contaminated
