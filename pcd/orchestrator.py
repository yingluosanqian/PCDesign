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
from pcd.roles import (
    CRITIC_ROLES,
    run_critic,
    run_exploration_critic,
    run_judge,
    run_proposer_revise,
    run_reframer,
)


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
    reframer_model: Optional[str] = None,
    reframer_reasoning: str = "medium",
    reframer_agent: str = "codex",
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

    # ---- Reframer / Exploration critics (conditional on trigger) ----
    # Reframer fires at most once per `run-until-stop`, gated by
    # meta.reframe_tested. Trigger: iteration >= meta.reframe_at_round
    # AND not yet tested. Its output flows through the Judge just like
    # the other three critics — critic → judge → proposer is a single
    # unified pipeline. Exploration critic runs on the same round,
    # sequentially after Reframer (it needs the Reframer package).
    #
    # No converge-gate here: the gate lives in run_until_stop, which
    # refuses to exit on converged while reframe_tested is still False.
    # Default reframe_at_round=2 means Reframer fires at iter 2 on
    # every project, long before any sane convergence can happen.
    reframer_fired = False
    reframer_trigger = ""
    exploration_contaminated = False
    if not meta.reframe_tested and iteration >= meta.reframe_at_round:
        reframer_trigger = "scheduled"
        print(
            f"[pcd] iter {iteration}: Reframer firing "
            f"(trigger={reframer_trigger})",
            file=sys.stderr,
            flush=True,
        )
        try:
            reframer_issues, reframer_package, reframer_contaminated = run_reframer(
                project_root=project.root,
                model=reframer_model,
                reasoning_effort=reframer_reasoning,
                agent=reframer_agent,
                on_progress=_make_role_progress(
                    f"iter {iteration} reframer"
                ),
            )
            reframer_fired = True
            critics_output["reframer"] = reframer_issues
            alt_path = project.append_alternatives(
                iteration=iteration,
                alternatives=reframer_package.get("alternatives") or [],
                hard_constraints=reframer_package.get("hard_constraints") or [],
                trigger=reframer_trigger,
            )
            print(
                f"[pcd] iter {iteration}: Reframer produced "
                f"{len(reframer_issues)} alternative(s); dumped to {alt_path}",
                file=sys.stderr,
                flush=True,
            )
            if reframer_contaminated:
                contaminated_critics.append("reframer")
                print(
                    f"[pcd] iter {iteration}: reframer wrote design.md; "
                    f"rolled back from snapshot",
                    file=sys.stderr,
                    flush=True,
                )

            # Exploration critic runs sequentially after Reframer —
            # it audits the Reframer package and needs it as input.
            print(
                f"[pcd] iter {iteration}: Exploration critic auditing "
                f"Reframer package",
                file=sys.stderr,
                flush=True,
            )
            try:
                exploration_issues, exploration_contaminated = run_exploration_critic(
                    project_root=project.root,
                    reframer_package=reframer_package,
                    model=reframer_model,
                    reasoning_effort=reframer_reasoning,
                    agent=reframer_agent,
                    on_progress=_make_role_progress(
                        f"iter {iteration} exploration"
                    ),
                )
                critics_output["exploration"] = exploration_issues
                if exploration_contaminated:
                    contaminated_critics.append("exploration")
                    print(
                        f"[pcd] iter {iteration}: exploration critic wrote "
                        f"design.md; rolled back from snapshot",
                        file=sys.stderr,
                        flush=True,
                    )
            except Exception as e:
                print(
                    f"[pcd] iter {iteration}: Exploration critic failed: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                critic_failures.append("exploration")
                critics_output["exploration"] = []
        except Exception as e:
            print(
                f"[pcd] iter {iteration}: Reframer failed: {e}",
                file=sys.stderr,
                flush=True,
            )
            critic_failures.append("reframer")
            critics_output["reframer"] = []

    # ---- Judge (now possibly on 5 critic inputs) ---------------------
    total_issues = sum(len(v) for v in critics_output.values())
    print(
        f"[pcd] iter {iteration}: {len(critics_output)} critics produced "
        f"{total_issues} raw issues; judging",
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
    # Convergence now requires reframe_tested too — but since Reframer
    # fires this round if not yet tested, reframe_tested is read AFTER
    # we've decided whether this round fires it. If Reframer fired this
    # round, reframe_tested is about-to-be-True.
    reframe_gate_ok = meta.reframe_tested or reframer_fired
    converged = quality_ok and stable and not degraded and reframe_gate_ok
    print(
        f"[pcd] iter {iteration}: summary {summary}; "
        f"quality_ok={quality_ok} stable={stable} "
        f"degraded={degraded} reframe_gate_ok={reframe_gate_ok} "
        f"converged={converged}",
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
    elif quality_ok and stable and not reframe_gate_ok:
        # Should not happen in practice — if quality_ok and stable and
        # Reframer didn't fire, reframe_at_round must be set absurdly
        # high. Force a revise anyway so the loop doesn't stall silently.
        revise_note = (
            "quality ok but reframe gate not satisfied — revising anyway"
        )
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
        summary, quality_ok, stable, degraded, degraded_reasons,
        reframe_gate_ok,
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
    if reframer_fired:
        # Reframer fired this round; its issues flowed through Judge
        # like any other critic, and the Proposer's revise (above)
        # consumed the resulting package. reframe_tested now holds for
        # the rest of this run — the convergence gate is satisfied.
        meta.reframe_tested = True
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
    reframer_model: Optional[str] = None,
    reframer_reasoning: str = "medium",
    reframer_agent: str = "codex",
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
            reframer_model=reframer_model,
            reframer_reasoning=reframer_reasoning,
            reframer_agent=reframer_agent,
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
    if not project.load_meta().reframe_tested:
        print(
            "[pcd] note: Reframer never fired in this run (reframe_tested "
            "still False). Set --max-iter higher or lower meta.reframe_at_round "
            "so structurally-different alternatives get a chance to challenge "
            "the current design.",
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
    reframe_gate_ok: bool,
) -> str:
    base = (
        f"must_fix={summary.get('must_fix_count', 0)}, "
        f"should_fix={summary.get('should_fix_count', 0)}, "
        f"high_severity={summary.get('high_severity_count', 0)}"
    )
    if degraded:
        reasons = "; ".join(degraded_reasons) or "unspecified"
        return f"degraded: {reasons}; {base}"
    if quality_ok and stable and reframe_gate_ok:
        return f"converged: {base}"
    if quality_ok and stable and not reframe_gate_ok:
        return (
            f"quality ok + stable but Reframer has not yet fired "
            f"(reframe_at_round not reached): {base}"
        )
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
