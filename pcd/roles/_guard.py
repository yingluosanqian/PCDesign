"""Snapshot / compare / rollback guard for read-only role calls.

Codex gives critics and the Judge `sandbox="read-only"`, which structurally
prevents them from writing anywhere. Claude has no equivalent — we launch
it with `--dangerously-skip-permissions` so Write / Edit are live tools —
so we enforce read-only by snapshotting design.md before the call, and
reverting it on mismatch afterwards.

The guard is applied universally (codex + claude). For codex it's
essentially a no-op assertion — design.md should never change under
`read-only` — but it also defends against accidental sandbox regressions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, TypeVar


T = TypeVar("T")


def readonly_guarded(
    project_root: Path, body: Callable[[], T]
) -> tuple[T, bool]:
    """Run `body()` with a design.md snapshot guard.

    If design.md changes during the call, restore it from the snapshot
    and return `contaminated=True`. The caller is expected to promote
    that to a degraded-round marker.
    """
    design_path = project_root / "design.md"
    snapshot: bytes | None = (
        design_path.read_bytes() if design_path.exists() else None
    )
    value = body()
    contaminated = False
    if snapshot is not None:
        after = design_path.read_bytes() if design_path.exists() else b""
        if after != snapshot:
            design_path.write_bytes(snapshot)
            contaminated = True
    return value, contaminated
