"""Conversation state DTO (Day 7).

A conversation is an append-only message log owned by this application:
``store=False`` upstream (Day 5) means no server-side history exists to lean
on, so what enters this log — and what never does — is our decision.
"""

from pydantic import BaseModel, Field

from azgenai_lab.models.chat import Message


class Conversation(BaseModel):
    id: str
    messages: list[Message] = Field(default_factory=list)
