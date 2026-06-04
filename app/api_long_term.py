"""API routes for long-term key management.

Admin-protected endpoints for managing API keys in long-term monitoring storage.

Security:
- ADMIN_PASSWORD: Set via environment variable (default: "change-me-in-production")
- JWT_SECRET: Set via environment variable for token signing
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.db import get_connection, init_db
from app.long_term_monitor import LongTermKeyManager

router = APIRouter(prefix="/api/long-term", tags=["long-term"])

# Admin password (configure via ADMIN_PASSWORD environment variable)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me-in-production")

# JWT configuration (configure via JWT_SECRET environment variable)
JWT_SECRET = os.getenv("JWT_SECRET", "final-checker-jwt-secret-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = 24 * 3600  # 24 hours

security = HTTPBearer()


# ============================================================================
# Models
# ============================================================================


class AuthRequest(BaseModel):
    """Admin authentication request."""

    password: str = Field(..., description="Admin password")


class AuthResponse(BaseModel):
    """Authentication response with JWT token."""

    token: str = Field(..., description="JWT token for authenticated requests")
    expires_in: int = Field(..., description="Token expiry in seconds")


class AddKeysRequest(BaseModel):
    """Request to add one or more keys to long-term monitoring."""

    keys: list[str] = Field(..., description="List of API keys or service account JSON")
    platform: str = Field(..., description="Platform: gemini/openai/anthropic/gcp")
    notes: str | None = Field(None, description="Optional notes for all keys")


class AddKeysResponse(BaseModel):
    """Response after adding keys."""

    added: int = Field(..., description="Number of newly added keys")
    duplicates: int = Field(..., description="Number of duplicate keys skipped")
    key_ids: list[int] = Field(..., description="Database IDs of added keys")


class MoveKeysRequest(BaseModel):
    """Request to move keys from short-term to long-term storage."""

    keys: list[str] = Field(..., description="List of API keys to move")
    platform: str = Field(..., description="Platform: gemini/openai/anthropic/gcp")
    notes: str | None = Field(None, description="Optional notes")


class CheckDuplicateRequest(BaseModel):
    """Request to check if keys are duplicates."""

    keys: list[str] = Field(..., description="List of keys to check")


class CheckDuplicateResponse(BaseModel):
    """Response with duplicate check results."""

    duplicates: list[dict[str, Any]] = Field(
        ...,
        description="List of duplicate info: {key_hash, exists, key_id}",
    )


class KeyRecord(BaseModel):
    """Long-term key record."""

    id: int
    masked_key: str
    platform: str
    status: str
    last_check: float | None
    error_code: str | None
    death_time: float | None
    retry_count: int
    created_at: float
    notes: str | None
    next_check_time: float | None


class ListKeysResponse(BaseModel):
    """Response with list of keys."""

    keys: list[KeyRecord]
    total: int


class CheckResultResponse(BaseModel):
    """Response after checking a key."""

    key_id: int
    status: str
    error_class: str | None
    error_detail: str | None
    response_time_ms: float | None
    checked_at: float


class BatchCheckResponse(BaseModel):
    """Response after batch checking keys."""

    checked: int
    results: list[CheckResultResponse]


# ============================================================================
# JWT Authentication
# ============================================================================


def create_token(data: dict[str, Any]) -> str:
    """Create a JWT token.

    Args:
        data: Payload to encode in the token

    Returns:
        Encoded JWT token string
    """
    import jwt

    to_encode = data.copy()
    expire = time.time() + JWT_EXPIRY_SECONDS
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict[str, Any]:
    """Verify and decode a JWT token.

    Args:
        token: JWT token string

    Returns:
        Decoded token payload

    Raises:
        HTTPException: If token is invalid or expired
    """
    import jwt

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")


async def require_admin(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict[str, Any]:
    """Middleware to verify admin JWT token.

    Args:
        credentials: HTTP Bearer token from request

    Returns:
        Decoded token payload

    Raises:
        HTTPException: If token is missing or invalid
    """
    token = credentials.credentials
    return verify_token(token)


# ============================================================================
# API Routes
# ============================================================================


@router.post("/auth", response_model=AuthResponse)
async def admin_auth(req: AuthRequest) -> AuthResponse:
    """Authenticate admin with password and return JWT token.

    Args:
        req: Authentication request with password

    Returns:
        JWT token and expiry time

    Raises:
        HTTPException: If password is incorrect
    """
    if req.password != ADMIN_PASSWORD:
        raise HTTPException(401, "Invalid password")

    token = create_token({"admin": True})
    return AuthResponse(token=token, expires_in=JWT_EXPIRY_SECONDS)


@router.post("/keys", response_model=AddKeysResponse, dependencies=[Depends(require_admin)])
async def add_keys(req: AddKeysRequest) -> AddKeysResponse:
    """Add one or more keys to long-term monitoring (batch).

    Keys are health-checked before adding. Only alive keys are accepted.
    If a key already exists, its status is updated to the latest check result.

    Requires admin authentication.

    Args:
        req: Request with keys to add

    Returns:
        Summary of added and duplicate keys

    Raises:
        HTTPException: If platform is invalid or key fails health check
    """
    manager = LongTermKeyManager()

    added = 0
    updated = 0
    failed = 0
    key_ids = []
    errors = []

    for key_data_item in req.keys:
        try:
            key_id, is_new, check_result = await manager.add_key_with_check(
                key_data=key_data_item.strip(),
                platform=req.platform,
                notes=req.notes,
                check_health=True,  # Always check health before adding
            )
            if is_new:
                added += 1
                key_ids.append(key_id)
            else:
                updated += 1
                key_ids.append(key_id)
        except ValueError as e:
            # Key failed health check or invalid platform
            failed += 1
            errors.append(str(e))

    # If all keys failed, raise exception
    if failed > 0 and added == 0 and updated == 0:
        raise HTTPException(
            400,
            f"All {failed} keys failed health check. Errors: {'; '.join(errors[:3])}"
        )

    return AddKeysResponse(
        added=added,
        duplicates=updated,  # Reuse "duplicates" field for updated keys
        key_ids=key_ids
    )


@router.get("/keys", response_model=ListKeysResponse)
async def list_keys(
    platform: str | None = Query(None, description="Filter by platform"),
    status: str | None = Query(None, description="Filter by status"),
    search: str | None = Query(None, description="Search in notes or masked key"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
) -> ListKeysResponse:
    """List long-term keys with filtering and pagination.

    Args:
        platform: Optional platform filter
        status: Optional status filter
        search: Optional search term
        limit: Maximum number of results
        offset: Pagination offset

    Returns:
        List of key records with pagination info
    """
    init_db()

    with get_connection() as conn:
        # Build query with filters
        where_clauses = []
        params = []

        if platform:
            where_clauses.append("platform = ?")
            params.append(platform)

        if status:
            where_clauses.append("status = ?")
            params.append(status)

        if search:
            where_clauses.append("(notes LIKE ? OR key_data LIKE ?)")
            search_pattern = f"%{search}%"
            params.extend([search_pattern, search_pattern])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # Get total count
        count_query = f"SELECT COUNT(*) as cnt FROM long_term_keys {where_sql}"
        total = conn.execute(count_query, params).fetchone()["cnt"]

        # Get paginated results
        params.extend([limit, offset])
        query = f"""
            SELECT * FROM long_term_keys
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query, params).fetchall()

        # Convert to KeyRecord models
        manager = LongTermKeyManager()
        keys = []
        for row in rows:
            # Mask the key_data for display
            from app.plugins.base import mask_key

            masked = mask_key(row["key_data"])

            # Calculate next check time
            next_check = manager.calculate_next_check_time(
                status=row["status"],
                retry_count=row["retry_count"],
                death_time=row["death_time"],
            )

            keys.append(
                KeyRecord(
                    id=row["id"],
                    masked_key=masked,
                    platform=row["platform"],
                    status=row["status"],
                    last_check=row["last_check"],
                    error_code=row["error_code"],
                    death_time=row["death_time"],
                    retry_count=row["retry_count"],
                    created_at=row["created_at"],
                    notes=row["notes"],
                    next_check_time=next_check,
                )
            )

        return ListKeysResponse(keys=keys, total=total)


