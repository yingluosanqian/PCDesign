"""`pcd` CLI entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pcd.agents import SUPPORTED_AGENTS, normalize_agent
from pcd.orchestrator import run_single_iteration, run_until_stop
from pcd.project import Project, ProjectMeta
from pcd.roles import run_proposer_create


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcd",
        description="PCDesign: multi-agent adversarial design iteration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser(
        "init", help="Create a new design project and produce v0."
    )
    p_init.add_argument("project_name", help="Directory name for the new project.")
    prompt_src = p_init.add_mutually_exclusive_group(required=True)
    prompt_src.add_argument(
        "--prompt",
        help="Initial user requirement, passed inline. Convenient for short "
        "one-liners; use --prompt-file for longer briefs.",
    )
    prompt_src.add_argument(
        "--prompt-file",
        help="Path to a file whose contents are used as the initial user "
        "requirement. Use `-` to read from stdin.",
    )
    p_init.add_argument("--proposer-model", default=None)
    p_init.add_argument("--critic-model", default=None)
    p_init.add_argument("--judge-model", default=None)
    p_init.add_argument(
        "--reasoning", default="medium", help="reasoning effort for the v0 pass"
    )
    p_init.add_argument(
        "--agent",
        choices=SUPPORTED_AGENTS,
        default="codex",
        help="Default backend CLI for all roles (codex | claude). Stored in "
        "meta.json and reused on subsequent run-once / run-until-stop calls. "
        "Per-role overrides below take precedence.",
    )
    p_init.add_argument("--proposer-agent", choices=SUPPORTED_AGENTS, default=None)
    p_init.add_argument("--critic-agent", choices=SUPPORTED_AGENTS, default=None)
    p_init.add_argument("--judge-agent", choices=SUPPORTED_AGENTS, default=None)
    p_init.add_argument(
        "--reframer-agent",
        choices=SUPPORTED_AGENTS,
        default=None,
        help="Agent for the Reframer (alternative-generator) role. "
        "Defaults to --agent if unset.",
    )
    p_init.add_argument(
        "--reframer-model",
        default=None,
        help="Model for the Reframer role (backend default if unset).",
    )

    p_once = sub.add_parser(
        "run-once", help="Run exactly one critic+judge(+revise) iteration."
    )
    _add_run_args(p_once)
    p_once.add_argument(
        "--manual-judge",
        action="store_true",
        help="After Judge emits its package, open it in $EDITOR for manual edits "
        "before the Proposer consumes it.",
    )

    p_until = sub.add_parser(
        "run-until-stop",
        help="Loop iterations until convergence or --max-iter.",
    )
    p_until.add_argument("--max-iter", type=int, required=True, dest="max_iter")
    _add_run_args(p_until)

    p_status = sub.add_parser(
        "status", help="Print project meta and the last judgment summary."
    )
    p_status.add_argument("project_name")

    p_check = sub.add_parser(
        "check",
        help="Record a human spot-check result; can promote a provisional "
        "stop to a confirmed stop or reopen.",
    )
    p_check.add_argument("project_name")
    p_check.add_argument(
        "--result",
        required=True,
        choices=("confirm_stop", "reopen", "advisory_only"),
        help="confirm_stop: mark project converged; reopen: mark not "
        "converged; advisory_only: record only, no meta change.",
    )
    p_check.add_argument("--scope", default="", help="What the human reviewed.")
    p_check.add_argument("--note", default="", help="Free-form note.")

    return parser


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("project_name")
    p.add_argument("--proposer-reasoning", default="medium")
    p.add_argument("--critic-reasoning", default="medium")
    p.add_argument("--judge-reasoning", default="medium")
    p.add_argument("--reframer-reasoning", default="medium")


def _resolve_initial_prompt(args: argparse.Namespace) -> str:
    """Return the inline --prompt, or the contents of --prompt-file (or stdin)."""
    if args.prompt is not None:
        return args.prompt
    path = args.prompt_file
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _stderr_progress(s: str) -> None:
    """Dump agent stream chunks to stderr verbatim, unbuffered.

    Claude emits text blocks as-is (no newline between blocks) plus
    `\\n[tool] ...\\n` markers for tool_use and `\\n[tool ✓]\\n` for
    tool_result, so a plain write-through produces readable live output.
    """
    print(s, end="", file=sys.stderr, flush=True)


def _cmd_init(args: argparse.Namespace) -> int:
    root = Path.cwd() / args.project_name
    project = Project(root)
    if root.exists():
        print(f"[pcd] error: {root} already exists", file=sys.stderr)
        return 1
    try:
        prompt_text = _resolve_initial_prompt(args)
    except (FileNotFoundError, OSError) as e:
        print(f"[pcd] error: cannot read --prompt-file: {e}", file=sys.stderr)
        return 1
    if not prompt_text.strip():
        print("[pcd] error: initial prompt is empty", file=sys.stderr)
        return 1
    proposer_agent = normalize_agent(args.proposer_agent or args.agent)
    critic_agent = normalize_agent(args.critic_agent or args.agent)
    judge_agent = normalize_agent(args.judge_agent or args.agent)
    reframer_agent = normalize_agent(args.reframer_agent or args.agent)
    project.create_layout(initial_prompt=prompt_text)
    print(f"[pcd] created project at {project.root}", file=sys.stderr)
    print(
        f"[pcd] proposer generating v0 via {proposer_agent} …",
        file=sys.stderr,
        flush=True,
    )
    thread_id = run_proposer_create(
        project_root=project.root,
        user_prompt=prompt_text,
        model=args.proposer_model,
        reasoning_effort=args.reasoning,
        agent=proposer_agent,
        on_progress=_stderr_progress,
    )
    project.save_meta(
        ProjectMeta(
            p_thread_id=thread_id,
            proposer_model=args.proposer_model,
            critic_model=args.critic_model,
            judge_model=args.judge_model,
            created_at=Project.now_iso(),
            iterations_done=0,
            proposer_agent=proposer_agent,
            critic_agent=critic_agent,
            judge_agent=judge_agent,
            reframer_agent=reframer_agent,
            reframer_model=args.reframer_model,
        )
    )
    if not project.design_path.exists():
        print(
            "[pcd] warning: proposer did not produce ./design.md",
            file=sys.stderr,
        )
        return 2
    print(f"[pcd] v0 design written to {project.design_path}", file=sys.stderr)
    return 0


def _cmd_run_once(args: argparse.Namespace) -> int:
    project = _load_project(args.project_name)
    if project is None:
        return 1
    meta = project.load_meta()
    if meta.converged:
        print(
            "[pcd] project already marked converged; run `status` to inspect",
            file=sys.stderr,
        )
    run_single_iteration(
        project=project,
        proposer_model=meta.proposer_model,
        critic_model=meta.critic_model,
        judge_model=meta.judge_model,
        proposer_reasoning=args.proposer_reasoning,
        critic_reasoning=args.critic_reasoning,
        judge_reasoning=args.judge_reasoning,
        proposer_agent=meta.proposer_agent,
        critic_agent=meta.critic_agent,
        judge_agent=meta.judge_agent,
        reframer_model=meta.reframer_model,
        reframer_reasoning=args.reframer_reasoning,
        reframer_agent=meta.reframer_agent,
        manual_judge=args.manual_judge,
    )
    meta = project.load_meta()
    print(
        f"[pcd] iteration {meta.iterations_done} complete; "
        f"converged={meta.converged}; design at {project.design_path}",
        file=sys.stderr,
    )
    return 0


def _cmd_run_until_stop(args: argparse.Namespace) -> int:
    project = _load_project(args.project_name)
    if project is None:
        return 1
    meta = project.load_meta()
    run_until_stop(
        project=project,
        max_iterations=args.max_iter,
        proposer_model=meta.proposer_model,
        critic_model=meta.critic_model,
        judge_model=meta.judge_model,
        proposer_reasoning=args.proposer_reasoning,
        critic_reasoning=args.critic_reasoning,
        judge_reasoning=args.judge_reasoning,
        proposer_agent=meta.proposer_agent,
        critic_agent=meta.critic_agent,
        judge_agent=meta.judge_agent,
        reframer_model=meta.reframer_model,
        reframer_reasoning=args.reframer_reasoning,
        reframer_agent=meta.reframer_agent,
    )
    meta = project.load_meta()
    print(
        f"[pcd] stopped at iteration {meta.iterations_done}; "
        f"converged={meta.converged}; design at {project.design_path}",
        file=sys.stderr,
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    project = _load_project(args.project_name)
    if project is None:
        return 1
    meta = project.load_meta()
    print(f"project: {project.root}")
    print(f"  created_at:       {meta.created_at}")
    print(f"  proposer_model:   {meta.proposer_model}")
    print(f"  critic_model:     {meta.critic_model}")
    print(f"  judge_model:      {meta.judge_model}")
    print(f"  proposer_agent:   {meta.proposer_agent}")
    print(f"  critic_agent:     {meta.critic_agent}")
    print(f"  judge_agent:      {meta.judge_agent}")
    print(f"  reframer_agent:   {meta.reframer_agent}")
    print(f"  reframer_model:   {meta.reframer_model}")
    print(f"  reframe_at_round: {meta.reframe_at_round}")
    print(f"  reframe_tested:   {meta.reframe_tested}")
    print(f"  iterations_done:  {meta.iterations_done}")
    print(f"  converged:        {meta.converged}")
    if meta.convergence_note:
        print(f"  convergence_note: {meta.convergence_note}")
    last = project.last_judgment()
    if last is None:
        print("  last_judgment:    (none)")
    else:
        j = last.get("judgment") or {}
        s = j.get("summary") or {}
        print(f"  last_iteration:   {last.get('iteration')}")
        print(
            "  last_summary:     "
            f"must_fix={s.get('must_fix_count', 0)}, "
            f"should_fix={s.get('should_fix_count', 0)}, "
            f"reject={s.get('reject_count', 0)}, "
            f"defer={s.get('defer_count', 0)}, "
            f"high_severity={s.get('high_severity_count', 0)}"
        )
        if last.get("degraded"):
            reasons = "; ".join(last.get("degraded_reasons") or []) or "unspecified"
            print(f"  last_degraded:    yes ({reasons})")
    mf_history = project.must_fix_history()
    if mf_history:
        print(f"  must_fix_trend:   {mf_history}  (non-degraded rounds only)")
    if project.human_checks_log_path.exists():
        print(f"  human_checks_log: {project.human_checks_log_path}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    project = _load_project(args.project_name)
    if project is None:
        return 1
    meta = project.load_meta()
    check_id = project.next_human_check_id()
    record = {
        "check_id": check_id,
        "iteration": meta.iterations_done,
        "checker": "human",
        "scope": args.scope,
        "result": args.result,
        "found_issues": [],
        "note": args.note,
        "timestamp": Project.now_iso(),
    }
    project.append_human_check(record)
    if args.result == "confirm_stop":
        meta.converged = True
        meta.convergence_note = f"confirmed_stop via human check #{check_id}"
        project.save_meta(meta)
    elif args.result == "reopen":
        meta.converged = False
        meta.convergence_note = f"reopened via human check #{check_id}"
        project.save_meta(meta)
    # advisory_only: no meta change.
    print(
        f"[pcd] human check #{check_id} recorded at iteration "
        f"{meta.iterations_done}: result={args.result}",
        file=sys.stderr,
    )
    return 0


def _load_project(project_name: str) -> Project | None:
    root = Path.cwd() / project_name
    project = Project(root)
    if not project.exists():
        print(f"[pcd] error: no project at {project.root}", file=sys.stderr)
        return None
    return project


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "init": _cmd_init,
        "run-once": _cmd_run_once,
        "run-until-stop": _cmd_run_until_stop,
        "status": _cmd_status,
        "check": _cmd_check,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 2
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
