"""Critic agents: ephemeral agent threads that review one section each."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from pcd.agents import make_agent_client
from pcd.issues import parse_critic_issues
from pcd.roles._guard import readonly_guarded
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
    model: Optional[str],
    reasoning_effort: str = "medium",
    agent: str = "codex",
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict], bool]:
    """Run one fresh critic session for the given role.

    Returns `(issues, contaminated)`. `contaminated` is True if the
    critic subprocess modified design.md (claude has no read-only
    sandbox, so this is the structural enforcement); in that case the
    file has been restored from a pre-call snapshot.
    """
    if role not in _PROMPT_BY_ROLE:
        raise ValueError(f"unknown critic role: {role!r}")

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
                prompt=_PROMPT_BY_ROLE[role](),
                on_progress=on_progress,
            )

    result, contaminated = readonly_guarded(project_root, _body)
    return parse_critic_issues(role, result.final_text), contaminated
