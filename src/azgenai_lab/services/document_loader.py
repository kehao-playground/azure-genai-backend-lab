"""Load authored knowledge-base files into SourceDocument (indexing stage: load).

Front matter is a closed set of five fields, validated the same way prompt
templates are (Day 8): strictly, at load time, with the filename as the check
on identity.
"""

from datetime import date
from pathlib import Path

import yaml

from azgenai_lab.models.rag import DOC_ID_MAX_LENGTH, SourceDocument
from azgenai_lab.models.search_index import DocumentKeyError, validate_document_key

# Repo-relative: the corpus is sample data checked into the repository, not
# packaged with the wheel. Tests pass an explicit directory.
SAMPLE_DOCS_DIR = Path(__file__).resolve().parents[3] / "data" / "sample-docs"

_DELIMITER = "---"
_REQUIRED_FIELDS = ("doc_id", "title", "doc_type", "tenant_id", "effective_date")
_KNOWN_FIELDS = frozenset(_REQUIRED_FIELDS)


class SourceDocumentError(Exception):
    """A source document is missing, malformed, or inconsistent."""


def load_document(path: Path) -> SourceDocument:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith(_DELIMITER + "\n"):
        raise SourceDocumentError(f"{path.name}: missing YAML front matter block")

    front, separator, body = raw.removeprefix(_DELIMITER + "\n").partition(
        "\n" + _DELIMITER + "\n"
    )
    if not separator:
        raise SourceDocumentError(f"{path.name}: missing YAML front matter close")

    try:
        meta = yaml.safe_load(front)
    except yaml.YAMLError as exc:
        raise SourceDocumentError(f"{path.name}: invalid YAML front matter: {exc}") from exc
    if not isinstance(meta, dict):
        raise SourceDocumentError(f"{path.name}: front matter must be a YAML mapping")

    unknown = sorted(set(meta) - _KNOWN_FIELDS)
    if unknown:
        raise SourceDocumentError(
            f"{path.name}: unknown front matter field(s): {', '.join(unknown)}"
        )
    for field in _REQUIRED_FIELDS:
        if field not in meta:
            raise SourceDocumentError(f"{path.name}: front matter missing field: {field}")

    for field in ("doc_id", "title", "doc_type", "tenant_id"):
        value = meta[field]
        if not isinstance(value, str) or not value.strip():
            raise SourceDocumentError(f"{path.name}: {field} must be a non-empty string")
    # datetime is a subclass of date, so isinstance(..., date) would also
    # accept a YAML timestamp. The exact-type check is intentional: this
    # field is date-only, and the schema field it eventually feeds
    # (Edm.DateTimeOffset) is a separate seam that a future reader must not
    # paper over here by relaxing this back to isinstance.
    if type(meta["effective_date"]) is not date:
        raise SourceDocumentError(
            f"{path.name}: effective_date must be a YAML date (YYYY-MM-DD), "
            "not a timestamp"
        )

    doc_id: str = meta["doc_id"]
    if doc_id != path.stem:
        raise SourceDocumentError(
            f"{path.name}: doc_id {doc_id!r} does not match filename"
        )
    if len(doc_id) > DOC_ID_MAX_LENGTH:
        raise SourceDocumentError(
            f"{path.name}: doc_id is {len(doc_id)} characters; the limit is "
            f"{DOC_ID_MAX_LENGTH} so that derived chunk keys stay within the "
            "search service's own limit"
        )
    try:
        validate_document_key(doc_id, field="doc_id")
    except DocumentKeyError as exc:
        raise SourceDocumentError(f"{path.name}: {exc}") from exc

    text = body.strip()
    if not text:
        raise SourceDocumentError(f"{path.name}: document body is empty")

    return SourceDocument(
        doc_id=doc_id,
        title=meta["title"],
        doc_type=meta["doc_type"],
        tenant_id=meta["tenant_id"],
        effective_date=meta["effective_date"],
        body=text,
    )


def load_documents(base_dir: Path = SAMPLE_DOCS_DIR) -> list[SourceDocument]:
    paths = sorted(base_dir.glob("*.md"))
    if not paths:
        raise SourceDocumentError(f"no documents found in {base_dir}")
    return [load_document(path) for path in paths]
