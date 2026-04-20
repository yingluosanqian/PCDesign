"""P ↔ (C_req, C_design, C_rationale) ↔ J iteration loop."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from pcd.critic import CRITIC_ROLES, run_critic
from pcd.issues import (
    format_issue_package_for_proposer,
    is_converged,
    is_stable,
    no_progress,
    parse_judgment,
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
    manual_judge: bool = False,
) -> dict:
    """One full round: 3 parallel critics -> judge -> (maybe) proposer revise.

    Returns the judgment dict. Updates meta on disk and appends to logs.

    If `manual_judge` is True, the Judge's package is opened in `$EDITOR`
    before convergence/revise. The edited package is what gets logged
    and fed to the Proposer.
    """
    meta = project.load_meta()
    iteration = meta.iterations_done + 1

    prev_record = project.last_judgment()
    prev_judgment = (prev_record or {}).get("judgment") if prev_record else None

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

    manually_edited = False
    if manual_judge:
        edited = _open_judgment_in_editor(project, iteration, judgment)
        if edited is not None:
            judgment = edited
            manually_edited = True

    project.append_judgment(
        iteration=iteration,
        judgment=judgment,
        critics_output=critics_output,
        manually_edited=manually_edited,
    )

    summary = judgment.get("summary") or {}
    quality_ok = is_converged(judgment)
    stable = is_stable(prev_judgment, judgment)
    converged = quality_ok and stable
    convergence_note = _describe_convergence(summary, quality_ok, stable)
    print(
        f"[pcd] iter {iteration}: summary {summary}; "
        f"quality_ok={quality_ok} stable={stable} converged={converged}",
        file=sys.stderr,
        flush=True,
    )

    if converged:
        meta.iterations_done = iteration
        meta.converged = True
        meta.convergence_note = convergence_note
        project.save_meta(meta)
        project.append_revision(iteration=iteration, note="converged — no revise")
        return judgment

    if quality_ok and not stable:
        # Clean round but no stability evidence yet — skip revise; next
        # round will supply the comparison. Avoids wasting a P call on
        # an empty package.
        meta.iterations_done = iteration
        meta.converged = False
        meta.convergence_note = convergence_note
        project.save_meta(meta)
        project.append_revision(
            iteration=iteration,
            note="quality ok, awaiting stability — no revise",
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
    """Loop run_single_iteration until convergence, no-progress, or max_iterations."""
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
        if project.load_meta().converged:
            print("[pcd] converged; stopping loop", file=sys.stderr, flush=True)
            return
        history = project.must_fix_history()
        if no_progress(history):
            tail = history[-3:]
            print(
                f"[pcd] no must_fix progress for 2 rounds (history tail {tail}); "
                "halting. Recommend: review design.md and run "
                "`pcd check <name> --result <confirm_stop|reopen|advisory_only>`",
                file=sys.stderr,
                flush=True,
            )
            _ = judgment  # silence unused warning
            return
    print(
        f"[pcd] reached max-iter={max_iterations} without convergence",
        file=sys.stderr,
        flush=True,
    )


def _describe_convergence(summary: dict, quality_ok: bool, stable: bool) -> str:
    base = (
        f"must_fix={summary.get('must_fix_count', 0)}, "
        f"should_fix={summary.get('should_fix_count', 0)}, "
        f"high_severity={summary.get('high_severity_count', 0)}"
    )
    if quality_ok and stable:
        return f"converged: {base}"
    if quality_ok and not stable:
        return f"quality ok but stability not yet established: {base}"
    return f"not converged: {base}"


def _open_judgment_in_editor(
    project: Project, iteration: int, judgment: dict
) -> Optional[dict]:
    """Drop the judgment to a tmp JSON file, open $EDITOR, re-parse on save.

    Returns the edited judgment, or None if the edit failed to parse
    (caller should keep the original).
    """
    tmp_path = project.meta_dir / f"tmp_judgment_iter{iteration}.json"
    tmp_path.write_text(
        json.dumps(judgment, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    editor = os.environ.get("EDITOR") or "vi"
    print(
        f"[pcd] iter {iteration}: opening judgment in $EDITOR ({editor}) — "
        f"edit and save to continue",
        file=sys.stderr,
        flush=True,
    )
    try:
        subprocess.run([editor, str(tmp_path)], check=True)
    except subprocess.CalledProcessError as e:
        print(
            f"[pcd] iter {iteration}: editor exited non-zero ({e.returncode}); "
            "keeping original judgment",
            file=sys.stderr,
            flush=True,
        )
        tmp_path.unlink(missing_ok=True)
        return None
    try:
        edited_text = tmp_path.read_text(encoding="utf-8")
        edited = parse_judgment(edited_text)
    except Exception as e:
        print(
            f"[pcd] iter {iteration}: manual edit unparseable ({e}); "
            "keeping original judgment",
            file=sys.stderr,
            flush=True,
        )
        return None
    finally:
        tmp_path.unlink(missing_ok=True)
    return edited
