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

* reservation lifecycle (R3.3 P1-01, refined in R3.3.1):
    - reserve() atomically spends one attempt slot in both dimensions
      and records it as a PENDING member ("p:{attempt_id}")
    - a FAILED password check COMMITS the reservation: one atomic Lua
      rename "p:{id}" -> "f:{id}" (score preserved, so the window
      still anchors at attempt time). The commit is a REQUIRED step,
      not an optional one: without it a success could not tell an
      in-flight reservation from a completed failure record
    - a SUCCESSFUL login clears the (ip, username) pair bucket's
      FAILED history (an authenticated user is not an attacker; the
      accepted R3.1 semantics) and removes ONLY its own pending
      member. R3.3.1: the old whole-key DEL also destroyed OTHER
      concurrently verifying requests' reservations - a failure that
      completed during another request's success vanished from the
      budget. In-flight pendings of other requests now survive, and
      commit when their own verdict arrives. The pure-IP spray
      bucket only ever loses this attempt's own slot, so every other
      recorded failure stays intact
    - an ABORTED verification (timeout, crash, client gone) releases
      ONLY its own pending member in both dimensions: it is NOT a
      failure
    - a crashed process leaks at most its in-flight pendings, and the
      window trim reclaims those slots once they age out - quota is
      never permanently lost
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
# R3.3.1: pair members carry a state prefix - "p:{attempt_id}" while the
# request is still verifying, "f:{attempt_id}" once the password check
# has FAILED. The ip spray bucket keeps plain attempt ids: nothing ever
# bulk-deletes from it, so it needs no state.
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
redis.call('ZADD', KEYS[1], now, 'p:' .. ARGV[3])
redis.call('ZADD', KEYS[2], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
redis.call('PEXPIRE', KEYS[2], ARGV[2])
return 1
"""

# R3.3.1: a wrong password COMMITS its reservation as a failure record:
# one atomic rename "p:{id}" -> "f:{id}" with the score preserved, so
# the sliding window still anchors at attempt time (identical to the
# local layer's timestamp). Idempotent: re-committing an already
# committed (or removed) reservation is a no-op. The ip spray bucket is
# untouched - its member (plain attempt id) already occupies the budget
# and stays exactly as it is.
#
# KEYS[1] = pair zset
# ARGV[1] = now_ms, ARGV[2] = window_ms, ARGV[3] = attempt_id
_COMMIT_LUA = """
local now = tonumber(ARGV[1])
local cutoff = now - tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
local pending = 'p:' .. ARGV[3]
local failed = 'f:' .. ARGV[3]
local score = redis.call('ZSCORE', KEYS[1], pending)
if score then
  redis.call('ZADD', KEYS[1], score, failed)
  redis.call('ZREM', KEYS[1], pending)
end
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""

# R3.3.1: a correct password clears the pair bucket's FAILED history
# (accepted R3.1 semantics) but must NOT touch other requests'
# in-flight pendings - the old whole-key DEL destroyed reservations of
# requests that were still verifying, so their later failures were
# never recorded (the R3.3.1 acceptance scenario: A succeeds and wipes
# B's pending; B's password then verifies wrong and the budget never
# learns about it). One script, executed serially by Redis: trim, drop
# every "f:*" member, drop ONLY this attempt's own "p:{id}" pending,
# and release this attempt's slot from the ip spray bucket.
#
# KEYS[1] = pair zset, KEYS[2] = ip zset
# ARGV[1] = now_ms, ARGV[2] = window_ms, ARGV[3] = attempt_id
_SETTLE_SUCCESS_LUA = """
local now = tonumber(ARGV[1])
local cutoff = now - tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', cutoff)
local members = redis.call('ZRANGE', KEYS[1], 0, -1)
for i = 1, #members do
  if string.sub(members[i], 1, 2) == 'f:' then
    redis.call('ZREM', KEYS[1], members[i])
  end
end
redis.call('ZREM', KEYS[1], 'p:' .. ARGV[3])
redis.call('ZREM', KEYS[2], ARGV[3])
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
        self._commit_script = self._client.register_script(_COMMIT_LUA)
        self._settle_success_script = self._client.register_script(_SETTLE_SUCCESS_LUA)
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

    def commit_failure(self, username: str, remote_ip: str, attempt_id: str) -> None:
        """A wrong password: this attempt's PENDING reservation becomes
        a FAILED record ("p:{id}" -> "f:{id}", score preserved, one
        atomic script) - R3.3.1.

        Unlike settle_success / release this RAISES
        LoginThrottleUnavailable when Redis cannot record the failure:
        a lost failure record under-counts the budget, which is the
        attacker-favourable direction, so redis_required mode must
        fail CLOSED (503 in app.py) instead of silently dropping the
        record. Success/release bookkeeping may stay best-effort
        because losing THOSE only over-counts."""
        if self.degraded:
            raise LoginThrottleUnavailable(
                "redis_required: cannot record the failed login - "
                "Redis is unreachable (mid-verification outage)"
            )
        pair_key, _ = self._keys(username, remote_ip)
        try:
            self._commit_script(
                keys=[pair_key],
                args=[int(time.time() * 1000), self._window * 1000, attempt_id],
            )
        except Exception as exc:  # noqa: BLE001 - must NOT be swallowed
            self._mark_degraded(exc)
            raise LoginThrottleUnavailable(
                f"redis_required: recording the failed login failed: {exc}"
            ) from exc

    def settle_success(self, username: str, remote_ip: str, attempt_id: str) -> None:
        """A correct password: the (ip, username) pair bucket's FAILED
        history is cleared (authenticated, not an attacker; accepted
        R3.1 semantics) and ONLY this attempt's own pending member +
        spray slot are removed - every other in-flight pending and
        every other recorded failure stays (R3.3.1: the previous
        whole-key DEL also destroyed concurrent requests' pendings, so
        their failures were silently dropped; a success must not wipe
        the spray statistics either)."""
        if self.degraded:
            # best effort: the own pending ages out via the window trim;
            # losing success-side bookkeeping only OVER-counts (safe)
            return
        pair_key, ip_key = self._keys(username, remote_ip)
        try:
            self._settle_success_script(
                keys=[pair_key, ip_key],
                args=[int(time.time() * 1000), self._window * 1000, attempt_id],
            )
        except Exception as exc:  # noqa: BLE001
            self._mark_degraded(exc)

    def release(self, username: str, remote_ip: str, attempt_id: str) -> None:
        """An aborted verification (timeout / crash / client gone) is
        NOT a failure: this attempt's own pending member is returned in
        both dimensions (R3.3.1: the pair member is "p:{id}", so an
        abort can never remove another request's committed failure
        record either). Best effort - if Redis is down the slot simply
        ages out through the sliding-window trim, so quota is never
        permanently lost (R3.3 P1-01 leak handling)."""
        if self.degraded:
            return
        pair_key, ip_key = self._keys(username, remote_ip)
        try:
            pipe = self._client.pipeline()
            pipe.zrem(pair_key, f"p:{attempt_id}")
            pipe.zrem(ip_key, attempt_id)
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            self._mark_degraded(exc)
