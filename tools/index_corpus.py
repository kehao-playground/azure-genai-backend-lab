"""Create the index and load the sample corpus with real embeddings.

This is the live-session instrument that makes retrieval measurable. It is
also where the per-document serialized size is *measured* rather than
estimated — the article may only cite the number this prints.

Two ways to bring the schema up to date, and they are not interchangeable:

* ``--create-index`` is non-destructive: it PUTs the current schema
  definition (creating the index if absent, updating it in place if
  present) and nothing else. It does **not** remove documents already in
  the index, and it does not backfill ACL fields onto documents indexed
  under an older schema — a field added to the schema after those documents
  were written stays absent on them until they are re-indexed.
* ``--recreate-index`` is destructive: it deletes the index, then
  recreates it from the current schema, then runs the full corpus load
  below. This lab's Day 15 migration (ACL fields added to every document)
  is genuinely incomplete without it, because ``--create-index`` alone
  would leave every previously-indexed chunk without the fields the new
  ACL filter depends on.

Production would not use either flag this way: a production migration
builds a **new** index generation, loads it in full, and cuts an alias over
to it, so the old generation keeps serving reads until the new one is
verified — this lab's single, aliasless index has no such rollback, which
is why the destructive flag is safe only in a lab.

Usage:
    uv run python tools/index_corpus.py --create-index
    uv run python tools/index_corpus.py --recreate-index
    uv run python tools/index_corpus.py --create-index --tenant-id <entra-tenant-guid>
"""

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import replace

from azgenai_lab.core.config import Settings, get_settings
from azgenai_lab.core.logging import configure_logging
from azgenai_lab.models.principal import validate_identifier
from azgenai_lab.models.rag import Chunk, IndexingAction, SourceDocument, make_parent_id
from azgenai_lab.services.chunking import chunk_markdown
from azgenai_lab.services.document_loader import SAMPLE_DOCS_DIR, load_documents
from azgenai_lab.services.embeddings import build_embedding_client, embed_chunks
from azgenai_lab.services.indexing_results import IndexingResult
from azgenai_lab.services.search_data_plane import IndexingBatch, SearchDataPlane, plan_batches
from azgenai_lab.services.search_indexing import DocumentReplacer


def _tenant_id_type(value: str) -> str:
    try:
        return validate_identifier(value, field="--tenant-id")
    except ValueError as exc:
        # argparse turns this into a usage error and exits before any of
        # this run's network calls -- an operator's typo should not be
        # discovered partway through an indexing pass.
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    schema_group = parser.add_mutually_exclusive_group()
    schema_group.add_argument(
        "--create-index",
        action="store_true",
        help=(
            "PUT the schema first (non-destructive: creates the index if "
            "absent, updates it in place if present). Does not remove stale "
            "documents already in the index and does not backfill ACL "
            "fields onto documents indexed under an older schema."
        ),
    )
    schema_group.add_argument(
        "--recreate-index",
        action="store_true",
        help=(
            "DELETE the index, then recreate it from the current schema, "
            "before the corpus load below (destructive — every document "
            "already in the index is gone, including any this run does not "
            "re-load). Lab-only: production migrates via a new index "
            "generation plus an alias cutover, not by deleting the one "
            "serving reads."
        ),
    )
    parser.add_argument(
        "--tenant-id",
        type=_tenant_id_type,
        default=None,
        help=(
            "Override every loaded document's front-matter tenant_id for "
            "this run only (the source files on disk are untouched). "
            "Every chunk, parent/chunk key, and stale-cleanup enumeration "
            "in this run then uses this value instead of each document's "
            "own tenant. Omitting this flag is the default and leaves "
            "today's behavior unchanged: acme/globex/opsdemo stay separate "
            "tenants, which Day 15's multi-tenant behave scenarios and the "
            "local workflow depend on. Passing a value COLLAPSES every "
            "source tenant into this one tenant for the run — use it "
            "deliberately, e.g. to point a single-tenant live smoke "
            "(indexing against a real Entra tenant GUID) at the corpus, "
            "never as a default."
        ),
    )
    return parser


async def main() -> None:
    arguments = _build_parser().parse_args()

    settings = get_settings()
    if settings.use_fake_embeddings:
        raise SystemExit(
            "USE_FAKE_EMBEDDINGS is true — refusing to run a live indexing "
            "session with fake vectors. The fake's vectors carry no "
            "semantics; indexing them against a real search service would "
            "produce a populated, queryable index that looks real and means "
            "nothing. Set USE_FAKE_EMBEDDINGS=false and provide real Azure "
            "OpenAI embedding credentials before running this tool."
        )
    configure_logging(settings.log_level)

    # The data plane owns its connection pool here, so it is closed on the way
    # out — including when a SystemExit below aborts the run partway.
    async with SearchDataPlane(settings) as plane:
        await _index(
            plane,
            settings,
            create_index=arguments.create_index,
            recreate_index=arguments.recreate_index,
            tenant_id=arguments.tenant_id,
        )


