"""Judge agent: ephemeral agent thread that merges critic issues into a package."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from pcd.agents import make_agent_client
from pcd.issues import parse_judgment
from pcd.roles.prompts import judge_prompt


def run_judge(
    *,
    project_root: Path,
    critics_output: dict,
    model: Optional[str],
    reasoning_effort: str = "medium",
    agent: str = "codex",
    on_progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Run one fresh judge session. Returns a normalized judgment dict.

    `critics_output` is {role: [issue, ...]} as produced by `run_critic`.
    The Judge is given read-only access so it can ground decisions in
    the actual ./design.md.
    """
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
        result = client.run_turn(
            thread_id=thread_id,
            cwd=str(project_root),
            model=model,
            prompt=judge_prompt(critics_output),
            on_progress=on_progress,
        )
    return parse_judgment(result.final_text)
