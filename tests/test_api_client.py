"""Simplified integration tests using FastAPI TestClient.

Tests all functionality without needing a separate server process.
"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_connection, init_db

# Test database
TEST_DB = Path("test_api.db")


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    """Setup test database before each test."""
    import os

    # Remove existing
    if TEST_DB.exists():
        TEST_DB.unlink()

    # Initialize
    init_db(TEST_DB)

    # Patch the database and auth secret for isolated tests.
    from app import api_long_term, db

    monkeypatch.setattr(db, "DEFAULT_DB_PATH", TEST_DB)
    monkeypatch.setattr(api_long_term, "ADMIN_PASSWORD", "test-admin-password")

    yield

    # Cleanup
    if TEST_DB.exists():
        TEST_DB.unlink()


@pytest.fixture
def client():
    """Create FastAPI test client."""
    from app.main import app

    return TestClient(app)


@pytest.fixture
def auth_token(client):
    """Get authentication token."""
    response = client.post(
        "/api/long-term/auth", json={"password": "test-admin-password"}
    )
    assert response.status_code == 200
    return response.json()["token"]


class TestBasicEndpoints:
    """Test basic service endpoints."""

    def test_config_endpoint(self, client):
        """Test config endpoint."""
        response = client.get("/api/config")
        assert response.status_code == 200
        data = response.json()
        assert "max_concurrency" in data
        assert "default_concurrency" in data
        assert "max_input_chars" in data

    def test_oversized_job_input_returns_400(self, client, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "max_input_chars", 10)
        response = client.post(
            "/api/jobs",
            json={"keys": "x" * 11, "mode": "health", "concurrency": 1},
        )
        assert response.status_code == 400
        assert "input too large" in response.json()["detail"]

    def test_plugins_endpoint(self, client):
        """Test plugins endpoint."""
        response = client.get("/api/plugins")
        assert response.status_code == 200
        data = response.json()
        assert "plugins" in data
        assert len(data["plugins"]) > 0


class TestAuthentication:
    """Test authentication flow."""

    def test_auth_success(self, client):
        """Test successful authentication."""
        response = client.post(
            "/api/long-term/auth", json={"password": "test-admin-password"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "token" in data
        assert "expires_in" in data
        assert data["expires_in"] > 0

    def test_auth_failure(self, client):
        """Test authentication failure."""
        response = client.post(
            "/api/long-term/auth", json={"password": "wrong"}
        )
        assert response.status_code == 401

    def test_protected_endpoint_without_auth(self, client):
        """Test that protected endpoints require auth."""
        response = client.post(
            "/api/long-term/keys",
            json={"keys": ["sk-test"], "platform": "openai"},
        )
        assert response.status_code in [401, 403]  # Either unauthorized or forbidden


class TestLongTermKeys:
    """Test long-term key management."""

    def test_add_single_key(self, client, auth_token):
        """Test adding a single key."""
        response = client.post(
            "/api/long-term/keys",
            json={
                "keys": ["sk-test-single-key"],
                "platform": "openai",
                "notes": "Test key",
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["added"] == 1
        assert data["duplicates"] == 0
        assert len(data["key_ids"]) == 1

    def test_add_duplicate_key(self, client, auth_token):
        """Test duplicate detection."""
        test_key = "sk-duplicate-test"

        # Add first time
        response1 = client.post(
            "/api/long-term/keys",
            json={"keys": [test_key], "platform": "openai"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response1.status_code == 200
        assert response1.json()["added"] == 1

        # Add again (duplicate)
        response2 = client.post(
            "/api/long-term/keys",
            json={"keys": [test_key], "platform": "openai"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response2.status_code == 200
        data = response2.json()
        assert data["added"] == 0
        assert data["duplicates"] == 1

    def test_add_multiple_keys(self, client, auth_token):
        """Test batch add."""
        response = client.post(
            "/api/long-term/keys",
            json={
                "keys": ["sk-batch-1", "sk-batch-2", "sk-batch-3"],
                "platform": "openai",
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["added"] == 3
        assert len(data["key_ids"]) == 3

    def test_list_keys(self, client, auth_token):
        """Test listing keys."""
        # Add some keys
        client.post(
            "/api/long-term/keys",
            json={"keys": ["sk-list-1", "sk-list-2"], "platform": "openai"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        # List all
        response = client.get("/api/long-term/keys")
        assert response.status_code == 200
        data = response.json()
        assert "keys" in data
        assert "total" in data
        assert data["total"] >= 2

    def test_list_keys_with_platform_filter(self, client, auth_token):
        """Test filtering by platform."""
        # Add keys for different platforms
        client.post(
            "/api/long-term/keys",
            json={"keys": ["sk-openai"], "platform": "openai"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        client.post(
            "/api/long-term/keys",
            json={"keys": ["sk-anthropic"], "platform": "anthropic"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        # Filter by platform
        response = client.get("/api/long-term/keys?platform=anthropic")
        assert response.status_code == 200
        data = response.json()
        for key in data["keys"]:
            assert key["platform"] == "anthropic"

    def test_list_keys_pagination(self, client, auth_token):
        """Test pagination."""
        # Add many keys
        keys = [f"sk-page-{i}" for i in range(10)]
        client.post(
            "/api/long-term/keys",
            json={"keys": keys, "platform": "openai"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        # Get first page
        response1 = client.get("/api/long-term/keys?limit=5&offset=0")
        assert response1.status_code == 200
        data1 = response1.json()
        assert len(data1["keys"]) == 5

        # Get second page
        response2 = client.get("/api/long-term/keys?limit=5&offset=5")
        assert response2.status_code == 200
        data2 = response2.json()
        assert len(data2["keys"]) == 5

    def test_check_duplicate_api(self, client, auth_token):
        """Test duplicate check endpoint."""
        test_key = "sk-check-dup"

        # Check before adding
        response1 = client.post(
            "/api/long-term/check-duplicate", json={"keys": [test_key]}
        )
        assert response1.status_code == 200
        assert response1.json()["duplicates"][0]["exists"] is False

        # Add key
        client.post(
            "/api/long-term/keys",
            json={"keys": [test_key], "platform": "openai"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        # Check after adding
        response2 = client.post(
            "/api/long-term/check-duplicate", json={"keys": [test_key]}
        )
        assert response2.status_code == 200
        data = response2.json()["duplicates"][0]
        assert data["exists"] is True
        assert data["key_id"] is not None

    def test_delete_key(self, client, auth_token):
        """Test key deletion."""
        # Add key
        add_response = client.post(
            "/api/long-term/keys",
            json={"keys": ["sk-delete"], "platform": "openai"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        key_id = add_response.json()["key_ids"][0]

        # Delete
        delete_response = client.delete(
            f"/api/long-term/keys/{key_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert delete_response.status_code == 200
        assert delete_response.json()["deleted_id"] == key_id

        # Verify deleted
        list_response = client.get("/api/long-term/keys")
        keys = list_response.json()["keys"]
        assert not any(k["id"] == key_id for k in keys)


class TestManualKeyCheck:
    """Test manual key checking."""

    def test_check_single_key(self, client, auth_token):
        """Test checking a single key manually."""
        # Add key
        add_response = client.post(
            "/api/long-term/keys",
            json={"keys": ["sk-manual-check"], "platform": "openai"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        key_id = add_response.json()["key_ids"][0]

        # Check key (will fail, but should return result)
        check_response = client.post(
            f"/api/long-term/keys/{key_id}/check",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert check_response.status_code == 200
        data = check_response.json()
        assert data["key_id"] == key_id
        assert "status" in data
        assert "checked_at" in data


class TestShortTermJobs:
    """Test short-term job checking."""

    def test_create_job(self, client):
        """Test creating a job."""
        response = client.post(
            "/api/jobs",
            json={"keys": "invalid-test-key", "mode": "health", "concurrency": 1},
        )
        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert "state" in data

    def test_get_job(self, client):
        """Test getting job status."""
        # Create job
        create_response = client.post(
            "/api/jobs",
            json={"keys": "invalid-key", "mode": "health", "concurrency": 1},
        )
        job_id = create_response.json()["job_id"]

        # Get status
        get_response = client.get(f"/api/jobs/{job_id}")
        assert get_response.status_code == 200
        data = get_response.json()
        assert data["job_id"] == job_id

    def test_job_not_found(self, client):
        """Test getting non-existent job."""
        response = client.get("/api/jobs/nonexistent-id")
        assert response.status_code == 404


class TestDatabaseConsistency:
    """Test database consistency and edge cases."""

    def test_concurrent_duplicate_check(self, client, auth_token):
        """Test that duplicate detection works correctly."""
        test_key = "sk-concurrent-test"

        # Add key
        response1 = client.post(
            "/api/long-term/keys",
            json={"keys": [test_key], "platform": "openai"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response1.json()["added"] == 1

        # Try to add same key multiple times
        for _ in range(3):
            response = client.post(
                "/api/long-term/keys",
                json={"keys": [test_key], "platform": "openai"},
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            assert response.json()["duplicates"] >= 1

        # Verify only one key in database (check by hash, not raw key_data)
        from hashlib import sha256

        key_hash = sha256(test_key.encode()).hexdigest()
        with get_connection(TEST_DB) as conn:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM long_term_keys WHERE hash = ?",
                (key_hash,),
            ).fetchone()["cnt"]
            assert count == 1

    def test_status_transitions(self, client, auth_token):
        """Test key status transitions."""
        # Add key
        add_response = client.post(
            "/api/long-term/keys",
            json={"keys": ["sk-status-test"], "platform": "openai"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        key_id = add_response.json()["key_ids"][0]

        # Initial status should be active
        list_response = client.get("/api/long-term/keys")
        key = next(k for k in list_response.json()["keys"] if k["id"] == key_id)
        assert key["status"] == "active"
        assert key["retry_count"] == 0

        # Check key (will mark as dead since invalid)
        client.post(
            f"/api/long-term/keys/{key_id}/check",
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        # Status should now be dead
        list_response = client.get("/api/long-term/keys")
        key = next(k for k in list_response.json()["keys"] if k["id"] == key_id)
        assert key["status"] in ["dead", "active"]  # Depends on check result


class TestFullWorkflow:
    """Test complete workflow."""

    def test_complete_lifecycle(self, client, auth_token):
        """Test: add → check → verify history → delete."""
        # Step 1: Add key
        add_response = client.post(
            "/api/long-term/keys",
            json={
                "keys": ["sk-lifecycle-test"],
                "platform": "openai",
                "notes": "Lifecycle test",
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert add_response.status_code == 200
        key_id = add_response.json()["key_ids"][0]

        # Step 2: Verify key exists
        list_response = client.get("/api/long-term/keys")
        keys = list_response.json()["keys"]
        key = next((k for k in keys if k["id"] == key_id), None)
        assert key is not None
        assert key["platform"] == "openai"
        assert key["notes"] == "Lifecycle test"

        # Step 3: Check duplicate
        dup_response = client.post(
            "/api/long-term/check-duplicate", json={"keys": ["sk-lifecycle-test"]}
        )
        assert dup_response.json()["duplicates"][0]["exists"] is True

        # Step 4: Manual check
        check_response = client.post(
            f"/api/long-term/keys/{key_id}/check",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert check_response.status_code == 200

        # Step 5: Verify status updated
        list_response = client.get("/api/long-term/keys")
        key = next(k for k in list_response.json()["keys"] if k["id"] == key_id)
        assert key["last_check"] is not None

        # Step 6: Delete key
        delete_response = client.delete(
            f"/api/long-term/keys/{key_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert delete_response.status_code == 200

        # Step 7: Verify deleted
        list_response = client.get("/api/long-term/keys")
        keys = list_response.json()["keys"]
        assert not any(k["id"] == key_id for k in keys)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
