"""Database layer for long-term key monitoring.

SQLite schema with safe migrations. All schema changes happen in initDb().
Uses the safe migration pattern: CREATE new → INSERT OR IGNORE → DROP old → RENAME.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

# Default database path
DEFAULT_DB_PATH = Path("data.db")


@contextmanager
def get_connection(db_path: Path = DEFAULT_DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database connections with proper cleanup."""
    conn = sqlite3.Connection(db_path)
    conn.row_factory = sqlite3.Row  # Enable dict-like access
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Initialize database schema with safe migrations.

    Migration strategy:
    - CREATE TABLE IF NOT EXISTS for new tables
    - For schema changes: CREATE new table → INSERT OR IGNORE → DROP old → RENAME
    - Wrap in try/catch to handle concurrent migrations
    - Always check column existence before ALTER TABLE ADD COLUMN
    """
    with get_connection(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")

        try:
            # ============================================================
            # LONG-TERM KEY MONITORING TABLE
            # ============================================================
            conn.execute("""
                CREATE TABLE IF NOT EXISTS long_term_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    -- Key identity
                    key_data TEXT NOT NULL,  -- Encrypted or masked key
                    hash TEXT UNIQUE NOT NULL,  -- SHA256 for deduplication
                    platform TEXT NOT NULL,  -- gemini/openai/anthropic/gcp

                    -- Status tracking
                    status TEXT NOT NULL DEFAULT 'active',  -- active/dead/abandoned
                    last_check REAL,  -- Unix timestamp of last check
                    error_code TEXT,  -- Last error classification
                    death_time REAL,  -- Unix timestamp when key first died
                    retry_count INTEGER DEFAULT 0,  -- Number of retry attempts since death

                    -- Metadata
                    created_at REAL NOT NULL,  -- Unix timestamp
                    notes TEXT  -- Optional user notes
                )
            """)

            # Indexes for efficient queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ltk_platform
                ON long_term_keys(platform)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ltk_status
                ON long_term_keys(status)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ltk_last_check
                ON long_term_keys(last_check)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ltk_hash
                ON long_term_keys(hash)
            """)

            # Index for finding keys needing retry
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ltk_retry_schedule
                ON long_term_keys(status, death_time, retry_count)
                WHERE status = 'dead'
            """)

            # ============================================================
            # CHECK HISTORY TABLE
            # ============================================================
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ltk_check_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_id INTEGER NOT NULL,
                    checked_at REAL NOT NULL,

                    -- Check outcome
                    status TEXT NOT NULL,  -- alive/dead/error
                    error_class TEXT,  -- auth/rate_limit/billing/permission/etc
                    error_detail TEXT,

                    -- Performance
                    response_time_ms REAL,

                    -- Additional details (JSON)
                    details TEXT,  -- JSON blob for extra info

                    FOREIGN KEY (key_id) REFERENCES long_term_keys(id) ON DELETE CASCADE
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ltkh_key_time
                ON ltk_check_history(key_id, checked_at DESC)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ltkh_time
                ON ltk_check_history(checked_at DESC)
            """)

        except sqlite3.OperationalError as e:
            # Handle concurrent migrations gracefully
            if "already exists" not in str(e):
                raise


def get_next_retry_delay(retry_count: int) -> float:
    """Calculate next retry delay based on death retry strategy.

    Strategy:
    - Initial death (0): 2 hours
    - 1st retry: 24 hours
    - 2nd retry: 36 hours
    - 3rd retry: 48 hours
    - After 3 retries: mark as abandoned

    Returns delay in seconds, or None if should be abandoned.
    """
    delays = {
        0: 2 * 3600,      # 2 hours
        1: 24 * 3600,     # 24 hours
        2: 36 * 3600,     # 36 hours
        3: 48 * 3600,     # 48 hours
    }
    return delays.get(retry_count)


def should_abandon(retry_count: int) -> bool:
    """Check if key should be marked as abandoned based on retry count."""
    return retry_count >= 4


# ============================================================
# CRUD OPERATIONS
# ============================================================

