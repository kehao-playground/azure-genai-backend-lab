from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    # Frozen: messages are entries in an append-only log; letting callers
    # mutate a shared instance would rewrite committed history through the
    # alias (Day 7 review r01 finding 6).
    model_config = ConfigDict(frozen=True)

    role: str = Field(pattern="^(system|user|assistant|tool)$")
    content: str = Field(min_length=1)
