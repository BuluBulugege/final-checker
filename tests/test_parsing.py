"""Credential input parsing tests."""

from __future__ import annotations

import json

from app.parsing import parse_credentials


def test_parse_combo_bundle_expands_supported_entries_and_deduplicates():
    service_account_key = "-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----\n"
    bundle = {
        "summary": {"total": 6},
        "aws_iam_pairs": [
            {
                "access_key_id": "ASIA" + "C" * 16,
                "secret_access_key": "u" * 40,
                "session_token": "temporary-session-token",
            },
            {
                "access_key_id": "AKIA" + "A" * 16,
                "secret_access_key": "s" * 40,
            },
            {
                "access_key_id": "AKIA" + "A" * 16,
                "secret_access_key": "s" * 40,
            },
            {
                "access_key_id": "ASIA" + "B" * 16,
                "secret_access_key": "t" * 40,
            },
        ],
        "azure_openai_pairs": [
            {
                "endpoint": "https://example.openai.azure.com/openai/v1/responses",
                "api_key": "z" * 32,
            },
            {
                "endpoint": "https://example.openai.azure.com/openai/v1/responses",
                "api_key": "z" * 32,
            },
        ],
        "gcp_service_accounts": [
            {
                "client_email": "worker@demo-project.iam.gserviceaccount.com",
                "private_key": service_account_key,
            }
        ],
    }

    credentials = parse_credentials(json.dumps(bundle))

    assert len(credentials) == 3
    assert credentials[0] == f"AKIA{'A' * 16}:{'s' * 40}"
    assert credentials[1] == "https://example.openai.azure.com|" + "z" * 32

    gcp = json.loads(credentials[2])
    assert gcp == {
        "type": "service_account",
        "project_id": "demo-project",
        "private_key": service_account_key,
        "client_email": "worker@demo-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def test_parse_combo_bundle_canonicalizes_azure_origins():
    bundle = {
        "azure_openai_pairs": [
            {
                "endpoint": "HTTPS://Example.OpenAI.Azure.Com/openai/v1/responses",
                "api_key": "z" * 32,
            },
            {
                "endpoint": "https://example.openai.azure.com/other/path",
                "api_key": "z" * 32,
            },
            {
                "endpoint": "https://example.openai.azure.com:443/third/path",
                "api_key": "z" * 32,
            },
        ]
    }

    assert parse_credentials(json.dumps(bundle)) == [
        "https://example.openai.azure.com|" + "z" * 32
    ]


def test_parse_combo_bundle_rejects_non_azure_and_userinfo_endpoints():
    bundle = {
        "azure_openai_pairs": [
            {
                "endpoint": "https://foo.openai.azure.com@attacker.example/path",
                "api_key": "a" * 32,
            },
            {
                "endpoint": "https://attacker.example/?openai.azure.com",
                "api_key": "b" * 32,
            },
            {
                "endpoint": "http://example.openai.azure.com",
                "api_key": "c" * 32,
            },
            {
                "endpoint": "https://[bad",
                "api_key": "d" * 32,
            },
            {
                "endpoint": "https://example.openai.azure.com:8443",
                "api_key": "e" * 32,
            },
        ]
    }

    assert parse_credentials(json.dumps(bundle)) == []


def test_parse_combo_bundle_uses_explicit_gcp_project_id_and_safe_token_uri():
    bundle = {
        "gcp_service_accounts": [
            {
                "project_id": "explicit-project",
                "client_email": "unusual-email",
                "private_key": "not-a-valid-key",
                "token_uri": "http://127.0.0.1/internal",
            }
        ]
    }

    [credential] = parse_credentials(json.dumps(bundle))
    info = json.loads(credential)

    assert info["project_id"] == "explicit-project"
    assert info["client_email"] == "unusual-email"
    assert info["token_uri"] == "https://oauth2.googleapis.com/token"


def test_parse_combo_bundle_keeps_provider_shaped_bad_gcp_for_diagnostics():
    bundle = {
        "aws_iam_pairs": [{"access_key_id": "AKIA" + "A" * 16}],
        "azure_openai_pairs": [{"endpoint": "https://example.openai.azure.com"}],
        "gcp_service_accounts": [
            {"client_email": "worker@example.com", "private_key": "malformed-key"}
        ],
    }

    [credential] = parse_credentials(json.dumps(bundle))
    info = json.loads(credential)
    assert info["type"] == "service_account"
    assert info["project_id"] == ""
    assert info["client_email"] == "worker@example.com"
    assert info["private_key"] == "malformed-key"


def test_parse_regular_service_account_json_is_unchanged():
    info = {
        "type": "service_account",
        "project_id": "demo-project",
        "private_key": "fake-key",
        "client_email": "worker@demo-project.iam.gserviceaccount.com",
        "gcp_service_accounts": "unrelated metadata",
    }
    raw = json.dumps(info, indent=2)

    [credential] = parse_credentials(raw)
    assert json.loads(credential) == info


def test_malformed_combo_schema_is_not_silently_discarded():
    raw = json.dumps({"aws_iam_pairs": "not-a-list", "name": "ordinary-json"})

    assert parse_credentials(raw) == [raw]


def test_parse_credentials_rejects_oversized_input(monkeypatch):
    import pytest

    from app.config import settings

    monkeypatch.setattr(settings, "max_input_chars", 10)
    with pytest.raises(ValueError, match="input too large"):
        parse_credentials("x" * 11)


def test_parse_normal_line_input_is_unchanged():
    assert parse_credentials("sk-first\nsk-second\n") == ["sk-first", "sk-second"]
