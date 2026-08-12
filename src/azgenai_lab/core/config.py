from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "azure-genai-backend-lab"
    app_env: str = "local"
    log_level: str = "INFO"

    # v1 GA API (2025-08): plain OpenAI client against <endpoint>/openai/v1/, no api-version
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_deployment_name: str | None = None

    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    # Hard cap per call, passed as max_output_tokens on every request: an
    # unbounded reply is the single fastest way to burn budget. Streams that
    # hit it end with message.done incomplete/max_output_tokens (Day 6).
    # gt=0: zero or negative caps are deployment mistakes — fail at startup,
    # not as a confusing upstream 400 (Day 9 review r01 finding 6).
    llm_max_output_tokens: int = Field(default=1000, gt=0)
    # Per-conversation lifetime budget in provider-reported tokens (input +
    # output across all committed turns). Checked before inference: an
    # exhausted conversation is rejected with 429 token_budget_exceeded
    # without touching the upstream. None is the only way to disable the
    # guardrail; zero or negative values fail startup validation.
    conversation_token_budget: int | None = Field(default=50_000, gt=0)

    # Chunk sizing is measured in characters, not tokens. Day 9 settled the
    # series position: meter what the provider reports, do not estimate. The
    # service's own generally-available text splitter measures characters too
    # (token splitting is preview only), and the real safety margin is the
    # embedding model's 8,192-token input ceiling. The bound is universal, not
    # sampled: a UTF-8 character is at most 4 bytes, so 2,000 chars <= 8,000
    # bytes; a BPE token decodes to at least one UTF-8 byte, so token count
    # <= byte count, giving 2,000 chars <= 8,000 tokens < 8,192 for any input.
    chunk_max_chars: int = Field(default=2000, gt=0)
    # Overlap applies only within one oversized section (see services/chunking).
    chunk_overlap_chars: int = Field(default=500, ge=0)

    # Where the bundled sample corpus lives. None keeps the repo-relative
    # default in services/document_loader.py, which is computed from that
    # module's own path -- correct in a source tree, wrong once the project
    # is installed non-editable (the container does exactly that, Day 23).
    # Deployments that ship the corpus elsewhere point this at it.
    sample_docs_dir: Path | None = None

    azure_openai_embedding_deployment: str | None = None

    azure_search_endpoint: str | None = None
    # Admin key: creating an index, uploading and deleting documents all need
    # management capability. Production should use Microsoft Entra ID / RBAC
    # with roles split by read/write responsibility; the key keeps the lab's
    # ephemeral sessions cheap to configure. Never logged, never returned.
    azure_search_admin_key: SecretStr | None = None

    use_fake_llm: bool = Field(default=True)
    use_fake_search: bool = Field(default=True)
    use_fake_embeddings: bool = Field(default=True)

    # How many chunks retrieval hands to generation. Small on purpose: every
    # hit is prompt input the model must read and the caller pays for
    # (Day 9 metering); top=5 of 2,000-char chunks is ~10k chars of context.
    # Recall/precision tuning belongs to retrieval evaluation, not this knob.
    # Upper bound (Day 14 review finding 2): DEFAULT_VECTOR_K=50 is the
    # vector leg's candidate pool (models/search.py), so top above 50 asks
    # generation to read more chunks than retrieval's own vector leg ever
    # offers. It also bounds assembled context to <= 50 * 2,000 chars =
    # ~100k chars plus the question, keeping context growth predictable
    # instead of an unbounded multiple of chunk_max_chars.
    rag_top: int = Field(default=5, gt=0, le=50)

    # Day 17 agent loop guardrails. The framework defaults (40 iterations,
    # unlimited tool calls) are never silently accepted; these are the values
    # the article's Q4 cost-ceiling answer quotes.
    agent_max_iterations: int = Field(default=5, gt=0)
    agent_max_tool_calls: int = Field(default=10, gt=0)

    # Total budget, in seconds, for the four sequential lifespan closers on
    # shutdown (principal resolver, conversation service, RAG service, agent
    # turn service — see main.py). This is a shared *total*, not a per-closer
    # allowance: Container Apps allows 30s from SIGTERM to SIGKILL, request
    # drain (docker/Dockerfile's --timeout-graceful-shutdown) can use up to
    # 20s of that before lifespan cleanup even starts, and 8s leaves the
    # remaining ~10s some margin for runtime overhead around the cleanup
    # itself (Day 23 review A1). gt=0: a non-positive budget could never let
    # a closer run at all, which is a deployment mistake, not a legitimate
    # zero-time policy.
    shutdown_cleanup_budget_seconds: float = Field(default=8.0, gt=0)

    # Day 19: caller authentication mode, selected once at startup. "headers"
    # is the existing trusted-development path (require_principal reads
    # X-Tenant-Id/X-User-Id directly); "entra" validates a real Microsoft
    # Entra ID JWT. The Entra fields below are only required — and only
    # validated for presence — when auth_mode is "entra"; headers mode never
    # touches them.
    auth_mode: Literal["headers", "entra"] = "headers"
    # GUIDs, not api:// URIs: normalized to canonical lower-case string form
    # by _canonical_guid below regardless of mode, so a value supplied in
    # headers mode (e.g. left over in a shared .env) is still validated as a
    # GUID if present, rather than silently accepted as free text.
    entra_tenant_id: str | None = None
    entra_audience: str | None = None
    # At least one of these two is required in entra mode (checked below);
    # blank strings normalize to None so ".env" placeholders and unset
    # values behave identically.
    entra_required_scope: str | None = None
    entra_required_app_role: str | None = None

    @field_validator("entra_tenant_id", "entra_audience")
    @classmethod
    def _canonical_guid(cls, value: str | None) -> str | None:
        # Normalizes whenever a value is present, in either mode; whether the
        # value is *required* is a cross-field question, handled below.
        if value is None or not value.strip():
            return None
        try:
            return str(UUID(value.strip()))
        except ValueError as exc:
            raise ValueError("must be a GUID") from exc

    @field_validator("entra_required_scope", "entra_required_app_role")
    @classmethod
    def _blank_is_absent(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def _overlap_must_leave_room_to_advance(self) -> "Settings":
        if self.chunk_overlap_chars * 2 >= self.chunk_max_chars:
            raise ValueError(
                "chunk_overlap_chars must be less than half of chunk_max_chars "
                f"(got {self.chunk_overlap_chars} against {self.chunk_max_chars})"
            )
        return self

    @model_validator(mode="after")
    def _entra_fields_required_in_entra_mode(self) -> "Settings":
        # Check-only: never assign to self here. Settings is not frozen
        # today, so mutation would work, but field validators are the
        # supported normalization mechanism and stay correct if Settings
        # ever gains frozen=True or validate_assignment=True.
        if self.auth_mode == "headers":
            return self
        if self.entra_tenant_id is None or self.entra_audience is None:
            raise ValueError("entra_tenant_id and entra_audience are required in entra mode")
        if self.entra_required_scope is None and self.entra_required_app_role is None:
            raise ValueError("entra mode requires a delegated scope or application role")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
