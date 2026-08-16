"""`--tenant-id` overrides every loaded document's front-matter tenant for a run.

Two things must both hold: the override, when supplied, must reach every
document (so the whole run lands under one tenant, not a mix), and its
absence must be a strict no-op (so `acme`/`globex`/`opsdemo` stay separate
tenants for the local workflow and Day 15's multi-tenant behave scenarios).
`_apply_tenant_override()` isolates that decision from the network, the
embedding client, and the corpus load itself -- the same isolation
`test_index_recreate.py` uses for `_rebuild_schema()`.
"""

import importlib.util
import sys
from dataclasses import fields
from datetime import date
from pathlib import Path

import pytest

# tools/ is not a package (no __init__.py, not installed) -- a plain file
# import, the same pattern `tests/unit/test_index_recreate.py` uses.
_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "index_corpus.py"
_SPEC = importlib.util.spec_from_file_location("index_corpus", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
index_corpus = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = index_corpus
_SPEC.loader.exec_module(index_corpus)

_build_parser = index_corpus._build_parser
_apply_tenant_override = index_corpus._apply_tenant_override
SourceDocument = index_corpus.SourceDocument


def _document(tenant_id: str, doc_id: str = "policy") -> "SourceDocument":  # type: ignore[name-defined]
    return SourceDocument(
        doc_id=doc_id,
        title="Policy",
        doc_type="policy",
        tenant_id=tenant_id,
        effective_date=date(2026, 1, 1),
        allowed_groups=("everyone",),
        body="Some prose.",
    )


def test_tenant_id_option_defaults_to_none() -> None:
    arguments = _build_parser().parse_args([])
    assert arguments.tenant_id is None


def test_tenant_id_option_parses_a_value() -> None:
    arguments = _build_parser().parse_args(["--tenant-id", "smoketenant"])
    assert arguments.tenant_id == "smoketenant"


def test_tenant_id_option_rejects_an_invalid_value() -> None:
    # validate_identifier's alphabet is [A-Za-z0-9_-]; a space is outside it.
    # Failing fast at argument parsing means an operator's typo surfaces
    # before any network call, not partway through an indexing run.
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--tenant-id", "not a tenant"])


def test_apply_tenant_override_is_a_no_op_when_absent() -> None:
    # This is the regression that matters most: Day 15's multi-tenant
    # behave scenarios and the local workflow depend on acme and globex
    # staying distinct tenants when the flag is not passed.
    documents = [_document("acme", "a"), _document("globex", "b")]

    result = _apply_tenant_override(documents, None)

    assert [document.tenant_id for document in result] == ["acme", "globex"]
    assert result == documents


def test_apply_tenant_override_replaces_every_documents_tenant_id() -> None:
    documents = [_document("acme", "a"), _document("globex", "b")]

    result = _apply_tenant_override(documents, "99999999-9999-9999-9999-999999999999")

    assert [document.tenant_id for document in result] == [
        "99999999-9999-9999-9999-999999999999",
        "99999999-9999-9999-9999-999999999999",
    ]
    # Every other field survives untouched -- this is a tenant_id override,
    # not a document rebuild.
    for original, overridden in zip(documents, result, strict=True):
        for field in fields(overridden):
            if field.name == "tenant_id":
                continue
            assert getattr(overridden, field.name) == getattr(original, field.name)
