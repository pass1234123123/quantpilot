"""Agent configuration from environment / .env (doc 17.2: no secrets in code)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from quantpilot_contracts.envfile import load_env_file

_REPO_ENV = Path(os.environ.get("QP_ENV_FILE", ".env"))
load_env_file(_REPO_ENV)


@dataclass(frozen=True)
class AgentConfig:
    host: str
    port: int
    token: str
    device_id: str
    api_base_url: str
    nats_url: str
    agent_version: str
    state_dir: Path

    @classmethod
    def from_env(cls) -> "AgentConfig":
        token = os.environ.get("QP_AGENT_TOKEN", "")
        if not token:
            raise RuntimeError("QP_AGENT_TOKEN is required (see .env.example)")
        return cls(
            host=os.environ.get("QP_AGENT_HOST", "127.0.0.1"),
            port=int(os.environ.get("QP_AGENT_PORT", "8787")),
            token=token,
            device_id=os.environ["QP_DEVICE_ID"],
            api_base_url=os.environ.get("QP_API_BASE_URL", "http://127.0.0.1:8000"),
            nats_url=os.environ["QP_NATS_URL"],
            agent_version=os.environ.get("QP_AGENT_VERSION", "0.1.0"),
            state_dir=Path(os.environ.get("QP_STATE_DIR", ".state/agent")),
        )
