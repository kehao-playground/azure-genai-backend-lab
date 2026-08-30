"""Assemble a distractor corpus from real repository text.

Source paths are runtime arguments, never hard-coded: this tool ships in the
public lab while one of its inputs (unpublished planning drafts) must never
reach it. What ships is the rule, not the material.

Three failure modes this file exists to make loud:

* Two source files mapping to one ``doc_id``. Ids come from the source-root
  relative path, not the basename -- seven distinct ``README.md`` files live
  under the lab's docs tree. Collision still aborts, as a backstop against a
  slug rule that maps two paths together.
* A document that answers a frozen query being indexed as a distractor. The
  exclusion scan is preregistered and its hits are published in the manifest.
  It catches literal leakage only; semantic relevance is caught by the
  author's preregistration pass, not here.
* A manifest that cannot be reconciled. Serialization is deterministic and
  the digest is detached, because a file cannot contain the hash of its own
  complete bytes.

Usage:
    uv run python tools/build_distractor_corpus.py \\
        --english-root docs --chinese-root ../drafts/zh-tw \\
        --chinese-glob 'day-0*.md' \\
        --tenant-id acme --effective-date 2026-08-30 --out-dir /tmp/bonus7/g3

    # Then, and this step is not optional: copy the base corpus in beside
    # the distractors, because an index generation is base corpus *plus*
    # distractors and this tool writes only the distractors.
    cp -R data/sample-docs/* /tmp/bonus7/g3/

    # It cannot be done first: `--out-dir` must be absent or empty, so a
    # directory already holding the base corpus is refused. Skipping it is
    # silent -- `index_corpus.py --corpus-dir /tmp/bonus7/g3` succeeds, and
    # every pre-registered chunk then reads `absent` with no error anywhere.

The output directory belongs outside the repository (``/tmp/bonus7/`` is what
the measured run used). Building it inside the worktree makes
``git status --porcelain`` non-empty, and ``compare_retrieval.py`` refuses to
spend a run against a tree it cannot name a commit for.
"""

import argparse
import fnmatch
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from azgenai_lab.models.principal import validate_identifier

# Frozen before the run. Rule 1 is every frozen query string, rule 2 every
# base-corpus doc_id, rule 3 the ranking figures this series has published.
# Extending this list after seeing rankings is barred: the exclusions are
# part of the preregistration, not a cleanup pass.
FROZEN_QUERY_TERMS = (
    "99.9% monthly uptime",
    "How long do I have to send something back",
    "when do customers get credit",
    "what happens if the customer misconfigured their own system",
    "how do I escalate a Sev 1 outage at 3am",
    "What is the parental leave policy",
    "how are invoices delivered",
    "what cards can I pay with",
    "how do I dispute a charge",
)
BASE_CORPUS_DOC_IDS = (
    "service-sla",
    "returns-policy",
    "billing-faq",
    "oncall-runbook",
    "error-contract",
    "streaming-sse",
    "token-budget",
)
PUBLISHED_RANKING_FIGURES = ("0.032796", "0.032787", "2.113", "1.104")

MANIFEST_FORBIDDEN_KEYS = frozenset({"sha256", "manifest_sha256", "digest"})

# The labels that prefix every doc_id, one per source root. They are part of
# the id, so changing one renames every document of that root.
ENGLISH_LABEL = "labdocs"
CHINESE_LABEL = "draft"

MANIFEST_NAME = "manifest.json"
DIGEST_NAME = "manifest.json.sha256"

_FRONT_MATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n\s*", re.DOTALL)
_ILLEGAL = re.compile(r"[^0-9a-z]+")
_HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ExclusionRule:
    number: int
    description: str
    terms: tuple[str, ...]


EXCLUSION_RULES: tuple[ExclusionRule, ...] = (
    ExclusionRule(1, "contains a frozen query string verbatim", FROZEN_QUERY_TERMS),
    ExclusionRule(2, "names a base-corpus doc_id", BASE_CORPUS_DOC_IDS),
    ExclusionRule(3, "quotes published ranking figures", PUBLISHED_RANKING_FIGURES),
)


