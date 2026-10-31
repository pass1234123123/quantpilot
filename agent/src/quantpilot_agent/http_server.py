"""Local loopback HTTP server for MT5 EAs (PRD 6.2: 127.0.0.1 + short-term token).

Endpoints (all JSON):
    GET  /healthz              liveness (no token)
    POST /ea/v1/register       account + terminal identity  -> ea.registered
    POST /ea/v1/heartbeat      doc A.3 heartbeat            -> ea.heartbeat
    POST /ea/v1/symbols        symbol spec batch            -> ea.symbols
    POST /ea/v1/positions      open-position snapshot       -> ea.positions
    POST /ea/v1/deals          incremental history deals    -> ea.deals
    POST /ea/v1/orders         pending-order snapshot       -> ea.orders
    POST /ea/v1/calendar       MT5 built-in economic calendar -> ea.calendar
    POST /ea/v1/bars           closed bars per symbol+timeframe  -> ea.bars
                                (one closed-bar batch; cloud upserts
                                idempotently and evaluates strategies)
    POST /ea/v1/config         identity -> cloud lookup -> config response
                               (synchronous; NOT an event; informational for
                               the EA, OBSERVE_ONLY - PRD 21.3)
    POST /ea/v1/commands/poll  identity + recently_acked -> cloud pending
                               commands -> relay to the EA (PRD 2.5: at most
                               one command per account per poll)
    POST /ea/v1/commands/ack   command receipt (OBSERVE_ONLY_ACK, never
                               execution) -> ea.command_ack event

Slice 1 command channel: the EA only ACKs commands, it never executes them
(OBSERVE_ONLY - PRD 21.3). The agent contains no order-execution code of any
kind; it relays opaque command payloads and receipts only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from quantpilot_contracts.constants import (
    EVENT_EA_BARS,
    EVENT_EA_CALENDAR,
    EVENT_EA_COMMAND_ACK,
    EVENT_EA_DEALS,
    EVENT_EA_HEARTBEAT,
    EVENT_EA_ORDERS,
    EVENT_EA_POSITIONS,
    EVENT_EA_REGISTERED,
    EVENT_EA_SHUTDOWN,
    EVENT_EA_SYMBOLS,
)
from quantpilot_contracts.models import (
    EaBarsData,
    EaCalendarData,
    EaCommandAckData,
    EaCommandPollRequest,
    EaDealsData,
    EaHeartbeatData,
    EaOrdersData,
    EaPositionsData,
    EaRegisteredData,
    EaShutdownData,
    EaSymbolsData,
)

from .log import info, warn
from .publisher import make_event_item

MAX_BODY_BYTES = 10 * 1024 * 1024
# Observation fields excluded from spec_hash (they change with every quote,
# they are market data, not trading rules - PRD 5.4 lists only rule fields).
_SPEC_HASH_EXCLUDE = {"bid", "ask", "spread"}


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sanitize(data: dict[str, Any]) -> dict[str, Any]:
    """Publish hashed terminal paths only - the cloud never receives raw
    local filesystem paths (privacy by design; DB stores path hashes)."""
    terminal = data.get("terminal")
    if isinstance(terminal, dict):
        data_path = terminal.get("data_path") or ""
        data["terminal"] = {
            "build": terminal.get("build"),
            "company": terminal.get("company", ""),
            "data_path_hash": _sha256(data_path) if data_path else "",
        }
    return data


class AgentHttpServer:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        event_queue: "queue.Queue[dict[str, Any]]",
        api_base_url: str = "http://127.0.0.1:8000",
        api_key: str = "",
    ) -> None:
        outer = self
        cloud_headers = {"X-API-Key": api_key} if api_key else {}
        outer._cloud = httpx.Client(
            base_url=api_base_url, timeout=httpx.Timeout(5.0), headers=cloud_headers
        )

        class Handler(BaseHTTPRequestHandler):
            server_version = "QuantPilotAgent/0.1"
            protocol_version = "HTTP/1.1"

            def _respond(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _ok(self, extra: dict[str, Any] | None = None) -> None:
                payload: dict[str, Any] = {"ok": True}
                if extra:
                    payload.update(extra)
                self._respond(200, payload)

            def _err(self, status: int, code: str, message: str) -> None:
                self._respond(status, {"ok": False, "error": {"code": code, "message": message}})

            def _authorized(self) -> bool:
                supplied = self.headers.get("X-QP-Token", "")
                return hmac.compare_digest(supplied, outer._token)

            def _drain_request_body(self) -> None:
                """Consume an unread request body so HTTP/1.1 keep-alive
                framing stays intact for the next request on this connection."""
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return
                if length <= 0:
                    return
                if length <= MAX_BODY_BYTES:
                    self.rfile.read(length)
                else:
                    self.close_connection = True  # too big to drain cheaply

            def _read_json(self) -> Any:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length <= 0:
                    return None
                if length > MAX_BODY_BYTES:
                    self.close_connection = True  # refuse without draining
                    return None
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None

            def _handle_config(self, body: dict[str, Any]) -> None:
                """EA identity -> cloud config lookup -> relay to the EA.

                The response is informational for the EA (it only logs it);
                OBSERVE_ONLY, never a trade command (PRD 21.3). The raw
                data_path stays local - only its sha256 goes to the cloud.
                """
                model = EaRegisteredData.model_validate(body)
                resp = outer._cloud.post(
                    "/api/v1/config/lookup",
                    json={
                        "data_path_hash": _sha256(model.terminal.data_path),
                        "login_ref": model.account.login_ref,
                        "server": model.account.server,
                    },
                )
                resp.raise_for_status()
                data = resp.json().get("data") or {}
                info(
                    "http.config_lookup",
                    resolved=data.get("resolved"),
                    version=data.get("version"),
                )
                self._ok(
                    {
                        "resolved": bool(data.get("resolved")),
                        "version": data.get("version") or 0,
                        "source": data.get("source"),
                        "config": data.get("config"),
                    }
                )

            def _handle_command_poll(self, body: dict[str, Any]) -> None:
                """EA identity + recently_acked -> cloud pending -> relay.

                PRD 2.5: the cloud returns at most one QUEUED command per
                account per poll (per-account serial execution). The agent
                relays the payload verbatim; command contents are opaque to
                it (no execution code here - OBSERVE_ONLY, PRD 21.3).
                recently_acked echoes the command_ids the EA already
                processed, letting the cloud reconcile UNKNOWN rows (AT-006)
                even when the original ack event was lost in transit.
                """
                model = EaCommandPollRequest.model_validate(body)
                sanitized = _sanitize(model.model_dump(mode="json"))
                terminal = sanitized.get("terminal") or {}
                account = sanitized.get("account") or {}
                resp = outer._cloud.post(
                    "/api/v1/commands/pending",
                    json={
                        "data_path_hash": terminal.get("data_path_hash", ""),
                        "login_ref": account.get("login_ref", ""),
                        "server": account.get("server", ""),
                        "recently_acked": sanitized.get("recently_acked") or [],
                    },
                )
                resp.raise_for_status()
                data = resp.json().get("data") or {}
                commands = data.get("commands") or []
                info(
                    "http.command_poll",
                    resolved=data.get("resolved"),
                    delivered=len(commands),
                    reconciled=data.get("reconciled", 0),
                )
                self._ok(
                    {
                        "commands": commands,
                        "reconciled": data.get("reconciled", 0),
                        "resolved": bool(data.get("resolved")),
                    }
                )

            def log_message(self, fmt: str, *args: Any) -> None:  # route stdlib logs through structured logger
                info("http.access", line=fmt % args)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/healthz":
                    self._ok()
                    return
                if not self._authorized():
                    self._err(401, "UNAUTHORIZED", "missing or invalid X-QP-Token")
                    return
                self._err(404, "NOT_FOUND", f"no route for GET {self.path}")

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._drain_request_body()
                    self._err(401, "UNAUTHORIZED", "missing or invalid X-QP-Token")
                    return
                route = self.path.rstrip("/")
                body = self._read_json()
                if body is None:
                    self._err(400, "BAD_JSON", "body is not valid JSON")
                    return

                # synchronous config poll: cloud lookup, NOT an event stream item
                if route == "/ea/v1/config":
                    try:
                        self._handle_config(body)
                    except Exception as exc:
                        warn("http.config_lookup_failed", error=str(exc))
                        self._err(502, "CLOUD_LOOKUP_FAILED", str(exc))
                    return

                # synchronous command poll: cloud pending -> relay to the EA
                if route == "/ea/v1/commands/poll":
                    try:
                        self._handle_command_poll(body)
                    except Exception as exc:
                        warn("http.command_poll_failed", error=str(exc))
                        self._err(502, "CLOUD_POLL_FAILED", str(exc))
                    return

                # command receipt -> event stream (at-least-once; the cloud
                # side dedupes by command_id, PRD AT-005)
                if route == "/ea/v1/commands/ack":
                    try:
                        model = EaCommandAckData.model_validate(body)
                    except Exception as exc:
                        warn("http.validation_failed", route=route, error=str(exc))
                        self._err(400, "VALIDATION_FAILED", str(exc))
                        return
                    data = _sanitize(model.model_dump(mode="json"))
                    outer._event_queue.put(make_event_item(EVENT_EA_COMMAND_ACK, data))
                    info("http.enqueued", route=route, event_type=EVENT_EA_COMMAND_ACK)
                    self._ok()
                    return

                try:
                    if route == "/ea/v1/register":
                        data = EaRegisteredData.model_validate(body).model_dump(mode="json")
                        event_type = EVENT_EA_REGISTERED
                    elif route == "/ea/v1/heartbeat":
                        model = EaHeartbeatData.model_validate(body)
                        data = model.model_dump(mode="json")
                        event_type = EVENT_EA_HEARTBEAT
                    elif route == "/ea/v1/symbols":
                        model = EaSymbolsData.model_validate(body)
                        data = model.model_dump(mode="json")
                        _stamp_spec_hashes(data)
                        event_type = EVENT_EA_SYMBOLS
                    elif route == "/ea/v1/positions":
                        data = EaPositionsData.model_validate(body).model_dump(mode="json")
                        event_type = EVENT_EA_POSITIONS
                    elif route == "/ea/v1/deals":
                        data = EaDealsData.model_validate(body).model_dump(mode="json")
                        event_type = EVENT_EA_DEALS
                    elif route == "/ea/v1/orders":
                        data = EaOrdersData.model_validate(body).model_dump(mode="json")
                        event_type = EVENT_EA_ORDERS
                    elif route == "/ea/v1/calendar":
                        data = EaCalendarData.model_validate(body).model_dump(mode="json")
                        event_type = EVENT_EA_CALENDAR
                    elif route == "/ea/v1/bars":
                        data = EaBarsData.model_validate(body).model_dump(mode="json")
                        event_type = EVENT_EA_BARS
                    elif route == "/ea/v1/shutdown":
                        data = EaShutdownData.model_validate(body).model_dump(mode="json")
                        event_type = EVENT_EA_SHUTDOWN
                    else:
                        self._err(404, "NOT_FOUND", f"no route for POST {self.path}")
                        return
                except Exception as exc:
                    warn("http.validation_failed", route=route, error=str(exc))
                    self._err(400, "VALIDATION_FAILED", str(exc))
                    return
                outer._event_queue.put(make_event_item(event_type, _sanitize(data)))
                info("http.enqueued", route=route, event_type=event_type)
                self._ok()

        outer._token = token
        outer._event_queue = event_queue
        outer._httpd = ThreadingHTTPServer((host, port), Handler)

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._cloud.close()


def _stamp_spec_hashes(symbols_event_data: dict[str, Any]) -> None:
    """Compute deterministic spec_hash per symbol (agent-side; EA stays thin)."""
    for sym in symbols_event_data.get("symbols", []):
        hash_input = {k: v for k, v in sym.items() if k not in _SPEC_HASH_EXCLUDE}
        canonical = json.dumps(hash_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        sym["spec_hash"] = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