@router.post("/keys/move", response_model=AddKeysResponse, dependencies=[Depends(require_admin)])
async def move_keys_from_short_term(req: MoveKeysRequest) -> AddKeysResponse:
    """Move keys from short-term checking to long-term monitoring.

    This is essentially an alias for add_keys with different semantics.
    Requires admin authentication.

    Args:
        req: Request with keys to move

    Returns:
        Summary of moved keys
    """
    # Reuse add_keys logic
    return await add_keys(
        AddKeysRequest(keys=req.keys, platform=req.platform, notes=req.notes)
    )


@router.post("/keys/{key_id}/check", response_model=CheckResultResponse, dependencies=[Depends(require_admin)])
async def check_single_key(key_id: int) -> CheckResultResponse:
    """Manually trigger health check for a single key.

    Requires admin authentication.

    Args:
        key_id: Database key ID

    Returns:
        Check result

    Raises:
        HTTPException: If key not found
    """
    init_db()

    with get_connection() as conn:
        row = conn.execute(
            "SELECT key_data FROM long_term_keys WHERE id = ?", (key_id,)
        ).fetchone()

        if not row:
            raise HTTPException(404, f"Key {key_id} not found")

        key_data = row["key_data"]

    manager = LongTermKeyManager()

    # Perform check
    result = await manager.check_key(key_id, key_data)

    # Update status
    manager.update_status(
        key_id=result["key_id"],
        status=result["status"],
        error_code=result["error_class"],
        checked_at=result["checked_at"],
    )

    # Record in history
    manager.record_check_result(
        key_id=result["key_id"],
        checked_at=result["checked_at"],
        status=result["status"],
        error_class=result["error_class"],
        error_detail=result["error_detail"],
        response_time_ms=result["response_time_ms"],
    )

    return CheckResultResponse(**result)


