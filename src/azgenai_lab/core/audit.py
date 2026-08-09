"""Audit event schema and emitter (Day 22). Reference-only terminal events:
who/when/which ids/outcome — never content. Presence rules are enforced by
outcome variants (structural) plus runtime validators (the
provider_call_attempted layer); see docs/audit-logging.md."""

import time
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from azgenai_lab.models.chat import TokenUsage

_FROZEN = ConfigDict(frozen=True, extra="forbid")

ChatRejectedCode = Literal["validation_error", "conversation_not_found",
                           "token_budget_exceeded", "invalid_input", "content_filtered"]
ChatErrorCode = Literal["configuration_error", "storage_error", "upstream_throttled",
                        "upstream_timeout", "upstream_error", "client_disconnect"]
RagRejectedCode = Literal["validation_error", "content_filtered"]
RagErrorCode = Literal["configuration_error", "embedding_rejected", "rag_context_overflow",
                       "search_unavailable", "search_request_rejected",
                       "upstream_throttled", "upstream_timeout", "upstream_error"]
AgentRejectedCode = Literal["validation_error", "invalid_input",
                            "conversation_not_found", "token_budget_exceeded"]
AgentErrorCode = Literal["storage_error", "upstream_error"]
AuthRejectionReason = Literal["bearer_missing", "token_invalid", "permission_missing",
                              "headers_missing", "headers_invalid"]
IncompleteReason = Literal["max_output_tokens", "content_filter", "other"]

# Runtime-only constraints surfaced into the exported JSON Schema (spec §5).
# The schema-export test asserts each string inside its OWN $def.
ATTEMPTED_CONSTRAINT = (
    "attempted=true requires full prompt/deployment attribution; "
    "attempted=false forbids attribution and provider terminal data"
)
BUDGET_CONSTRAINT = (
    "spent/budget appear as a pair and exactly with error_code=token_budget_exceeded"
)
IDENTITY_CONSTRAINT = "401 carries no identity; 403 requires verified identity"
CHAT_SUCCESS_CONSTRAINT = "chat.turn success requires provider_call_attempted=true"
AGENT_SUCCESS_CONSTRAINT = "agent.run success requires provider_call_attempted=true"
RAG_SUCCESS_CONSTRAINT = (
    "rag.query answered requires attempted=true; no_answer requires attempted=false"
)


def audit_now() -> datetime:
    return datetime.now(UTC)


def duration_since(start: float) -> float:
    return max(0.0, (time.perf_counter() - start) * 1000)


def check_budget_pair(error_code: str, spent: int | None, budget: int | None) -> None:
    """Iff binding (decision 1): a budget rejection carries both counters;
    every other code carries neither."""
    if error_code == "token_budget_exceeded":
        if spent is None or budget is None:
            raise ValueError(BUDGET_CONSTRAINT)
    elif spent is not None or budget is not None:
        raise ValueError(BUDGET_CONSTRAINT)


class AuditUsage(BaseModel):
    model_config = _FROZEN
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int = Field(ge=0)

    @classmethod
    def from_token_usage(cls, usage: TokenUsage) -> "AuditUsage":
        return cls(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                   reasoning_tokens=usage.reasoning_tokens, total_tokens=usage.total_tokens)


class AuditTool(BaseModel):
    model_config = _FROZEN
    name: str
    executed: bool
    round_index: int | None = Field(default=None, ge=1)


class _AuditBase(BaseModel):
    model_config = _FROZEN
    schema_version: Literal[1] = 1
    occurred_at: AwareDatetime
    correlation_id: str
    duration_ms: float = Field(ge=0)

    @field_validator("occurred_at")
    @classmethod
    def _normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class _RouteEventBase(_AuditBase):
    tenant_id: str
    user_id: str
    provider_call_attempted: bool = Field(description=ATTEMPTED_CONSTRAINT)
    prompt_name: str | None = None
    prompt_version: int | None = Field(default=None, ge=1)
    prompt_sha256: str | None = None
    deployment: str | None = None
    usage: AuditUsage | None = None

    @model_validator(mode="after")
    def _attempted_layer(self) -> "_RouteEventBase":
        attribution = (self.prompt_name, self.prompt_version, self.prompt_sha256, self.deployment)
        if self.provider_call_attempted:
            if any(field is None for field in attribution):
                raise ValueError(ATTEMPTED_CONSTRAINT)
        else:
            if any(field is not None for field in attribution) or self.usage is not None:
                raise ValueError(ATTEMPTED_CONSTRAINT)
        return self


class _ChatTurnBase(_RouteEventBase):
    event: Literal["chat.turn"] = "chat.turn"
    conversation_id: str | None
    streaming: bool
    committed: bool
    model_version: str | None = None
    status: Literal["completed", "incomplete"] | None = None
    incomplete_reason: IncompleteReason | None = None

    @model_validator(mode="after")
    def _terminal_shape(self) -> "_ChatTurnBase":
        if not self.provider_call_attempted and (
            self.status is not None or self.model_version is not None
        ):
            raise ValueError(ATTEMPTED_CONSTRAINT)
        if self.incomplete_reason is not None and self.status != "incomplete":
            raise ValueError("incomplete_reason requires status=incomplete")
        return self


class ChatTurnSuccess(_ChatTurnBase):
    outcome: Literal["success"] = Field(default="success", description=CHAT_SUCCESS_CONSTRAINT)

    @model_validator(mode="after")
    def _success_invariants(self) -> "ChatTurnSuccess":
        if not self.provider_call_attempted or self.status is None:
            raise ValueError(CHAT_SUCCESS_CONSTRAINT)
        return self


class ChatTurnRejected(_ChatTurnBase):
    outcome: Literal["rejected"] = "rejected"
    error_code: ChatRejectedCode
    spent: int | None = Field(default=None, ge=0, description=BUDGET_CONSTRAINT)
    budget: int | None = Field(default=None, ge=0, description=BUDGET_CONSTRAINT)

    @model_validator(mode="after")
    def _rejected_invariants(self) -> "ChatTurnRejected":
        check_budget_pair(self.error_code, self.spent, self.budget)
        return self


class ChatTurnError(_ChatTurnBase):
    outcome: Literal["error"] = "error"
    error_code: ChatErrorCode
