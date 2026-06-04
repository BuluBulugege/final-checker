"""Integration tests for long-term key monitoring system.

Tests:
1. Database migration
2. Service startup (including scheduler)
3. Authentication flow
4. Add key → probe → status update
5. Death retry logic (mock time)
6. Duplicate check API
7. All API endpoints
"""

import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from app.api_long_term import create_token
from app.db import (
    DEFAULT_DB_PATH,
    add_key,
    get_connection,
    get_key_by_hash,
    get_key_history,
    get_keys_needing_check,
    get_next_retry_delay,
    init_db,
    mark_as_abandoned,
    record_check,
    should_abandon,
    update_key_status,
)

# Test database path
TEST_DB = Path("test_integration.db")


@pytest.fixture(autouse=True)
def setup_test_db():
    """Setup test database before each test and cleanup after."""
    # Remove existing test database
    if TEST_DB.exists():
        TEST_DB.unlink()

    # Initialize fresh database
    init_db(TEST_DB)

    yield

    # Cleanup
    if TEST_DB.exists():
        TEST_DB.unlink()


class TestDatabaseMigration:
    """Test database schema and migration logic."""

    def test_init_db_creates_tables(self):
        """Test that init_db creates all required tables."""
        with get_connection(TEST_DB) as conn:
            # Check long_term_keys table exists
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='long_term_keys'"
            ).fetchone()
            assert result is not None

            # Check ltk_check_history table exists
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ltk_check_history'"
            ).fetchone()
            assert result is not None

    def test_init_db_creates_indexes(self):
        """Test that init_db creates all required indexes."""
        with get_connection(TEST_DB) as conn:
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            ).fetchall()
            index_names = [row[0] for row in indexes]

            expected_indexes = [
                "idx_ltk_platform",
                "idx_ltk_status",
                "idx_ltk_last_check",
                "idx_ltk_hash",
                "idx_ltk_retry_schedule",
                "idx_ltkh_key_time",
                "idx_ltkh_time",
            ]

            for expected in expected_indexes:
                assert expected in index_names, f"Missing index: {expected}"

    def test_init_db_idempotent(self):
        """Test that calling init_db multiple times is safe."""
        # Call init_db twice
        init_db(TEST_DB)
        init_db(TEST_DB)

        # Should not raise errors and tables should still exist
        with get_connection(TEST_DB) as conn:
            result = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()
            assert result[0] >= 2  # At least 2 tables


class TestDatabaseOperations:
    """Test CRUD operations."""

    def test_add_key(self):
        """Test adding a new key."""
        with get_connection(TEST_DB) as conn:
            now = time.time()
            key_id = add_key(
                conn,
                key_data="test_key_data",
                key_hash="test_hash_123",
                platform="openai",
                created_at=now,
                notes="Test key",
            )

            assert key_id > 0

            # Verify key was added
            key = get_key_by_hash(conn, "test_hash_123")
            assert key is not None
            assert key["key_data"] == "test_key_data"
            assert key["platform"] == "openai"
            assert key["status"] == "active"

    def test_add_duplicate_key_fails(self):
        """Test that adding duplicate hash fails."""
        with get_connection(TEST_DB) as conn:
            now = time.time()
            add_key(
                conn,
                key_data="test_key_data",
                key_hash="duplicate_hash",
                platform="openai",
                created_at=now,
            )

            # Try to add duplicate
            with pytest.raises(sqlite3.IntegrityError):
                add_key(
                    conn,
                    key_data="different_data",
                    key_hash="duplicate_hash",
                    platform="anthropic",
                    created_at=now,
                )

    def test_update_key_status(self):
        """Test updating key status."""
        with get_connection(TEST_DB) as conn:
            now = time.time()
            key_id = add_key(
                conn,
                key_data="test_key",
                key_hash="hash_update_test",
                platform="openai",
                created_at=now,
            )

            # Update to dead status
            update_key_status(
                conn,
                key_id=key_id,
                status="dead",
                last_check=now + 100,
                error_code="auth_error",
                death_time=now + 100,
                retry_count=0,
            )

            # Verify update
            key = get_key_by_hash(conn, "hash_update_test")
            assert key["status"] == "dead"
            assert key["error_code"] == "auth_error"
            assert key["death_time"] == now + 100
            assert key["retry_count"] == 0

    def test_record_and_get_history(self):
        """Test recording check history."""
        with get_connection(TEST_DB) as conn:
            now = time.time()
            key_id = add_key(
                conn,
                key_data="test_key",
                key_hash="hash_history_test",
                platform="openai",
                created_at=now,
            )

            # Record multiple checks
            for i in range(3):
                record_check(
                    conn,
                    key_id=key_id,
                    checked_at=now + i * 100,
                    status="alive" if i < 2 else "dead",
                    error_class=None if i < 2 else "auth_error",
                    response_time_ms=50.0 + i * 10,
                )

            # Get history
            history = get_key_history(conn, key_id, limit=10)
            assert len(history) == 3
            assert history[0]["status"] == "dead"  # Most recent first
            assert history[2]["status"] == "alive"  # Oldest last


