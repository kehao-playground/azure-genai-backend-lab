"""``top`` and ``vector_k`` are bounded, and both clients bound them alike.

The bound is on the domain, not just the range: a count has to be an ``int``
within its limits, and neither half of that is redundant. ``top`` above the
documented ceiling is not rejected by the service — it is silently answered as
1,000, so the caller gets a 200 for a question it did not ask. A count below 1
reaches the wire as ``"top": -1`` or ``"k": -5``, and the fake, scoring and
slicing in Python, quietly returns a *different* result instead of the same
error. A ``bool`` or a ``float`` clears every range comparison and then parts
the two implementations the same way. Either way the mistake surfaces
somewhere other than the call that made it, which is what these tests are for.

Every case runs against the fake and the real adapter through the same
parametrization. A bound the service enforces and the fake does not is a green
suite that fails in production; the reverse is a fake nobody can develop
against. So the two are asserted to agree, not merely to each work.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from azgenai_lab.core.config import Settings
from azgenai_lab.models.search import (
    DEFAULT_VECTOR_K,
    INT32_MAX,
    MAX_TOP,
    SearchMode,
    SearchResult,
)
from azgenai_lab.models.search_index import EMBEDDING_DIMENSIONS
from azgenai_lab.services.azure_search import AzureSearchClient, FakeSearchClient

VECTOR = [0.1] * EMBEDDING_DIMENSIONS

DOCUMENTS = [
    {
        "chunk_id": "returns-policy-0001",
        "parent_id": "returns-policy",
        "title": "Returns Policy",
        "heading_path": "Returns Policy > Refund window",
        "content": "Customers may return most items within 30 days.",
        "tenant_id": "t1",
        "allowed_groups": [],
    }
]

Search = Callable[..., Awaitable[SearchResult]]

# Requests that were sent, if any. A bound is only enforced if nothing goes out
# — a rejection after the request has left has already spent the call.
sent: list[httpx.Request] = []


def _real() -> Search:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"value": []})

    settings = Settings(
        _env_file=None,
        azure_search_endpoint="https://example.search.windows.net",
        azure_search_admin_key=SecretStr("k"),
        use_fake_search=False,
    )
    client = AzureSearchClient(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return client.search


def _fake() -> Search:
    return FakeSearchClient(DOCUMENTS).search


CLIENTS = [pytest.param(_fake, id="fake"), pytest.param(_real, id="real")]

REJECTED: list[tuple[str, dict[str, Any]]] = [
    ("top-zero", {"top": 0}),
    ("top-negative", {"top": -1}),
    ("top-above-the-page-ceiling", {"top": MAX_TOP + 1}),
    ("vector_k-zero", {"top": 5, "vector_k": 0}),
    ("vector_k-negative", {"top": 5, "vector_k": -5}),
    # `bool` is a subclass of `int`, so `True` satisfies both an
    # `isinstance(x, int)` check and `x >= 1`. Unrejected it reaches the wire
    # as the JSON literal `true` where the service documents a number, and
    # `False` slips past as a zero that never trips the lower bound as a
    # *type* error — it is caught, if at all, as the wrong complaint.
    ("top-true", {"top": True}),
    ("top-false", {"top": False}),
    ("vector_k-true", {"top": 5, "vector_k": True}),
    # A positive float clears every range comparison and then the two
    # implementations part ways: one slices a list, the other sends a JSON
    # float. Both integral and fractional, because a float that happens to
    # equal an integer is the one a `== int(value)` shortcut would wave
    # through — and it is still a float on the wire.
    ("top-float", {"top": 5.0}),
    ("top-fractional-float", {"top": 2.5}),
    ("vector_k-float", {"top": 5, "vector_k": 2.0}),
    # Values with no business being counts at all, which is what a JSON body
    # or a CLI argument can hand over without the type system noticing.
    ("top-string", {"top": "5"}),
    ("vector_k-none", {"top": 5, "vector_k": None}),
    # The REST reference declares `k` an int32. A larger value is not the
    # field being described, whatever the service does with it.
    ("vector_k-above-int32", {"top": 5, "vector_k": INT32_MAX + 1}),
]


@pytest.mark.parametrize("build", CLIENTS)
@pytest.mark.parametrize(("label", "kwargs"), REJECTED, ids=[case[0] for case in REJECTED])
async def test_both_clients_reject_the_same_counts(
    build: Callable[[], Search], label: str, kwargs: dict[str, Any]
) -> None:
    sent.clear()
    search = build()
    with pytest.raises(ValueError):
        await search("refund", VECTOR, mode=SearchMode.HYBRID, **kwargs)
    assert sent == [], "the request was sent before the argument was rejected"


@pytest.mark.parametrize("build", CLIENTS)
async def test_both_clients_accept_the_edges_of_the_range(
    build: Callable[[], Search],
) -> None:
    # The boundary values themselves are legal. A check written with `<=` where
    # it meant `<` would reject exactly these and nothing else.
    search = build()
    await search("refund", VECTOR, mode=SearchMode.HYBRID, top=1, vector_k=1)
    await search("refund", VECTOR, mode=SearchMode.HYBRID, top=MAX_TOP)


@pytest.mark.parametrize("build", CLIENTS)
async def test_the_largest_int32_vector_k_is_accepted(
    build: Callable[[], Search],
) -> None:
    # The upper bound on `vector_k` is the declared type and nothing tighter.
    # No source publishes a limit on how many neighbours may be asked for, so
    # a smaller ceiling here would be this repository inventing a number and
    # then being cited for it. This test is what makes that deliberate rather
    # than an oversight: tightening the bound has to break it.
    search = build()
    await search("refund", VECTOR, mode=SearchMode.HYBRID, top=5, vector_k=INT32_MAX)


async def test_a_boolean_count_is_rejected_as_a_type_not_read_as_a_number() -> None:
    # `isinstance(True, int)` is True and `True >= 1` is True, so a range
    # check alone accepts it and the caller learns nothing. The complaint has
    # to name the type, or the next reader "fixes" it by widening the range.
    fake = FakeSearchClient(DOCUMENTS)
    with pytest.raises(ValueError, match="top must be an int; got bool"):
        await fake.search("refund", VECTOR, mode=SearchMode.HYBRID, top=True)
    with pytest.raises(ValueError, match="vector_k must be an int; got bool"):
        await fake.search("refund", VECTOR, mode=SearchMode.HYBRID, top=5, vector_k=False)


@pytest.mark.parametrize("build", CLIENTS)
async def test_vector_k_is_bounded_even_where_it_is_not_used(
    build: Callable[[], Search],
) -> None:
    # KEYWORD sends no vector query, so a negative k is inert there — and a
    # caller that passes one still has a bug. The rule does not vary by mode.
    sent.clear()
    search = build()
    with pytest.raises(ValueError):
        await search("refund", mode=SearchMode.KEYWORD, top=5, vector_k=-1)
    assert sent == []


async def test_the_error_names_the_parameter_that_was_wrong() -> None:
    # "invalid argument" would leave a caller with two candidates and no way to
    # tell which; both are counts and both default to something plausible.
    fake = FakeSearchClient(DOCUMENTS)
    with pytest.raises(ValueError, match="top"):
        await fake.search("refund", VECTOR, mode=SearchMode.HYBRID, top=0)
    with pytest.raises(ValueError, match="vector_k"):
        await fake.search("refund", VECTOR, mode=SearchMode.HYBRID, top=5, vector_k=0)


async def test_the_default_vector_k_is_inside_the_range() -> None:
    # A default outside its own contract would make every call that omits the
    # argument fail, which is the kind of thing a suite full of explicit
    # arguments never notices.
    await FakeSearchClient(DOCUMENTS).search(
        "refund", VECTOR, mode=SearchMode.HYBRID, top=5
    )
    assert DEFAULT_VECTOR_K >= 1
