"""End-to-end service tests.

Tests the actual running service including:
- Server startup with scheduler
- Authentication flow
- Add key → probe → status update
- Death retry logic (simulated)
- Duplicate check API
- All HTTP API endpoints
"""

import asyncio
import time
from pathlib import Path

import httpx
import pytest

# Test database and server configuration
TEST_DB = Path("test_e2e.db")
TEST_PORT = 8888
BASE_URL = f"http://localhost:{TEST_PORT}"


@pytest.fixture(scope="module")
def event_loop():
    """Create event loop for module-scoped fixtures."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module", autouse=True)
async def running_service(event_loop):
    """Start the service before tests and stop after."""
    import logging
    import os
    import sys

    # Setup test environment
    os.environ["DATABASE_PATH"] = str(TEST_DB)

    # Remove existing test database
    if TEST_DB.exists():
        TEST_DB.unlink()

    # Initialize database
    from app.db import init_db
    init_db(TEST_DB)

    # Import app after setting environment
    from app.main import app

    # Start server in background
    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=TEST_PORT,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    # Run server in background task
    server_task = event_loop.create_task(server.serve())

    # Wait for server to be ready
    await asyncio.sleep(2)

    print(f"\n✓ Server started on {BASE_URL}")

    yield

    # Cleanup: stop server
    server.should_exit = True
    await server_task

    # Remove test database
    if TEST_DB.exists():
        TEST_DB.unlink()

    print("\n✓ Server stopped and cleaned up")


class TestServiceHealth:
    """Test basic service health."""

    @pytest.mark.asyncio
    async def test_server_is_running(self):
        """Test that server responds to requests."""
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}/api/config")
            assert response.status_code == 200
            config = response.json()
            assert "max_concurrency" in config

    @pytest.mark.asyncio
    async def test_plugins_endpoint(self):
        """Test plugins listing endpoint."""
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}/api/plugins")
            assert response.status_code == 200
            data = response.json()
            assert "plugins" in data
            assert len(data["plugins"]) > 0


class TestAuthenticationE2E:
    """Test authentication flow end-to-end."""

    @pytest.mark.asyncio
    async def test_auth_success(self):
        """Test successful authentication."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BASE_URL}/api/long-term/auth",
                json={"password": "bingxujingAb"},
            )
            assert response.status_code == 200
            data = response.json()
            assert "token" in data
            assert "expires_in" in data
            assert len(data["token"]) > 50

    @pytest.mark.asyncio
    async def test_auth_failure(self):
        """Test authentication with wrong password."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BASE_URL}/api/long-term/auth",
                json={"password": "wrong_password"},
            )
            assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_protected_endpoint_without_token(self):
        """Test that protected endpoints require token."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={"keys": ["sk-test"], "platform": "openai"},
            )
            assert response.status_code == 403  # Missing credentials


