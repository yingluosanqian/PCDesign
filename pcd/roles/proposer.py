"""Proposer agent: long-lived agent thread that writes ./design.md."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from pcd.agents import make_agent_client
from pcd.roles.prompts import proposer_create_prompt, proposer_revise_prompt


def run_proposer_create(
    *,
    project_root: Path,
    user_prompt: str,
    model: Optional[str],
    reasoning_effort: str = "medium",
    agent: str = "codex",
    on_progress: Optional[Callable[[str], None]] = None,
) -> str:
    """Start a fresh P session and produce v0 design.md. Returns thread_id."""
    with make_agent_client(
        agent,
        cwd=str(project_root),
        reasoning_effort=reasoning_effort,
    ) as client:
        thread_id = client.start_thread(
            cwd=str(project_root),
            model=model,
            sandbox="workspace-write",
        )
        result = client.run_turn(
            thread_id=thread_id,
            cwd=str(project_root),
            model=model,
            prompt=proposer_create_prompt(user_prompt),
            on_progress=on_progress,
        )
        return result.thread_id


def run_proposer_revise(
    *,
    project_root: Path,
    thread_id: str,
    issue_package_markdown: str,
    model: Optional[str],
    reasoning_effort: str = "medium",
    agent: str = "codex",
    on_progress: Optional[Callable[[str], None]] = None,
    guidance_markdown: str = "",
) -> None:
    """Resume P's long session, apply the Judge's issue package, rewrite design.md.

    The issue package may contain `section="alternatives"` clusters
    emitted by the Reframer + Exploration Critic. If `guidance_markdown`
    is non-empty, the Proposer also sees user-injected guidance and
    must acknowledge each item in Rationale's `## User Guidance
    Received` subsection.
    """
    with make_agent_client(
        agent,
        cwd=str(project_root),
        reasoning_effort=reasoning_effort,
    ) as client:
        client.resume_thread(
            thread_id=thread_id,
            cwd=str(project_root),
            model=model,
            sandbox="workspace-write",
        )
        client.run_turn(
            thread_id=thread_id,
            cwd=str(project_root),
            model=model,
            prompt=proposer_revise_prompt(
                issue_package_markdown, guidance_markdown
            ),
            on_progress=on_progress,
        )
