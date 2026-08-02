"""The opsdemo corpus must be discovered by the standard loader and carry
the content the Day 17 demo questions ground on."""

from azgenai_lab.services.document_loader import load_documents


def test_opsdemo_tenant_is_discovered() -> None:
    docs = load_documents()
    ops = [d for d in docs if d.tenant_id == "opsdemo"]
    assert {d.doc_id for d in ops} == {"token-budget", "error-contract", "streaming-sse"}
    for d in ops:
        assert d.allowed_groups == ()  # tenant-wide: any opsdemo principal may read


def test_opsdemo_covers_demo_question_grounding() -> None:
    docs = {d.doc_id: d for d in load_documents() if d.tenant_id == "opsdemo"}
    budget = docs["token-budget"].body
    assert "token_budget_exceeded" in budget
    assert "429" in budget
    assert "new conversation" in budget
