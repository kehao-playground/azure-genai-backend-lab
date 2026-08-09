"""Audit event schema and emitter (Day 22). Reference-only terminal events:
who/when/which ids/outcome — never content. Presence rules are enforced by
outcome variants (structural) plus runtime validators (the
provider_call_attempted layer); see docs/audit-logging.md."""

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from azgenai_lab.core.config import Settings
from azgenai_lab.core.correlation import correlation_id_var, request_start_var
from azgenai_lab.models.chat import TokenUsage
from azgenai_lab.prompts.loader import PromptTemplate

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

_audit_logger = logging.getLogger("audit")


def emit_audit_event(event: AuditEventModel) -> None:
    """The emission boundary: whatever reaches the log line has passed the
    audit schema. A foreign model raises before anything is written — the
    never-log guarantee is enforced here, not by a type hint."""
    validated = AUDIT_EVENT_ADAPTER.validate_python(event)
    payload = AUDIT_EVENT_ADAPTER.dump_python(validated, mode="json")
    _audit_logger.info(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def has_audit_context() -> bool:
    """True inside a request (the correlation middleware ran). Service-level
    producers emit only under this guard; direct service invocation neither
    fabricates request fields nor raises (spec §3c)."""
    return request_start_var.get() is not None and correlation_id_var.get() is not None


def request_duration_ms() -> float:
    start = request_start_var.get()
    return 0.0 if start is None else duration_since(start)


# --- Route event builders (Day 22 Task 6): chat.turn ---
#
# core.audit never imports core.errors (see module docstring in
# api/chat.py): builders below take only primitives, dataclasses defined in
# this module, and models already defined above — never an UpstreamError
# subtype. The exception -> primitive classification that decides *which*
# builder to call and what to pass it lives in core.errors instead
# (upstream_outcome / chat_upstream_audit_args), so api/chat.py and
# api/streaming.py share one classification without either module reaching
# into the other's private symbols.
#
# Every field is passed by name rather than by dict-splat: BaseModel's
# synthesized __init__ (pydantic's dataclass_transform) has one concrete
# parameter type per field, and an untyped dict (however convenient to
# assemble) cannot satisfy that under mypy strict — the explicit form below
# is what actually type-checks, and it matches the rest of this codebase's
# style for constructing these models (e.g. AuthRejected in api/principal.py).


@dataclass(frozen=True)
class AuditAttribution:
    """The prompt/deployment identity actually used by a request — built once
    per composition point from the same PromptTemplate instance handed to the
    adapter, so this describes what was sent, not a same-looking reload."""

    prompt_name: str
    prompt_version: int
    prompt_sha256: str
    deployment: str


def build_audit_attribution(settings: Settings, prompt: PromptTemplate) -> AuditAttribution:
    deployment = "fake" if settings.use_fake_llm else settings.azure_openai_deployment_name
    if deployment is None:
        raise ValueError("real mode requires AZURE_OPENAI_DEPLOYMENT_NAME")
    return AuditAttribution(prompt.name, prompt.version, prompt.sha256, deployment)


@dataclass(frozen=True)
class ChatCommitSnapshot:
    """Terminal data captured at the point a turn tries to commit — carried on
    ChatStorageCommitError so a commit failure's audit event can still report
    what the provider returned, not just that storage failed."""

    model_version: str | None
    usage: TokenUsage | None
    status: Literal["completed", "incomplete"]
    incomplete_reason: IncompleteReason | None


@dataclass(frozen=True)
class ChatEventBase:
    """The fields every chat.turn variant carries, collected once at handler
    entry — grouped so a finalizer's several emit call sites cannot each
    independently misspell or omit one. ``conversation_id`` starts as
    whatever the request carried (``None`` for a first turn) and the handler
    replaces it with the resolved id once ``complete()`` returns one."""

    tenant_id: str
    user_id: str
    correlation_id: str
    conversation_id: str | None
    streaming: bool


def chat_base_fields(
    *, tenant_id: str, user_id: str, correlation_id: str,
    conversation_id: str | None, streaming: bool,
) -> ChatEventBase:
    return ChatEventBase(
        tenant_id=tenant_id, user_id=user_id, correlation_id=correlation_id,
        conversation_id=conversation_id, streaming=streaming,
    )


def chat_success_event(
    *, base: ChatEventBase, duration_ms: float, attribution: AuditAttribution,
    model_version: str | None, usage: TokenUsage | None,
    status: Literal["completed", "incomplete"], incomplete_reason: IncompleteReason | None,
    committed: bool,
) -> ChatTurnSuccess:
    return ChatTurnSuccess(
        tenant_id=base.tenant_id, user_id=base.user_id, correlation_id=base.correlation_id,
        conversation_id=base.conversation_id, streaming=base.streaming,
        occurred_at=audit_now(), duration_ms=duration_ms,
        provider_call_attempted=True,
        prompt_name=attribution.prompt_name, prompt_version=attribution.prompt_version,
        prompt_sha256=attribution.prompt_sha256, deployment=attribution.deployment,
        model_version=model_version,
        usage=AuditUsage.from_token_usage(usage) if usage else None,
        status=status, incomplete_reason=incomplete_reason, committed=committed,
    )


def chat_rejected_event(
    *, base: ChatEventBase, duration_ms: float, error_code: ChatRejectedCode,
    spent: int | None = None, budget: int | None = None,
) -> ChatTurnRejected:
    return ChatTurnRejected(
        tenant_id=base.tenant_id, user_id=base.user_id, correlation_id=base.correlation_id,
        conversation_id=base.conversation_id, streaming=base.streaming,
        occurred_at=audit_now(), duration_ms=duration_ms,
        provider_call_attempted=False,
        prompt_name=None, prompt_version=None, prompt_sha256=None, deployment=None,
        committed=False, error_code=error_code, spent=spent, budget=budget,
    )


def chat_failure_event(
    *, base: ChatEventBase, duration_ms: float,
    outcome: Literal["rejected", "error"], error_code: str,
    provider_call_attempted: bool, attribution: AuditAttribution | None,
    snapshot: ChatCommitSnapshot | None = None,
) -> "ChatTurnRejected | ChatTurnError":
    """Primitives only — core.audit never imports core.errors. No synthetic
    HTTP status for non-HTTP failures: the caller names the outcome.

    ``error_code`` arrives as a plain ``str`` (UpstreamError.code is a class
    attribute typed ``str``, not a Literal, since one exception hierarchy
    backs several distinct wire vocabularies) rather than the narrower
    ChatRejectedCode/ChatErrorCode each branch's model expects. The cast
    below documents that widening, not an assumption it papers over: an
    error_code outside the model's Literal set still fails loudly at
    emit_audit_event's validated boundary, the same as any other schema
    violation this module enforces.
    """
    effective_attribution = attribution if provider_call_attempted else None
    prompt_name = effective_attribution.prompt_name if effective_attribution else None
    prompt_version = effective_attribution.prompt_version if effective_attribution else None
    prompt_sha256 = effective_attribution.prompt_sha256 if effective_attribution else None
    deployment = effective_attribution.deployment if effective_attribution else None
    if snapshot is not None:
        model_version = snapshot.model_version
        auditable_usage = AuditUsage.from_token_usage(snapshot.usage) if snapshot.usage else None
        status = snapshot.status
        incomplete_reason = snapshot.incomplete_reason
    else:
        model_version, auditable_usage, status, incomplete_reason = None, None, None, None
    if outcome == "rejected":
        return ChatTurnRejected(
            tenant_id=base.tenant_id, user_id=base.user_id, correlation_id=base.correlation_id,
            conversation_id=base.conversation_id, streaming=base.streaming,
            occurred_at=audit_now(), duration_ms=duration_ms,
            provider_call_attempted=provider_call_attempted,
            prompt_name=prompt_name, prompt_version=prompt_version,
            prompt_sha256=prompt_sha256, deployment=deployment,
            model_version=model_version, usage=auditable_usage,
            status=status, incomplete_reason=incomplete_reason,
            committed=False, error_code=cast(ChatRejectedCode, error_code),
        )
    return ChatTurnError(
        tenant_id=base.tenant_id, user_id=base.user_id, correlation_id=base.correlation_id,
        conversation_id=base.conversation_id, streaming=base.streaming,
        occurred_at=audit_now(), duration_ms=duration_ms,
        provider_call_attempted=provider_call_attempted,
        prompt_name=prompt_name, prompt_version=prompt_version,
        prompt_sha256=prompt_sha256, deployment=deployment,
        model_version=model_version, usage=auditable_usage,
        status=status, incomplete_reason=incomplete_reason,
        committed=False, error_code=cast(ChatErrorCode, error_code),
    )
