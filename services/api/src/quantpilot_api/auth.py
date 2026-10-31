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

JWTs are stateless: disabling a user does not kill live tokens, but the
account gate re-checks grants on every request and role changes apply at
the next login. Passwords are bcrypt cost 12 and login is timing-safe
against user enumeration (unknown usernames burn one dummy check).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass

import bcrypt

from . import db

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


def issue_token(user_id: str, username: str, role: str, ttl: int | None = None) -> str:
    secret = os.environ.get("QP_JWT_SECRET", "")
    if not secret:
        raise RuntimeError("QP_JWT_SECRET not configured; refusing to issue tokens")
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
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
    if not authorization.startswith("Bearer "):
        return None
    payload = verify_token(authorization[len("Bearer "):].strip())
    if payload is None:
        return None
    return Principal(
        kind="user",
        user_id=payload.get("sub"),
        username=payload.get("username", ""),
        role=payload.get("role"),
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
    """Burn one bcrypt check for unknown usernames (anti-enumeration)."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("timing-equalizer", rounds=4)
    verify_password(password, _dummy_hash)


def verify_login(username: str, password: str) -> dict | None:
    """username + password -> {id, username, role} or None."""
    row = db.query_one(
        """
        SELECT id::text AS id, username, password_hash, role, is_active
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
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def generate_api_key() -> tuple[str, str]:
    """-> (plaintext key, sha256 hex). Plaintext is shown exactly once."""
    plaintext = USER_KEY_PREFIX + secrets.token_hex(24)
    return plaintext, hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# authorization: deny code or None (allowed) - pure, no IO
# --------------------------------------------------------------------------


def authorize(principal: Principal, method: str, path: str) -> str | None:
    """Role matrix (phase-3 R2, acceptance v2 P1 #2):

    * service lane: everything (machine channel, unchanged)
    * machine routes: service-key only, for every user role
    * /api/v1/auth/users* (incl. GET): ADMIN only
    * GET/HEAD/OPTIONS otherwise: every user role (row isolation is
      enforced separately by account_granted / list_accounts filtering)
    * /api/v1/auth/* self-service (me, api-keys): every user role
    * analyst-lifted writes (backtests, ai/analyze): ADMIN/TRADER/ANALYST
    * all other writes (commands, config, risk): ADMIN/TRADER
    """
    if principal.is_service:
        return None
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
