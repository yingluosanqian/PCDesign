"""Judge agent: ephemeral codex thread that merges critic issues into a package."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pcd.codex_client import CodexClient
from pcd.issues import parse_judgment
from pcd.prompts import judge_prompt


def run_judge(
    *,
    project_root: Path,
    critics_output: dict,
    model: Optional[str],
    reasoning_effort: str = "medium",
) -> dict:
    """Run one fresh judge session. Returns a normalized judgment dict.

    `critics_output` is {role: [issue, ...]} as produced by `run_critic`.
    The Judge is given read-only access so it can ground decisions in
    the actual ./design.md.
    """
    with CodexClient(
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
        )
    return parse_judgment(result.final_text)
