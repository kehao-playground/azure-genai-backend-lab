"""Conversation state DTOs (Day 7).

A conversation is an append-only log owned by this application: ``store=False``
upstream (Day 5) means no server-side history exists to lean on, so what enters
this log — and what never does — is our decision.

Each committed turn is recorded twice, at different fidelities:

- ``messages`` — the client-visible transcript (display, audit, the fake).
- ``replay_items`` — the provider-shaped items that must be resent verbatim as
  the next request's input: the user input item plus every response output
  item, **including encrypted reasoning items**. With ``store=False`` and a
  reasoning model, replaying only the visible text silently drops reasoning
  context between turns — the transcript alone is a projection, never the
  model's context (review r01 finding 1).

Replay items stay opaque ``dict``s at this layer: they are provider payload,
not domain objects, and handlers never look inside them.
"""

from typing import Any

from pydantic import BaseModel, Field

from azgenai_lab.models.chat import Message

ReplayItem = dict[str, Any]


class Conversation(BaseModel):
    id: str
    messages: list[Message] = Field(default_factory=list)
    replay_items: list[ReplayItem] = Field(default_factory=list)
    # Number of committed turns; the token a conditional append must present.
    revision: int = 0
    # Billed tokens accumulated across committed turns (Day 9): the ledger the
    # budget guardrail reads. Failed turns are billed upstream but leave no
    # trace here — turn-commit semantics win over billing completeness, and
    # the gap is disclosed rather than papered over.
    total_tokens: int = 0
