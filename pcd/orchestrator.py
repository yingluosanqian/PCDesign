"""P ↔ (C_req, C_design, C_rationale) ↔ J iteration loop."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from pcd.issues import (
    format_issue_package_for_proposer,
    is_converged,
    is_stable,
    no_progress,
    parse_judgment,
)
from pcd.project import Project
from pcd.roles import CRITIC_ROLES, run_critic, run_judge, run_proposer_revise


def run_single_iteration(
    *,
    project: Project,
    proposer_model: Optional[str],
    critic_model: Optional[str],
    judge_model: Optional[str],
    proposer_reasoning: str = "medium",
    critic_reasoning: str = "medium",
    judge_reasoning: str = "medium",
    proposer_agent: str = "codex",
    critic_agent: str = "codex",
    judge_agent: str = "codex",
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

    # Stability always looks back to the last NON-degraded round — a
    # Proposer no-op or critic failure must not supply stability evidence.
    prev_record = project.last_non_degraded_judgment()
    prev_judgment = (prev_record or {}).get("judgment") if prev_record else None

    print(
        f"[pcd] iter {iteration}: launching {len(CRITIC_ROLES)} critics in parallel",
        file=sys.stderr,
        flush=True,
    )
    critics_output: dict[str, list[dict]] = {}
    critic_failures: list[str] = []
    contaminated_critics: list[str] = []
    with ThreadPoolExecutor(max_workers=len(CRITIC_ROLES)) as pool:
        futures = {
            pool.submit(
                run_critic,
                role=role,
                project_root=project.root,
                model=critic_model,
                reasoning_effort=critic_reasoning,
                agent=critic_agent,
                on_progress=_make_role_progress(
                    f"iter {iteration} critic:{role}"
                ),
            ): role
            for role in CRITIC_ROLES
        }
        for future in futures:
            role = futures[future]
            try:
                issues, contaminated = future.result()
                critics_output[role] = issues
                if contaminated:
                    contaminated_critics.append(role)
                    print(
                        f"[pcd] iter {iteration}: critic {role!r} wrote "
                        f"design.md; rolled back from snapshot",
                        file=sys.stderr,
                        flush=True,
                    )
            except Exception as e:
                print(
                    f"[pcd] iter {iteration}: critic {role!r} failed: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                critics_output[role] = []
                critic_failures.append(role)

    total_issues = sum(len(v) for v in critics_output.values())
    print(
        f"[pcd] iter {iteration}: critics produced {total_issues} raw issues; judging",
        file=sys.stderr,
        flush=True,
    )
    judgment, judge_contaminated = run_judge(
        project_root=project.root,
        critics_output=critics_output,
        model=judge_model,
        reasoning_effort=judge_reasoning,
        agent=judge_agent,
        on_progress=_make_role_progress(f"iter {iteration} judge"),
    )
    if judge_contaminated:
        print(
            f"[pcd] iter {iteration}: judge wrote design.md; rolled back "
            f"from snapshot",
            file=sys.stderr,
            flush=True,
        )

    manually_edited = False
    if manual_judge:
        edited = _open_judgment_in_editor(project, iteration, judgment)
        if edited is not None:
            judgment = edited
            manually_edited = True

    summary = judgment.get("summary") or {}
    quality_ok = is_converged(judgment)
    stable = is_stable(prev_judgment)
    degraded_reasons: list[str] = []
    if critic_failures:
        degraded_reasons.append(
            f"critics failed: {', '.join(critic_failures)}"
        )
    if contaminated_critics:
        degraded_reasons.append(
            "critics modified design.md (rolled back): "
            + ", ".join(contaminated_critics)
        )
    if judge_contaminated:
        degraded_reasons.append("judge modified design.md (rolled back)")
    degraded = bool(degraded_reasons)
    # A degraded round can never declare convergence — its quality_ok may
    # be an artifact of missing input, not real quality.
    converged = quality_ok and stable and not degraded
    print(
        f"[pcd] iter {iteration}: summary {summary}; "
        f"quality_ok={quality_ok} stable={stable} "
        f"degraded={degraded} converged={converged}",
        file=sys.stderr,
        flush=True,
    )

    revise_note = ""
    if degraded:
        revise_note = (
            "degraded — no revise (" + "; ".join(degraded_reasons) + ")"
        )
    elif converged:
        revise_note = "converged — no revise"
    elif quality_ok and not stable:
        # Clean round but no stability evidence yet — skip revise; next
        # round will supply the comparison. Avoids wasting a P call on
        # an empty package.
        revise_note = "quality ok, awaiting stability — no revise"
    else:
        print(
            f"[pcd] iter {iteration}: proposer revising with judge package",
            file=sys.stderr,
            flush=True,
        )
        issue_package_md = format_issue_package_for_proposer(judgment)
        before_hash = project.design_hash()
        run_proposer_revise(
            project_root=project.root,
            thread_id=meta.p_thread_id,
            issue_package_markdown=issue_package_md,
            model=proposer_model,
            reasoning_effort=proposer_reasoning,
            agent=proposer_agent,
            on_progress=_make_role_progress(f"iter {iteration} proposer"),
        )
        after_hash = project.design_hash()
        if (
            before_hash is not None
            and after_hash is not None
            and before_hash == after_hash
        ):
            degraded = True
            degraded_reasons.append("proposer no-op (design.md unchanged)")
            revise_note = "proposer no-op — degraded"
            print(
                f"[pcd] iter {iteration}: proposer produced no change to "
                f"design.md; marking round degraded",
                file=sys.stderr,
                flush=True,
            )
        else:
            revise_note = "revised"

    convergence_note = _describe_convergence(
        summary, quality_ok, stable, degraded, degraded_reasons
    )

    project.append_judgment(
        iteration=iteration,
        judgment=judgment,
        critics_output=critics_output,
        manually_edited=manually_edited,
        degraded=degraded,
        degraded_reasons=degraded_reasons,
    )
    round_dir = project.dump_round(
        iteration=iteration,
        critics_output=critics_output,
        judgment=judgment,
        manually_edited=manually_edited,
        degraded=degraded,
        degraded_reasons=degraded_reasons,
    )
    print(
        f"[pcd] iter {iteration}: round artifacts at {round_dir}",
        file=sys.stderr,
        flush=True,
    )

    meta.iterations_done = iteration
    meta.converged = converged
    meta.convergence_note = convergence_note
    project.save_meta(meta)
    project.append_revision(iteration=iteration, note=revise_note)
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
    proposer_agent: str = "codex",
    critic_agent: str = "codex",
    judge_agent: str = "codex",
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
            proposer_agent=proposer_agent,
            critic_agent=critic_agent,
            judge_agent=judge_agent,
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


def _make_role_progress(role_label: str) -> Callable[[str], None]:
    """Return a stderr printer that prefixes each tool event with `role_label`.

    Plain text chunks (the agent's reasoning output) are dropped — during
    an iteration they'd flood the terminal and interleave across the three
    parallel critics. Tool-use markers (emitted by the client as
    `\\n[tool] ...\\n` / `\\n[tool ✓]\\n`) survive and give a clean
    per-role activity trace.
    """
    def progress(s: str) -> None:
        if "[tool" not in s:
            return
        line = s.strip()
        if line:
            print(f"[{role_label}] {line}", file=sys.stderr, flush=True)
    return progress


def _describe_convergence(
    summary: dict,
    quality_ok: bool,
    stable: bool,
    degraded: bool,
    degraded_reasons: list[str],
) -> str:
    base = (
        f"must_fix={summary.get('must_fix_count', 0)}, "
        f"should_fix={summary.get('should_fix_count', 0)}, "
        f"high_severity={summary.get('high_severity_count', 0)}"
    )
    if degraded:
        reasons = "; ".join(degraded_reasons) or "unspecified"
        return f"degraded: {reasons}; {base}"
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
