"""P ↔ (C_req, C_design, C_rationale) ↔ J iteration loop."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from pcd.issues import (
    format_guidance_for_proposer,
    format_issue_package_for_proposer,
    is_converged,
    is_stable,
    no_progress,
    parse_judgment,
)
from pcd.project import (
    Project,
    STATUS_COMMITTED,
    STATUS_CRITICS_DONE,
    STATUS_JUDGE_DONE,
    STATUS_JUDGED,
    STATUS_PROPOSER_DONE,
    STATUS_STARTED,
)
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

    # ---- Recovery check -----------------------------------------------
    # If this iter's directory has a STATUS file from a prior crashed
    # invocation, skip over whichever expensive steps have already run.
    resume_status = project.read_round_status(iteration)
    if resume_status == STATUS_COMMITTED:
        # Shouldn't happen — committed means meta.iterations_done was
        # bumped on the prior invocation. Be safe: warn and proceed.
        print(
            f"[pcd] iter {iteration}: WARNING STATUS=committed but "
            f"iteration was not reflected in meta; restarting fresh",
            file=sys.stderr,
            flush=True,
        )
        resume_status = None
    resumed_flags = project.load_round_flags(iteration) or {}
    if resume_status:
        print(
            f"[pcd] iter {iteration}: resuming from STATUS={resume_status} "
            f"(skipping already-completed phases)",
            file=sys.stderr,
            flush=True,
        )

    critics_output: dict[str, list[dict]] = {}
    critic_failures: list[str] = list(resumed_flags.get("critic_failures") or [])
    contaminated_critics: list[str] = list(
        resumed_flags.get("contaminated_critics") or []
    )
    reframer_fired: bool = bool(resumed_flags.get("reframer_fired", False))
    reframer_trigger: str = str(resumed_flags.get("reframer_trigger") or "")
    manually_edited: bool = bool(resumed_flags.get("manually_edited", False))
    judge_contaminated: bool = bool(resumed_flags.get("judge_contaminated", False))
    reframer_package: Optional[dict] = None

    if _past(resume_status, STATUS_CRITICS_DONE):
        # Phase 1 already done: load everything critics produced.
        resume_roles = list(CRITIC_ROLES)
        if reframer_fired:
            resume_roles += ["reframer", "exploration"]
        critics_output = project.load_critics_output_from_disk(
            iteration, iter(resume_roles)
        )
        if reframer_fired:
            reframer_package = project.load_reframer_package(iteration)
        total_issues = sum(len(v) for v in critics_output.values())
        print(
            f"[pcd] iter {iteration}: recovered {len(critics_output)} critics' "
            f"output ({total_issues} raw issues) from disk",
            file=sys.stderr,
            flush=True,
        )
    else:
        # Round-level audit snapshot — captures the design.md state that
        # critics read, for forensics after contamination / rollback.
        project.snapshot_design_pre_critics(iteration)
        project.write_round_status(iteration, STATUS_STARTED)
        _run_critics_phase(
            project=project,
            iteration=iteration,
            critic_model=critic_model,
            critic_reasoning=critic_reasoning,
            critic_agent=critic_agent,
            critics_output=critics_output,
            critic_failures=critic_failures,
            contaminated_critics=contaminated_critics,
        )
        # Reframer / exploration (conditional).
        fired, trigger, pkg = _maybe_run_reframer_phase(
            project=project,
            iteration=iteration,
            meta=meta,
            reframer_model=reframer_model,
            reframer_reasoning=reframer_reasoning,
            reframer_agent=reframer_agent,
            critics_output=critics_output,
            critic_failures=critic_failures,
            contaminated_critics=contaminated_critics,
        )
        reframer_fired = fired
        reframer_trigger = trigger
        reframer_package = pkg
        # Persist per-critic outputs as we go for crash recovery.
        for role, issues in critics_output.items():
            project.persist_critic_output(iteration, role, issues)
        if reframer_package is not None:
            project.persist_reframer_package(iteration, reframer_package)
        project.persist_round_flags(
            iteration,
            {
                "critic_failures": critic_failures,
                "contaminated_critics": contaminated_critics,
                "reframer_fired": reframer_fired,
                "reframer_trigger": reframer_trigger,
            },
        )
        project.write_round_status(iteration, STATUS_CRITICS_DONE)

    # ---- Judge (skipped if resumed at/after judge_done) ---------------
    if _past(resume_status, STATUS_JUDGE_DONE):
        judgment = project.load_judgment_artifact(iteration) or {}
        print(
            f"[pcd] iter {iteration}: recovered judgment.json from disk",
            file=sys.stderr,
            flush=True,
        )
    else:
        total_issues = sum(len(v) for v in critics_output.values())
        print(
            f"[pcd] iter {iteration}: {len(critics_output)} critics produced "
            f"{total_issues} raw issues; judging",
            file=sys.stderr,
            flush=True,
        )
        judgment, judge_contaminated = run_judge(
            project_root=project.root,
            iteration=iteration,
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
        project.persist_judgment(iteration, judgment)
        project.persist_round_flags(
            iteration,
            {
                "critic_failures": critic_failures,
                "contaminated_critics": contaminated_critics,
                "reframer_fired": reframer_fired,
                "reframer_trigger": reframer_trigger,
                "judge_contaminated": judge_contaminated,
            },
        )
        project.write_round_status(iteration, STATUS_JUDGE_DONE)

    if _past(resume_status, STATUS_JUDGED):
        # On resume, manually_edited was already restored from round_flags.
        pass
    elif manual_judge:
        edited = _open_judgment_in_editor(project, iteration, judgment)
        if edited is not None:
            judgment = edited
            manually_edited = True
            project.persist_judgment(iteration, judgment)
            project.persist_round_flags(
                iteration,
                {
                    **(project.load_round_flags(iteration) or {}),
                    "manually_edited": True,
                },
            )
    if not _past(resume_status, STATUS_JUDGED):
        project.write_round_status(iteration, STATUS_JUDGED)

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
    # Convergence gate (see design doc §2.4):
    #   (i)  reframe_tested — Reframer fired and its issues flowed through
    #        Judge. Satisfied either already (prior round) or this round
    #        (we just fired it).
    #   (ii) reframe_degraded_confirmed — Reframer failed twice, the
    #        human explicitly acknowledged the missing coverage via
    #        `pcd check --result confirm_reframe_degraded`.
    reframe_gate_ok = (
        meta.reframe_tested
        or reframer_fired
        or meta.reframe_degraded_confirmed
    )
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
    resumed_revise_outcome = resumed_flags.get("revise_outcome")
    if _past(resume_status, STATUS_PROPOSER_DONE) and resumed_revise_outcome:
        # Revise already completed in the crashed run; restore its outcome.
        revise_note = str(resumed_revise_outcome.get("note") or "")
        if resumed_revise_outcome.get("proposer_noop"):
            degraded = True
            degraded_reasons.append("proposer no-op (design.md unchanged)")
    elif degraded:
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
        pending_guidance = project.pending_guidance()
        guidance_md = format_guidance_for_proposer(pending_guidance)
        if pending_guidance:
            print(
                f"[pcd] iter {iteration}: proposer revising with judge "
                f"package + {len(pending_guidance)} pending guidance entry(ies)",
                file=sys.stderr,
                flush=True,
            )
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
            guidance_markdown=guidance_md,
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
            # Consumed guidance gets logged so the next round doesn't
            # re-feed it. Only mark on a successful (non-degraded) revise.
            if pending_guidance:
                guidance_ids = [g["id"] for g in pending_guidance if "id" in g]
                project.mark_guidance_consumed(iteration, guidance_ids)
                print(
                    f"[pcd] iter {iteration}: consumed {len(guidance_ids)} "
                    f"guidance entry(ies)",
                    file=sys.stderr,
                    flush=True,
                )
        # Persist revise outcome for crash recovery.
        project.persist_round_flags(
            iteration,
            {
                **(project.load_round_flags(iteration) or {}),
                "revise_outcome": {
                    "note": revise_note,
                    "proposer_noop": degraded
                    and any("proposer no-op" in r for r in degraded_reasons),
                },
            },
        )
        project.write_round_status(iteration, STATUS_PROPOSER_DONE)

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
    project.write_round_status(iteration, STATUS_COMMITTED)
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


def _past(status: Optional[str], milestone: str) -> bool:
    """True iff `status` is at or past `milestone` in STATUS_SEQUENCE."""
    if status is None:
        return False
    from pcd.project import STATUS_SEQUENCE
    try:
        return STATUS_SEQUENCE.index(status) >= STATUS_SEQUENCE.index(milestone)
    except ValueError:
        return False


def _run_critics_phase(
    *,
    project: Project,
    iteration: int,
    critic_model: Optional[str],
    critic_reasoning: str,
    critic_agent: str,
    critics_output: dict[str, list[dict]],
    critic_failures: list[str],
    contaminated_critics: list[str],
) -> None:
    """Execute the three section critics in parallel, mutating the
    passed-in dicts/lists. Kept as a helper so the critics-phase
    code path is reusable across fresh and resumed rounds."""
    with ThreadPoolExecutor(max_workers=len(CRITIC_ROLES)) as pool:
        futures = {
            pool.submit(
                run_critic,
                role=role,
                project_root=project.root,
                iteration=iteration,
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


def _maybe_run_reframer_phase(
    *,
    project: Project,
    iteration: int,
    meta,
    reframer_model: Optional[str],
    reframer_reasoning: str,
    reframer_agent: str,
    critics_output: dict[str, list[dict]],
    critic_failures: list[str],
    contaminated_critics: list[str],
) -> tuple[bool, str, Optional[dict]]:
    """Fire Reframer + Exploration if the scheduled trigger says so.

    Returns `(fired, trigger, package)`. `package` is the raw
    `{hard_constraints, alternatives}` dict if Reframer succeeded,
    else None.
    """
    if meta.reframe_tested or iteration < meta.reframe_at_round:
        return False, "", None
    if meta.reframe_attempts >= 2:
        # Already tried twice and failed both times — gate (ii) applies.
        # Don't burn more budget; the human must acknowledge via
        # `pcd check --result confirm_reframe_degraded`.
        return False, "", None

    trigger = "scheduled"
    print(
        f"[pcd] iter {iteration}: Reframer firing (trigger={trigger}, "
        f"attempt {meta.reframe_attempts + 1}/2)",
        file=sys.stderr,
        flush=True,
    )
    meta.reframe_attempts += 1
    project.save_meta(meta)
    try:
        reframer_issues, reframer_package, reframer_contaminated = run_reframer(
            project_root=project.root,
            iteration=iteration,
            model=reframer_model,
            reasoning_effort=reframer_reasoning,
            agent=reframer_agent,
            on_progress=_make_role_progress(f"iter {iteration} reframer"),
        )
        critics_output["reframer"] = reframer_issues
        alt_path = project.append_alternatives(
            iteration=iteration,
            alternatives=reframer_package.get("alternatives") or [],
            hard_constraints=reframer_package.get("hard_constraints") or [],
            trigger=trigger,
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
    except Exception as e:
        print(
            f"[pcd] iter {iteration}: Reframer failed: {e}",
            file=sys.stderr,
            flush=True,
        )
        critic_failures.append("reframer")
        critics_output["reframer"] = []
        return False, trigger, None

    # Exploration critic runs sequentially after Reframer.
    print(
        f"[pcd] iter {iteration}: Exploration critic auditing Reframer package",
        file=sys.stderr,
        flush=True,
    )
    try:
        exploration_issues, exploration_contaminated = run_exploration_critic(
            project_root=project.root,
            iteration=iteration,
            reframer_package=reframer_package,
            model=reframer_model,
            reasoning_effort=reframer_reasoning,
            agent=reframer_agent,
            on_progress=_make_role_progress(f"iter {iteration} exploration"),
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

    return True, trigger, reframer_package


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