@dataclass(frozen=True)
class SourceFile:
    root_label: str
    relative_path: Path


@dataclass(frozen=True)
class Manifest:
    """What one run actually wrote, and the detached digest of its manifest."""

    out_dir: Path
    manifest_path: Path
    digest_path: Path
    digest: str
    data: dict[str, Any]


def strip_front_matter(text: str) -> str:
    """Remove a leading YAML front matter block, if there is one.

    Drafts carry review metadata (decision refs, body hashes) in front
    matter; lab docs carry none. Indexing the metadata would put private
    planning material into a search service and would also add text nobody
    wrote as prose.
    """
    return _FRONT_MATTER.sub("", text)


def slug_for(source_root_label: str, relative_path: Path) -> str:
    """Derive a ``doc_id`` from the source-root-relative path."""
    stem = relative_path.with_suffix("")
    body = _ILLEGAL.sub("-", str(stem).lower()).strip("-")
    return validate_identifier(f"{source_root_label}-{body}", field="doc_id")


def scan_exclusions(text: str) -> tuple[int, ...]:
    """Return the numbers of every exclusion rule this text hits."""
    lowered = text.lower()
    return tuple(
        rule.number
        for rule in EXCLUSION_RULES
        if any(term.lower() in lowered for term in rule.terms)
    )


def interleave(
    english: Sequence[SourceFile], chinese: Sequence[SourceFile]
) -> list[SourceFile]:
    """Alternate the two sorted lists, English first, remainder appended.

    A generation cut taken from a language-sorted list would change the
    language mix and the size together. Alternating holds the ratio roughly
    steady so the cut moves size alone -- roughly, because the two languages
    do not produce chunks at the same rate per file.
    """
    merged: list[SourceFile] = []
    for index in range(max(len(english), len(chinese))):
        if index < len(english):
            merged.append(english[index])
        if index < len(chinese):
            merged.append(chinese[index])
    return merged


def serialize_manifest(manifest: dict[str, Any]) -> bytes:
    """One input, one byte sequence -- so the detached digest means something."""
    for key in MANIFEST_FORBIDDEN_KEYS:
        if key in manifest:
            raise ValueError(
                f"manifest must not contain {key!r}: the digest is detached"
            )
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    return (text + "\n").encode("utf-8")


def digest_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _title_of(body: str, fallback: str) -> str:
    """The first ATX H1, or the slug when the document has none."""
    match = _HEADING.search(body)
    return match.group(1) if match else fallback


def _collect(root: Path, label: str, globs: Sequence[str] | None = None) -> list[SourceFile]:
    """Every ``*.md`` under ``root``, sorted by source-root-relative path.

    ``globs`` filters by **file name** -- not by the relative path, so a
    pattern never has to know how deep a file sits -- and a file is admitted
    when it matches at least one pattern. ``None`` means no filter at all,
    and only the English root may be collected that way -- every file in the
    lab's docs tree is publishable. ``build`` refuses a Chinese root with no
    patterns rather than falling back to this.
    """
    return [
        SourceFile(label, path.relative_to(root))
        for path in sorted(root.rglob("*.md"))
        if globs is None or any(fnmatch.fnmatchcase(path.name, glob) for glob in globs)
    ]


def _render(
    *, doc_id: str, title: str, tenant_id: str, effective_date: date, body: str
) -> bytes:
    """The six front-matter fields `services/document_loader.py` requires."""
    front = yaml.safe_dump(
        {
            "doc_id": doc_id,
            "title": title,
            "doc_type": "reference",
            "tenant_id": tenant_id,
            "effective_date": effective_date,
            "allowed_groups": [],
        },
        sort_keys=False,
        allow_unicode=True,
    )
    return f"---\n{front}---\n\n{body}\n".encode()


