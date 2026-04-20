"""`pcd` CLI entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pcd.orchestrator import run_iterations
from pcd.project import Project, ProjectMeta
from pcd.proposer import run_proposer_create


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcd",
        description="PCDesign: Proposer/Critic adversarial design iteration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser(
        "create", help="Create a new design project (produces v0 only)."
    )
    p_create.add_argument("project_name", help="Directory name for the new project.")
    p_create.add_argument("--prompt", required=True, help="Initial user requirement.")
    p_create.add_argument("--proposer-model", default=None)
    p_create.add_argument("--critic-model", default=None)
    p_create.add_argument(
        "--reasoning", default="medium", help="reasoning effort for P (v0 pass)"
    )

    p_cont = sub.add_parser("continue", help="Run k more P↔C iterations.")
    p_cont.add_argument("project_name")
    p_cont.add_argument("--iter", type=int, required=True, dest="iterations")
    p_cont.add_argument("--proposer-reasoning", default="medium")
    p_cont.add_argument("--critic-reasoning", default="medium")

    return parser


def _cmd_create(args: argparse.Namespace) -> int:
    root = Path.cwd() / args.project_name
    project = Project(root)
    if root.exists():
        print(f"[pcd] error: {root} already exists", file=sys.stderr)
        return 1
    project.create_layout(initial_prompt=args.prompt)
    print(f"[pcd] created project at {project.root}", file=sys.stderr)
    print("[pcd] proposer generating v0 …", file=sys.stderr, flush=True)
    thread_id = run_proposer_create(
        project_root=project.root,
        user_prompt=args.prompt,
        model=args.proposer_model,
        reasoning_effort=args.reasoning,
    )
    project.save_meta(
        ProjectMeta(
            p_thread_id=thread_id,
            proposer_model=args.proposer_model,
            critic_model=args.critic_model,
            created_at=Project.now_iso(),
            iterations_done=0,
        )
    )
    if not project.design_path.exists():
        print(
            "[pcd] warning: proposer did not produce ./design.md",
            file=sys.stderr,
        )
        return 2
    print(
        f"[pcd] v0 design written to {project.design_path}",
        file=sys.stderr,
    )
    return 0


def _cmd_continue(args: argparse.Namespace) -> int:
    root = Path.cwd() / args.project_name
    project = Project(root)
    if not project.exists():
        print(f"[pcd] error: no project at {project.root}", file=sys.stderr)
        return 1
    meta = project.load_meta()
    run_iterations(
        project=project,
        iterations=args.iterations,
        proposer_model=meta.proposer_model,
        critic_model=meta.critic_model,
        proposer_reasoning=args.proposer_reasoning,
        critic_reasoning=args.critic_reasoning,
    )
    print(
        f"[pcd] {args.iterations} iterations complete; design at {project.design_path}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "create":
        return _cmd_create(args)
    if args.command == "continue":
        return _cmd_continue(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