def add_key(
    conn: sqlite3.Connection,
    key_data: str,
    key_hash: str,
    platform: str,
    created_at: float,
    notes: str | None = None,
) -> int:
    """Add a new long-term key to monitor.

    Returns the new key ID.
    Raises sqlite3.IntegrityError if hash already exists.
    """
    cursor = conn.execute(
        """
        INSERT INTO long_term_keys (key_data, hash, platform, status, created_at, notes)
        VALUES (?, ?, ?, 'active', ?, ?)
        """,
        (key_data, key_hash, platform, created_at, notes),
    )
    return cursor.lastrowid


def update_key_status(
    conn: sqlite3.Connection,
    key_id: int,
    status: str,
    last_check: float,
    error_code: str | None = None,
    death_time: float | None = None,
    retry_count: int | None = None,
) -> None:
    """Update key status after a check."""
    updates = ["status = ?", "last_check = ?", "error_code = ?"]
    params = [status, last_check, error_code]

    if death_time is not None:
        updates.append("death_time = ?")
        params.append(death_time)

    if retry_count is not None:
        updates.append("retry_count = ?")
        params.append(retry_count)

    params.append(key_id)

    conn.execute(
        f"UPDATE long_term_keys SET {', '.join(updates)} WHERE id = ?",
        params,
    )


def record_check(
    conn: sqlite3.Connection,
    key_id: int,
    checked_at: float,
    status: str,
    error_class: str | None = None,
    error_detail: str | None = None,
    response_time_ms: float | None = None,
    details: str | None = None,
) -> None:
    """Record a check result in history."""
    conn.execute(
        """
        INSERT INTO ltk_check_history
        (key_id, checked_at, status, error_class, error_detail, response_time_ms, details)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (key_id, checked_at, status, error_class, error_detail, response_time_ms, details),
    )


def get_keys_needing_check(
    conn: sqlite3.Connection,
    current_time: float,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Get active keys that need checking or dead keys ready for retry.

    Returns keys where:
    - status = 'active' and last_check is NULL or old enough
    - status = 'dead' and enough time has passed for the next retry
    """
    # Active keys needing regular check (check every hour)
    active_keys = conn.execute(
        """
        SELECT * FROM long_term_keys
        WHERE status = 'active'
        AND (last_check IS NULL OR last_check < ?)
        ORDER BY last_check ASC NULLS FIRST
        LIMIT ?
        """,
        (current_time - 3600, limit),
    ).fetchall()

    # Dead keys ready for retry based on death retry strategy
    dead_keys = conn.execute(
        """
        SELECT * FROM long_term_keys
        WHERE status = 'dead'
        AND retry_count < 4
        AND death_time IS NOT NULL
        AND (
            (retry_count = 0 AND death_time < ?) OR  -- 2h after death
            (retry_count = 1 AND death_time < ?) OR  -- 24h after death
            (retry_count = 2 AND death_time < ?) OR  -- 36h after death
            (retry_count = 3 AND death_time < ?)     -- 48h after death
        )
        ORDER BY death_time ASC
        LIMIT ?
        """,
        (
            current_time - 2 * 3600,   # 2 hours
            current_time - 24 * 3600,  # 24 hours
            current_time - 36 * 3600,  # 36 hours
            current_time - 48 * 3600,  # 48 hours
            limit,
        ),
    ).fetchall()

    return active_keys + dead_keys


def get_key_history(
    conn: sqlite3.Connection,
    key_id: int,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Get check history for a specific key."""
    return conn.execute(
        """
        SELECT * FROM ltk_check_history
        WHERE key_id = ?
        ORDER BY checked_at DESC
        LIMIT ?
        """,
        (key_id, limit),
    ).fetchall()


def get_key_by_hash(conn: sqlite3.Connection, key_hash: str) -> sqlite3.Row | None:
    """Get key by hash (for deduplication)."""
    return conn.execute(
        "SELECT * FROM long_term_keys WHERE hash = ?",
        (key_hash,),
    ).fetchone()


def mark_as_abandoned(conn: sqlite3.Connection, key_id: int) -> None:
    """Mark a key as abandoned after too many failed retries."""
    conn.execute(
        "UPDATE long_term_keys SET status = 'abandoned' WHERE id = ?",
        (key_id,),
    )
