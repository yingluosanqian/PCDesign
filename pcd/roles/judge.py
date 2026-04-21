"""Judge agent: ephemeral agent thread that merges critic issues into a package."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from pcd.agents import make_agent_client
from pcd.issues import parse_judgment
from pcd.roles._guard import private_staging
from pcd.roles.prompts import judge_prompt


def run_judge(
    *,
    project_root: Path,
    iteration: int,
    critics_output: dict,
    model: Optional[str],
    reasoning_effort: str = "medium",
    agent: str = "codex",
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[dict, bool]:
    """Run one fresh judge session in its private staging dir.

    Returns `(judgment, contaminated)`. `critics_output` is
    `{role: [issue, ...]}` as produced by `run_critic` (+ optional
    reframer / exploration entries). Contamination semantics same as
    critics — if the Judge wrote to its private design.md, discard.
    """
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
                prompt=judge_prompt(critics_output),
                on_progress=on_progress,
            )

    result, contaminated = private_staging(
        project_root=project_root,
        iteration=iteration,
        role_name="judge",
        body=_body,
    )
    return parse_judgment(result.final_text), contaminated
