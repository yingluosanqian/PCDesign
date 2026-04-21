"""Reframer agent: proposes structurally-different alternative designs.

Reframer is architecturally a critic — its output flows through Judge
like any other critic, and the Proposer's revise turn handles its
issues through the unified Judge package.

What makes it different from requirement / design / rationale critics:
- It reads the ORIGINAL requirement (`.pcd/initial_prompt.txt`) rather
  than judging the current design for flaws. Its output is alternative
  skeletons that would also satisfy the user's need.
- Its issues carry `section="alternatives"` and the alternative sketch
  as evidence. The `suggested_direction` asks the Proposer to engage
  in a new Rationale subsection (`Rejected/Adopted Alternatives`).
- The alt sketches are paired with an Exploration Critic (see
  `pcd.roles.exploration`) that independently audits whether each alt
  is coherent and respects the hard constraints — its issues also
  land in `section="alternatives"` but critique the alts rather than
  propose them.

Sandbox is read-only like other critics. Enforced by `readonly_guarded`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from pcd.agents import make_agent_client
from pcd.issues import alternatives_to_issues, parse_reframer_output
from pcd.roles._guard import private_staging
from pcd.roles.prompts import reframer_prompt


def run_reframer(
    *,
    project_root: Path,
    iteration: int,
    model: Optional[str],
    reasoning_effort: str = "medium",
    agent: str = "codex",
    on_progress: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict], dict, bool]:
    """Run one fresh Reframer session in its private staging dir.

    Returns `(issues, package, contaminated)` — same contamination
    semantics as the other read-only roles.
    """
    def _body(staging_dir: Path):
        with make_agent_client(
            agent,
            cwd=str(staging_dir),
            reasoning_effort=reasoning_effort,
        ) as client:
            thread_id = client.start_thread(
                cwd=str(staging_dir),
                model=model,
                sandbox="read-only",
            )
            return client.run_turn(
                thread_id=thread_id,
                cwd=str(staging_dir),
                model=model,
                prompt=reframer_prompt(),
                on_progress=on_progress,
            )

    result, contaminated = private_staging(
        project_root=project_root,
        iteration=iteration,
        role_name="reframer",
        body=_body,
    )
    hard_constraints, alternatives = parse_reframer_output(result.final_text)
    package = {
        "hard_constraints": hard_constraints,
        "alternatives": alternatives,
    }
    issues = alternatives_to_issues(alternatives, hard_constraints)
    return issues, package, contaminated