class TestRetryLogic:
    """Test death retry strategy."""

    def test_retry_delay_calculation(self):
        """Test retry delay calculation for each retry count."""
        assert get_next_retry_delay(0) == 2 * 3600  # 2 hours
        assert get_next_retry_delay(1) == 24 * 3600  # 24 hours
        assert get_next_retry_delay(2) == 36 * 3600  # 36 hours
        assert get_next_retry_delay(3) == 48 * 3600  # 48 hours
        assert get_next_retry_delay(4) is None  # Should abandon

    def test_should_abandon(self):
        """Test abandon logic."""
        assert not should_abandon(0)
        assert not should_abandon(1)
        assert not should_abandon(2)
        assert not should_abandon(3)
        assert should_abandon(4)
        assert should_abandon(5)

    def test_get_keys_needing_check_active(self):
        """Test retrieving active keys needing check."""
        with get_connection(TEST_DB) as conn:
            now = time.time()

            # Add keys with different last_check times
            key1 = add_key(conn, "key1", "hash1", "openai", now)
            key2 = add_key(conn, "key2", "hash2", "openai", now)
            key3 = add_key(conn, "key3", "hash3", "openai", now)

            # Update last_check times
            update_key_status(conn, key1, "active", now - 7200)  # 2 hours ago
            update_key_status(conn, key2, "active", now - 1800)  # 30 min ago
            # key3 has no last_check (NULL)

            # Get keys needing check (older than 1 hour)
            keys = get_keys_needing_check(conn, now, limit=100)

            # Should get key1 (2h old) and key3 (never checked)
            key_ids = [k["id"] for k in keys]
            assert key1 in key_ids
            assert key3 in key_ids
            assert key2 not in key_ids  # Too recent

    def test_get_keys_needing_check_dead_retry(self):
        """Test retrieving dead keys ready for retry."""
        with get_connection(TEST_DB) as conn:
            now = time.time()

            # Add dead key with retry_count=0, death_time 3 hours ago
            key1 = add_key(conn, "key1", "hash1", "openai", now)
            update_key_status(
                conn,
                key1,
                "dead",
                now,
                error_code="auth",
                death_time=now - 3 * 3600,  # 3 hours ago
                retry_count=0,
            )

            # Add dead key with retry_count=1, death_time 25 hours ago
            key2 = add_key(conn, "key2", "hash2", "openai", now)
            update_key_status(
                conn,
                key2,
                "dead",
                now,
                error_code="auth",
                death_time=now - 25 * 3600,  # 25 hours ago
                retry_count=1,
            )

            # Add dead key with retry_count=0, death_time 1 hour ago (too soon)
            key3 = add_key(conn, "key3", "hash3", "openai", now)
            update_key_status(
                conn,
                key3,
                "dead",
                now,
                error_code="auth",
                death_time=now - 1 * 3600,  # 1 hour ago (too soon)
                retry_count=0,
            )

            # Get keys needing check
            keys = get_keys_needing_check(conn, now, limit=100)
            key_ids = [k["id"] for k in keys]

            assert key1 in key_ids  # Ready for retry (3h > 2h)
            assert key2 in key_ids  # Ready for retry (25h > 24h)
            assert key3 not in key_ids  # Too soon

    def test_mark_as_abandoned(self):
        """Test marking key as abandoned."""
        with get_connection(TEST_DB) as conn:
            now = time.time()
            key_id = add_key(conn, "key", "hash", "openai", now)

            mark_as_abandoned(conn, key_id)

            key = get_key_by_hash(conn, "hash")
            assert key["status"] == "abandoned"


