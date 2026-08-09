"""Audit event schema and emitter (Day 22). Reference-only terminal events:
who/when/which ids/outcome — never content. Presence rules are enforced by
outcome variants (structural) plus runtime validators (the
provider_call_attempted layer); see docs/audit-logging.md."""

import time
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

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


class _RagQueryBase(_RouteEventBase):
    event: Literal["rag.query"] = "rag.query"
    model_version: str | None = None
    hit_count: int | None = Field(default=None, ge=0)
    selected_chunk_ids: tuple[str, ...] | None = None
    status: Literal["answered", "no_answer", "error"] | None = None
    failed_stage: Literal["retrieve", "assemble_context", "generation"] | None = None

    @model_validator(mode="after")
    def _terminal_shape(self) -> "_RagQueryBase":
        if not self.provider_call_attempted and self.model_version is not None:
            raise ValueError(ATTEMPTED_CONSTRAINT)
        return self


class RagQuerySuccess(_RagQueryBase):
    outcome: Literal["success"] = Field(default="success", description=RAG_SUCCESS_CONSTRAINT)
    status: Literal["answered", "no_answer"]

    @model_validator(mode="after")
    def _success_invariants(self) -> "RagQuerySuccess":
        if (self.status == "answered") != self.provider_call_attempted:
            raise ValueError(RAG_SUCCESS_CONSTRAINT)
        if self.failed_stage is not None:
            raise ValueError("failed_stage is error-path data")
        return self


class RagQueryRejected(_RagQueryBase):
    outcome: Literal["rejected"] = "rejected"
    error_code: RagRejectedCode
    status: Literal["error"] | None = None  # null only when the pipeline never ran (422)


class RagQueryError(_RagQueryBase):
    outcome: Literal["error"] = "error"
    error_code: RagErrorCode
    status: Literal["error"] = "error"

    @model_validator(mode="after")
    def _error_invariants(self) -> "RagQueryError":
        if self.failed_stage is None:
            raise ValueError("a rag error names its failed stage")
        return self


class _AgentRunBase(_RouteEventBase):
    event: Literal["agent.run"] = "agent.run"
    conversation_id: str | None
    committed: bool
    model_calls: int | None = Field(default=None, ge=0)
    tool_call_count: int | None = Field(default=None, ge=0)
    refused_call_count: int | None = Field(default=None, ge=0)
    tools: tuple[AuditTool, ...] | None = None
    stop_reason: Literal["natural", "iteration_limit", "function_call_limit"] | None = None


class AgentRunSuccess(_AgentRunBase):
    outcome: Literal["success"] = Field(default="success", description=AGENT_SUCCESS_CONSTRAINT)

    @model_validator(mode="after")
    def _success_invariants(self) -> "AgentRunSuccess":
        if not self.provider_call_attempted:
            raise ValueError(AGENT_SUCCESS_CONSTRAINT)
        return self


class AgentRunRejected(_AgentRunBase):
    outcome: Literal["rejected"] = "rejected"
    error_code: AgentRejectedCode
    spent: int | None = Field(default=None, ge=0, description=BUDGET_CONSTRAINT)
    budget: int | None = Field(default=None, ge=0, description=BUDGET_CONSTRAINT)

    @model_validator(mode="after")
    def _rejected_invariants(self) -> "AgentRunRejected":
        check_budget_pair(self.error_code, self.spent, self.budget)
        return self


class AgentRunErrorEvent(_AgentRunBase):
    outcome: Literal["error"] = "error"
    error_code: AgentErrorCode


class AuthRejected(_AuditBase):
    """401 carries no identity; 403 requires verified identity."""

    event: Literal["auth.rejected"] = "auth.rejected"
    outcome: Literal["rejected"] = "rejected"
    tenant_id: str | None = Field(description=IDENTITY_CONSTRAINT)
    user_id: str | None = Field(description=IDENTITY_CONSTRAINT)
    path: str  # request.url.path — never the query string
    auth_mode: Literal["headers", "entra"]
    reason: AuthRejectionReason
    http_status: Literal[401, 403]

    @model_validator(mode="after")
    def _identity_rules(self) -> "AuthRejected":
        if self.http_status == 401 and (self.tenant_id is not None or self.user_id is not None):
            raise ValueError(IDENTITY_CONSTRAINT)
        if self.http_status == 403 and (self.tenant_id is None or self.user_id is None):
            raise ValueError(IDENTITY_CONSTRAINT)
        if (self.reason == "permission_missing") != (self.http_status == 403):
            raise ValueError("permission_missing is exactly the 403 reason")
        if self.auth_mode == "headers":
            if self.reason not in ("headers_missing", "headers_invalid") or self.http_status != 401:
                raise ValueError("headers mode rejects only as 401 headers_missing/headers_invalid")
        elif self.http_status == 401 and self.reason not in ("bearer_missing", "token_invalid"):
            raise ValueError("entra 401 reasons are bearer_missing/token_invalid")
        return self


ChatTurnEvent = Annotated[
    ChatTurnSuccess | ChatTurnRejected | ChatTurnError, Field(discriminator="outcome")
]
RagQueryEvent = Annotated[
    RagQuerySuccess | RagQueryRejected | RagQueryError, Field(discriminator="outcome")
]
AgentRunEvent = Annotated[
    AgentRunSuccess | AgentRunRejected | AgentRunErrorEvent, Field(discriminator="outcome")
]
AuditEvent = Annotated[
    ChatTurnEvent | RagQueryEvent | AgentRunEvent | AuthRejected,
    Field(discriminator="event"),
]

AUDIT_EVENT_ADAPTER: TypeAdapter[AuditEvent] = TypeAdapter(AuditEvent)

AuditEventModel = (
    ChatTurnSuccess | ChatTurnRejected | ChatTurnError
    | RagQuerySuccess | RagQueryRejected | RagQueryError
    | AgentRunSuccess | AgentRunRejected | AgentRunErrorEvent
    | AuthRejected
)
