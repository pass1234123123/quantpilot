"""Command sweeper: time-based EXPIRED / UNKNOWN transitions (AT-006, PRD 13.5).

Runs as a background thread inside the API process, every INTERVAL seconds:

  QUEUED    + expires_at passed          -> EXPIRED (never delivered in time)
  DELIVERED + expires_at passed + grace  -> UNKNOWN (delivered but never
      acked). UNKNOWN commands are NEVER resent (PRD 5.5); reconciliation
      happens later via a late ea.command_ack event or the EA's
      recently-acked echo on the next poll (both flip UNKNOWN -> ACKED).

Every transition is written to system_events (AT-011 audit trail).
"""

from __future__ import annotations

import logging
import threading
import time

from . import db, writers

logger = logging.getLogger("quantpilot.sweeper")

INTERVAL_SECONDS = 10
# how long a DELIVERED command may still receive its ack before it is
# declared UNKNOWN; generous vs. JetStream latency, shorter than any sane
# strategy cycle (doc 22.2 M15/H1 baseline)
UNKNOWN_GRACE_SECONDS = 15


class CommandSweeper:
    def __init__(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, name="qp-command-sweeper", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._thread.join(timeout=10)

    def _run(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception as exc:  # never die: a broken tick retries next round
                logger.warning("sweeper tick failed: %s", exc)
            time.sleep(INTERVAL_SECONDS)

    def _tick(self) -> None:
        with db.conn() as connection:
            expired = connection.execute(
                """
                UPDATE commands
                   SET status = 'EXPIRED', expired_at = now(),
                       updated_at = now(), version = version + 1
                 WHERE status = 'QUEUED' AND expires_at < now()
                RETURNING command_id::text, account_id::text
                """
            ).fetchall()
            unknown = connection.execute(
                """
                UPDATE commands
                   SET status = 'UNKNOWN', unknown_at = now(),
                       updated_at = now(), version = version + 1
                 WHERE status = 'DELIVERED'
                   AND expires_at < now() - make_interval(secs => %s)
                RETURNING command_id::text, account_id::text
                """,
                (UNKNOWN_GRACE_SECONDS,),
            ).fetchall()
        for row in expired:
            writers.add_system_event(
                source="api-sweeper",
                event_type="command.expired",
                severity="INFO",
                payload={
                    "command_id": row[0],
                    "account_id": row[1],
                    "detail": "expired before delivery; never sent to the EA",
                },
            )
        for row in unknown:
            writers.add_system_event(
                source="api-sweeper",
                event_type="command.unknown",
                severity="WARN",
                payload={
                    "command_id": row[0],
                    "account_id": row[1],
                    "detail": "delivered but not acked before expiry; will NOT be"
                              " resent - awaits reconciliation (late ack or"
                              " recently-acked echo) or manual review (AT-006)",
                },
            )
        if expired or unknown:
            logger.info("sweeper: %d expired, %d unknown", len(expired), len(unknown))
