"""Small HTTP helpers shared by plugins: a configured async client factory and
response-time measurement. Error-class normalization is provider-specific and
lives in each plugin, but the ProbeOutcome shape and header extraction are shared.
"""

from __future__ import annotations

import time

import httpx

from app.config import settings
from app.models import ErrorClass, ProbeOutcome
from app.redact import redact as _redact


def make_client(timeout_s: float) -> httpx.AsyncClient:
    timeout = httpx.Timeout(timeout_s, connect=settings.http_connect_timeout_s)
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    return httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header value (seconds form only; HTTP-date is ignored)."""
    if not value:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def parse_duration_secs(value: str | None) -> float | None:
    """Parse Go-style duration strings used by OpenAI reset headers, e.g.
    '1s', '6m0s', '1m30s', '500ms'. Returns seconds."""
    if not value:
        return None
    s = value.strip()
    total = 0.0
    num = ""
    i = 0
    try:
        while i < len(s):
            c = s[i]
            if c.isdigit() or c == ".":
                num += c
                i += 1
            elif c == "m" and i + 1 < len(s) and s[i + 1] == "s":
                total += float(num) / 1000.0
                num = ""
                i += 2
            elif c == "m":
                total += float(num) * 60.0
                num = ""
                i += 1
            elif c == "s":
                total += float(num)
                num = ""
                i += 1
            elif c == "h":
                total += float(num) * 3600.0
                num = ""
                i += 1
            else:
                i += 1
        return total if total else None
    except ValueError:
        return None


async def timed_request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs
) -> tuple[httpx.Response | None, float, Exception | None]:
    """Perform a request, returning (response, elapsed_ms, exception)."""
    start = time.monotonic()
    try:
        resp = await client.request(method, url, **kwargs)
        return resp, (time.monotonic() - start) * 1000.0, None
    except Exception as exc:
        return None, (time.monotonic() - start) * 1000.0, exc


def base_outcome(
    resp: httpx.Response | None, elapsed_ms: float, exc: Exception | None
) -> ProbeOutcome:
    """Build a ProbeOutcome shell with headers/status filled; error_class is set
    to NETWORK on exception, OK/UNKNOWN otherwise. Plugins refine error_class."""
    if exc is not None or resp is None:
        return ProbeOutcome(
            ok=False,
            error_class=ErrorClass.NETWORK,
            elapsed_ms=round(elapsed_ms, 1),
            detail=_redact(repr(exc)) if exc else "no response",
        )
    headers = {k.lower(): v for k, v in resp.headers.items()}
    return ProbeOutcome(
        ok=resp.is_success,
        status_code=resp.status_code,
        error_class=ErrorClass.OK if resp.is_success else ErrorClass.UNKNOWN,
        elapsed_ms=round(elapsed_ms, 1),
        retry_after_s=parse_retry_after(headers.get("retry-after")),
        headers=headers,
    )
