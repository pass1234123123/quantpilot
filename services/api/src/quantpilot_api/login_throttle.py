"""Shared login throttling (R3 P2-03).

auth.py keeps its in-memory sliding window as the always-on local layer
(single-instance deployments and the unit tests depend on it). This
module adds an OPTIONAL Redis-backed layer so several API instances
share one login-failure count:

* two dimensions: per (ip, username) AND per ip alone, so a distributed
  password spray across many usernames from one source is still caught
* INCR + EXPIRE: every failure refreshes the window TTL, so counters
  survive API restarts and never reset because a process went down
* fail-safe degradation: when Redis is unreachable the layer logs a
  warning, stands down for 60 s and the in-memory buckets stay
  authoritative - no crash, no silent pass-through

Enabled with QP_REDIS_URL (e.g. redis://127.0.0.1:6379/0); unset means
memory-only limiting, unchanged from before.
"""

from __future__ import annotations

import logging
import time

import redis

logger = logging.getLogger("quantpilot.login_throttle")

_DEGRADED_SECONDS = 60.0

_PAIR_KEY = "qp:login:pair:{ip}|{username}"
_IP_KEY = "qp:login:ip:{ip}"


class RedisLoginThrottle:
    """Sliding-window login failure counters shared across instances."""

    def __init__(
        self,
        url: str,
        max_failures: int,
        ip_max_failures: int,
        window_seconds: int,
    ) -> None:
        self.url = url
        self._client = redis.Redis.from_url(
            url, socket_connect_timeout=2, socket_timeout=2,
            decode_responses=True,
        )
        self._max = max_failures
        self._ip_max = ip_max_failures
        self._window = window_seconds
        self._degraded_until = 0.0

    @property
    def degraded(self) -> bool:
        """True while standing down after a Redis error (skip retries,
        keep latency off the login path; memory buckets stay live)."""
        return time.time() < self._degraded_until

    def _mark_degraded(self, exc: Exception) -> None:
        self._degraded_until = time.time() + _DEGRADED_SECONDS
        logger.warning(
            "login throttle Redis unavailable (%s); memory-only limiting "
            "for %.0fs",
            exc,
            _DEGRADED_SECONDS,
        )

    def too_many_attempts(self, username: str, remote_ip: str) -> bool:
        if self.degraded:
            return False
        try:
            pair = self._client.get(_PAIR_KEY.format(ip=remote_ip, username=username.strip().lower()))
            ip = self._client.get(_IP_KEY.format(ip=remote_ip))
        except Exception as exc:  # noqa: BLE001 - any redis error degrades
            self._mark_degraded(exc)
            return False
        return (pair is not None and int(pair) >= self._max) or (
            ip is not None and int(ip) >= self._ip_max
        )

    def record_failure(self, username: str, remote_ip: str) -> None:
        if self.degraded:
            return
        pair_key = _PAIR_KEY.format(ip=remote_ip, username=username.strip().lower())
        ip_key = _IP_KEY.format(ip=remote_ip)
        try:
            pipe = self._client.pipeline(transaction=False)
            pipe.incr(pair_key)
            pipe.expire(pair_key, self._window)
            pipe.incr(ip_key)
            pipe.expire(ip_key, self._window)
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            self._mark_degraded(exc)

    def clear_failures(self, username: str, remote_ip: str) -> None:
        """A successful login clears the (ip, username) pair counter. The
        ip-only counter is deliberately kept: it tracks spray patterns
        across usernames and ages out via the sliding window."""
        if self.degraded:
            return
        try:
            self._client.delete(
                _PAIR_KEY.format(ip=remote_ip, username=username.strip().lower())
            )
        except Exception as exc:  # noqa: BLE001
            self._mark_degraded(exc)
