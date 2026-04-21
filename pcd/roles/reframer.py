"""Reframer agent: proposes structurally-different alternative designs.

Reframer is a generator, not a discriminator. Unlike critics (which find
flaws in the current design) and the Judge (which merges flaws into
decisions), Reframer reads the ORIGINAL requirement and generates
alternative skeletons that would also satisfy it — each coming from a
different cognitive move (analogy / inversion / minimalization /
rederivation / requirement_pushback / scale_extrapolation).

The Proposer's next revise turn is forced to consume the alternatives
and take an explicit position on each (adopt / partial_adopt / reject).
This is the tool's exploration primitive — without it the round loop
can only refine, never reframe.

Sandbox is read-only like critics and Judge: Reframer reads files but
must not write them. Enforced by `pcd.roles._guard.readonly_guarded`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from pcd.agents import make_agent_client
from pcd.issues import parse_alternatives
from pcd.roles._guard import readonly_guarded
from pcd.roles.prompts import reframer_prompt


def run_reframer(
    *,
    project_root: Path,
    model: Optional[str],
    reasoning_effort: str = "medium",
    agent: str = "codex",
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict], bool]:
    """Run one fresh Reframer session.

    Returns `(alternatives, contaminated)`. `contaminated=True` means
    the subprocess modified design.md and the file has been restored
    from a pre-call snapshot (same guard as critics / Judge).
    """
    def _body():
        with make_agent_client(
            agent,
            cwd=str(project_root),
            reasoning_effort=reasoning_effort,
        ) as client:
            thread_id = client.start_thread(
                cwd=str(project_root),
                model=model,
                sandbox="read-only",
            )
            return client.run_turn(
                thread_id=thread_id,
                cwd=str(project_root),
                model=model,
                prompt=reframer_prompt(),
                on_progress=on_progress,
            )

    result, contaminated = readonly_guarded(project_root, _body)
    return parse_alternatives(result.final_text), contaminated
