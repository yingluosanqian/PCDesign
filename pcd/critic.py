"""Critic agent: ephemeral codex thread that reviews ./design.md."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pcd.codex_client import CodexClient
from pcd.prompts import critic_prompt


def run_critic(
    *,
    project_root: Path,
    model: Optional[str],
    reasoning_effort: str = "medium",
) -> str:
    """Run a fresh, one-shot Critic session. Returns the review text."""
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
            prompt=critic_prompt(),
        )
        return result.final_text
