"""Background scheduler for long-term key monitoring.

Runs periodic health checks on keys in long-term storage:
- Every 10 minutes check loop
- Death retry strategy: 2h → 24h → 36h → 48h → abandon
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.db import get_connection, get_keys_needing_check
from app.long_term_monitor import LongTermKeyManager

logger = logging.getLogger(__name__)


class BackgroundScheduler:
    """Background scheduler for long-term key monitoring."""

    def __init__(self, check_interval_seconds: int = 600):
        """Initialize the scheduler.

        Args:
            check_interval_seconds: How often to run the check loop (default: 600s = 10 min)
        """
        self.check_interval = check_interval_seconds
        self.manager = LongTermKeyManager()
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the background scheduler."""
        if self._running:
            logger.warning("Scheduler already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info(f"Long-term key scheduler started (interval: {self.check_interval}s)")

    async def stop(self) -> None:
        """Stop the background scheduler."""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Long-term key scheduler stopped")

    async def _check_loop(self) -> None:
        """Main check loop that runs every 10 minutes."""
        while self._running:
            try:
                await self._run_checks()
            except Exception as e:
                logger.error(f"Error in check loop: {e}", exc_info=True)

            # Wait for next interval
            await asyncio.sleep(self.check_interval)

    async def _run_checks(self) -> None:
        """Run health checks on all keys that need checking.

        This implements the core scheduling logic:
        1. Get keys needing check (get_keys_need_check)
        2. Concurrently check each key (check_key)
        3. Update status and next_check_time
        4. Handle death retry logic:
           - Initial death: continuous 2 hours dead → mark death_time
           - Retry at: 24h/36h/48h (retry_count++)
           - If still dead after 48h → status=abandoned, stop monitoring
        """
        current_time = time.time()

        # Get keys that need checking
        keys = self.manager.get_keys_need_check(limit=1000)

        if not keys:
            logger.debug("No keys need checking")
            return

        logger.info(f"Checking {len(keys)} keys")

        # Check keys concurrently with rate limiting
        semaphore = asyncio.Semaphore(5)  # Max 5 concurrent checks
        results = []

        async def check_one(key_record: dict[str, Any]) -> None:
            """Check a single key and update its status."""
            async with semaphore:
                try:
                    # Perform the health check
                    result = await self.manager.check_key(
                        key_record["id"],
                        key_record["key_data"]
                    )

                    key_id = key_record["id"]
                    platform = key_record.get("platform", "unknown")
                    status = result.get("status", "error")
                    error_class = result.get("error_class")
                    error_detail = result.get("error_detail")
                    response_time_ms = result.get("response_time_ms")
                    checked_at = result.get("checked_at", current_time)

                    # Log the result
                    logger.debug(
                        f"Checked key {key_id} ({platform}): "
                        f"{status} {f'({error_class})' if error_class else ''}"
                    )

                    # Record check in history
                    self.manager.record_check_result(
                        key_id=key_id,
                        checked_at=checked_at,
                        status=status,
                        error_class=error_class,
                        error_detail=error_detail,
                        response_time_ms=response_time_ms,
                    )

                    # Update key status with death retry logic
                    self.manager.update_status(
                        key_id=key_id,
                        status=status,
                        error_code=error_class,
                        checked_at=checked_at,
                    )

                    results.append({
                        "key_id": key_id,
                        "platform": platform,
                        "status": status,
                        "error_class": error_class,
                    })

                except Exception as e:
                    logger.error(f"Failed to check key {key_record['id']}: {e}", exc_info=True)
                    # Record error in history
                    try:
                        self.manager.record_check_result(
                            key_id=key_record["id"],
                            checked_at=current_time,
                            status="error",
                            error_class="exception",
                            error_detail=f"{type(e).__name__}: {str(e)}",
                        )
                    except Exception as record_error:
                        logger.error(f"Failed to record check error: {record_error}")

        # Execute all checks concurrently
        await asyncio.gather(*[check_one(k) for k in keys], return_exceptions=True)

        # Log summary
        if results:
            status_counts = {}
            for r in results:
                status = r["status"]
                status_counts[status] = status_counts.get(status, 0) + 1

            summary = ", ".join(f"{status}: {count}" for status, count in sorted(status_counts.items()))
            logger.info(f"Completed checking {len(keys)} keys ({summary})")
        else:
            logger.info(f"Completed checking {len(keys)} keys")


# Global scheduler instance
_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler()
    return _scheduler


async def start_scheduler() -> None:
    """Start the global scheduler."""
    scheduler = get_scheduler()
    await scheduler.start()


async def stop_scheduler() -> None:
    """Stop the global scheduler."""
    global _scheduler
    if _scheduler:
        await _scheduler.stop()
