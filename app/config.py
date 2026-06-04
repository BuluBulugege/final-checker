"""Central configuration. Every rate, duration, model id, and threshold the
plugins use lives here so the whole tool is tunable without touching logic.

Values can be overridden via environment variables (prefix FC_) or a .env file.
Provider-specific knobs are plain nested models with sane spec-derived defaults.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GeminiConfig(BaseModel):
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    pro_model: str = "gemini-3.1-pro"
    image_model: str = "gemini-3.1-flash-image"
    # spec: 50 req/s for 30s, observe first-429 timing
    burst_rps: float = 50.0
    burst_seconds: float = 30.0
    # gentle defaults when full_load is off
    gentle_rps: float = 5.0
    gentle_seconds: float = 6.0
    # timing buckets (seconds): <t1_cut => t1, <t2_cut => t2, else t3
    t1_cut_s: float = 15.0
    t2_cut_s: float = 30.0
    request_timeout_s: float = 30.0


class OpenAIConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    probe_model: str = "gpt-4o-mini"  # cheap model to read x-ratelimit-* headers
    image_model: str = "gpt-image-2"
    # spec: image probe 30 req/s, cap at 40 calls, derive max rpm
    image_burst_rps: float = 30.0
    image_burst_cap: int = 40
    image_burst_seconds: float = 30.0
    gentle_image_rps: float = 3.0
    gentle_image_cap: int = 6
    request_timeout_s: float = 30.0
    # TPM thresholds (flagship chat model) -> tier. Compared ascending; pick the
    # highest tier whose threshold the observed TPM meets or exceeds.
    # Keep configurable; numbers drift. (GPT-5.5-class, early-2026 docs.)
    tpm_tiers: dict[str, float] = Field(
        default_factory=lambda: {
            "T1": 500_000,
            "T2": 1_000_000,
            "T3": 2_000_000,
            "T4": 4_000_000,
            "T5": 40_000_000,
        }
    )
    # image rpm -> bucket index. (lo, hi] -> bucket. Per spec.
    # 0 -> 0; (0,25] -> 1; (25,100] -> 2; (100,200] -> 3; (200,300] -> 4; >300 -> 5
    rpm_buckets: list[tuple[float, int]] = Field(
        default_factory=lambda: [
            (0, 0),
            (25, 1),
            (100, 2),
            (200, 3),
            (300, 4),
            (float("inf"), 5),
        ]
    )


class AnthropicConfig(BaseModel):
    base_url: str = "https://api.anthropic.com/v1"
    anthropic_version: str = "2023-06-01"
    probe_model: str = "claude-haiku-4-5"  # cheapest fallback if listing fails
    request_timeout_s: float = 30.0
    max_models_to_probe: int = 30
    # RPM is the cleanest tier discriminator on Anthropic: 50/1000/2000/4000.
    rpm_to_tier: dict[int, str] = Field(
        default_factory=lambda: {50: "T1", 1000: "T2", 2000: "T3", 4000: "T4"}
    )


class GCPConfig(BaseModel):
    request_timeout_s: float = 30.0
    # Per-request cap so unreachable region endpoints fail fast.
    probe_timeout_s: float = 15.0
    # Vertex regions to sweep when the live locations list can't be fetched.
    # Empty -> the plugin falls back to its built-in DEFAULT_LOCATIONS.
    locations: list[str] = Field(default_factory=list)
    # Cap how many regions to actually enumerate models in. 0 = no cap (all).
    max_locations: int = 12
    # How many region scans run concurrently.
    region_concurrency: int = 12
    # Model Garden publishers to enumerate per region (the real, non-hardcoded
    # model list comes from v1beta1/publishers/{publisher}/models).
    publishers: list[str] = Field(
        default_factory=lambda: ["google", "anthropic", "meta", "mistralai"]
    )
    # Region used for the actual rate/throughput measurements (quota is shared
    # across regions, so one region's numbers represent the project ceiling).
    probe_region: str = "us-central1"
    # RPM measurement (full_load only — real calls). Fire small requests fast.
    rpm_probe_rps: float = 50.0
    rpm_probe_seconds: float = 8.0
    rpm_probe_cap: int = 400
    # TPM measurement (full_load only). Send LARGE inputs so a few requests can
    # saturate the per-minute token quota; tokens_per_request ~ how big each call.
    tpm_tokens_per_request: int = 200_000
    tpm_probe_max_requests: int = 40
    tpm_probe_concurrency: int = 8
    # Only measure rate/throughput for this many generative models (cheapest/
    # most-capable first); enumeration still lists ALL models.
    rate_probe_max_models: int = 3


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FC_", env_file=".env", env_nested_delimiter="__", extra="ignore"
    )

    # global probe politeness
    default_concurrency: int = 5
    max_concurrency: int = 100
    http_connect_timeout_s: float = 10.0
    # how long a completed job is kept in memory before eviction
    job_ttl_seconds: float = 3600.0
    max_keys_per_job: int = 500

    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    gcp: GCPConfig = Field(default_factory=GCPConfig)


settings = Settings()
