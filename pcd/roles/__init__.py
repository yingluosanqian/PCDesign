"""High-level role drivers: Proposer (stateful) + Critics + Judge (ephemeral).

Each role function picks up its agent-client via `pcd.agents.make_agent_client`
and speaks the shared start_thread / run_turn surface, so the same role can
run on either codex or claude by passing `agent="codex"` or `agent="claude"`.
"""
from pcd.roles.critic import CRITIC_ROLES, SECTION_BY_ROLE, run_critic
from pcd.roles.exploration import run_exploration_critic
from pcd.roles.judge import run_judge
from pcd.roles.proposer import run_proposer_create, run_proposer_revise
from pcd.roles.reframer import run_reframer

__all__ = [
    "CRITIC_ROLES",
    "SECTION_BY_ROLE",
    "run_critic",
    "run_exploration_critic",
    "run_judge",
    "run_proposer_create",
    "run_proposer_revise",
    "run_reframer",
]
