"""JetStream durable pull consumer: node.*.events -> PostgreSQL.

Runs in a dedicated thread with its own asyncio loop. Durable name is stable
so redeliveries resume where the previous process stopped. Every message is
acked only after its DB write succeeds; handlers are idempotent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

import nats
import nats.errors

from quantpilot_contracts.constants import (
    NATS_DURABLE_API,
    NATS_EVENTS_SUBJECT_WILDCARD,
    NATS_STREAM_NODE_EVENTS,
)

from . import config, writers

logger = logging.getLogger("quantpilot.consumer")


class NodeEventsConsumer:
    def __init__(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, name="qp-events-consumer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._thread.join(timeout=10)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _main(self) -> None:
        while self._running:
            try:
                nc = await nats.connect(
                    servers=[config.nats_url()],
                    connect_timeout=5,
                    ping_interval=20,
                    max_outstanding_pings=3,
                    allow_reconnect=True,
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=2,
                )
                js = nc.jetstream()
                sub = await js.pull_subscribe(
                    NATS_EVENTS_SUBJECT_WILDCARD,
                    durable=NATS_DURABLE_API,
                    stream=NATS_STREAM_NODE_EVENTS,
                )
                logger.info("consumer subscribed (durable=%s)", NATS_DURABLE_API)
                while self._running:
                    try:
                        msgs = await sub.fetch(batch=10, timeout=1.0)
                    except nats.errors.TimeoutError:
                        continue
                    except nats.errors.ConnectionClosedError:
                        break
                    for msg in msgs:
                        self._process(msg)
                        await msg.ack()
                await nc.close() if nc.is_connected else None
            except Exception as exc:
                logger.warning("consumer error: %s; reconnecting in 10s", exc)
                await asyncio.sleep(10)

    def _process(self, msg: Any) -> None:
        try:
            envelope = json.loads(msg.data.decode("utf-8"))
        except Exception as exc:
            logger.error("bad event payload (acking, dropping): %s", exc)
            return
        event_type = envelope.get("event_type", "")
        data = dict(envelope.get("data") or {})
        node_id = envelope.get("node_id", "")
        data["node_id"] = data.get("node_id") or node_id
        handler = writers.HANDLERS.get(event_type)
        if handler is None:
            logger.warning("no handler for event_type=%s (acking, dropping)", event_type)
            return
        try:
            handler(data)
            touch = envelope.get("occurred_at")
            logger.info("consumed %s event_id=%s at=%s", event_type, envelope.get("event_id"), touch)
        except Exception as exc:
            # handler failure => do not ack; JetStream will redeliver
            logger.exception("handler failed for %s: %s", event_type, exc)
            raise
