"""Private staging dir for read-only role calls.

Codex gives critics and the Judge `sandbox="read-only"`, which
structurally prevents them from writing anywhere. Claude has no
equivalent — we launch it with `--dangerously-skip-permissions` so
Write / Edit are live tools.

Rather than relying on post-hoc "detect and rollback" on the shared
design.md, we give each read-only role its OWN staging directory:

    .pcd/rounds/iter_NNN/<role_name>/
        design.md           # COPY of current shared design.md
        initial_prompt.txt  # SYMLINK to shared initial_prompt.txt

The agent is launched with cwd set to the staging dir. From inside
that subprocess, `./design.md` IS its private copy — writes to it
don't touch the shared file, reads to it aren't polluted by peer
critics' writes (because each critic has its OWN staging dir). The
shared design.md is not in any critic's cwd, so the only way a
rogue agent could touch it is to navigate by absolute or relative
path — which `codex` blocks structurally (read-only sandbox) and
`claude` allows. For the claude case, the shared design.md is
already guarded by an orchestrator-level hash check on every round
(it's the proposer's responsibility to change it, nobody else's).

After `body` returns, we hash the staging copy. If it differs from
the seed bytes, the agent wrote to its own design.md — that's a C11
violation. We return `contaminated=True` and the caller should
discard the agent's output.

This is pre-hoc prevention (nothing shared is writable) plus a
per-role consistency check — stronger than the previous post-hoc
detect/rollback on a single shared file.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, TypeVar


T = TypeVar("T")

STAGING_DESIGN_FILENAME = "design.md"
STAGING_PROMPT_FILENAME = "initial_prompt.txt"


def private_staging(
    *,
    project_root: Path,
    iteration: int,
    role_name: str,
    body: Callable[[Path], T],
) -> tuple[T, bool]:
    """Run `body(staging_dir)` inside a per-role staging directory.

    `role_name` is used as the leaf directory name (e.g. "critic_design",
    "judge", "reframer", "exploration"). `body` is called with the
    absolute path of the staging dir and is expected to launch its
    agent with `cwd=staging_dir`.

    Returns `(body_result, contaminated)` where contaminated=True
    means the agent wrote to its private design.md (C11 violation).
    """
    meta_dir = project_root / ".pcd"
    shared_design = project_root / "design.md"
    shared_prompt = meta_dir / "initial_prompt.txt"
    rounds_dir = meta_dir / "rounds" / f"iter_{iteration:03d}"
    staging_dir = rounds_dir / role_name
    staging_dir.mkdir(parents=True, exist_ok=True)

    # Seed: copy design.md, symlink initial_prompt.txt.
    staging_design = staging_dir / STAGING_DESIGN_FILENAME
    seed_bytes = (
        shared_design.read_bytes() if shared_design.exists() else b""
    )
    staging_design.write_bytes(seed_bytes)

    staging_prompt = staging_dir / STAGING_PROMPT_FILENAME
    if staging_prompt.is_symlink() or staging_prompt.exists():
        try:
            staging_prompt.unlink()
        except FileNotFoundError:
            pass
    if shared_prompt.exists():
        # Relative symlink keeps the project dir movable; os.path.relpath
        # from the staging dir (not from cwd).
        rel_target = os.path.relpath(shared_prompt, staging_dir)
        os.symlink(rel_target, staging_prompt)

    value = body(staging_dir)

    # C11 check: did the agent modify its private design.md?
    contaminated = False
    if staging_design.exists():
        after = staging_design.read_bytes()
        if after != seed_bytes:
            contaminated = True
    else:
        # Private design.md got deleted — treat as contamination.
        contaminated = True

    return value, contaminated
