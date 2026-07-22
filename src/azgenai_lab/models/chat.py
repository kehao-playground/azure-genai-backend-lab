from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    # Frozen: messages are entries in an append-only log; letting callers
    # mutate a shared instance would rewrite committed history through the
    # alias (Day 7 review r01 finding 6).
    model_config = ConfigDict(frozen=True)

    role: str = Field(pattern="^(system|user|assistant|tool)$")
    content: str = Field(min_length=1)


class TokenUsage(BaseModel):
    """Provider-reported token counts from the Responses API ``usage`` block (Day 9).

    This is metering, not estimation — but it is a request-level usage signal
    for attribution and guardrails, not a billing record: the invoice and Cost
    Management meter records remain the source of truth, and no per-request
    1:1 reconciliation against them is implied. ``input_tokens`` covers the
    full replay context, which grows every turn.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    # Hidden reasoning tokens, from usage.output_tokens_details — a subset of
    # output_tokens. None when the provider omits the detail.
    reasoning_tokens: int | None = Field(default=None, ge=0)
