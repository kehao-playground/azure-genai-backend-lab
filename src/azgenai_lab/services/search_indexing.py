"""Write-path orchestration for Azure AI Search: batching, retry, enumeration,
replacement. No transport lives here — see `services/search_data_plane.py`.

Three contracts hold this together, and none substitutes for another:

* a per-key **terminal state** for every document in a batch (upload *and*
  delete), so a retry's later success is representable without a duplicate;
* the **fail-closed gate** in `indexing_results.may_delete_stale()`;
* a per-`parent_id` **critical section**, because two replacements of one
  document can otherwise each succeed and still destroy each other's work.
"""

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from azgenai_lab.core.errors import UpstreamError

# Documented API request limits (checked 2026-07): at most 1,000 documents per
# batch of uploads, merges or deletes, and a 16 MB payload ceiling applying to
# the whole request.
MAX_BATCH_DOCUMENTS = 1_000
MAX_REQUEST_BYTES = 16 * 1024 * 1024

_OPEN = b'{"value":['
_CLOSE = b"]}"
_WRAPPER_BYTES = len(_OPEN) + len(_CLOSE)


class DocumentTooLargeError(UpstreamError):
    """One document cannot fit in a request on its own.

    Raised before anything is sent, and **not** retryable: no number of
    attempts makes an oversized document smaller. The retry coordinator
    deliberately does not catch this.
    """

    status_code = 500
    code = "document_too_large"
    message = "A document exceeds the maximum indexing request size."


@dataclass(frozen=True)
class IndexingBatch:
    """One request body and exactly the keys it carries.

    The keys travel with the body because a request-level failure must requeue
    only this batch. Requeuing the whole attempt would re-send keys an earlier
    batch already settled as succeeded.
    """

    body: bytes
    keys: tuple[str, ...]


def serialize_batches(
    documents: Sequence[dict[str, Any]], action: str
) -> Iterator[IndexingBatch]:
    """Yield request bodies, each within both ceilings.

    Serialize **once**. The yielded ``body`` is exactly what gets sent — the
    caller passes it as raw content rather than handing Python objects back to
    an encoder, because two serializations of the same object are not
    guaranteed to be byte-identical, and then the limit guarded here would not
    be the limit that travels.
    """
    encoded: list[tuple[str, bytes]] = []
    for document in documents:
        key = str(document["chunk_id"])
        payload = {**document, "@search.action": action}
        blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if _WRAPPER_BYTES + len(blob) > MAX_REQUEST_BYTES:
            raise DocumentTooLargeError(
                f"document {key} serializes to {len(blob)} bytes, above the "
                f"{MAX_REQUEST_BYTES} byte request limit"
            )
        encoded.append((key, blob))

    batch: list[bytes] = []
    keys: list[str] = []
    size = _WRAPPER_BYTES
    for key, blob in encoded:
        # The separating comma is payload too, and has to be counted or a
        # batch can land one byte over the ceiling.
        extra = len(blob) + (1 if batch else 0)
        if batch and (len(batch) >= MAX_BATCH_DOCUMENTS or size + extra > MAX_REQUEST_BYTES):
            yield IndexingBatch(_OPEN + b",".join(batch) + _CLOSE, tuple(keys))
            batch, keys, size = [], [], _WRAPPER_BYTES
            extra = len(blob)
        batch.append(blob)
        keys.append(key)
        size += extra
    if batch:
        yield IndexingBatch(_OPEN + b",".join(batch) + _CLOSE, tuple(keys))
