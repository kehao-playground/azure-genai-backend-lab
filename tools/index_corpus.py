"""Create the index and load the sample corpus with real embeddings.

This is the live-session instrument that makes retrieval measurable. It is
also where the per-document serialized size is *measured* rather than
estimated — the article may only cite the number this prints.

Usage:
    uv run python tools/index_corpus.py --create-index
"""

import argparse
import asyncio
import json

from azgenai_lab.core.config import get_settings
from azgenai_lab.core.logging import configure_logging
from azgenai_lab.models.rag import Chunk
from azgenai_lab.services.chunking import chunk_markdown
from azgenai_lab.services.document_loader import load_documents
from azgenai_lab.services.embeddings import build_embedding_client, embed_chunks
from azgenai_lab.services.indexing_results import IndexingResult
from azgenai_lab.services.search_data_plane import SearchDataPlane
from azgenai_lab.services.search_indexing import DocumentReplacer


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--create-index", action="store_true", help="PUT the schema first")
    arguments = parser.parse_args()

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

    plane = SearchDataPlane(settings)
    if arguments.create_index:
        await plane.create_or_update_index()
        print("index created or updated")

    embedding_client = build_embedding_client(settings)
    print(
        f"embedding client: {embedding_client.__class__.__name__} "
        f"deployment={settings.azure_openai_embedding_deployment}"
    )

    # Measure the buffer that actually travels, by wrapping the transport
    # rather than serializing a second time. A separate json.dumps() here
    # would exclude the batch wrapper and separators, and would drift the
    # moment either serializer changed — the article's number has to come from
    # the same bytes Task 8's exact-bytes test proves are transmitted.
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

    async def measured_post(body: bytes) -> list[IndexingResult]:
        nonlocal first_upload_bytes, first_upload_documents, other_batches, attempted_bytes
        attempted_bytes += len(body)
        # Parsing the bytes we are about to send is not a second serializer —
        # it cannot drift from the buffer, because it *is* the buffer.
        entries = json.loads(body)["value"]
        actions = {entry["@search.action"] for entry in entries}
        keys = [entry["chunk_id"] for entry in entries]
        if actions == {"upload"} and not seen_upload_keys.intersection(keys):
            first_upload_bytes += len(body)
            first_upload_documents += len(entries)
            seen_upload_keys.update(keys)
        else:
            other_batches += 1
        return await plane.post_index(body)

    replacer = DocumentReplacer(measured_post, plane.post_search)

    total_documents = 0
    for source in load_documents():
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

        outcome = await replacer.replace(source.doc_id, documents)
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
