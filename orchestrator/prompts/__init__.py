"""Registry of Agent 3 prompt versions -- used by the eval harness
(orchestrator/evaluation.py) to compare prompt versions. The main
migrate_agents.py pipeline always uses agent3_v1 directly, unaffected by this.
"""
from __future__ import annotations

from types import ModuleType

from . import agent3_v1, agent3_v2

_VERSIONS: dict[str, ModuleType] = {
    agent3_v1.PROMPT_VERSION: agent3_v1,
    agent3_v2.PROMPT_VERSION: agent3_v2,
}


def get_prompt_module(prompt_version: str = "v1") -> ModuleType:
    """Return the orchestrator.prompts.agent3_v<N> module for `prompt_version`,
    defaulting to v1 for anything unrecognized."""
    return _VERSIONS.get(prompt_version, agent3_v1)
