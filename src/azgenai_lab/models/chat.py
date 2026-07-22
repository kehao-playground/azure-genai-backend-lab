from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    # Frozen: messages are entries in an append-only log; letting callers
    # mutate a shared instance would rewrite committed history through the
    # alias (Day 7 review r01 finding 6).
    model_config = ConfigDict(frozen=True)

    role: str = Field(pattern="^(system|user|assistant|tool)$")
    content: str = Field(min_length=1)


class TokenUsage(BaseModel):
    """Token counts as billed upstream, reported by the Responses API (Day 9).

    This is metering, not estimation: the numbers come from the provider's
    ``usage`` block, the same numbers the invoice is built from. ``input_tokens``
    covers the full replay context (history grows it every turn), so it is the
    number that dominates conversation cost.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