def build(
    english_root: Path,
    chinese_root: Path | None,
    out_dir: Path,
    tenant_id: str,
    effective_date: str,
    limit: int | None,
    chinese_globs: Sequence[str] | None = None,
) -> Manifest:
    """Assemble one corpus generation and return its manifest.

    ``limit`` cuts the interleaved, already-admitted list, so a smaller
    generation is a prefix of a larger one at the same language mix.

    ``chinese_globs`` selects which drafts are eligible, matched against each
    file's **name** (see ``_collect``). It is required whenever
    ``chinese_root`` is given and there is deliberately no permissive
    default: the English root needs no filter because every file in the
    lab's docs tree is already public, while the drafts root holds
    unpublished articles that must not reach a search service.
    """
    validate_identifier(tenant_id, field="tenant_id")
    parsed_date = date.fromisoformat(effective_date)
    if limit is not None and limit < 1:
        raise SystemExit(f"--limit must be positive, got {limit}")

    if out_dir.exists() and any(out_dir.iterdir()):
        # Leftovers from an earlier generation would be indexed alongside
        # this one and counted by neither manifest.
        raise SystemExit(f"{out_dir} is not empty; remove it before rebuilding a corpus")

    # rglob() over a path that does not exist yields nothing rather than
    # raising, so without this a mistyped root builds a corpus that is a
    # whole language short and says nothing about it.
    for root_path in (english_root, chinese_root):
        if root_path is not None and not root_path.is_dir():
            raise SystemExit(f"{root_path} is not a directory")

    if chinese_root is not None and not chinese_globs:
        # Fail closed. An omitted filter is a mistake, never a request for
        # every draft: the drafts root holds published and unpublished
        # articles side by side, and this is the only thing standing between
        # the unpublished ones and a cloud search index.
        raise SystemExit(
            "--chinese-glob is required with --chinese-root: an unfiltered "
            "drafts root would index unpublished articles"
        )
    globs = tuple(chinese_globs) if chinese_globs is not None else ()
    english = _collect(english_root, ENGLISH_LABEL)
    chinese = _collect(chinese_root, CHINESE_LABEL, globs) if chinese_root is not None else []

    roots = {ENGLISH_LABEL: english_root}
    if chinese_root is not None:
        roots[CHINESE_LABEL] = chinese_root
    admitted: list[tuple[SourceFile, str, str]] = []
    excluded: list[dict[str, Any]] = []
    claimed: dict[str, SourceFile] = {}
    for source in interleave(english, chinese):
        root = roots[source.root_label]
        body = strip_front_matter((root / source.relative_path).read_text(encoding="utf-8")).strip()
        if not body:
            raise SystemExit(
                f"{root / source.relative_path} has no body once front matter is "
                "stripped; a document with no prose must not enter a corpus"
            )
        rules = scan_exclusions(body)
        if rules:
            excluded.append(
                {
                    "root_label": source.root_label,
                    "source_path": source.relative_path.as_posix(),
                    "rules": list(rules),
                }
            )
            continue
        doc_id = slug_for(source.root_label, source.relative_path)
        # Before any write, and before ``limit`` can hide the second half of
        # a collision: two sources on one id means one document silently
        # replaces the other, and the corpus is a document short with nothing
        # to say so.
        previous = claimed.get(doc_id)
        if previous is not None:
            raise SystemExit(
                f"duplicate doc_id {doc_id!r}: "
                f"{previous.root_label}:{previous.relative_path.as_posix()} and "
                f"{source.root_label}:{source.relative_path.as_posix()} map to the same id"
            )
        claimed[doc_id] = source
        admitted.append((source, doc_id, body))

    if limit is not None:
        admitted = admitted[:limit]
    if not admitted:
        raise SystemExit(
            "no documents admitted: every source file was filtered out, "
            "excluded or cut -- check --chinese-glob and --limit"
        )

    tenant_dir = out_dir / tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)
    documents: list[dict[str, Any]] = []
    for source, doc_id, body in admitted:
        path = tenant_dir / f"{doc_id}.md"
        path.write_bytes(
            _render(
                doc_id=doc_id,
                title=_title_of(body, doc_id),
                tenant_id=tenant_id,
                effective_date=parsed_date,
                body=body,
            )
        )
        # Digest what is on disk, not what we meant to put there: the
        # manifest's job is to pin the bytes a later run indexes.
        written = path.read_bytes()
        documents.append(
            {
                "doc_id": doc_id,
                "root_label": source.root_label,
                "source_path": source.relative_path.as_posix(),
                "output_path": path.relative_to(out_dir).as_posix(),
                "bytes": len(written),
                "content_sha256": digest_of(written),
            }
        )

    data: dict[str, Any] = {
        "tool": "tools/build_distractor_corpus.py",
        "tenant_id": tenant_id,
        "effective_date": effective_date,
        "limit": limit,
        "source_roots": [
            {
                "label": ENGLISH_LABEL,
                "path": english_root.as_posix(),
                "name_globs": ["*.md"],
                "files_seen": len(english),
            },
            {
                "label": CHINESE_LABEL,
                "path": chinese_root.as_posix() if chinese_root is not None else None,
                "name_globs": list(globs) if chinese_root is not None else None,
                "files_seen": len(chinese),
            },
        ],
        "exclusion_rules": [
            {"number": rule.number, "description": rule.description, "terms": list(rule.terms)}
            for rule in EXCLUSION_RULES
        ],
        "excluded_documents": excluded,
        "excluded_count": len(excluded),
        "documents": documents,
        "document_count": len(documents),
        "total_bytes": sum(int(document["bytes"]) for document in documents),
    }

    payload = serialize_manifest(data)
    manifest_path = out_dir / MANIFEST_NAME
    manifest_path.write_bytes(payload)
    digest = digest_of(payload)
    digest_path = out_dir / DIGEST_NAME
    digest_path.write_text(f"{digest}\n", encoding="utf-8")

    return Manifest(
        out_dir=out_dir,
        manifest_path=manifest_path,
        digest_path=digest_path,
        digest=digest,
        data=data,
    )