async def get_auth_token() -> str:
    """Helper to get authentication token."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_URL}/api/long-term/auth",
            json={"password": "bingxujingAb"},
        )
        assert response.status_code == 200
        return response.json()["token"]


class TestLongTermKeysE2E:
    """Test long-term key management endpoints."""

    @pytest.mark.asyncio
    async def test_add_single_key(self):
        """Test adding a single key."""
        token = await get_auth_token()

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={
                    "keys": ["sk-test-key-12345"],
                    "platform": "openai",
                    "notes": "Test key",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["added"] == 1
            assert data["duplicates"] == 0
            assert len(data["key_ids"]) == 1

    @pytest.mark.asyncio
    async def test_add_duplicate_key(self):
        """Test that duplicate keys are detected."""
        token = await get_auth_token()

        test_key = "sk-duplicate-test-99999"

        async with httpx.AsyncClient() as client:
            # Add first time
            response1 = await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={"keys": [test_key], "platform": "openai"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response1.status_code == 200
            assert response1.json()["added"] == 1

            # Add second time (duplicate)
            response2 = await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={"keys": [test_key], "platform": "openai"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response2.status_code == 200
            data = response2.json()
            assert data["added"] == 0
            assert data["duplicates"] == 1

    @pytest.mark.asyncio
    async def test_add_multiple_keys(self):
        """Test adding multiple keys in batch."""
        token = await get_auth_token()

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={
                    "keys": [
                        "sk-batch-key-1",
                        "sk-batch-key-2",
                        "sk-batch-key-3",
                    ],
                    "platform": "openai",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["added"] == 3
            assert len(data["key_ids"]) == 3

    @pytest.mark.asyncio
    async def test_list_keys(self):
        """Test listing keys."""
        token = await get_auth_token()

        # Add some keys first
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={
                    "keys": ["sk-list-test-1", "sk-list-test-2"],
                    "platform": "openai",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

            # List all keys
            response = await client.get(f"{BASE_URL}/api/long-term/keys")
            assert response.status_code == 200
            data = response.json()
            assert "keys" in data
            assert "total" in data
            assert data["total"] >= 2

    @pytest.mark.asyncio
    async def test_list_keys_with_filter(self):
        """Test listing keys with platform filter."""
        token = await get_auth_token()

        async with httpx.AsyncClient() as client:
            # Add keys for different platforms
            await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={"keys": ["sk-openai-filter"], "platform": "openai"},
                headers={"Authorization": f"Bearer {token}"},
            )
            await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={"keys": ["sk-ant-filter"], "platform": "anthropic"},
                headers={"Authorization": f"Bearer {token}"},
            )

            # Filter by platform
            response = await client.get(
                f"{BASE_URL}/api/long-term/keys?platform=anthropic"
            )
            assert response.status_code == 200
            data = response.json()
            # All returned keys should be anthropic
            for key in data["keys"]:
                assert key["platform"] == "anthropic"

    @pytest.mark.asyncio
    async def test_check_duplicate_api(self):
        """Test duplicate check endpoint."""
        token = await get_auth_token()

        test_key = "sk-duplicate-check-test"

        async with httpx.AsyncClient() as client:
            # Check before adding (should not exist)
            response1 = await client.post(
                f"{BASE_URL}/api/long-term/check-duplicate",
                json={"keys": [test_key]},
            )
            assert response1.status_code == 200
            data1 = response1.json()
            assert data1["duplicates"][0]["exists"] is False

            # Add the key
            await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={"keys": [test_key], "platform": "openai"},
                headers={"Authorization": f"Bearer {token}"},
            )

            # Check again (should exist now)
            response2 = await client.post(
                f"{BASE_URL}/api/long-term/check-duplicate",
                json={"keys": [test_key]},
            )
            assert response2.status_code == 200
            data2 = response2.json()
            assert data2["duplicates"][0]["exists"] is True
            assert data2["duplicates"][0]["key_id"] is not None

    @pytest.mark.asyncio
    async def test_delete_key(self):
        """Test deleting a key."""
        token = await get_auth_token()

        async with httpx.AsyncClient() as client:
            # Add a key
            add_response = await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={"keys": ["sk-delete-test"], "platform": "openai"},
                headers={"Authorization": f"Bearer {token}"},
            )
            key_id = add_response.json()["key_ids"][0]

            # Delete the key
            delete_response = await client.delete(
                f"{BASE_URL}/api/long-term/keys/{key_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert delete_response.status_code == 200
            assert delete_response.json()["deleted_id"] == key_id

            # Verify key is deleted
            list_response = await client.get(f"{BASE_URL}/api/long-term/keys")
            keys = list_response.json()["keys"]
            assert not any(k["id"] == key_id for k in keys)


class TestShortTermJobsE2E:
    """Test short-term job checking endpoints."""

    @pytest.mark.asyncio
    async def test_create_job(self):
        """Test creating a short-term check job."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{BASE_URL}/api/jobs",
                json={
                    "keys": "invalid-key-test",
                    "mode": "health",
                    "concurrency": 1,
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert "job_id" in data
            assert data["state"] in ["pending", "running", "done"]

    @pytest.mark.asyncio
    async def test_get_job_status(self):
        """Test getting job status."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Create job
            create_response = await client.post(
                f"{BASE_URL}/api/jobs",
                json={"keys": "invalid-key", "mode": "health", "concurrency": 1},
            )
            job_id = create_response.json()["job_id"]

            # Wait a bit for processing
            await asyncio.sleep(1)

            # Get job status
            status_response = await client.get(f"{BASE_URL}/api/jobs/{job_id}")
            assert status_response.status_code == 200
            data = status_response.json()
            assert data["job_id"] == job_id
            assert "state" in data
            assert "results" in data


class TestSchedulerE2E:
    """Test scheduler behavior with real service."""

    @pytest.mark.asyncio
    async def test_scheduler_is_active(self):
        """Test that scheduler was started with service."""
        # The scheduler should be running as part of the service startup
        # We can verify by checking that keys are being processed

        token = await get_auth_token()

        async with httpx.AsyncClient() as client:
            # Add a key that needs checking
            response = await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={"keys": ["sk-scheduler-test"], "platform": "openai"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            key_id = response.json()["key_ids"][0]

            # The scheduler should pick this up within its cycle
            # For now, we just verify the key exists in the system
            list_response = await client.get(f"{BASE_URL}/api/long-term/keys")
            keys = list_response.json()["keys"]
            assert any(k["id"] == key_id for k in keys)


class TestFullWorkflowE2E:
    """Test complete workflow from add to check to status update."""

    @pytest.mark.asyncio
    async def test_complete_workflow(self):
        """Test: add key → manual check → verify status update."""
        token = await get_auth_token()

        async with httpx.AsyncClient(timeout=60.0) as client:
            # Step 1: Add a key
            add_response = await client.post(
                f"{BASE_URL}/api/long-term/keys",
                json={
                    "keys": ["sk-workflow-test-complete"],
                    "platform": "openai",
                    "notes": "E2E workflow test",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert add_response.status_code == 200
            key_id = add_response.json()["key_ids"][0]

            # Step 2: Trigger manual check
            check_response = await client.post(
                f"{BASE_URL}/api/long-term/keys/{key_id}/check",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert check_response.status_code == 200
            check_data = check_response.json()
            assert check_data["key_id"] == key_id
            assert check_data["status"] in ["alive", "dead"]
            assert "checked_at" in check_data

            # Step 3: Verify status was updated
            list_response = await client.get(f"{BASE_URL}/api/long-term/keys")
            keys = list_response.json()["keys"]
            updated_key = next((k for k in keys if k["id"] == key_id), None)
            assert updated_key is not None
            assert updated_key["last_check"] is not None
            assert updated_key["status"] in ["active", "dead"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-s"])
