"""P↔C iteration loop used by `pcd continue`."""
from __future__ import annotations

import sys
from typing import Optional

from pcd.critic import run_critic
from pcd.project import Project
from pcd.proposer import run_proposer_revise


def run_iterations(
    *,
    project: Project,
    iterations: int,
    proposer_model: Optional[str],
    critic_model: Optional[str],
    proposer_reasoning: str = "medium",
    critic_reasoning: str = "medium",
) -> None:
    meta = project.load_meta()
    for i in range(1, iterations + 1):
        print(
            f"[pcd] iteration {i}/{iterations} — critic reviewing",
            file=sys.stderr,
            flush=True,
        )
        critique = run_critic(
            project_root=project.root,
            model=critic_model,
            reasoning_effort=critic_reasoning,
        )
        if not critique.strip():
            print(
                f"[pcd] iteration {i}: critic returned empty review; skipping revise",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"[pcd] iteration {i} — proposer revising",
                file=sys.stderr,
                flush=True,
            )
            run_proposer_revise(
                project_root=project.root,
                thread_id=meta.p_thread_id,
                critique=critique,
                model=proposer_model,
                reasoning_effort=proposer_reasoning,
            )
        meta.iterations_done += 1
        project.save_meta(meta)
