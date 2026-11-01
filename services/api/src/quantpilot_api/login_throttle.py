"""Shared login throttling (R3 P2-03 / R3.1 P1 / R3.3 P1-01 + P2-01).

auth.py keeps its in-memory sliding window as the always-on local layer
(single-instance deployments and the unit tests depend on it). This
module adds the Redis-backed layer so several API instances share ONE
login-attempt budget:

* two dimensions: per (ip, username) AND per ip alone, so a distributed
  password spray across many usernames from one source is still caught

* R3.3 P1-01 (concurrency race): the R3.2 design checked the counters
  first and only incremented them AFTER the (slow) password check - N
  concurrent requests all read "under the limit" and all proceeded,
  which is how the acceptance measured 17 password verifications past a
  limit of 10. The limit check and the slot spend are now ONE atomic
  Lua call: window trim, ZCARD check and ZADD reservation execute
  together inside Redis, so a concurrent attempt can never squeeze
  under a limit that has already been reached.

* R3.3 P2-01 (window semantics): each attempt is a ZSET member scored
  with its own timestamp and evicted by ZREMRANGEBYSCORE once it leaves
  the window - a true sliding window identical to the local deque
  layer. The R3.1 INCR+EXPIRE design refreshed the WHOLE key's TTL on
  every failure, which meant "no failures for 15 minutes" rather than
  "the last 15 minutes"; the two layers disagreed and Redis-mode users
  could accumulate failures indefinitely.

* reservation lifecycle (R3.3 P1-01):
    - reserve() atomically spends one attempt slot in both dimensions
    - a FAILED password check keeps the reservation: it IS the failure
      record, in both layers - there is no separate increment step
      left to race with
    - a SUCCESSFUL login clears the whole (ip, username) pair bucket
      (an authenticated user is not an attacker; R3.1 semantics) and
      releases ONLY its own slot from the pure-IP spray bucket, which
      keeps every other recorded failure intact
    - an ABORTED verification (timeout, crash, client gone) releases
      the reservation from both dimensions: it is NOT a failure
    - a crashed process leaks at most its in-flight reservations, and
      the window trim reclaims those slots once they age out - quota
      is never permanently lost
    - a request refused with 429 reserves nothing, so refusals can
      never extend the lockout

* key components (ip / username) are truncated before formatting so an
  oversized hostile input cannot bloat Redis keys
* failure semantics are explicit (R3.1, unchanged):
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

# R3.3 P1-01: window trim + limit check + slot spend in ONE script.
# Two concurrent callers may both arrive when 9 attempts are recorded,
# but Redis executes scripts serially: the second caller re-runs ZCARD
# after the first caller's ZADD and observes 10 -> blocked. No
# check-then-act gap remains anywhere.
#
# R3.3 P2-01: the ZSET members are per-attempt ids scored with their
# own timestamp; ZREMRANGEBYSCORE evicts exactly the attempts that left
# the window, so the shared verdict is the same true sliding window the
# local layer computes - failures age out one by one instead of the
# whole bucket's TTL resetting on every new failure.
#
# KEYS[1] = pair zset, KEYS[2] = ip zset
# ARGV[1] = now_ms, ARGV[2] = window_ms, ARGV[3] = attempt_id,
# ARGV[4] = pair_max, ARGV[5] = ip_max
_RESERVE_LUA = """
local now = tonumber(ARGV[1])
local cutoff = now - tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', cutoff)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then
  return 0
end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[5]) then
  return 0
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('ZADD', KEYS[2], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
redis.call('PEXPIRE', KEYS[2], ARGV[2])
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
    """Sliding-window login attempt budget shared across instances."""

    def __init__(
        self,
        url: str,
        max_failures: int,
        ip_max_failures: int,
        window_seconds: int,
    ) -> None:
        self.url = url
        # protocol=2 (RESP2): redis-py >= 6 defaults to the RESP3 handshake
        # (HELLO 3), which real Redis 5.x servers reject with
        # "unknown command 'HELLO'" - every ping would then fail and
        # redis_required mode would answer 503 forever. RESP2 is supported
        # by every Redis from 2.x on, so this is the portable choice.
        self._client = redis.Redis.from_url(
            url, socket_connect_timeout=2, socket_timeout=2,
            decode_responses=True, protocol=2,
        )
        self._reserve_script = self._client.register_script(_RESERVE_LUA)
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

    def _keys(self, username: str, remote_ip: str) -> tuple[str, str]:
        return (
            _PAIR_KEY.format(
                ip=_part(remote_ip), username=_part(username.strip().lower())
            ),
            _IP_KEY.format(ip=_part(remote_ip)),
        )

    def reserve(self, username: str, remote_ip: str, attempt_id: str) -> bool | None:
        """Atomically check both limits and spend one attempt slot.

        True  = slot spent (the caller owes a settle/release)
        False = a limit is already reached - nothing was written
        None  = Redis currently unusable (connection error or the
                degraded stand-down window); the caller decides what
                None means per mode (redis_required -> 503)
        """
        if self.degraded:
            return None
        pair_key, ip_key = self._keys(username, remote_ip)
        try:
            verdict = self._reserve_script(
                keys=[pair_key, ip_key],
                args=[
                    int(time.time() * 1000),
                    self._window * 1000,
                    attempt_id,
                    self._max,
                    self._ip_max,
                ],
            )
        except Exception as exc:  # noqa: BLE001 - any redis error degrades
            self._mark_degraded(exc)
            return None
        return bool(int(verdict))

    def settle_success(self, username: str, remote_ip: str, attempt_id: str) -> None:
        """A correct password: the (ip, username) pair bucket is
        cleared whole (authenticated, not an attacker) and ONLY this
        attempt's slot is released from the pure-IP spray bucket -
        every other recorded failure stays (R3.3 P1-01: a success must
        not wipe the spray statistics)."""
        if self.degraded:
            # the slot ages out via the window trim; no permanent loss
            return
        pair_key, ip_key = self._keys(username, remote_ip)
        try:
            pipe = self._client.pipeline()
            pipe.delete(pair_key)
            pipe.zrem(ip_key, attempt_id)
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            self._mark_degraded(exc)

    def release(self, username: str, remote_ip: str, attempt_id: str) -> None:
        """An aborted verification (timeout / crash / client gone) is
        NOT a failure: this attempt's slot is returned in both
        dimensions. Best effort - if Redis is down the slot simply ages
        out through the sliding-window trim, so quota is never
        permanently lost (R3.3 P1-01 leak handling)."""
        if self.degraded:
            return
        pair_key, ip_key = self._keys(username, remote_ip)
        try:
            pipe = self._client.pipeline()
            pipe.zrem(pair_key, attempt_id)
            pipe.zrem(ip_key, attempt_id)
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            self._mark_degraded(exc)
