"""Day 18 spike: how the pinned framework accepts externally-supplied history.

Probes `agent_framework` 1.13.0 directly (no app Protocol involved): the
adapter design in Task 7 is only allowed to assume what this file proves.
"""

import itertools
from typing import Any
from unittest.mock import AsyncMock

import openai
from agent_framework import Agent, Message
from agent_framework.openai import OpenAIChatClient
from openai.types.responses import Response, ResponseOutputMessage, ResponseOutputText
from openai.types.responses.response_usage import ResponseUsage

# Same stub shape as test_agent_real_adapter.py's `_RawResponseStub`: the
# adapter's `.parse()` seam for the SDK's raw-response wrapper.


class _RawResponseStub:
    def __init__(self, response: Response) -> None:
        self._response = response
        self.headers: dict[str, str] = {}

    def parse(self) -> Response:
        return self._response


def _final_text_response(counter: "itertools.count[int]", text: str) -> Response:
    index = next(counter)
    return Response(
        id="resp_mock",
        created_at=0.0,
        model="chat-mini",
        object="response",
        output=[
            ResponseOutputMessage(
                id=f"msg_{index}",
                type="message",
                role="assistant",
                status="completed",
                content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
            )
        ],
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        status="completed",
        usage=ResponseUsage.model_validate({
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 2},
        }),
    )


def _stubbed_chat_client(captured: list[dict[str, Any]]) -> OpenAIChatClient:
    async_client = openai.AsyncOpenAI(
        api_key="test-key",
        base_url="https://masked.example/openai/v1/",
    )
    counter = itertools.count()

    async def _create(**kwargs: Any) -> _RawResponseStub:
        captured.append(kwargs)
        return _RawResponseStub(_final_text_response(counter, "Nothing changed."))

    async_client.responses.with_raw_response.create = AsyncMock(side_effect=_create)
    return OpenAIChatClient(model="chat-mini", async_client=async_client)


async def test_messages_sequence_reaches_wire_in_order_task_once() -> None:
    captured: list[dict[str, Any]] = []
    client = _stubbed_chat_client(captured)
    agent = Agent(
        client=client, instructions="You are a test.", default_options={"store": False}
    )
    await agent.run(
        [
            Message("user", ["Hello"]),
            Message("assistant", ["Hi there"]),
            Message("user", ["what changed?"]),
        ]
    )
    assert captured
    body = captured[0]
    assert body["store"] is False
    roles = [item["role"] for item in body["input"] if item.get("role")]
    assert roles[:3] == ["user", "assistant", "user"]
    assert str(body["input"]).count("what changed?") == 1
