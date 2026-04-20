"""P ↔ (C_req, C_design, C_rationale) ↔ J iteration loop."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from pcd.critic import CRITIC_ROLES, run_critic
from pcd.issues import (
    format_issue_package_for_proposer,
    is_converged,
)
from pcd.judge import run_judge
from pcd.project import Project
from pcd.proposer import run_proposer_revise


def run_single_iteration(
    *,
    project: Project,
    proposer_model: Optional[str],
    critic_model: Optional[str],
    judge_model: Optional[str],
    proposer_reasoning: str = "medium",
    critic_reasoning: str = "medium",
    judge_reasoning: str = "medium",
) -> dict:
    """One full round: 3 parallel critics -> judge -> (maybe) proposer revise.

    Returns the judgment dict. Updates meta on disk and appends to logs.
    """
    meta = project.load_meta()
    iteration = meta.iterations_done + 1

    print(
        f"[pcd] iter {iteration}: launching {len(CRITIC_ROLES)} critics in parallel",
        file=sys.stderr,
        flush=True,
    )
    critics_output: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=len(CRITIC_ROLES)) as pool:
        futures = {
            pool.submit(
                run_critic,
                role=role,
                project_root=project.root,
                model=critic_model,
                reasoning_effort=critic_reasoning,
            ): role
            for role in CRITIC_ROLES
        }
        for future in futures:
            role = futures[future]
            try:
                critics_output[role] = future.result()
            except Exception as e:
                print(
                    f"[pcd] iter {iteration}: critic {role!r} failed: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                critics_output[role] = []

    total_issues = sum(len(v) for v in critics_output.values())
    print(
        f"[pcd] iter {iteration}: critics produced {total_issues} raw issues; judging",
        file=sys.stderr,
        flush=True,
    )
    judgment = run_judge(
        project_root=project.root,
        critics_output=critics_output,
        model=judge_model,
        reasoning_effort=judge_reasoning,
    )
    project.append_judgment(
        iteration=iteration,
        judgment=judgment,
        critics_output=critics_output,
    )

    summary = judgment.get("summary") or {}
    converged = is_converged(judgment)
    convergence_note = _describe_convergence(summary, converged)
    print(
        f"[pcd] iter {iteration}: judge summary {summary}; converged={converged}",
        file=sys.stderr,
        flush=True,
    )

    if converged:
        meta.iterations_done = iteration
        meta.converged = True
        meta.convergence_note = convergence_note
        project.save_meta(meta)
        project.append_revision(
            iteration=iteration,
            note="converged — no revise",
        )
        return judgment

    print(
        f"[pcd] iter {iteration}: proposer revising with judge package",
        file=sys.stderr,
        flush=True,
    )
    issue_package_md = format_issue_package_for_proposer(judgment)
    run_proposer_revise(
        project_root=project.root,
        thread_id=meta.p_thread_id,
        issue_package_markdown=issue_package_md,
        model=proposer_model,
        reasoning_effort=proposer_reasoning,
    )
    meta.iterations_done = iteration
    meta.converged = False
    meta.convergence_note = convergence_note
    project.save_meta(meta)
    project.append_revision(iteration=iteration, note="revised")
    return judgment


def run_until_stop(
    *,
    project: Project,
    max_iterations: int,
    proposer_model: Optional[str],
    critic_model: Optional[str],
    judge_model: Optional[str],
    proposer_reasoning: str = "medium",
    critic_reasoning: str = "medium",
    judge_reasoning: str = "medium",
) -> None:
    """Loop run_single_iteration until convergence or max_iterations."""
    for _ in range(max_iterations):
        judgment = run_single_iteration(
            project=project,
            proposer_model=proposer_model,
            critic_model=critic_model,
            judge_model=judge_model,
            proposer_reasoning=proposer_reasoning,
            critic_reasoning=critic_reasoning,
            judge_reasoning=judge_reasoning,
        )
        if is_converged(judgment):
            print("[pcd] converged; stopping loop", file=sys.stderr, flush=True)
            return
    print(
        f"[pcd] reached max-iter={max_iterations} without convergence",
        file=sys.stderr,
        flush=True,
    )


def _describe_convergence(summary: dict, converged: bool) -> str:
    if converged:
        return (
            f"converged: must_fix={summary.get('must_fix_count', 0)}, "
            f"should_fix={summary.get('should_fix_count', 0)}, "
            f"high_severity={summary.get('high_severity_count', 0)}"
        )
    return (
        f"not converged: must_fix={summary.get('must_fix_count', 0)}, "
        f"should_fix={summary.get('should_fix_count', 0)}, "
        f"high_severity={summary.get('high_severity_count', 0)}"
    )
