"""Shared login throttling (R3 P2-03 / R3.1 P1 fail-closed).

auth.py keeps its in-memory sliding window as the always-on local layer
(single-instance deployments and the unit tests depend on it). This
module adds a Redis-backed layer so several API instances share one
login-failure count:

* two dimensions: per (ip, username) AND per ip alone, so a distributed
  password spray across many usernames from one source is still caught
* failures are recorded with a Lua script: INCR + EXPIRE execute as one
  atomic unit, so concurrent writers can never leave a counter behind
  without its TTL
* key components (ip / username) are truncated before formatting so an
  oversized hostile input cannot bloat Redis keys
* failure semantics are explicit (R3.1):
    - mode "memory_only": the Redis layer is not consulted at all; the
      local in-process buckets stay authoritative
    - mode "redis_required": when Redis is unreachable the throttle
      reports unavailable and auth.py rejects NEW password logins with
      503 instead of silently degrading to per-instance counters
  Either way the client stands down for 60 s after an error and then
  retries, so a recovered Redis automatically resumes shared counting.

Enabled with QP_LOGIN_THROTTLE_REDIS_URL plus QP_LOGIN_THROTTLE_MODE
(see auth.py); unset URL means memory-only limiting.
"""

from __future__ import annotations

import logging
import time

import redis

logger = logging.getLogger("quantpilot.login_throttle")

_DEGRADED_SECONDS = 60.0
_KEY_PART_MAX = 128

_PAIR_KEY = "qp:login:pair:{ip}|{username}"
_IP_KEY = "qp:login:ip:{ip}"

# Atomic failure recording: both counters increment AND get their window
# TTL refreshed inside one script call - no interleaving writer can
# observe a counter without its expiry (R3.1 P1 #7).
_RECORD_LUA = """
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], ARGV[1])
return 1
"""


class LoginThrottleUnavailable(Exception):
    """redis_required mode + an unreachable Redis: new logins must be
    rejected (503) rather than silently degrading to memory-only
    counting (R3.1 P1)."""


def _part(value: str) -> str:
    """Truncate a key component so hostile oversized inputs cannot
    consume Redis memory through key length (R3.1 P1 #9)."""
    return value[:_KEY_PART_MAX]


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
        self._record_script = self._client.register_script(_RECORD_LUA)
        self._max = max_failures
        self._ip_max = ip_max_failures
        self._window = window_seconds
        self._degraded_until = 0.0

    @property
    def degraded(self) -> bool:
        """True while standing down after a Redis error (skip retries,
        keep latency off the login path)."""
        return time.time() < self._degraded_until

    def _mark_degraded(self, exc: Exception) -> None:
        self._degraded_until = time.time() + _DEGRADED_SECONDS
        logger.warning(
            "login throttle Redis unavailable (%s); standing down %.0fs "
            "(redis_required mode rejects new logins meanwhile)",
            exc,
            _DEGRADED_SECONDS,
        )

    def too_many_attempts(self, username: str, remote_ip: str) -> bool | None:
        """True/False = shared verdict; None = Redis currently unusable
        (connection error or degraded stand-down window)."""
        if self.degraded:
            return None
        try:
            pair = self._client.get(
                _PAIR_KEY.format(ip=_part(remote_ip), username=_part(username.strip().lower()))
            )
            ip = self._client.get(_IP_KEY.format(ip=_part(remote_ip)))
        except Exception as exc:  # noqa: BLE001 - any redis error degrades
            self._mark_degraded(exc)
            return None
        return (pair is not None and int(pair) >= self._max) or (
            ip is not None and int(ip) >= self._ip_max
        )

    def record_failure(self, username: str, remote_ip: str) -> None:
        if self.degraded:
            return
        try:
            self._record_script(
                keys=[
                    _PAIR_KEY.format(ip=_part(remote_ip), username=_part(username.strip().lower())),
                    _IP_KEY.format(ip=_part(remote_ip)),
                ],
                args=[self._window],
            )
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
                _PAIR_KEY.format(ip=_part(remote_ip), username=_part(username.strip().lower()))
            )
        except Exception as exc:  # noqa: BLE001
            self._mark_degraded(exc)
