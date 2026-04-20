"""Proposer agent: long-lived codex thread that writes ./design.md."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pcd.codex_client import CodexClient
from pcd.prompts import proposer_create_prompt, proposer_revise_prompt


def run_proposer_create(
    *,
    project_root: Path,
    user_prompt: str,
    model: Optional[str],
    reasoning_effort: str = "medium",
) -> str:
    """Start a fresh P session and produce v0 design.md. Returns thread_id."""
    with CodexClient(
        cwd=str(project_root),
        reasoning_effort=reasoning_effort,
    ) as client:
        thread_id = client.start_thread(
            cwd=str(project_root),
            model=model,
            sandbox="workspace-write",
        )
        client.run_turn(
            thread_id=thread_id,
            cwd=str(project_root),
            model=model,
            prompt=proposer_create_prompt(user_prompt),
        )
        return thread_id


def run_proposer_revise(
    *,
    project_root: Path,
    thread_id: str,
    critique: str,
    model: Optional[str],
    reasoning_effort: str = "medium",
) -> None:
    """Resume P's long session, apply critique, rewrite design.md in place."""
    with CodexClient(
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
            prompt=proposer_revise_prompt(critique),
        )
