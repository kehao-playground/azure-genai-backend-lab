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


def test_corpus_dir_and_index_name_default_to_the_built_ins() -> None:
    from tools.index_corpus import _build_parser, _resolve_corpus_dir

    from azgenai_lab.core.config import Settings
    from azgenai_lab.models.search_index import INDEX_NAME
    from azgenai_lab.services.document_loader import SAMPLE_DOCS_DIR

    arguments = _build_parser().parse_args([])
    # Unset at the parser, because the fallback below needs settings the
    # parser is built before reading.
    assert arguments.corpus_dir is None
    assert arguments.index_name == INDEX_NAME
    assert _resolve_corpus_dir(arguments.corpus_dir, Settings()) == SAMPLE_DOCS_DIR


def test_an_unset_corpus_dir_still_honours_the_sample_docs_dir_setting(
    tmp_path: Path,
) -> None:
    """`SAMPLE_DOCS_DIR` is a documented setting (`docs/docker.md`,
    `docs/container-apps.md`) that `services/agent_tools.py` and
    `tools/eval_run.py` both honour. A tool that quietly stopped reading it
    would index a different corpus than the rest of the lab, with nothing
    saying so.
    """
    from tools.index_corpus import _resolve_corpus_dir

    from azgenai_lab.core.config import Settings

    assert _resolve_corpus_dir(None, Settings(sample_docs_dir=tmp_path)) == tmp_path


def test_an_explicit_corpus_dir_wins_over_the_setting(tmp_path: Path) -> None:
    # The experiment pins its corpus with an argument on purpose; an
    # environment variable must not be able to redirect a named one.
    from tools.index_corpus import _resolve_corpus_dir

    from azgenai_lab.core.config import Settings

    other = tmp_path / "elsewhere"
    assert _resolve_corpus_dir(other, Settings(sample_docs_dir=tmp_path)) == other


def test_corpus_dir_and_index_name_are_overridable(tmp_path) -> None:
    from tools.index_corpus import _build_parser

    arguments = _build_parser().parse_args(
        ["--corpus-dir", str(tmp_path), "--index-name", "azgenai-lab-chunks-g3"]
    )
    assert arguments.corpus_dir == tmp_path
    assert arguments.index_name == "azgenai-lab-chunks-g3"