def _apply_tenant_override(
    documents: Sequence[SourceDocument], tenant_id: str | None
) -> list[SourceDocument]:
    """Override every document's tenant_id for this run, or pass them through.

    ``tenant_id=None`` (the default) is a strict no-op: the returned list
    carries the same per-document tenant_id values ``load_documents()``
    produced, so ``acme``/``globex``/``opsdemo`` stay separate tenants.

    A supplied value COLLAPSES every source document onto that one tenant
    for this run — every chunk's ``tenant_id`` field, every parent/chunk key
    derived from it, and the stale-cleanup enumeration all follow, because
    they are all derived from ``SourceDocument.tenant_id``. This is the
    deliberate, documented consequence for a single-tenant live smoke (e.g.
    indexing against a real Entra tenant GUID); it destroys the isolation
    Day 15's per-document ACL model exists to enforce, so it must never be
    the default.
    """
    if tenant_id is None:
        return list(documents)
    return [replace(document, tenant_id=tenant_id) for document in documents]


async def _rebuild_schema(
    plane: SearchDataPlane, *, create_index: bool, recreate_index: bool
) -> None:
    """The schema half of a run, isolated from the corpus load below.

    Isolated on purpose: this is the part `tests/unit/test_index_recreate.py`
    pins the call order of, with a fake data plane and no corpus, no
    embeddings and no network — the two schema calls are the whole surface
    under test, and a full ingestion run would only obscure that.
    """
    if recreate_index:
        # Strictly delete-then-create: a schema PUT against an index that
        # still exists is an update, not a rebuild, and would carry forward
        # exactly the stale documents this flag exists to clear.
        await plane.delete_index()
        print("index deleted")
        await plane.create_or_update_index()
        print("index created or updated")
    elif create_index:
        await plane.create_or_update_index()
        print("index created or updated")


async def _index(
    plane: SearchDataPlane,
    settings: Settings,
    *,
    create_index: bool,
    recreate_index: bool,
    tenant_id: str | None = None,
) -> None:
    await _rebuild_schema(plane, create_index=create_index, recreate_index=recreate_index)

    embedding_client = build_embedding_client(settings)
    print(
        f"embedding client: {embedding_client.__class__.__name__} "
        f"deployment={settings.azure_openai_embedding_deployment}"
    )

    # Measure the buffer that actually travels, by wrapping the transport
    # rather than serializing a second time. A separate json.dumps() here
    # would exclude the batch wrapper and separators, and would drift the
    # moment either serializer changed — a published figure has to come from
    # the same bytes the data plane sends, which is what the batching layer's
    # own exact-bytes test pins.
    #
    # Two counters, because they answer different questions. A retry or a
    # stale-delete batch is real transport, but counting it into a
    # per-document average silently changes what the average *means* — one
    # retry and the figure becomes "bytes attempted this run per document",
    # which is not what the article claims. So the citable figure comes only
    # from first-attempt upload batches.
    seen_upload_keys: set[str] = set()
    first_upload_bytes = 0
    first_upload_documents = 0
    other_batches = 0
    attempted_bytes = 0

    async def measured_post(batch: IndexingBatch) -> list[IndexingResult]:
        nonlocal first_upload_bytes, first_upload_documents, other_batches, attempted_bytes
        attempted_bytes += len(batch.body)
        if batch.action is IndexingAction.UPSERT and not seen_upload_keys.intersection(batch.keys):
            first_upload_bytes += len(batch.body)
            first_upload_documents += len(batch.keys)
            seen_upload_keys.update(batch.keys)
        else:
            other_batches += 1
        return await plane.post_batch(batch)

    replacer = DocumentReplacer(plan_batches, measured_post, plane.list_chunk_ids)

    documents_to_index = _apply_tenant_override(
        load_documents(settings.sample_docs_dir or SAMPLE_DOCS_DIR), tenant_id
    )

    total_documents = 0
    for source in documents_to_index:
        chunks: list[Chunk] = chunk_markdown(
            source,
            max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars,
        )
        vectors = await embed_chunks(embedding_client, chunks)
        documents = [
            chunk.to_index_document(vector)
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

        total_documents += len(documents)

        parent_id = make_parent_id(source.tenant_id, source.doc_id)
        outcome = await replacer.replace(source.tenant_id, parent_id, documents)
        status = "complete" if outcome.completed else "INCOMPLETE"
        print(
            f"{source.doc_id}: {len(documents)} chunks, {status}, "
            f"deleted={list(outcome.deleted_keys)}, "
            f"unresolved={list(outcome.unresolved_stale_ids)}, "
            f"stale_state_unknown={outcome.stale_state_unknown}"
        )
        if not outcome.completed:
            if outcome.stale_state_unknown:
                # The new chunks are already live — this is not "nothing
                # happened", it is "cleanup crashed after the upload landed".
                raise SystemExit(
                    f"upload for {source.doc_id} succeeded but stale cleanup crashed "
                    "afterward; the new chunks are indexed but whether stale ones "
                    "were removed is unknown — check the log line above and re-run"
                )
            raise SystemExit(f"replacement did not complete for {source.doc_id}")

    average = first_upload_bytes / first_upload_documents if first_upload_documents else 0
    print(f"\nchunks indexed: {total_documents}")
    print(f"first-attempt upload bytes: {first_upload_bytes} "
          f"across {first_upload_documents} documents")
    print(f"mean transmitted bytes per document: {average:.0f}  <- cite this one")
    print("(includes the batch wrapper and separators, first upload attempt only)")
    print(f"total bytes attempted this run: {attempted_bytes}")
    print(f"retry / delete batches: {other_batches} (operational detail, not part of the mean)")


if __name__ == "__main__":
    asyncio.run(main())
