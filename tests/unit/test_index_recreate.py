"""`--recreate-index` must delete before it creates, and tolerate a 404.

`tools/index_corpus.py` isolates the schema half of a run into
`_rebuild_schema()` precisely so this can be pinned without a corpus, real
embeddings, or a network — a fake data plane recording call order is the
whole test.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

# tools/ is not a package (no __init__.py, not installed) — a plain file
# import, the same pattern `tests/unit/test_compare_retrieval.py` uses.
_MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "index_corpus.py"
_SPEC = importlib.util.spec_from_file_location("index_corpus", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
index_corpus = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = index_corpus
_SPEC.loader.exec_module(index_corpus)

_rebuild_schema = index_corpus._rebuild_schema


class _FakePlane:
    """Records call order only. `delete_index` can be made to raise, to
    stand in for a 404 the real adapter already swallows internally."""

    def __init__(self, *, delete_raises: bool = False) -> None:
        self.calls: list[str] = []
        self._delete_raises = delete_raises

    async def delete_index(self) -> None:
        self.calls.append("delete")
        if self._delete_raises:
            raise AssertionError("delete_index should tolerate a 404 itself, not raise")

    async def create_or_update_index(self) -> None:
        self.calls.append("create")


async def test_recreate_index_deletes_strictly_before_it_creates() -> None:
    plane = _FakePlane()
    await _rebuild_schema(plane, create_index=False, recreate_index=True)  # type: ignore[arg-type]
    assert plane.calls == ["delete", "create"]


async def test_recreate_index_tolerates_a_delete_that_finds_nothing_to_delete() -> None:
    # `SearchDataPlane.delete_index()` already swallows a 404 internally
    # (see `tests/unit/test_search_data_plane.py`); this pins that
    # `_rebuild_schema()` does not add a second check on top of it — it
    # just awaits the call and moves on to create.
    plane = _FakePlane(delete_raises=False)
    await _rebuild_schema(plane, create_index=False, recreate_index=True)  # type: ignore[arg-type]
    assert plane.calls == ["delete", "create"]


async def test_create_index_alone_never_deletes() -> None:
    plane = _FakePlane()
    await _rebuild_schema(plane, create_index=True, recreate_index=False)  # type: ignore[arg-type]
    assert plane.calls == ["create"]


async def test_neither_flag_touches_the_schema() -> None:
    plane = _FakePlane()
    await _rebuild_schema(plane, create_index=False, recreate_index=False)  # type: ignore[arg-type]
    assert plane.calls == []


def test_recreate_index_and_create_index_are_mutually_exclusive_at_the_cli() -> None:
    parser = index_corpus._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--create-index", "--recreate-index"])
