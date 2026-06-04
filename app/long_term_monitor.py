"""Long-term key monitoring core logic.

Manages the lifecycle of API keys in long-term storage:
- Add keys with deduplication
- Schedule periodic health checks (every hour for active keys)
- Implement death retry strategy (2h, 24h, 36h, 48h, then abandon)
- Record check history for analytics
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

from app.config import settings
from app.db import (
    add_key as db_add_key,
    get_connection,
    get_key_by_hash,
    get_keys_needing_check,
    init_db,
    mark_as_abandoned,
    record_check,
    should_abandon,
    update_key_status,
)
from app.models import CheckMode, ErrorClass, KeyResult, KeyStatus
from app.plugins.base import CheckContext, mask_key
from app.plugins.registry import dispatch


class LongTermKeyManager:
    """Manager for long-term key monitoring operations."""

    def __init__(self, db_path: str | None = None):
        """Initialize the key manager.

        Args:
            db_path: Optional custom database path. Uses default if None.
        """
        from pathlib import Path

        self.db_path = Path(db_path) if db_path else None
        # Ensure database is initialized
        init_db(self.db_path) if self.db_path else init_db()

    def hash_key(self, key_data: str) -> str:
        """Generate SHA256 hash of key data for deduplication.

        Args:
            key_data: Raw key string or JSON

        Returns:
            Hex-encoded SHA256 hash
        """
        return hashlib.sha256(key_data.encode("utf-8")).hexdigest()

    def check_duplicate(self, key_hash: str) -> int | None:
        """Check if a key with this hash already exists.

        Args:
            key_hash: SHA256 hash to check

        Returns:
            Existing key ID if found, None otherwise
        """
        if self.db_path:
            with get_connection(self.db_path) as conn:
                existing = get_key_by_hash(conn, key_hash)
        else:
            with get_connection() as conn:
                existing = get_key_by_hash(conn, key_hash)

        return existing["id"] if existing else None

    def add_key(
        self,
        key_data: str,
        platform: str,
        notes: str | None = None,
    ) -> tuple[int, bool]:
        """Add a new key to long-term monitoring.

        Args:
            key_data: Raw API key or service account JSON
            platform: Platform identifier (gemini/openai/anthropic/gcp)
            notes: Optional user notes

        Returns:
            Tuple of (key_id, is_new) where is_new indicates if key was newly added

        Raises:
            ValueError: If platform is not recognized
        """
        valid_platforms = {"gemini", "openai", "anthropic", "gcp"}
        if platform not in valid_platforms:
            raise ValueError(
                f"Invalid platform '{platform}'. Must be one of: {valid_platforms}"
            )

        key_hash = self.hash_key(key_data)

        # Check for duplicate
        existing_id = self.check_duplicate(key_hash)
        if existing_id:
            return (existing_id, False)

        # Add new key
        current_time = time.time()
        if self.db_path:
            with get_connection(self.db_path) as conn:
                key_id = db_add_key(
                    conn,
                    key_data=key_data,
                    key_hash=key_hash,
                    platform=platform,
                    created_at=current_time,
                    notes=notes,
                )
        else:
            with get_connection() as conn:
                key_id = db_add_key(
                    conn,
                    key_data=key_data,
                    key_hash=key_hash,
                    platform=platform,
                    created_at=current_time,
                    notes=notes,
                )

        return (key_id, True)

    async def check_key(self, key_id: int, key_data: str) -> dict[str, Any]:
        """Perform health check on a single key using the plugin system.

        Args:
            key_id: Database key ID
            key_data: Raw key string

        Returns:
            Dict with check results:
            {
                "key_id": int,
                "status": str,  # alive/dead/error
                "error_class": str | None,
                "error_detail": str | None,
                "response_time_ms": float | None,
                "checked_at": float,
            }
        """
        # Dispatch to appropriate plugin
        plugin = dispatch(key_data)
        if not plugin:
            return {
                "key_id": key_id,
                "status": "error",
                "error_class": "unsupported",
                "error_detail": "No plugin matched this key",
                "response_time_ms": None,
                "checked_at": time.time(),
            }

        # Create a minimal KeyResult for the plugin to populate
        result = KeyResult(
            index=0,
            masked_key=mask_key(key_data),
            mode=CheckMode.HEALTH,
        )

        # Create check context
        async with httpx.AsyncClient(timeout=30.0) as client:

            async def noop_progress(frac: float, label: str | None) -> None:
                pass

            ctx = CheckContext(
                client=client,
                settings=settings,
                mode=CheckMode.HEALTH,
                full_load=False,
                progress=noop_progress,
            )

            start_time = time.time()
            try:
                # Use health_check for long-term monitoring (lighter than grade_check)
                await plugin.health_check(key_data, result, ctx)
                elapsed_ms = (time.time() - start_time) * 1000

                # Determine status from KeyResult
                if result.status == KeyStatus.ALIVE:
                    check_status = "alive"
                    error_class = None
                    error_detail = None
                elif result.status == KeyStatus.DEAD:
                    check_status = "dead"
                    # Try to extract error class from details
                    error_class = (
                        result.details.get("error_class")
                        if result.details
                        else None
                    )
                    error_detail = result.error
                else:
                    check_status = "error"
                    error_class = None
                    error_detail = result.error

                return {
                    "key_id": key_id,
                    "status": check_status,
                    "error_class": error_class,
                    "error_detail": error_detail,
                    "response_time_ms": elapsed_ms,
                    "checked_at": time.time(),
                }

            except Exception as exc:
                elapsed_ms = (time.time() - start_time) * 1000
                return {
                    "key_id": key_id,
                    "status": "error",
                    "error_class": "exception",
                    "error_detail": f"{type(exc).__name__}: {str(exc)}",
                    "response_time_ms": elapsed_ms,
                    "checked_at": time.time(),
                }

    def update_status(
        self,
        key_id: int,
        status: str,
        error_code: str | None = None,
        checked_at: float | None = None,
    ) -> None:
        """Update key status after a check.

        Args:
            key_id: Database key ID
            status: New status (alive/dead/abandoned)
            error_code: Optional error classification
            checked_at: Check timestamp (defaults to now)

        Handles death retry logic:
        - First death: schedule retry in 2 hours
        - Subsequent retries: 24h, 36h, 48h
        - After 4 failed retries: mark as abandoned
        """
        current_time = checked_at or time.time()

        if self.db_path:
            with get_connection(self.db_path) as conn:
                self._update_status_internal(
                    conn, key_id, status, error_code, current_time
                )
        else:
            with get_connection() as conn:
                self._update_status_internal(
                    conn, key_id, status, error_code, current_time
                )

    def _update_status_internal(
        self,
        conn,
        key_id: int,
        status: str,
        error_code: str | None,
        current_time: float,
    ) -> None:
        """Internal helper for update_status with connection."""
        # Get current key state
        current = conn.execute(
            "SELECT status, death_time, retry_count FROM long_term_keys WHERE id = ?",
            (key_id,),
        ).fetchone()

        if not current:
            return

        old_status = current["status"]
        death_time = current["death_time"]
        retry_count = current["retry_count"]

        if status == "dead":
            if old_status == "active":
                # First death - record death time and reset retry count
                death_time = current_time
                retry_count = 0
            elif old_status == "dead":
                # Still dead after retry - increment retry count
                retry_count += 1
                # Check if should abandon
                if should_abandon(retry_count):
                    mark_as_abandoned(conn, key_id)
                    return

            update_key_status(
                conn,
                key_id=key_id,
                status="dead",
                last_check=current_time,
                error_code=error_code,
                death_time=death_time,
                retry_count=retry_count,
            )

        elif status == "alive":
            # Key recovered - reset to active state
            update_key_status(
                conn,
                key_id=key_id,
                status="active",
                last_check=current_time,
                error_code=None,
                death_time=None,
                retry_count=0,
            )

        else:
            # Error or other status - just update last_check
            update_key_status(
                conn,
                key_id=key_id,
                status=old_status,  # Keep current status
                last_check=current_time,
                error_code=error_code,
            )

    def get_keys_need_check(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get keys that need checking based on monitoring schedule.

        Returns both:
        - Active keys not checked in the last hour
        - Dead keys ready for retry based on death retry strategy

        Args:
            limit: Maximum number of keys to return

        Returns:
            List of key records as dicts
        """
        current_time = time.time()

        if self.db_path:
            with get_connection(self.db_path) as conn:
                rows = get_keys_needing_check(conn, current_time, limit)
        else:
            with get_connection() as conn:
                rows = get_keys_needing_check(conn, current_time, limit)

        return [dict(row) for row in rows]

    def calculate_next_check_time(
        self, status: str, retry_count: int, death_time: float | None = None
    ) -> float | None:
        """Calculate when a key should be checked next.

        Args:
            status: Current key status (active/dead/abandoned)
            retry_count: Number of retry attempts for dead keys
            death_time: Timestamp when key first died (for dead keys)

        Returns:
            Unix timestamp of next check, or None if abandoned
        """
        current_time = time.time()

        if status == "abandoned":
            return None

        if status == "active":
            # Active keys: check every hour
            return current_time + 3600

        if status == "dead":
            if death_time is None or should_abandon(retry_count):
                return None

            # Death retry schedule
            from app.db import get_next_retry_delay

            delay = get_next_retry_delay(retry_count)
            if delay is None:
                return None

            return death_time + delay

        return None

    def record_check_result(
        self,
        key_id: int,
        checked_at: float,
        status: str,
        error_class: str | None = None,
        error_detail: str | None = None,
        response_time_ms: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record a check result in history.

        Args:
            key_id: Database key ID
            checked_at: Check timestamp
            status: Check outcome (alive/dead/error)
            error_class: Error classification
            error_detail: Detailed error message
            response_time_ms: Response time in milliseconds
            details: Additional structured details
        """
        details_json = json.dumps(details) if details else None

        if self.db_path:
            with get_connection(self.db_path) as conn:
                record_check(
                    conn,
                    key_id=key_id,
                    checked_at=checked_at,
                    status=status,
                    error_class=error_class,
                    error_detail=error_detail,
                    response_time_ms=response_time_ms,
                    details=details_json,
                )
        else:
            with get_connection() as conn:
                record_check(
                    conn,
                    key_id=key_id,
                    checked_at=checked_at,
                    status=status,
                    error_class=error_class,
                    error_detail=error_detail,
                    response_time_ms=response_time_ms,
                    details=details_json,
                )
