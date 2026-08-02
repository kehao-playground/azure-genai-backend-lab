"""Front matter is validated at load time. A malformed sample document is an
authoring mistake, and the cheapest place to find it is here — not on Day 13
when the upload rejects the key.
"""

import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from azgenai_lab.core.config import Settings
from azgenai_lab.models.search_index import DocumentKeyError
from azgenai_lab.services.document_loader import (
    SAMPLE_DOCS_DIR,
    SourceDocumentError,
    load_document,
    load_documents,
)

_HEADING = re.compile(r"^(#{1,6}) (.+)$", re.MULTILINE)
_CJK = re.compile(r"[　-鿿＀-￯]")

VALID = """---
doc_id: returns-policy
title: Returns Policy
doc_type: policy
tenant_id: acme
effective_date: 2026-01-15
allowed_groups: []
---

# Returns Policy

We accept returns within 14 days.
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_in_tenant_dir(tmp_path: Path, tenant: str, filename: str, content: str) -> Path:
    return _write(tmp_path, f"{tenant}/{filename}", content)


def test_loads_a_valid_document(tmp_path: Path) -> None:
    document = load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", VALID))

    assert document.doc_id == "returns-policy"
    assert document.title == "Returns Policy"
    assert document.doc_type == "policy"
    assert document.tenant_id == "acme"
    assert document.effective_date == date(2026, 1, 15)
    assert document.allowed_groups == ()
    assert document.body.startswith("# Returns Policy")
    assert "doc_id" not in document.body


def test_doc_id_must_match_the_filename_stem(tmp_path: Path) -> None:
    with pytest.raises(SourceDocumentError, match="does not match filename"):
        load_document(_write_in_tenant_dir(tmp_path, "acme", "other-name.md", VALID))


@pytest.mark.parametrize(
    "field", ["doc_id", "title", "doc_type", "tenant_id", "effective_date", "allowed_groups"]
)
def test_missing_field_is_rejected(tmp_path: Path, field: str) -> None:
    content = "\n".join(
        line for line in VALID.splitlines() if not line.startswith(f"{field}:")
    )
    with pytest.raises(SourceDocumentError, match=field):
        load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", content))


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    content = VALID.replace("tenant_id: acme", "tenant_id: acme\nauthor: someone")
    with pytest.raises(SourceDocumentError, match="author"):
        load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", content))


def test_missing_front_matter_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SourceDocumentError, match="front matter"):
        load_document(
            _write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", "# No front matter\n")
        )


def test_effective_date_must_be_a_date(tmp_path: Path) -> None:
    content = VALID.replace("effective_date: 2026-01-15", "effective_date: yesterday")
    with pytest.raises(SourceDocumentError, match="effective_date"):
        load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", content))


def test_a_yaml_timestamp_effective_date_is_rejected(tmp_path: Path) -> None:
    # datetime is a subclass of date, so isinstance alone would let a
    # timestamp through a field that documents itself as date-only.
    content = VALID.replace(
        "effective_date: 2026-01-15", "effective_date: 2026-01-15T12:34:56Z"
    )
    with pytest.raises(SourceDocumentError, match="effective_date"):
        load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", content))


def test_a_timezone_naive_yaml_timestamp_is_rejected(tmp_path: Path) -> None:
    content = VALID.replace(
        "effective_date: 2026-01-15", "effective_date: 2026-01-15 12:34:56"
    )
    with pytest.raises(SourceDocumentError, match="effective_date"):
        load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", content))


def test_a_plain_yaml_date_is_still_accepted(tmp_path: Path) -> None:
    document = load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", VALID))

    assert document.effective_date == date(2026, 1, 15)


def test_empty_body_is_rejected(tmp_path: Path) -> None:
    content = VALID.split("---\n\n")[0] + "---\n\n"
    with pytest.raises(SourceDocumentError, match="body"):
        load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", content))


def test_doc_id_over_the_authoring_limit_is_rejected(tmp_path: Path) -> None:
    long_id = "a" * 65
    content = VALID.replace("doc_id: returns-policy", f"doc_id: {long_id}")
    with pytest.raises(SourceDocumentError, match="64"):
        load_document(_write_in_tenant_dir(tmp_path, "acme", f"{long_id}.md", content))


def test_doc_id_with_illegal_characters_is_rejected(tmp_path: Path) -> None:
    content = VALID.replace("doc_id: returns-policy", "doc_id: _returns")
    with pytest.raises((SourceDocumentError, DocumentKeyError)):
        load_document(_write_in_tenant_dir(tmp_path, "acme", "_returns.md", content))


def test_load_documents_reads_every_markdown_file(tmp_path: Path) -> None:
    _write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", VALID)
    globex_content = VALID.replace("returns-policy", "billing-faq").replace(
        "tenant_id: acme", "tenant_id: globex"
    )
    _write_in_tenant_dir(tmp_path, "globex", "billing-faq.md", globex_content)

    documents = load_documents(tmp_path)

    # Paths are walked in full sorted order (tenant directory first), so
    # "acme/returns-policy.md" sorts before "globex/billing-faq.md".
    assert [document.doc_id for document in documents] == ["returns-policy", "billing-faq"]


def test_allowed_groups_must_be_a_list(tmp_path: Path) -> None:
    content = VALID.replace("allowed_groups: []", "allowed_groups: finance")
    with pytest.raises(SourceDocumentError, match="allowed_groups"):
        load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", content))


def test_allowed_groups_duplicates_are_rejected(tmp_path: Path) -> None:
    content = VALID.replace("allowed_groups: []", "allowed_groups: [finance, finance]")
    with pytest.raises(SourceDocumentError, match="duplicate"):
        load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", content))


def test_allowed_groups_entries_are_validated_as_identifiers(tmp_path: Path) -> None:
    content = VALID.replace("allowed_groups: []", "allowed_groups: [not a valid group]")
    with pytest.raises(SourceDocumentError, match="allowed_groups"):
        load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", content))


def test_directory_and_front_matter_tenant_mismatch_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SourceDocumentError, match="tenant"):
        load_document(_write_in_tenant_dir(tmp_path, "globex", "returns-policy.md", VALID))


def test_non_identifier_tenant_directory_name_is_rejected(tmp_path: Path) -> None:
    content = VALID.replace("tenant_id: acme", "tenant_id: not valid")
    with pytest.raises(SourceDocumentError, match="tenant"):
        load_document(_write_in_tenant_dir(tmp_path, "not valid", "returns-policy.md", content))


def test_allowed_groups_propagates_from_document_to_chunk_to_index_document(
    tmp_path: Path,
) -> None:
    from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS
    from azgenai_lab.services.chunking import chunk_markdown

    content = VALID.replace("allowed_groups: []", "allowed_groups: [oncall]")
    document = load_document(_write_in_tenant_dir(tmp_path, "acme", "returns-policy.md", content))

    assert document.allowed_groups == ("oncall",)

    chunks = chunk_markdown(document, max_chars=2000, overlap_chars=500)
    assert all(chunk.allowed_groups == ("oncall",) for chunk in chunks)

    index_document = chunks[0].to_index_document([0.0] * EMBEDDING_DIMENSIONS)
    assert index_document["allowed_groups"] == ["oncall"]


def test_load_documents_rejects_an_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(SourceDocumentError, match="no documents"):
        load_documents(tmp_path)


def test_the_shipped_corpus_loads() -> None:
    documents = load_documents(SAMPLE_DOCS_DIR)

    # 7, not 4: the opsdemo tenant (3 docs) was added for the Day 17
    # ops-assistant demo's search_docs tool, alongside the original acme/
    # globex corpus (4 docs).
    assert len(documents) == 7
    assert {document.doc_id for document in documents} == {
        "billing-faq",
        "oncall-runbook",
        "returns-policy",
        "service-sla",
        "error-contract",
        "streaming-sse",
        "token-budget",
    }


# The tests below pin down structural properties of the shipped corpus that
# document_loader itself does not check (it validates front matter only, not
# headings). Later tasks — chunking (6-7), tenant filtering (15) — rely on
# these properties; a corpus test that would still pass with any one of them
# deleted would not be proving anything.


def test_the_shipped_corpus_has_no_cjk_characters() -> None:
    for path in sorted(SAMPLE_DOCS_DIR.glob("**/*.md")):
        assert not _CJK.search(path.read_text(encoding="utf-8")), (
            f"{path.name} contains a CJK character; this directory must stay English-only"
        )


def test_the_shipped_corpus_splits_tenants_two_two_and_three() -> None:
    # opsdemo (3 docs) joined the original acme/globex split (2 and 2) when
    # the Day 17 ops-assistant demo corpus was added.
    tenants = [document.tenant_id for document in load_documents(SAMPLE_DOCS_DIR)]
    assert sorted(tenants) == [
        "acme",
        "acme",
        "globex",
        "globex",
        "opsdemo",
        "opsdemo",
        "opsdemo",
    ]


def test_the_shipped_corpus_has_third_level_headings_in_at_least_two_files() -> None:
    files_with_h3 = [
        path
        for path in sorted(SAMPLE_DOCS_DIR.glob("**/*.md"))
        if re.search(r"^### ", path.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert len(files_with_h3) >= 2, "expected at least two documents with ### subsections"


def test_the_shipped_corpus_has_a_section_over_the_chunk_max_chars_threshold() -> None:
    threshold = Settings(_env_file=None).chunk_max_chars
    longest = 0
    for path in sorted(SAMPLE_DOCS_DIR.glob("**/*.md")):
        text = path.read_text(encoding="utf-8")
        headings = list(_HEADING.finditer(text))
        for index, match in enumerate(headings):
            if match.group(1) != "##":
                continue
            start = match.end()
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            longest = max(longest, len(text[start:end].strip()))
    assert longest > threshold, (
        f"longest ## section is {longest} characters; expected at least one section "
        f"over the {threshold}-character chunk_max_chars threshold so the oversize "
        "split path has real data to run against"
    )


def test_the_shipped_corpus_repeats_a_heading_under_two_different_parents() -> None:
    # A heading-path collision: the same heading text nested under two
    # distinct parent sections. Chunking must key chunks by full heading
    # path, not by leaf heading text alone — this fixture is what would catch
    # a chunker that got that wrong.
    for path in sorted(SAMPLE_DOCS_DIR.glob("**/*.md")):
        parents_by_heading: dict[str, set[str]] = defaultdict(set)
        current_h2 = ""
        for match in _HEADING.finditer(path.read_text(encoding="utf-8")):
            level, title = match.group(1), match.group(2)
            if level == "##":
                current_h2 = title
            elif level == "###":
                parents_by_heading[title].add(current_h2)
        if any(len(parents) > 1 for parents in parents_by_heading.values()):
            return
    pytest.fail(
        "no heading text repeats under two different parent sections anywhere "
        "in the corpus"
    )
