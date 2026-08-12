"""The opsdemo corpus must be discovered by the standard loader and carry
the content the Day 17 demo questions ground on."""

from pathlib import Path

from azgenai_lab.core.config import Settings
from azgenai_lab.services.agent_tools import _seed_index_documents
from azgenai_lab.services.document_loader import load_documents
from azgenai_lab.services.embeddings import FakeEmbeddingClient


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


SOLO_DOC = """---
doc_id: solo-doc
title: Solo Doc
doc_type: policy
tenant_id: solotenant
effective_date: 2026-01-15
allowed_groups: []
---

# Solo Doc

The only document in this corpus.
"""


def test_seeding_reads_the_configured_corpus_directory(tmp_path: Path) -> None:
    # An installed (non-editable) layout cannot find the repo-relative
    # default, so the corpus directory has to be configurable (Day 23).
    doc = tmp_path / "solotenant" / "solo-doc.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(SOLO_DOC, encoding="utf-8")

    documents = _seed_index_documents(
        FakeEmbeddingClient(), Settings(_env_file=None, sample_docs_dir=tmp_path)
    )

    assert len(documents) == 1
    assert documents[0]["tenant_id"] == "solotenant"
