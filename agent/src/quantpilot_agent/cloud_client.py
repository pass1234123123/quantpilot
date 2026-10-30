"""HTTP client for the cloud API (register/heartbeat, PRD 13.2)."""

from __future__ import annotations

import platform

import httpx

from .log import info, warn


class CloudClient:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(5.0))

    def close(self) -> None:
        self._client.close()

    def register_node(self, device_id: str, agent_version: str) -> dict:
        resp = self._client.post(
            "/api/v1/nodes/register",
            json={
                "device_id": device_id,
                "hostname": platform.node() or "unknown-host",
                "os_info": f"{platform.system()} {platform.release()}",
                "agent_version": agent_version,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or {}
        info("cloud.node_registered", node_id=data.get("node_id"), status=data.get("status"))
        return data

    def node_heartbeat(self, node_id: str) -> None:
        resp = self._client.post("/api/v1/nodes/heartbeat", json={"node_id": node_id})
        resp.raise_for_status()
