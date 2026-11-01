"""User authentication + RBAC (phase-3 R2).

Layered ON TOP of the unchanged service-key gate in app.py (fail-closed:
X-API-Key == QP_API_KEY keeps authenticating the machine channel). This
module adds per-identity credentials for humans:

* login          username + bcrypt password -> stateless HS256 JWT
                 (stdlib only - no new dependency), default TTL 8h
* roles          ADMIN / TRADER / ANALYST / VIEWER -> authorize() deny codes
* user API keys  qpk_... tokens, sha256-hashed at rest, revocable,
                 last-used tracking; resolved only after the service key
* account grants users reach /api/v1/accounts/{uuid}/... only when granted
                 (checked per request, so revoking a grant is immediate)

JWTs are NOT trusted on their own: every request re-validates the user
row (still active, current role, matching token_version), so disabling a
user, changing a role or resetting a password kills live tokens at once
(R2 acceptance P2-01/P2-02). The shared service key is a MACHINE lane
only (R2 acceptance P1-01): it reaches agent/EA routes and nothing else.
Passwords are bcrypt cost 12 and login is timing-safe against user
enumeration (unknown usernames burn one dummy check) and rate-limited
per IP + username.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass

import bcrypt

from . import db
from .login_throttle import LoginThrottleUnavailable, RedisLoginThrottle

logger = logging.getLogger("quantpilot.auth")

ROLES = ("ADMIN", "TRADER", "ANALYST", "VIEWER")
USER_KEY_PREFIX = "qpk_"
TOKEN_TTL_SECONDS = int(os.environ.get("QP_JWT_TTL_SECONDS", "28800"))

# machine channel: service-key only, no user lane (acceptance v2 P1 #2)
_MACHINE_PATHS = re.compile(
    r"^/api/v1/(nodes/register|nodes/heartbeat|config/lookup|commands/pending)$"
)
# user administration (incl. GET) - ADMIN only
_ADMIN_PATHS = re.compile(r"^/api/v1/auth/users(/|$)")
# analyst-lifted writes: research yes, trading no
_ANALYST_WRITE_PATHS = re.compile(
    r"^/api/v1/(backtests|accounts/[0-9a-f-]{36}/ai/analyze)$"
)
# account-scoped routes -> tenant isolation
_ACCOUNT_IN_PATH = re.compile(r"^/api/v1/accounts/([0-9a-f-]{36})(/|$)")

DENY_MESSAGES = {
    "SERVICE_KEY_ONLY": "this route carries machine traffic; it requires the service key",
    "USER_IDENTITY_REQUIRED": (
        "the service key only carries machine traffic; log in as a user"
    ),
    "ADMIN_ONLY": "administrator role required",
    "ROLE_FORBIDDEN": "your role is not allowed to perform this action",
}


@dataclass(frozen=True)
class Principal:
    """Authenticated identity, carried on request.state.principal."""

    kind: str  # "service" | "user"
    user_id: str | None = None
    username: str = "service"
    role: str | None = None

    @property
    def is_service(self) -> bool:
        return self.kind == "service"

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"


# --------------------------------------------------------------------------
# passwords (bcrypt; str hashes in the DB, bytes in tests)
# --------------------------------------------------------------------------


def hash_password(password: str, rounds: int = 12) -> bytes:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds))


def verify_password(password: str, password_hash: str | bytes) -> bool:
    try:
        if isinstance(password_hash, str):
            password_hash = password_hash.encode("utf-8")
        return bcrypt.checkpw(password.encode("utf-8"), password_hash)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# JWT: hand-rolled HS256 (stdlib hmac/base64/json only, no new dependency)
# --------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def issue_token(
    user_id: str,
    username: str,
    role: str,
    ttl: int | None = None,
    token_version: int = 0,
) -> str:
    secret = os.environ.get("QP_JWT_SECRET", "")
    if not secret:
        raise RuntimeError("QP_JWT_SECRET not configured; refusing to issue tokens")
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "ver": int(token_version),
        "iat": now,
        "exp": now + (ttl if ttl is not None else TOKEN_TTL_SECONDS),
    }
    head = _b64url(json.dumps(header, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(
        secret.encode("utf-8"), f"{head}.{body}".encode("ascii"), hashlib.sha256
    ).digest()
    return f"{head}.{body}.{_b64url(signature)}"


def verify_token(token: str) -> dict | None:
    """-> payload dict, or None for anything we did not issue ourselves.

    Fail-closed: no QP_JWT_SECRET configured -> every token is rejected.
    """
    secret = os.environ.get("QP_JWT_SECRET", "")
    if not secret:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    head_b, body_b, sig_b = parts
    try:
        header = json.loads(_b64url_decode(head_b))
        payload = json.loads(_b64url_decode(body_b))
        signature = _b64url_decode(sig_b)
    except (ValueError, TypeError):
        return None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return None
    if header.get("alg") != "HS256":
        return None  # alg confusion: only HS256 tokens we issued ourselves
    expected = hmac.new(
        secret.encode("utf-8"), f"{head_b}.{body_b}".encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(signature, expected):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    if "sub" not in payload:
        return None
    return payload


# --------------------------------------------------------------------------
# credential resolution (the middleware calls these; service key stays first)
# --------------------------------------------------------------------------


def authenticate_bearer(authorization: str) -> Principal | None:
    """Resolve a Bearer JWT -> Principal, re-validating the user row.

    The signature only proves the token was issued by us; it says nothing
    about the user TODAY. So every request re-reads the user: disabled
    users are rejected, role changes apply immediately and a bumped
    token_version (password reset / role change / disable) supersedes
    older tokens (R2 acceptance P2-01/P2-02). Tokens issued before the
    token_version column existed carry no "ver" claim and count as 0.
    """
    if not authorization.startswith("Bearer "):
        return None
    payload = verify_token(authorization[len("Bearer "):].strip())
    if payload is None:
        return None
    user_id = payload.get("sub")
    row = db.query_one(
        """
        SELECT username, role, is_active, token_version
          FROM users
         WHERE id = %s
        """,
        (user_id,),
    )
    if row is None or not row["is_active"]:
        return None
    token_version = payload.get("ver", 0)
    if not isinstance(token_version, int) or token_version != int(row["token_version"] or 0):
        return None
    return Principal(
        kind="user",
        user_id=str(user_id),
        username=row["username"],
        role=row["role"],
    )


def authenticate_user_key(supplied: str) -> Principal | None:
    """Resolve a qpk_... personal key (sha256 at rest) -> Principal.

    Fast path: anything without the qpk_ prefix never touches the DB, so
    a wrong service key costs no database round-trip.
    """
    if not supplied.startswith(USER_KEY_PREFIX):
        return None
    key_hash = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    row = db.query_one(
        """
        SELECT k.id::text AS key_id, k.user_id::text AS user_id,
               k.revoked_at, u.username, u.role, u.is_active
          FROM user_api_keys k
          JOIN users u ON u.id = k.user_id
         WHERE k.key_hash = %s
        """,
        (key_hash,),
    )
    if row is None or row["revoked_at"] is not None or not row["is_active"]:
        return None
    db.execute("UPDATE user_api_keys SET last_used_at = now() WHERE id = %s", (row["key_id"],))
    return Principal(
        kind="user", user_id=row["user_id"], username=row["username"], role=row["role"]
    )


_dummy_hash: bytes | None = None


def _dummy_verify(password: str) -> None:
    """Burn one bcrypt check for unknown usernames (anti-enumeration).

    Same cost 12 as the real hashes, so response timing cannot tell an
    unknown username apart from a wrong password (R2 acceptance P2-04).
    """
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("timing-equalizer", rounds=12)
    verify_password(password, _dummy_hash)


# --------------------------------------------------------------------------
# login rate limiting (R2 P2-03 + R3 P2-03 + R3.1 P1 / R3.3 P1-01 + P2-01):
# layered throttling with ATOMIC attempt reservations.
#
# Local layer (always on): in-memory sliding window per (ip, username) AND
# per ip alone - the pure-IP bucket catches password sprays that rotate
# usernames. Shared layer: RedisLoginThrottle via
# QP_LOGIN_THROTTLE_REDIS_URL so multiple API instances share one budget
# and restarts do not clear attack counters.
#
# R3.3 P1-01: the R3.2 flow checked first ("too many attempts?") and only
# recorded the failure AFTER the slow bcrypt check - N concurrent requests
# all read "under the limit" and all proceeded (the acceptance measured
# 17 verifications past a limit of 10). Both layers now spend the slot
# atomically UP FRONT (reserve), and the password verdict only decides
# what happens to the reservation:
#     wrong password   the reservation IS the failure record (kept)
#     correct password the pair bucket is cleared and only this
#                      attempt's spray slot is released
#     aborted verify   the reservation is released - not a failure
# Crashed processes leak at most their in-flight reservations, which
# the sliding-window trim reclaims (bounded leak, no permanent loss).
#
# QP_LOGIN_THROTTLE_MODE (R3.1, one name everywhere; R3.2 makes parsing
# STRICT - an unknown value is a config error that fails the boot, never
# a silent fallback to memory_only):
#   memory_only    default (unset/empty); the URL is IGNORED (one loud
#                  warning) and the in-process buckets stay authoritative
#   redis_required multi-instance deployments; when Redis is unreachable the
#                  login path raises LoginThrottleUnavailable and app.py
#                  answers 503 - NEVER a silent fallback to per-instance
#                  counters, which an attacker could shard requests around
# validate_login_throttle_config() runs in the app lifespan BEFORE the
# service accepts traffic (R3.2 P1): a typo'd mode or a missing URL fails
# the boot instead of quietly weakening the deployment.
# --------------------------------------------------------------------------

_LOGIN_MAX_FAILURES = int(os.environ.get("QP_LOGIN_MAX_FAILURES", "10"))
_LOGIN_WINDOW_SECONDS = int(os.environ.get("QP_LOGIN_WINDOW_SECONDS", "900"))
_LOGIN_IP_MAX_FAILURES = int(
    os.environ.get("QP_LOGIN_IP_MAX_FAILURES", str(_LOGIN_MAX_FAILURES * 5))
)
# each entry is (timestamp, attempt_id): the id lets a success or an
# abort remove EXACTLY its own reservation (R3.3 P1-01)
_login_failures: dict[str, list[tuple[float, str]]] = {}
_login_failures_ip: dict[str, list[tuple[float, str]]] = {}
# sync login endpoints run on FastAPI's thread pool; the reserve below is
# check-then-append, so it needs the same atomicity the Lua script gives
# the shared layer (R3.3 P1-01)
_login_lock = threading.Lock()
_shared_throttle: RedisLoginThrottle | None = None
_warned_memory_only = False

_THROTTLE_MODES = ("memory_only", "redis_required")


@dataclass(frozen=True)
class LoginReservation:
    """One atomically spent login-attempt slot (R3.3 P1-01).

    Carries everything the settle/release paths need, so the caller
    never has to re-derive the buckets."""

    username: str
    remote_ip: str
    attempt_id: str
    shared: bool  # the Redis layer holds a slot for this attempt too


class LoginThrottleConfigError(RuntimeError):
    """Invalid QP_LOGIN_THROTTLE_MODE / QP_LOGIN_THROTTLE_REDIS_URL
    combination (R3.2 P1). Raised by validate_login_throttle_config()
    during app startup: a typo like 'redis_require' must fail the boot
    instead of silently degrading a multi-instance deployment to
    per-instance counting."""


def _login_bucket(username: str, remote_ip: str) -> str:
    return f"{remote_ip}|{username.strip().lower()}"


def _prune_bucket(bucket: str, now: float) -> list[tuple[float, str]]:
    """Drop entries older than the window. Called under _login_lock, so
    the trim+count+append in reserve_login_attempt is atomic for the
    local layer exactly the way the Lua script is for the shared one."""
    stamps = [
        (t, a)
        for (t, a) in _login_failures.get(bucket, [])
        if now - t < _LOGIN_WINDOW_SECONDS
    ]
    _login_failures[bucket] = stamps
    return stamps


def _prune_ip_bucket(ip: str, now: float) -> list[tuple[float, str]]:
    stamps = [
        (t, a)
        for (t, a) in _login_failures_ip.get(ip, [])
        if now - t < _LOGIN_WINDOW_SECONDS
    ]
    _login_failures_ip[ip] = stamps
    return stamps


def _release_local_slot(bucket: str, remote_ip: str, attempt_id: str) -> None:
    """Give one reservation's slot back in both local buckets (an
    aborted verification, or the shared layer refusing the attempt)."""
    with _login_lock:
        _login_failures[bucket] = [
            (t, a) for (t, a) in _login_failures.get(bucket, []) if a != attempt_id
        ]
        _login_failures_ip[remote_ip] = [
            (t, a) for (t, a) in _login_failures_ip.get(remote_ip, []) if a != attempt_id
        ]


def _throttle_mode() -> str:
    """QP_LOGIN_THROTTLE_MODE (R3.2 P1): unset/empty -> the explicit
    memory_only default; any OTHER unknown value raises
    LoginThrottleConfigError. Mapping a typo like 'redis_require' back to
    memory_only would silently drop a multi-instance deployment to
    per-instance counting - the exact degradation R3.2 forbids."""
    raw = os.environ.get("QP_LOGIN_THROTTLE_MODE", "")
    mode = raw.strip().lower()
    if not mode:
        return "memory_only"
    if mode not in _THROTTLE_MODES:
        raise LoginThrottleConfigError(
            f"QP_LOGIN_THROTTLE_MODE={raw!r} is invalid: expected one of "
            f"{_THROTTLE_MODES} (R3.2: no silent fallback to memory_only)"
        )
    return mode


def validate_login_throttle_config() -> None:
    """Startup gate (R3.2 P1): app.py runs this first inside the lifespan,
    BEFORE the DB pool / consumers start and before the service accepts a
    single request.

      * unknown explicit mode        -> LoginThrottleConfigError (never a
        silent fallback to memory_only)
      * redis_required without a URL -> LoginThrottleConfigError (booting
        anyway would silently mean memory_only)
      * memory_only                  -> one explicit SECURITY warning
      * redis_required + unreachable Redis does NOT fail the boot:
        password logins keep answering 503 (LoginThrottleUnavailable)
        until Redis recovers; the rest of the API stays up
    """
    mode = _throttle_mode()  # unknown value -> LoginThrottleConfigError
    url = os.environ.get("QP_LOGIN_THROTTLE_REDIS_URL", "").strip()
    if mode == "redis_required":
        if not url:
            raise LoginThrottleConfigError(
                "QP_LOGIN_THROTTLE_MODE=redis_required requires "
                "QP_LOGIN_THROTTLE_REDIS_URL to be set; refusing to start "
                "instead of silently degrading to per-instance limiting"
            )
        logger.info(
            "login throttle: redis_required - failure counts are shared "
            "via Redis; while Redis is unreachable password logins "
            "answer 503"
        )
    else:
        logger.warning(
            "SECURITY: QP_LOGIN_THROTTLE_MODE=memory_only - login limiting "
            "is per-instance only; multi-instance deployments must set "
            "QP_LOGIN_THROTTLE_MODE=redis_required and "
            "QP_LOGIN_THROTTLE_REDIS_URL"
        )


def _throttle() -> RedisLoginThrottle | None:
    """Shared Redis layer, lazily built from QP_LOGIN_THROTTLE_REDIS_URL
    (R3 P2-03 / R3.1 P1). Returns None when shared counting is off or
    unbuildable; reserve_login_attempt decides what None means per mode.

    Rebuilt when the URL or the limits change so tests can point the env
    around without restarting the process."""
    global _shared_throttle, _warned_memory_only
    url = os.environ.get("QP_LOGIN_THROTTLE_REDIS_URL", "")
    if _throttle_mode() == "memory_only":
        if url and not _warned_memory_only:
            _warned_memory_only = True
            logger.warning(
                "SECURITY: QP_LOGIN_THROTTLE_MODE is memory_only, so "
                "QP_LOGIN_THROTTLE_REDIS_URL is IGNORED and login limiting "
                "is per-instance only - set QP_LOGIN_THROTTLE_MODE="
                "redis_required to share failure counts across instances"
            )
        _shared_throttle = None
        return None
    # redis_required: the shared layer is mandatory
    if not url:
        logger.error(
            "QP_LOGIN_THROTTLE_MODE=redis_required but "
            "QP_LOGIN_THROTTLE_REDIS_URL is empty - password logins are "
            "rejected with 503 until the URL is configured"
        )
        _shared_throttle = None
        return None
    current = _shared_throttle
    if (
        current is not None
        and current.url == url
        and current._max == _LOGIN_MAX_FAILURES
        and current._ip_max == _LOGIN_IP_MAX_FAILURES
        and current._window == _LOGIN_WINDOW_SECONDS
    ):
        return current
    try:
        current = RedisLoginThrottle(
            url, _LOGIN_MAX_FAILURES, _LOGIN_IP_MAX_FAILURES, _LOGIN_WINDOW_SECONDS
        )
    except Exception:  # noqa: BLE001 - reserve_login_attempt turns this into 503
        logger.exception("cannot build Redis login throttle (redis_required mode)")
        current = None
    _shared_throttle = current
    return current


def reserve_login_attempt(username: str, remote_ip: str) -> LoginReservation | None:
    """Atomically check BOTH limits and spend one attempt slot in both
    dimensions (R3.3 P1-01).

    Returns:
      LoginReservation - the slot is spent; the caller must settle or
                         release it after the password check
      None              - a limit is reached -> 429; NOTHING was spent,
                         so refusals can never extend the lockout
    Raises LoginThrottleUnavailable when redis_required mode cannot reach
    the shared budget (R3.1 P1): rejecting the attempt (503 in app.py)
    is strictly safer than letting each instance count on its own while
    an attacker spreads requests. The local slot is always given back
    before raising or refusing, so no path leaks a spent-but-unused
    slot into the local buckets."""
    attempt_id = secrets.token_hex(16)
    bucket = _login_bucket(username, remote_ip)
    with _login_lock:
        now = time.time()
        pair = _prune_bucket(bucket, now)
        ip = _prune_ip_bucket(remote_ip, now)
        if len(pair) >= _LOGIN_MAX_FAILURES or len(ip) >= _LOGIN_IP_MAX_FAILURES:
            # local budget already spent: no reservation anywhere
            return None
        _login_failures.setdefault(bucket, []).append((now, attempt_id))
        _login_failures_ip.setdefault(remote_ip, []).append((now, attempt_id))
    shared = _throttle()
    if shared is None:
        if _throttle_mode() == "redis_required":
            _release_local_slot(bucket, remote_ip, attempt_id)
            raise LoginThrottleUnavailable(
                "redis_required: shared login throttle not available"
            )
        return LoginReservation(username, remote_ip, attempt_id, shared=False)
    verdict = shared.reserve(username, remote_ip, attempt_id)
    if verdict is None:
        _release_local_slot(bucket, remote_ip, attempt_id)
        # _throttle() only returns an instance in redis_required mode,
        # so "unavailable" always means: fail closed (503)
        raise LoginThrottleUnavailable(
            "redis_required: Redis unreachable, refusing to degrade"
        )
    if verdict is False:
        # the shared budget is spent; the optimistic local slot goes back
        _release_local_slot(bucket, remote_ip, attempt_id)
        return None
    return LoginReservation(username, remote_ip, attempt_id, shared=True)


def settle_login_success(reservation: LoginReservation) -> None:
    """A correct password (R3.3 P1-01 lifecycle): the (ip, username)
    pair bucket is cleared whole - the user just authenticated, so the
    pair is not under attack - and ONLY this attempt's slot is released
    from the pure-IP spray bucket. Other users' recorded failures stay
    untouched, so a success never wipes the spray statistics."""
    bucket = _login_bucket(reservation.username, reservation.remote_ip)
    with _login_lock:
        _login_failures.pop(bucket, None)
        _login_failures_ip[reservation.remote_ip] = [
            (t, a)
            for (t, a) in _login_failures_ip.get(reservation.remote_ip, [])
            if a != reservation.attempt_id
        ]
    if reservation.shared:
        shared = _throttle()
        if shared is not None:
            shared.settle_success(
                reservation.username, reservation.remote_ip, reservation.attempt_id
            )


def release_login_reservation(reservation: LoginReservation) -> None:
    """An aborted verification (timeout, crash, client gone) is NOT a
    failure (R3.3 P1-01): the slot is returned in both dimensions.
    Best effort on the shared side - if Redis is down the slot ages out
    through the sliding-window trim, so quota is never permanently
    lost."""
    _release_local_slot(
        _login_bucket(reservation.username, reservation.remote_ip),
        reservation.remote_ip,
        reservation.attempt_id,
    )
    if reservation.shared:
        shared = _throttle()
        if shared is not None:
            shared.release(
                reservation.username, reservation.remote_ip, reservation.attempt_id
            )


def verify_login(username: str, password: str) -> dict | None:
    """username + password -> {id, username, role, token_version} or None."""
    row = db.query_one(
        """
        SELECT id::text AS id, username, password_hash, role, is_active,
               token_version
          FROM users
         WHERE username = %s
        """,
        (username,),
    )
    if row is None:
        _dummy_verify(password)
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    if not row["is_active"]:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "token_version": int(row["token_version"] or 0),
    }


def generate_api_key() -> tuple[str, str]:
    """-> (plaintext key, sha256 hex). Plaintext is shown exactly once."""
    plaintext = USER_KEY_PREFIX + secrets.token_hex(24)
    return plaintext, hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# authorization: deny code or None (allowed) - pure, no IO
# --------------------------------------------------------------------------


def authorize(principal: Principal, method: str, path: str) -> str | None:
    """Role matrix (phase-3 R2, acceptance v2 P1 #2 + R2 remediation P1-01):

    * service lane: MACHINE ROUTES ONLY - the shared service key must
      never impersonate a user on business routes, otherwise it would
      bypass per-user RBAC and account grants entirely
    * machine routes: service-key only, for every user role
    * /api/v1/auth/users* (incl. GET): ADMIN only
    * GET/HEAD/OPTIONS otherwise: every user role (row isolation is
      enforced separately by account_granted / list_accounts filtering)
    * /api/v1/auth/* self-service (me, api-keys): every user role
    * analyst-lifted writes (backtests, ai/analyze): ADMIN/TRADER/ANALYST
    * all other writes (commands, config, risk): ADMIN/TRADER
    """
    if principal.is_service:
        return None if _MACHINE_PATHS.match(path) else "USER_IDENTITY_REQUIRED"
    if _MACHINE_PATHS.match(path):
        return "SERVICE_KEY_ONLY"
    if _ADMIN_PATHS.match(path):
        return None if principal.is_admin else "ADMIN_ONLY"
    if method in ("GET", "HEAD", "OPTIONS"):
        return None
    if path.startswith("/api/v1/auth/"):
        return None  # self-service; handlers enforce ownership
    if _ANALYST_WRITE_PATHS.match(path):
        return None if principal.role in ("ADMIN", "TRADER", "ANALYST") else "ROLE_FORBIDDEN"
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return None if principal.role in ("ADMIN", "TRADER") else "ROLE_FORBIDDEN"
    return None


def account_id_from_path(path: str) -> str | None:
    match = _ACCOUNT_IN_PATH.match(path)
    return match.group(1) if match else None


def account_granted(principal: Principal, account_id: str) -> bool:
    """Per-account tenant isolation, checked per request.

    The service lane and ADMIN see everything; named users only the
    accounts they were granted - so revoking a grant takes effect
    immediately, even for live JWTs.
    """
    if principal.is_service or principal.is_admin:
        return True
    return (
        db.query_one(
            "SELECT 1 AS one FROM user_account_grants WHERE user_id = %s AND account_id = %s",
            (principal.user_id, account_id),
        )
        is not None
    )
