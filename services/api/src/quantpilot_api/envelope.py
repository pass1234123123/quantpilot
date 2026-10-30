"""Unified response envelope: {data, error, request_id, timestamp} (PRD 13.1)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


def _envelope(request: Request | None, data: Any, error: Any, status: int) -> JSONResponse:
    request_id = (
        request.headers.get("X-Request-ID") if request is not None else None
    ) or str(uuid.uuid4())
    body = {
        "data": data,
        "error": error,
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return JSONResponse(status_code=status, content=body)


def ok(data: Any, request: Request | None = None) -> JSONResponse:
    return _envelope(request, data, None, 200)


def err(status: int, code: str, message: str, request: Request | None = None) -> JSONResponse:
    return _envelope(request, None, {"code": code, "message": message}, status)
