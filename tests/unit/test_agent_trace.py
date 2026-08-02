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
    ToolExecution,
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


def test_zero_argument_forms_are_distinguishable_from_each_other_and_from_malformed() -> None:
    # Absent: no `arguments` kwarg passed at all -> attribute is None.
    absent_call = Content.from_function_call(call_id="c1", name="get_runtime_config")
    # Empty string: the provider sent literal "" for a no-arg call.
    empty_str_call = Content.from_function_call(
        call_id="c2", name="get_runtime_config", arguments=""
    )
    # Empty mapping: the provider (or a test double) handed back {} directly.
    empty_map_call = Content.from_function_call(
        call_id="c3", name="get_runtime_config", arguments={}
    )
    # Genuinely malformed JSON text.
    malformed_call = Content.from_function_call(
        call_id="c4", name="search_docs", arguments="{not json"
    )

    messages = [
        Message(role="assistant", contents=[absent_call]),
        Message(role="assistant", contents=[empty_str_call]),
        Message(role="assistant", contents=[empty_map_call]),
        Message(role="assistant", contents=[malformed_call]),
    ]
    calls = extract_run_shape(messages, executions=[], refusal_message=REFUSAL_MESSAGE).tool_calls
    absent, empty_str, empty_map, malformed = calls

    assert absent.arguments is None
    assert absent.arguments_canonical_json == ""

    assert empty_str.arguments == {}
    assert empty_str.arguments_canonical_json == "{}"

    assert empty_map.arguments == {}
    assert empty_map.arguments_canonical_json == "{}"

    assert malformed.arguments is None
    assert malformed.arguments_canonical_json == "{not json"

    # Distinguishable as groups: absent != malformed (both None-arguments,
    # different canonical text); empty forms != absent and != malformed.
    assert (absent.arguments, absent.arguments_canonical_json) != (
        malformed.arguments,
        malformed.arguments_canonical_json,
    )
    assert (empty_str.arguments, empty_str.arguments_canonical_json) != (
        absent.arguments,
        absent.arguments_canonical_json,
    )
    assert (empty_map.arguments, empty_map.arguments_canonical_json) != (
        malformed.arguments,
        malformed.arguments_canonical_json,
    )


def test_arguments_canonical_json_is_reserialized_not_raw() -> None:
    call_a = _assistant_call("search_docs", '{"a": 1, "b": 2}', "c1")
    call_b = _assistant_call("search_docs", '{"b": 2, "a": 1}', "c2")
    messages = [call_a, call_b]
    shape = extract_run_shape(messages, executions=[], refusal_message=REFUSAL_MESSAGE)
    canon_a, canon_b = (c.arguments_canonical_json for c in shape.tool_calls)
    assert canon_a == canon_b
    assert canon_a == '{"a": 1, "b": 2}'


def test_non_object_parsed_arguments_routed_to_unparseable() -> None:
    list_call = _assistant_call("search_docs", "[1,2]", "c1")
    null_call = _assistant_call("search_docs", "null", "c2")
    messages = [list_call, null_call]
    shape = extract_run_shape(messages, executions=[], refusal_message=REFUSAL_MESSAGE)
    list_result, null_result = shape.tool_calls
    assert list_result.arguments is None
    assert list_result.arguments_canonical_json == "[1,2]"
    assert null_result.arguments is None
    assert null_result.arguments_canonical_json == "null"


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


def test_text_less_terminal_message_yields_honest_empty_answer() -> None:
    # A middle round carries text ("checking..."); the terminal round is a
    # tool call with no accompanying text (e.g. reasoning-only completion).
    # `answer` must not fall back to the middle round's text.
    middle = Message(
        role="assistant",
        contents=[
            Content.from_text("checking..."),
            Content.from_function_call(call_id="c1", name="search_docs", arguments="{}"),
        ],
    )
    terminal = Message(
        role="assistant",
        contents=[
            Content.from_function_call(call_id="c2", name="get_runtime_config", arguments="{}")
        ],
    )
    messages = [middle, _tool_result("c1", "{}"), terminal, _tool_result("c2", "{}")]
    shape = extract_run_shape(messages, executions=[], refusal_message=REFUSAL_MESSAGE)
    assert shape.answer == ""


def test_missing_call_id_result_is_not_stored_under_literal_none_key() -> None:
    # A function_result with no call_id must be skipped when building the
    # result map, not keyed under the literal string "None" -- otherwise a
    # function_call that also lacks a call_id would cross-assign this
    # result (here, incorrectly reading as refused).
    call_without_id = Content.from_function_call(
        call_id=None, name="search_docs", arguments="{}"
    )
    result_without_id = Content.from_function_result(call_id=None, result=REFUSAL_MESSAGE)
    messages = [
        Message(role="assistant", contents=[call_without_id]),
        Message(role="tool", contents=[result_without_id]),
        _assistant_text("done"),
    ]
    shape = extract_run_shape(messages, executions=[], refusal_message=REFUSAL_MESSAGE)
    assert shape.tool_calls[0].executed is True


def test_per_round_latency_from_clean_join() -> None:
    messages = [
        _assistant_call("search_docs", '{"query": "budget"}', "c1"),
        _tool_result("c1", '{"hits": []}'),
        _assistant_call("get_runtime_config", "{}", "c2"),
        _tool_result("c2", REFUSAL_MESSAGE),
        _assistant_text("final answer"),
    ]
    executions = [
        ToolExecution("search_docs", executed=True, latency_ms=12.5),
        ToolExecution("get_runtime_config", executed=False, latency_ms=0.0),
    ]
    shape = extract_run_shape(messages, executions=executions, refusal_message=REFUSAL_MESSAGE)
    assert shape.per_round is not None
    by_round = {r.round_index: r for r in shape.per_round}
    assert by_round[1].latency_ms == 12.5
    assert by_round[1].usage is None
    assert by_round[2].latency_ms == 0.0
    assert by_round[2].usage is None


def test_per_round_latency_join_rejected_on_length_mismatch() -> None:
    messages = [
        _assistant_call("search_docs", '{"query": "budget"}', "c1"),
        _tool_result("c1", '{"hits": []}'),
        _assistant_text("final answer"),
    ]
    executions = [
        ToolExecution("search_docs", executed=True, latency_ms=12.5),
        ToolExecution("search_docs", executed=True, latency_ms=5.0),  # extra
    ]
    shape = extract_run_shape(messages, executions=executions, refusal_message=REFUSAL_MESSAGE)
    assert shape.per_round is None


def test_per_round_latency_join_rejected_on_name_mismatch() -> None:
    messages = [
        _assistant_call("search_docs", '{"query": "budget"}', "c1"),
        _tool_result("c1", '{"hits": []}'),
        _assistant_text("final answer"),
    ]
    executions = [
        ToolExecution("get_runtime_config", executed=True, latency_ms=12.5),  # wrong name
    ]
    shape = extract_run_shape(messages, executions=executions, refusal_message=REFUSAL_MESSAGE)
    assert shape.per_round is None


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


def test_stop_reason_boundary_exact_max_iterations_is_natural() -> None:
    # model_call_count == max_iterations (not max_iterations + 1) is a
    # natural stop, not a forced final.
    assert derive_stop(5, executed=0, refused=0, max_iterations=5, max_tool_calls=10) == (
        "natural",
        frozenset(),
    )


def test_stop_reason_boundary_one_below_max_tool_calls_is_natural() -> None:
    # executed == max_tool_calls - 1, no refusals: budget not exhausted.
    assert derive_stop(3, executed=9, refused=0, max_iterations=5, max_tool_calls=10) == (
        "natural",
        frozenset(),
    )
