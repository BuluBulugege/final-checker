"""Shared pydantic models — the fixed vocabulary every plugin, the job manager,
and the API/UI all speak. Plugins MUST NOT redefine these; they return them.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class CheckMode(str, enum.Enum):
    """The two operating modes."""

    HEALTH = "health"  # 测活 — can we call a model at all
    GRADE = "grade"  # 测等级 — full tier/rate-limit probe (implies health)


class KeyStatus(str, enum.Enum):
    """Outcome of checking a single key."""

    PENDING = "pending"
    RUNNING = "running"
    ALIVE = "alive"  # health check passed
    DEAD = "dead"  # key invalid / revoked / unauthorized
    GRADED = "graded"  # grade check produced a tier label
    ERROR = "error"  # unexpected failure (network, bug) — not a verdict on the key
    UNSUPPORTED = "unsupported"  # no plugin matched this key


class ErrorClass(str, enum.Enum):
    """Normalized classification of a provider response, so probe logic across
    plugins can branch on a shared taxonomy instead of raw HTTP codes."""

    OK = "ok"
    AUTH = "auth"  # bad/revoked/missing key (e.g. 401, API_KEY_INVALID)
    RATE_LIMIT = "rate_limit"  # true 429 rate limit (the timing signal)
    BILLING = "billing"  # no credits / quota exhausted / billing disabled
    PERMISSION = "permission"  # valid key, lacks access to resource/model
    NOT_FOUND = "not_found"  # unknown model id
    BAD_REQUEST = "bad_request"  # malformed request
    OVERLOADED = "overloaded"  # provider capacity saturated (NOT your limit)
    SERVER = "server"  # 5xx transient
    NETWORK = "network"  # connection/timeout error on our side
    UNKNOWN = "unknown"


class ProbeOutcome(BaseModel):
    """Result of a single HTTP probe against a provider, normalized."""

    ok: bool
    status_code: int | None = None
    error_class: ErrorClass = ErrorClass.UNKNOWN
    elapsed_ms: float | None = None
    retry_after_s: float | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body_snippet: str | None = None
    detail: str | None = None


class BurstResult(BaseModel):
    """Aggregate outcome of a burst test (N req/s for D s, or until cap)."""

    total_sent: int = 0
    success: int = 0
    rate_limited: int = 0
    other_errors: int = 0
    first_429_at_s: float | None = None  # seconds from burst start to first 429
    duration_s: float = 0.0
    target_rps: float = 0.0
    cap: int | None = None
    # derived empirical max requests-per-minute, when measurable from a cap probe
    measured_rpm: float | None = None
    error_breakdown: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class KeyResult(BaseModel):
    """The full record for one key, streamed to the UI as it fills in."""

    index: int  # position in the submitted list
    masked_key: str  # never expose the full key back to the client
    provider: str | None = None  # plugin name that claimed it, or None
    status: KeyStatus = KeyStatus.PENDING
    mode: CheckMode
    # grade outputs
    tier: str | None = None  # provider-specific label, e.g. "t3-1", "t5-r100", "T2", "企业级"
    alive: bool | None = None
    # human-readable extras (org name, per-model rpm/tpm/tpd, enterprise notes, etc.)
    remarks: list[str] = Field(default_factory=list)
    # structured detail bag for the UI / debugging
    details: dict[str, Any] = Field(default_factory=dict)
    progress: float = 0.0  # 0..1 for this key's own checks
    progress_label: str | None = None
    error: str | None = None
    # Optional large downloadable report (e.g. a full GCP scan). When set, the UI
    # shows a "下载" button next to the (necessarily summarized) remarks.
    # download_text is excluded from normal serialization (it can be large); it is
    # served only via the dedicated /download endpoint.
    download_filename: str | None = None
    download_text: str | None = Field(default=None, exclude=True)


class JobRequest(BaseModel):
    """Inbound request to start a check job."""

    keys: str = Field(..., description="raw text, one API key per line")
    mode: CheckMode = CheckMode.GRADE
    concurrency: int = Field(5, ge=1, le=100, description="max keys probed at once")
    full_load: bool = Field(
        True,
        description="run the full spec-rate burst (50 rps etc). If false, plugins "
        "use gentler defaults to save cost.",
    )


class JobSummary(BaseModel):
    job_id: str
    mode: CheckMode
    concurrency: int
    full_load: bool
    total: int
    state: Literal["queued", "running", "done", "cancelled"]
    completed: int
    started_at: float | None = None
    finished_at: float | None = None


class JobSnapshot(JobSummary):
    """Full job state including every key result (for poll/initial SSE)."""

    results: list[KeyResult] = Field(default_factory=list)
