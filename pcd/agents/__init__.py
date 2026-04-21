"""Agent-client layer.

Each agent ("codex", "claude") is a subprocess driver speaking its own
protocol (codex uses an app-server with JSON-RPC; claude is one-shot
per invocation with stream-json output). Both drivers expose the same
method surface — `start / close / start_thread / resume_thread /
run_turn` — so higher layers just call `make_agent_client(name, ...)`
and treat the return value as a context manager.
"""
from __future__ import annotations

from typing import Union

from pcd.agents.claude import ClaudeClient
from pcd.agents.codex import CodexClient, TurnResult


AgentClient = Union[CodexClient, ClaudeClient]
SUPPORTED_AGENTS: tuple[str, ...] = ("codex", "claude")
DEFAULT_AGENT = "codex"

__all__ = [
    "AgentClient",
    "ClaudeClient",
    "CodexClient",
    "DEFAULT_AGENT",
    "SUPPORTED_AGENTS",
    "TurnResult",
    "make_agent_client",
    "normalize_agent",
]


def normalize_agent(agent: str | None) -> str:
    """Return a canonical agent name, defaulting to codex. Raises on unknown."""
    a = (agent or DEFAULT_AGENT).lower()
    if a not in SUPPORTED_AGENTS:
        raise ValueError(
            f"unsupported agent {agent!r}; expected one of {SUPPORTED_AGENTS}"
        )
    return a


def make_agent_client(
    agent: str | None,
    *,
    cwd: str,
    reasoning_effort: str = "medium",
    timeout_seconds: int = 1800,
) -> AgentClient:
    """Construct the client for `agent`. Use it as a context manager."""
    name = normalize_agent(agent)
    if name == "codex":
        return CodexClient(
            cwd=cwd,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
        )
    return ClaudeClient(
        cwd=cwd,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
    )