def _tenant_id_type(value: str) -> str:
    try:
        return validate_identifier(value, field="--tenant-id")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _date_type(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--effective-date must be YYYY-MM-DD: {exc}") from exc
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--english-root",
        type=Path,
        required=True,
        help="root of the English source tree; every *.md under it is eligible",
    )
    parser.add_argument(
        "--chinese-root",
        type=Path,
        default=None,
        help=(
            "root of the Chinese source tree; optional, but requires at "
            "least one --chinese-glob when given"
        ),
    )
    parser.add_argument(
        "--chinese-glob",
        action="append",
        dest="chinese_globs",
        default=None,
        metavar="PATTERN",
        help=(
            "repeatable file-NAME pattern selecting which drafts are "
            "eligible; a file must match at least one. Required whenever "
            "--chinese-root is given -- there is no permissive default, "
            "because an unfiltered drafts root would index unpublished "
            "articles."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="corpus directory to write; must be absent or empty",
    )
    parser.add_argument(
        "--tenant-id",
        type=_tenant_id_type,
        required=True,
        help="tenant every written document belongs to (also its directory name)",
    )
    parser.add_argument(
        "--effective-date",
        type=_date_type,
        required=True,
        help="YYYY-MM-DD written into every document's front matter",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "keep only the first N admitted documents, so a smaller "
            "generation is a prefix of a larger one at the same language mix"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.chinese_root is not None and not arguments.chinese_globs:
        # argparse exits 2 here, before the out-dir is created: an operator
        # who forgets the filter gets a usage error, not a corpus with
        # unpublished drafts in it.
        parser.error("--chinese-root requires at least one --chinese-glob")
    manifest = build(
        english_root=arguments.english_root,
        chinese_root=arguments.chinese_root,
        out_dir=arguments.out_dir,
        tenant_id=arguments.tenant_id,
        effective_date=arguments.effective_date,
        limit=arguments.limit,
        chinese_globs=arguments.chinese_globs,
    )
    print(f"documents: {manifest.data['document_count']}")
    print(f"excluded: {manifest.data['excluded_count']}")
    print(f"manifest sha256: {manifest.digest}")


if __name__ == "__main__":
    main()