@router.post("/keys/check", response_model=BatchCheckResponse, dependencies=[Depends(require_admin)])
async def batch_check_keys(
    key_ids: list[int] | None = None,
    all: bool = Query(False, description="Check all keys if true"),
) -> BatchCheckResponse:
    """Batch check multiple keys or all keys.

    Requires admin authentication.

    Args:
        key_ids: Optional list of specific key IDs to check
        all: If true, check all active and dead keys

    Returns:
        Batch check results

    Raises:
        HTTPException: If neither key_ids nor all is specified
    """
    init_db()

    if not all and not key_ids:
        raise HTTPException(400, "Must specify key_ids or set all=true")

    with get_connection() as conn:
        if all:
            # Get all active and dead keys
            rows = conn.execute(
                "SELECT id, key_data FROM long_term_keys WHERE status IN ('active', 'dead')"
            ).fetchall()
        else:
            # Get specific keys
            placeholders = ",".join("?" * len(key_ids))
            rows = conn.execute(
                f"SELECT id, key_data FROM long_term_keys WHERE id IN ({placeholders})",
                key_ids,
            ).fetchall()

    manager = LongTermKeyManager()
    results = []

    for row in rows:
        key_id = row["id"]
        key_data = row["key_data"]

        # Perform check
        result = await manager.check_key(key_id, key_data)

        # Update status
        manager.update_status(
            key_id=result["key_id"],
            status=result["status"],
            error_code=result["error_class"],
            checked_at=result["checked_at"],
        )

        # Record in history
        manager.record_check_result(
            key_id=result["key_id"],
            checked_at=result["checked_at"],
            status=result["status"],
            error_class=result["error_class"],
            error_detail=result["error_detail"],
            response_time_ms=result["response_time_ms"],
        )

        results.append(CheckResultResponse(**result))

    return BatchCheckResponse(checked=len(results), results=results)


@router.delete("/keys/{key_id}", dependencies=[Depends(require_admin)])
async def delete_key(key_id: int) -> dict[str, Any]:
    """Delete a key from long-term monitoring.

    Requires admin authentication.

    Args:
        key_id: Database key ID

    Returns:
        Success confirmation

    Raises:
        HTTPException: If key not found
    """
    init_db()

    with get_connection() as conn:
        result = conn.execute(
            "DELETE FROM long_term_keys WHERE id = ?", (key_id,)
        )

        if result.rowcount == 0:
            raise HTTPException(404, f"Key {key_id} not found")

    return {"ok": True, "deleted_id": key_id}


@router.post("/check-duplicate", response_model=CheckDuplicateResponse)
async def check_duplicate(req: CheckDuplicateRequest) -> CheckDuplicateResponse:
    """Check if keys already exist in long-term storage (by hash).

    Does not require authentication (read-only operation).

    Args:
        req: Request with keys to check

    Returns:
        Duplicate check results for each key
    """
    manager = LongTermKeyManager()
    duplicates = []

    for key_data in req.keys:
        key_hash = manager.hash_key(key_data.strip())
        existing_id = manager.check_duplicate(key_hash)

        duplicates.append(
            {
                "key_hash": key_hash,
                "exists": existing_id is not None,
                "key_id": existing_id,
            }
        )

    return CheckDuplicateResponse(duplicates=duplicates)