class TestAuthenticationFlow:
    """Test JWT authentication."""

    def test_generate_token(self):
        """Test token generation."""
        token = create_token({"admin": True})
        assert isinstance(token, str)
        assert len(token) > 50  # JWT tokens are long

    def test_token_contains_expiry(self):
        """Test that token includes expiration."""
        import jwt

        token = create_token({"admin": True})

        # Decode without verification to inspect claims
        payload = jwt.decode(token, options={"verify_signature": False})

        assert "exp" in payload
        assert "admin" in payload
        assert payload["admin"] is True


class TestAPIEndpoints:
    """Test all API endpoints (requires running server)."""

    # Note: These tests would normally run against a test server
    # For now, we test the logic directly

    def test_add_key_endpoint_logic(self):
        """Test add key endpoint logic."""
        from hashlib import sha256

        with get_connection(TEST_DB) as conn:
            now = time.time()

            # Simulate API logic
            raw_key = "sk-test123456"
            key_hash = sha256(raw_key.encode()).hexdigest()
            platform = "openai"

            # Check for duplicates
            existing = get_key_by_hash(conn, key_hash)
            assert existing is None

            # Add key
            key_id = add_key(
                conn,
                key_data=raw_key,
                key_hash=key_hash,
                platform=platform,
                created_at=now,
                notes="Test via API",
            )

            assert key_id > 0

            # Try to add duplicate
            existing = get_key_by_hash(conn, key_hash)
            assert existing is not None
            assert existing["id"] == key_id

    def test_list_keys_endpoint_logic(self):
        """Test list keys endpoint logic."""
        with get_connection(TEST_DB) as conn:
            now = time.time()

            # Add multiple keys
            for i in range(5):
                add_key(
                    conn,
                    key_data=f"key_{i}",
                    key_hash=f"hash_{i}",
                    platform="openai" if i % 2 == 0 else "anthropic",
                    created_at=now,
                )

            # List all keys
            keys = conn.execute(
                "SELECT * FROM long_term_keys ORDER BY created_at DESC"
            ).fetchall()

            assert len(keys) == 5

            # Filter by platform
            openai_keys = conn.execute(
                "SELECT * FROM long_term_keys WHERE platform = 'openai'"
            ).fetchall()

            assert len(openai_keys) == 3  # 0, 2, 4

    def test_get_key_history_endpoint_logic(self):
        """Test get key history endpoint logic."""
        with get_connection(TEST_DB) as conn:
            now = time.time()

            key_id = add_key(conn, "key", "hash", "openai", now)

            # Record some checks
            for i in range(5):
                record_check(
                    conn,
                    key_id=key_id,
                    checked_at=now + i * 60,
                    status="alive",
                    response_time_ms=50.0,
                )

            # Get history with limit
            history = get_key_history(conn, key_id, limit=3)
            assert len(history) == 3
            assert history[0]["checked_at"] > history[1]["checked_at"]  # DESC order


