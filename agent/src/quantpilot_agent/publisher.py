"""JetStream publisher thread.

- Owns a dedicated asyncio loop (HTTP handler threads are sync).
- Stamps node_id at publish time; flushes disk-persisted pending events first.
- Uses Nats-Msg-Id = event_id for JetStream server-side dedupe (duplicates
  window), so at-least-once publish becomes effectively-once per event_id.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from datetime import datetime
from typing import Any, Callable

import nats
from nats.js.api import RetentionPolicy, StorageType, StreamConfig

from quantpilot_contracts.constants import (
    NATS_EVENTS_SUBJECT_TMPL,
    NATS_EVENTS_SUBJECT_WILDCARD,
    NATS_STREAM_NODE_EVENTS,
)

from .log import info, warn
from .pending import PendingEvents

MAX_PUBLISH_BATCH = 200


class NatsPublisher:
    def __init__(
        self,
        nats_url: str,
        pending: PendingEvents,
        event_queue: "queue.Queue[dict[str, Any]]",
        get_node_id: Callable[[], str | None],
    ) -> None:
        self._nats_url = nats_url
        self._pending = pending
        self._queue = event_queue
        self._get_node_id = get_node_id
        self._running = True
        self._thread = threading.Thread(target=self._run, name="qp-nats-publisher", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._thread.join(timeout=10)

    # ------------------------------------------------------------------ thread
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
        nc = None
        js = None
        while self._running:
            node_id = self._get_node_id()
            if node_id is None:
                await asyncio.sleep(1.0)
                continue
            if nc is None or not nc.is_connected:
                try:
                    nc, js = await self._connect()
                except Exception as exc:
                    warn("publisher.connect_failed", error=str(exc))
                    await asyncio.sleep(10)
                    continue
            try:
                await self._flush_pending(js, node_id)
                await self._drain_queue(js, node_id)
            except Exception as exc:
                warn("publisher.cycle_error", error=str(exc))
                await asyncio.sleep(5)
            await asyncio.sleep(0.25)

    async def _connect(self) -> tuple[Any, Any]:
        info("publisher.connecting", url=self._safe_url())
        nc = await nats.connect(
            servers=[self._nats_url],
            connect_timeout=5,
            ping_interval=20,
            max_outstanding_pings=3,
            allow_reconnect=True,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        js = nc.jetstream()
        cfg = StreamConfig(
            name=NATS_STREAM_NODE_EVENTS,
            subjects=[NATS_EVENTS_SUBJECT_WILDCARD],
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            max_msgs=500_000,
            max_age=7 * 24 * 3600,
            duplicate_window=120,  # Nats-Msg-Id dedupe horizon (seconds)
        )
        try:
            await js.add_stream(cfg)
            info("publisher.stream_created", stream=NATS_STREAM_NODE_EVENTS)
        except Exception:
            await js.update_stream(cfg)
            info("publisher.stream_updated", stream=NATS_STREAM_NODE_EVENTS)
        info("publisher.connected")
        return nc, js

    def _safe_url(self) -> str:
        try:
            proto, rest = self._nats_url.split("://", 1)
            creds = rest.split("@", 1)[0]
            masked = creds.split(":", 1)[0] + ":***"
            return f"{proto}://{masked}@{rest.split('@', 1)[1]}"
        except Exception:
            return "nats://***"

    async def _flush_pending(self, js: Any, node_id: str) -> None:
        for item in self._pending.snapshot()[:MAX_PUBLISH_BATCH]:
            if not self._running:
                return
            ok = await self._publish_item(js, node_id, item)
            if ok:
                self._pending.remove(item["event_id"])
            else:
                break  # connection issue; retry next cycle

    async def _drain_queue(self, js: Any, node_id: str) -> None:
        while self._running:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            ok = await self._publish_item(js, node_id, item)
            if not ok:
                self._pending.add(item)
                # keep draining into pending so EA requests never block
                while True:
                    try:
                        self._pending.add(self._queue.get_nowait())
                    except queue.Empty:
                        return

    async def _publish_item(self, js: Any, node_id: str, item: dict[str, Any]) -> bool:
        envelope = {
            "event_id": item["event_id"],
            "event_type": item["event_type"],
            "schema_version": item.get("schema_version", "1.0"),
            "node_id": node_id,
            "occurred_at": item["occurred_at"],
            "data": item["data"],
        }
        payload = json.dumps(envelope, ensure_ascii=False, default=str).encode("utf-8")
        subject = NATS_EVENTS_SUBJECT_TMPL.format(node_id=node_id)
        try:
            ack = await js.publish(
                subject,
                payload,
                headers={"Nats-Msg-Id": item["event_id"]},
                timeout=5,
            )
            info("publisher.published", event_id=item["event_id"], event_type=item["event_type"], stream_seq=getattr(ack, "seq", None))
            return True
        except Exception as exc:
            warn("publisher.publish_failed", event_id=item["event_id"], error=str(exc))
            return False

    # ------------------------------------------------------------- diagnostics
    @staticmethod
    def drain_to_pending(event_queue: "queue.Queue[dict[str, Any]]", pending: PendingEvents) -> int:
        """On shutdown: move never-published events into the persistent queue."""
        moved = 0
        while True:
            try:
                pending.add(event_queue.get_nowait())
                moved += 1
            except queue.Empty:
                return moved


def make_event_item(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    import uuid

    from quantpilot_contracts.constants import SCHEMA_VERSION

    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "schema_version": SCHEMA_VERSION,
        "occurred_at": datetime.now().astimezone().isoformat(),
        "data": data,
    }


def drain_shutdown_queue(event_queue: "queue.Queue[dict[str, Any]]", pending: PendingEvents) -> None:
    moved = NatsPublisher.drain_to_pending(event_queue, pending)
    if moved:
        info("publisher.shutdown_drained", moved=moved)
