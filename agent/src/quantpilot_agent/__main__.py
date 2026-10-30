"""QuantPilot local trading node agent - entrypoint.

python -m quantpilot_agent

Startup order:
  1. load config + .env, log discovered MT5 terminals (read-only)
  2. start JetStream publisher thread (flushes pending events from disk first)
  3. background: cloud node register (upsert by device_id) + 60s node heartbeat
  4. serve loopback HTTP for EAs until Ctrl+C

OBSERVE_ONLY slice: no trade commands, no order execution code (PRD 21.3).
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from .cloud_client import CloudClient
from .config import AgentConfig
from .discovery import discover_terminals
from .http_server import AgentHttpServer
from .log import error, info, warn
from .pending import PendingEvents
from .publisher import NatsPublisher, drain_shutdown_queue, make_event_item  # noqa: F401


def main() -> None:
    config = AgentConfig.from_env()
    info("agent.starting", device_id=config.device_id, version=config.agent_version)

    try:
        terminals = discover_terminals()
    except Exception as exc:
        warn("agent.discovery_failed", error=str(exc))
        terminals = []

    pending = PendingEvents(config.state_dir)
    if len(pending):
        info("agent.pending_loaded", count=len(pending))

    event_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()

    node_id_holder = _NodeIdHolder()
    publisher = NatsPublisher(config.nats_url, pending, event_queue, node_id_holder.get)
    publisher.start()

    registrar = _RegistrarThread(config, node_id_holder)
    registrar.start()

    server = AgentHttpServer(config.host, config.port, config.token, event_queue)
    info("agent.listening", host=config.host, port=config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        info("agent.stopping")
    finally:
        server.shutdown()
        publisher.stop()
        drain_shutdown_queue(event_queue, pending)
        registrar.stop()
        registrar.close()
        info("agent.stopped", pending_left=len(pending), terminals=len(terminals))


class _NodeIdHolder:
    def __init__(self) -> None:
        self._node_id: str | None = None
        self._event = threading.Event()

    def set(self, node_id: str) -> None:
        self._node_id = node_id
        self._event.set()

    def get(self) -> str | None:
        return self._node_id


class _RegistrarThread:
    """Cloud register at startup (retry until success), then 60s heartbeats."""

    HEARTBEAT_INTERVAL = 60
    REGISTER_RETRY = 15

    def __init__(self, config: AgentConfig, holder: _NodeIdHolder) -> None:
        self._config = config
        self._holder = holder
        self._client = CloudClient(config.api_base_url)
        self._running = True
        self._thread = threading.Thread(target=self._run, name="qp-registrar", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._thread.join(timeout=5)

    def close(self) -> None:
        self._client.close()

    def _run(self) -> None:
        node_id: str | None = None
        while self._running and node_id is None:
            try:
                data = self._client.register_node(self._config.device_id, self._config.agent_version)
                node_id = data.get("node_id")
                if node_id:
                    self._holder.set(node_id)
            except Exception as exc:
                warn("registrar.register_failed", error=str(exc), retry_in=self.REGISTER_RETRY)
                time.sleep(self.REGISTER_RETRY)
        while self._running:
            time.sleep(self.HEARTBEAT_INTERVAL)
            if not self._running:
                return
            try:
                self._client.node_heartbeat(self._holder.get() or node_id)  # type: ignore[arg-type]
            except Exception as exc:
                warn("registrar.heartbeat_failed", error=str(exc))
                error("registrar.heartbeat_failed", error=str(exc))


if __name__ == "__main__":
    main()
