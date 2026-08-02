"""Tests for run-trace extraction and app-owned stop-reason derivation
(Task 10). Both functions under test are pure: extract_run_shape walks a
framework transcript into an app-owned shape, derive_stop classifies why
the run stopped from the counts extract_run_shape produced.

Constructs real agent_framework Message/Content objects directly, which
doubles as the attribute-name verification the spec front-loads.
"""

from agent_framework import Content, Message

from azgenai_lab.services.agent_framework import (
    REFUSAL_MESSAGE,
    derive_stop,
    extract_run_shape,
)


def _assistant_call(name: str, arguments: str, call_id: str) -> Message:
    return Message(
        role="assistant",
        contents=[Content.from_function_call(call_id=call_id, name=name, arguments=arguments)],
    )


def _tool_result(call_id: str, result: str) -> Message:
    return Message(
        role="tool",
        contents=[Content.from_function_result(call_id=call_id, result=result)],
    )


def _assistant_text(text: str) -> Message:
    return Message(role="assistant", contents=[Content.from_text(text)])


def test_shape_of_two_round_run() -> None:
    messages = [
        _assistant_call("search_docs", '{"query": "budget"}', "c1"),
        _tool_result("c1", '{"hits": []}'),
        _assistant_call("get_runtime_config", "{}", "c2"),
        _tool_result("c2", '{"llm_max_output_tokens": 1000}'),
        _assistant_text("final answer"),
    ]
    shape = extract_run_shape(messages, executions=[], refusal_message=REFUSAL_MESSAGE)
    assert shape.answer == "final answer"  # terminal response text ONLY
    assert shape.model_call_count == 3
    assert shape.tool_round_count == 2
    calls = shape.tool_calls
    assert [c.tool_name for c in calls] == ["search_docs", "get_runtime_config"]
    assert calls[0].round_index == 1 and calls[1].round_index == 2  # 1-based
    assert calls[0].arguments == {"query": "budget"}
    assert all(c.executed for c in calls)


def test_refused_call_is_tagged_not_executed() -> None:
    messages = [
        _assistant_call("search_docs", '{"query": "q"}', "c1"),
        _tool_result("c1", REFUSAL_MESSAGE),
        _assistant_text("done without that tool"),
    ]
    shape = extract_run_shape(messages, executions=[], refusal_message=REFUSAL_MESSAGE)
    assert [c.executed for c in shape.tool_calls] == [False]


def test_unparseable_arguments_preserved_canonically() -> None:
    messages = [
        _assistant_call("search_docs", "{not json", "c1"),
        _tool_result("c1", '{"hits": []}'),
        _assistant_text("x"),
    ]
    [call] = extract_run_shape(
        messages, executions=[], refusal_message=REFUSAL_MESSAGE
    ).tool_calls
    assert call.arguments is None
    assert call.arguments_canonical_json == "{not json"


def test_earlier_round_text_never_concatenated_into_answer() -> None:
    thinking = Message(
        role="assistant",
        contents=[
            Content.from_text("let me check"),
            Content.from_function_call(call_id="c1", name="search_docs", arguments="{}"),
        ],
    )
    messages = [thinking, _tool_result("c1", "{}"), _assistant_text("answer")]
    shape = extract_run_shape(messages, executions=[], refusal_message=REFUSAL_MESSAGE)
    assert shape.answer == "answer"


def test_stop_reason_precedence_and_labels() -> None:
    # natural
    assert derive_stop(3, executed=2, refused=0, max_iterations=5, max_tool_calls=10) == (
        "natural",
        frozenset(),
    )
    # iteration limit: forced final => max_iterations + 1 model calls
    assert derive_stop(6, executed=5, refused=0, max_iterations=5, max_tool_calls=10) == (
        "iteration_limit",
        frozenset({"iteration_limit"}),
    )
    # function-call limit via refusals or executed cap
    assert (
        derive_stop(4, executed=3, refused=1, max_iterations=5, max_tool_calls=10)[0]
        == "function_call_limit"
    )
    assert (
        derive_stop(4, executed=10, refused=0, max_iterations=5, max_tool_calls=10)[0]
        == "function_call_limit"
    )
    # both fire in one round: iteration_limit wins the single label, set carries both
    stop, reasons = derive_stop(6, executed=10, refused=0, max_iterations=5, max_tool_calls=10)
    assert stop == "iteration_limit"
    assert reasons == frozenset({"iteration_limit", "function_call_limit"})