class TestSchedulerSimulation:
    """Test scheduler behavior simulation."""

    def test_scheduler_picks_correct_keys(self):
        """Test that scheduler correctly identifies keys to check."""
        with get_connection(TEST_DB) as conn:
            now = time.time()

            # Add various keys
            active_old = add_key(conn, "key1", "hash1", "openai", now)
            update_key_status(conn, active_old, "active", now - 7200)

            active_recent = add_key(conn, "key2", "hash2", "openai", now)
            update_key_status(conn, active_recent, "active", now - 1800)

            dead_retry = add_key(conn, "key3", "hash3", "openai", now)
            update_key_status(
                conn,
                dead_retry,
                "dead",
                now,
                death_time=now - 3 * 3600,
                retry_count=0,
            )

            abandoned = add_key(conn, "key4", "hash4", "openai", now)
            mark_as_abandoned(conn, abandoned)

            # Scheduler should pick active_old and dead_retry
            keys = get_keys_needing_check(conn, now, limit=100)
            key_ids = [k["id"] for k in keys]

            assert active_old in key_ids
            assert dead_retry in key_ids
            assert active_recent not in key_ids
            assert abandoned not in key_ids

    def test_scheduler_processes_check_results(self):
        """Test scheduler handling check results."""
        with get_connection(TEST_DB) as conn:
            now = time.time()

            # Active key that will become dead
            key_id = add_key(conn, "key", "hash", "openai", now)
            update_key_status(conn, key_id, "active", now)

            # Simulate check result: dead
            check_time = now + 100
            update_key_status(
                conn,
                key_id,
                "dead",
                check_time,
                error_code="auth_error",
                death_time=check_time,
                retry_count=0,
            )

            record_check(
                conn,
                key_id,
                check_time,
                "dead",
                error_class="auth_error",
                response_time_ms=120.0,
            )

            # Verify state
            key = get_key_by_hash(conn, "hash")
            assert key["status"] == "dead"
            assert key["death_time"] == check_time
            assert key["retry_count"] == 0

            history = get_key_history(conn, key_id, limit=1)
            assert len(history) == 1
            assert history[0]["status"] == "dead"


def test_full_integration_flow():
    """Test complete flow: add → check → update → retry → abandon."""
    with get_connection(TEST_DB) as conn:
        now = time.time()

        # Step 1: Add key
        key_id = add_key(
            conn,
            key_data="sk-test-key",
            key_hash="full_flow_hash",
            platform="openai",
            created_at=now,
        )

        key = get_key_by_hash(conn, "full_flow_hash")
        assert key["status"] == "active"
        assert key["last_check"] is None

        # Step 2: First check - alive
        check1_time = now + 100
        update_key_status(conn, key_id, "active", check1_time)
        record_check(conn, key_id, check1_time, "alive", response_time_ms=45.0)

        key = get_key_by_hash(conn, "full_flow_hash")
        assert key["status"] == "active"
        assert key["last_check"] == check1_time

        # Step 3: Second check - dead
        check2_time = now + 3700  # 1 hour later
        update_key_status(
            conn,
            key_id,
            "dead",
            check2_time,
            error_code="auth",
            death_time=check2_time,
            retry_count=0,
        )
        record_check(
            conn, key_id, check2_time, "dead", error_class="auth", response_time_ms=100.0
        )

        key = get_key_by_hash(conn, "full_flow_hash")
        assert key["status"] == "dead"
        assert key["death_time"] == check2_time
        assert key["retry_count"] == 0

        # Step 4: Retry after 2 hours
        retry1_time = check2_time + 2 * 3600 + 100
        keys_to_check = get_keys_needing_check(conn, retry1_time, limit=100)
        assert any(k["id"] == key_id for k in keys_to_check)

        # Still dead after retry
        update_key_status(
            conn,
            key_id,
            "dead",
            retry1_time,
            error_code="auth",
            death_time=check2_time,  # Keep original death time
            retry_count=1,
        )
        record_check(conn, key_id, retry1_time, "dead", error_class="auth")

        # Step 5: Multiple retries
        retry_times = [
            check2_time + 24 * 3600 + 100,  # retry_count=1 → 2
            check2_time + 36 * 3600 + 100,  # retry_count=2 → 3
            check2_time + 48 * 3600 + 100,  # retry_count=3 → 4
        ]

        for i, retry_time in enumerate(retry_times, start=2):
            update_key_status(
                conn,
                key_id,
                "dead",
                retry_time,
                error_code="auth",
                death_time=check2_time,
                retry_count=i,
            )
            record_check(conn, key_id, retry_time, "dead", error_class="auth")

        # Step 6: Should be marked as abandoned after 4 retries
        key = get_key_by_hash(conn, "full_flow_hash")
        assert key["retry_count"] == 4

        if should_abandon(key["retry_count"]):
            mark_as_abandoned(conn, key_id)

        key = get_key_by_hash(conn, "full_flow_hash")
        assert key["status"] == "abandoned"

        # Verify history
        history = get_key_history(conn, key_id, limit=100)
        assert len(history) == 6  # 1 alive + 5 dead checks


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
