"""Day 27: /agent's spans come from agent-framework, and we leave them alone.

This module adds no spans. It holds three things instead: that the framework
really does emit under this composition, that its span names and gen_ai
attributes still line up with the ones raised on /chat and /rag, and that the
counts are not quietly collapsed.

The framework emits as soon as a global TracerProvider exists -- enable_
instrumentation defaults to True and its get_tracer is a thin wrapper over the
global one -- so "we left it alone" is a real property with a real failure
mode, not a formality.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.unit.telemetry_helpers import attribute_names, children_named, span_tree
from tests.unit.test_agent_real_adapter import OPS, _service


async def _run_agent(monkeypatch) -> tuple[InMemorySpanExporter, object]:
    """Drive the real adapter with its SDK seam mocked, under a fresh exporter.

    The default fake agent never enters the framework at all, so a test written
    against it would assert nothing about framework spans while looking green.
    tests/unit/test_agent_real_adapter.py already owns the mocking of the one
    transport call the non-streaming path makes; this reuses it rather than
    inventing a second version that could drift.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider, raising=False)

    from azgenai_lab.core.config import get_settings
    from azgenai_lab.core.telemetry import configure_telemetry

    configure_telemetry(get_settings())

    service = _service(sequential=True, final_text="done")
    try:
        result = await service.run("look something up", (), principal=OPS)
    finally:
        await service.aclose()
    return exporter, result


async def test_framework_emits_the_agent_tree(monkeypatch, telemetry_enabled) -> None:
    exporter, _ = await _run_agent(monkeypatch)

    names = [span.name for span in exporter.get_finished_spans()]
    assert [name for name in names if name.startswith("invoke_agent")] != []
    assert [name for name in names if name.startswith("chat ")] != []
    assert [name for name in names if name.startswith("execute_tool ")] != []


async def test_span_counts_match_the_run_result(monkeypatch, telemetry_enabled) -> None:
    exporter, result = await _run_agent(monkeypatch)

    nodes = span_tree(exporter)
    chat_spans = children_named(nodes, "invoke_agent", "chat ")
    tool_spans = children_named(nodes, "invoke_agent", "execute_tool ")
    # Neither count may be hardcoded. One tool round is already two model calls,
    # and "an agent costs more because it runs the model more, not because it
    # runs tools" is the Day 16/17 conclusion this tree exists to make visible.
    # AgentResponse has no tool_call_count field -- the executed trace is
    # tool_calls, and its length is the number that matters.
    assert len(chat_spans) == result.model_call_count
    assert len(tool_spans) == len(result.tool_calls)
    assert len(chat_spans) > 1


async def test_framework_and_our_own_chat_spans_share_a_shape(
    monkeypatch, telemetry_enabled
) -> None:
    exporter, _ = await _run_agent(monkeypatch)

    chat_spans = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    assert chat_spans != []
    for span in chat_spans:
        attrs = span.attributes or {}
        # The same keys TracingChatService writes on /chat and /rag. Measured
        # rather than assumed: the framework names its span
        # "chat {deployment}" too, which is why the alignment is a decision we
        # can hold rather than a coincidence we hope for. A framework upgrade
        # that renames either turns this red instead of quietly splitting the
        # two trees apart.
        assert "gen_ai.operation.name" in attrs
        assert "gen_ai.request.model" in attrs


async def test_agent_spans_carry_no_tool_arguments_or_content(
    monkeypatch, telemetry_enabled
) -> None:
    exporter, _ = await _run_agent(monkeypatch)

    # enable_sensitive_data is off, so the framework must not be recording
    # prompts, completions or tool arguments. This is the assertion that would
    # notice if that switch stopped taking effect.
    for name in attribute_names(exporter):
        lowered = name.lower()
        for forbidden in ("argument", "content", "prompt", "completion"):
            assert forbidden not in lowered, f"{name} looks like it carries content"
