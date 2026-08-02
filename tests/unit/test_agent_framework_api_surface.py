"""Locks the agent-framework API facts the Day 17 spec (v7) relies on.

Not behavior tests: signature/constant assertions against the exact pins,
so a pin bump that invalidates a spec assumption fails here first.
"""

import inspect

import agent_framework._tools as af_tools
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient


def test_default_max_iterations_is_40() -> None:
    assert af_tools.DEFAULT_MAX_ITERATIONS == 40


def test_openai_client_accepts_async_client_and_invocation_config() -> None:
    params = inspect.signature(OpenAIChatClient.__init__).parameters
    assert "async_client" in params
    assert "function_invocation_configuration" in params


def test_agent_run_accepts_per_run_tools() -> None:
    assert "tools" in inspect.signature(Agent.run).parameters


def test_allow_multiple_tool_calls_is_wired() -> None:
    # Declared on ChatOptions and translated to provider parallel_tool_calls
    # by the openai client (spec v4 correction round).
    import agent_framework._types as af_types
    import agent_framework_openai._chat_client as af_openai_client

    assert "allow_multiple_tool_calls" in inspect.getsource(af_types)
    src = inspect.getsource(af_openai_client)
    assert '"allow_multiple_tool_calls": "parallel_tool_calls"' in src


def test_function_invocation_layer_is_in_openai_client_mro() -> None:
    # Limit-hit tests (Task 13) rely on the production MRO running the loop.
    mro = [c.__name__ for c in OpenAIChatClient.__mro__]
    assert "FunctionInvocationLayer" in mro
