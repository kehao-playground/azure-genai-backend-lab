"""Per-document outcomes of an indexing batch (indexing stage: persist).

A batch answers 200 when every document succeeded and 207 when at least one
did not — and 207 is a *successful* HTTP response. A client that only checks
whether the call raised will therefore treat a partial failure as a complete
one. Every 207 must be read document by document, which is what this module
exists to do.

Nothing here talks to the service. Day 12 delivers the contract; Day 13 wires
it to a real client.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum

_SUCCESS_CODES = frozenset({200, 201})
# Codes worth sending again: a version conflict, an index briefly unavailable,
# throttling, and an overloaded service.
_RETRYABLE_CODES = frozenset({409, 422, 429, 503})
# Throttling during indexing is usually a storage-capacity signal rather than a
# request-rate one, so it warrants its own alert on top of the retry.
_STORAGE_ALERT_CODE = 429


class Disposition(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class IndexingResult:
    """One entry from an indexing response's ``value`` array."""

    key: str
    status: bool
    status_code: int
    error_message: str | None = None


def classify(result: IndexingResult) -> Disposition:
    # ``status`` is the service's own per-document success flag and is
    # authoritative. A result that says status=false while carrying a success
    # code is contradictory, and the safe reading of a contradiction is failure.
    if result.status and result.status_code in _SUCCESS_CODES:
        return Disposition.SUCCEEDED
    if not result.status and result.status_code in _SUCCESS_CODES:
        return Disposition.PERMANENT
    if result.status_code in _RETRYABLE_CODES:
        return Disposition.RETRYABLE
    # Anything unrecognised is permanent on purpose: retrying an unknown
    # failure forever is worse than stopping and being told about it.
    return Disposition.PERMANENT


def is_storage_alert(result: IndexingResult) -> bool:
    return result.status_code == _STORAGE_ALERT_CODE


def may_delete_stale(
    results: Sequence[IndexingResult], *, expected_keys: Collection[str]
) -> bool:
    """Whether the stale-chunk deletion may proceed.

    True only when every expected key occurs exactly once on both sides —
    the expected keys themselves and the results returned for them — nothing
    unexpected came back, and every one of them succeeded. Any permanent
    failure or exhausted retry stops here: the document is left with old and
    new chunks both present, which is recoverable, rather than with nothing at
    all, which is not.

    Every branch here fails closed. Deduplicating by key would let a retry's
    success overwrite an earlier failure for the same document and silently
    open the gate, so a repeated key is rejected outright rather than resolved
    — on either side, since collapsing ``expected_keys`` to a set would erase
    an upstream chunk-id collision just as silently as collapsing the results
    would.
    """
    if not expected_keys:
        return False
    expected = list(expected_keys)
    if len(expected) != len(set(expected)):
        return False
    keys = [result.key for result in results]
    if len(keys) != len(set(keys)):
        return False
    if set(keys) != set(expected):
        return False
    return all(classify(result) is Disposition.SUCCEEDED for result in results)
