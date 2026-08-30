"""The assembler's failure modes are all silent ones.

A basename-derived id collides across seven README.md files and, without a
hard stop, would overwrite one document with another. An unstripped front
matter block would index a draft's review metadata. A missed exclusion would
index the article that contains the frozen query string, which then scores as
a distractor while being the most relevant document in the corpus. None of
these produce an error at query time; they produce a complete-looking
evidence file.
"""

from pathlib import Path

import pytest
from tools.build_distractor_corpus import (
    SourceFile,
    interleave,
    scan_exclusions,
    serialize_manifest,
    slug_for,
    strip_front_matter,
)


def test_slug_is_path_derived_so_seven_readmes_do_not_collide() -> None:
    a = slug_for("labdocs", Path("README.md"))
    b = slug_for("labdocs", Path("search/README.md"))
    c = slug_for("labdocs", Path("zh-tw/articles/README.md"))
    assert a == "labdocs-readme"
    assert b == "labdocs-search-readme"
    assert c == "labdocs-zh-tw-articles-readme"
    assert len({a, b, c}) == 3


def test_slug_prefixes_keep_the_two_source_roots_apart() -> None:
    assert slug_for("labdocs", Path("x.md")) != slug_for("draft", Path("x.md"))


def test_front_matter_is_removed_and_body_survives() -> None:
    text = "---\nday: 13\nstatus: published\n---\n\n# Title\n\nBody line.\n"
    assert strip_front_matter(text) == "# Title\n\nBody line.\n"


def test_body_without_front_matter_is_untouched() -> None:
    text = "# Title\n\nBody line.\n"
    assert strip_front_matter(text) == text


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("... 99.9% monthly uptime ...", 1),
        ("the returns-policy document", 2),
        ("hybrid RRF top score was 0.032796", 3),
    ],
)
def test_each_exclusion_rule_fires(text: str, rule: int) -> None:
    assert rule in scan_exclusions(text)


def test_clean_text_is_admitted() -> None:
    assert scan_exclusions("Container Apps scales to zero on idle revisions.") == ()


def test_interleave_alternates_and_keeps_every_file() -> None:
    english = [SourceFile("labdocs", Path(f"e{i}.md")) for i in range(3)]
    chinese = [SourceFile("draft", Path(f"c{i}.md")) for i in range(2)]
    merged = interleave(english, chinese)
    assert [f.root_label for f in merged] == [
        "labdocs",
        "draft",
        "labdocs",
        "draft",
        "labdocs",
    ]
    assert len(merged) == 5


def test_manifest_serialization_is_byte_stable() -> None:
    manifest = {"b": 2, "a": [3, 1]}
    assert serialize_manifest(manifest) == serialize_manifest(dict(manifest))
    assert serialize_manifest(manifest) == serialize_manifest({"a": [3, 1], "b": 2})


def test_manifest_carries_no_hash_of_itself() -> None:
    # A file cannot contain the digest of its own complete bytes: writing the
    # digest changes the bytes being digested. The digest is detached.
    from tools.build_distractor_corpus import MANIFEST_FORBIDDEN_KEYS

    assert "sha256" in MANIFEST_FORBIDDEN_KEYS
    assert "manifest_sha256" in MANIFEST_FORBIDDEN_KEYS


@pytest.mark.parametrize("key", ["sha256", "manifest_sha256", "digest"])
def test_serializing_a_self_hashing_manifest_is_refused(key: str) -> None:
    # The frozen set is only a list of names until something enforces it.
    with pytest.raises(ValueError, match="detached"):
        serialize_manifest({"document_count": 1, key: "0" * 64})


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_duplicate_doc_id_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import build_distractor_corpus as mod

    monkeypatch.setattr(mod, "slug_for", lambda root, path: "labdocs-same")
    root = tmp_path / "src"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "x.md").write_text("# A\n\nalpha\n", encoding="utf-8")
    (root / "b" / "x.md").write_text("# B\n\nbeta\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="duplicate doc_id"):
        mod.build(
            english_root=root,
            chinese_root=None,
            out_dir=tmp_path / "out",
            tenant_id="acme",
            effective_date="2026-08-30",
            limit=None,
        )


def test_duplicate_doc_id_aborts_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An abort that leaves half a corpus on disk invites a re-run against a
    # directory that already holds documents from the aborted attempt.
    from tools import build_distractor_corpus as mod

    monkeypatch.setattr(mod, "slug_for", lambda root, path: "labdocs-same")
    root = tmp_path / "src"
    _write(root / "a" / "x.md", "# A\n\nalpha\n")
    _write(root / "b" / "x.md", "# B\n\nbeta\n")
    out_dir = tmp_path / "out"
    with pytest.raises(SystemExit):
        mod.build(
            english_root=root,
            chinese_root=None,
            out_dir=out_dir,
            tenant_id="acme",
            effective_date="2026-08-30",
            limit=None,
        )
    assert list(out_dir.rglob("*.md")) == []


def test_chinese_globs_admit_only_matching_files(tmp_path: Path) -> None:
    # The Chinese root holds published and unpublished drafts side by side.
    # Unpublished material must not reach a cloud search service, and the
    # only thing standing between the two is this filter.
    from tools.build_distractor_corpus import build

    chinese = tmp_path / "zh-tw"
    _write(chinese / "day-01-a.md", "# One\n\nPublished body.\n")
    _write(chinese / "day-27-b.md", "# Twenty-seven\n\nUnpublished body.\n")
    _write(chinese / "notes.md", "# Notes\n\nPrivate body.\n")
    english = tmp_path / "docs"
    english.mkdir()

    manifest = build(
        english_root=english,
        chinese_root=chinese,
        out_dir=tmp_path / "out",
        tenant_id="acme",
        effective_date="2026-08-30",
        limit=None,
        chinese_globs=("day-0*.md",),
    )

    assert [document["source_path"] for document in manifest.data["documents"]] == [
        "day-01-a.md"
    ]
    assert sorted(path.name for path in (tmp_path / "out" / "acme").iterdir()) == [
        "draft-day-01-a.md"
    ]


def test_chinese_globs_match_the_file_name_at_any_depth(tmp_path: Path) -> None:
    from tools.build_distractor_corpus import build

    chinese = tmp_path / "zh-tw"
    _write(chinese / "archive" / "day-01-nested.md", "# Nested\n\nBody.\n")
    _write(chinese / "archive" / "notes.md", "# Notes\n\nBody.\n")
    english = tmp_path / "docs"
    english.mkdir()

    manifest = build(
        english_root=english,
        chinese_root=chinese,
        out_dir=tmp_path / "out",
        tenant_id="acme",
        effective_date="2026-08-30",
        limit=None,
        chinese_globs=("day-0*.md",),
    )

    assert [document["source_path"] for document in manifest.data["documents"]] == [
        "archive/day-01-nested.md"
    ]


def test_a_chinese_root_without_globs_is_refused(tmp_path: Path) -> None:
    # Failing open here would index every unpublished draft in the root. An
    # omitted filter is a mistake, never a request for "all of them".
    from tools.build_distractor_corpus import build

    chinese = tmp_path / "zh-tw"
    _write(chinese / "day-01-a.md", "# One\n\nBody.\n")
    _write(chinese / "day-27-unpublished.md", "# Unpublished\n\nBody.\n")
    english = tmp_path / "docs"
    _write(english / "clean.md", "# Clean\n\nBody.\n")
    out_dir = tmp_path / "out"

    for globs in (None, ()):
        with pytest.raises(SystemExit, match="--chinese-glob is required"):
            build(
                english_root=english,
                chinese_root=chinese,
                out_dir=out_dir,
                tenant_id="acme",
                effective_date="2026-08-30",
                limit=None,
                chinese_globs=globs,
            )
    assert not out_dir.exists()


def test_english_root_is_never_glob_filtered(tmp_path: Path) -> None:
    from tools.build_distractor_corpus import build

    english = tmp_path / "docs"
    _write(english / "architecture.md", "# Architecture\n\nBody.\n")
    chinese = tmp_path / "zh-tw"
    _write(chinese / "notes.md", "# Notes\n\nBody.\n")

    manifest = build(
        english_root=english,
        chinese_root=chinese,
        out_dir=tmp_path / "out",
        tenant_id="acme",
        effective_date="2026-08-30",
        limit=None,
        chinese_globs=("day-0*.md",),
    )

    assert [document["doc_id"] for document in manifest.data["documents"]] == [
        "labdocs-architecture"
    ]


def test_written_documents_load_back_through_the_real_loader(tmp_path: Path) -> None:
    # The corpus is only useful if `index_corpus.py --corpus-dir` can read it,
    # and that loader validates all six front-matter fields, the tenant
    # directory and the filename/doc_id agreement.
    from tools.build_distractor_corpus import build

    from azgenai_lab.services.document_loader import load_documents

    english = tmp_path / "docs"
    _write(english / "search" / "README.md", "# Search: modes\n\nEnglish body.\n")
    chinese = tmp_path / "zh-tw"
    _write(
        chinese / "day-01-a.md",
        "---\nday: 1\nstatus: published\nspeak_human_review:\n  status: applied\n---\n\n"
        "# 標題：一\n\n中文內文。\n",
    )

    build(
        english_root=english,
        chinese_root=chinese,
        out_dir=tmp_path / "out",
        tenant_id="acme",
        effective_date="2026-08-30",
        limit=None,
        chinese_globs=("day-0*.md",),
    )

    documents = {document.doc_id: document for document in load_documents(tmp_path / "out")}
    assert sorted(documents) == ["draft-day-01-a", "labdocs-search-readme"]
    draft = documents["draft-day-01-a"]
    assert draft.title == "標題：一"
    assert draft.doc_type == "reference"
    assert draft.tenant_id == "acme"
    assert draft.effective_date.isoformat() == "2026-08-30"
    assert draft.allowed_groups == ()
    # The draft's review metadata must not have travelled with the body.
    assert "speak_human_review" not in draft.body
    assert draft.body == "# 標題：一\n\n中文內文。"


def test_excluded_document_is_recorded_and_never_written(tmp_path: Path) -> None:
    from tools.build_distractor_corpus import build

    english = tmp_path / "docs"
    _write(english / "clean.md", "# Clean\n\nContainer Apps scales to zero.\n")
    _write(english / "leaky.md", "# Leaky\n\nWe promise 99.9% monthly uptime.\n")

    manifest = build(
        english_root=english,
        chinese_root=None,
        out_dir=tmp_path / "out",
        tenant_id="acme",
        effective_date="2026-08-30",
        limit=None,
    )

    assert [document["doc_id"] for document in manifest.data["documents"]] == ["labdocs-clean"]
    assert manifest.data["excluded_documents"] == [
        {"root_label": "labdocs", "source_path": "leaky.md", "rules": [1]}
    ]
    assert manifest.data["excluded_count"] == 1
    assert not (tmp_path / "out" / "acme" / "labdocs-leaky.md").exists()


def test_detached_digest_file_pins_the_manifest_bytes(tmp_path: Path) -> None:
    # A later step passes this hex to the query runner as --manifest-sha256.
    # If the file held anything but the digest of manifest.json's bytes, the
    # evidence would pin a corpus nobody indexed.
    from tools.build_distractor_corpus import build, digest_of

    english = tmp_path / "docs"
    _write(english / "clean.md", "# Clean\n\nBody.\n")

    manifest = build(
        english_root=english,
        chinese_root=None,
        out_dir=tmp_path / "out",
        tenant_id="acme",
        effective_date="2026-08-30",
        limit=None,
    )

    written = manifest.manifest_path.read_bytes()
    assert manifest.digest_path.read_text(encoding="utf-8").strip() == digest_of(written)
    assert manifest.digest_path.read_text(encoding="utf-8").strip() == manifest.digest
    assert "sha256" not in manifest.data
    # Each document entry pins the bytes actually on disk.
    entry = manifest.data["documents"][0]
    on_disk = (tmp_path / "out" / entry["output_path"]).read_bytes()
    assert entry["content_sha256"] == digest_of(on_disk)
    assert entry["bytes"] == len(on_disk)


def test_limit_cuts_the_interleaved_list_so_a_smaller_run_is_a_prefix(tmp_path: Path) -> None:
    from tools.build_distractor_corpus import build

    english = tmp_path / "docs"
    chinese = tmp_path / "zh-tw"
    for index in range(3):
        _write(english / f"e{index}.md", f"# E{index}\n\nBody.\n")
        _write(chinese / f"c{index}.md", f"# C{index}\n\nBody.\n")

    def doc_ids(out: str, limit: int | None) -> list[str]:
        manifest = build(
            english_root=english,
            chinese_root=chinese,
            out_dir=tmp_path / out,
            tenant_id="acme",
            effective_date="2026-08-30",
            limit=limit,
            chinese_globs=("c*.md",),
        )
        return [document["doc_id"] for document in manifest.data["documents"]]

    full = doc_ids("full", None)
    assert len(full) == 6
    assert doc_ids("cut", 3) == full[:3] == ["labdocs-e0", "draft-c0", "labdocs-e1"]


def test_building_into_a_non_empty_directory_aborts(tmp_path: Path) -> None:
    # Leftovers from an earlier generation would be indexed alongside this
    # one and counted by neither manifest.
    from tools.build_distractor_corpus import build

    english = tmp_path / "docs"
    _write(english / "clean.md", "# Clean\n\nBody.\n")
    out_dir = tmp_path / "out"
    _write(out_dir / "acme" / "stale.md", "# Stale\n\nBody.\n")

    with pytest.raises(SystemExit, match="not empty"):
        build(
            english_root=english,
            chinese_root=None,
            out_dir=out_dir,
            tenant_id="acme",
            effective_date="2026-08-30",
            limit=None,
        )


def test_a_missing_source_root_aborts(tmp_path: Path) -> None:
    # rglob() over a path that does not exist yields nothing rather than
    # raising, so a mistyped --english-root would otherwise produce a
    # complete-looking corpus with a whole language missing from it.
    from tools.build_distractor_corpus import build

    english = tmp_path / "docs"
    _write(english / "clean.md", "# Clean\n\nBody.\n")

    with pytest.raises(SystemExit, match="is not a directory"):
        build(
            english_root=tmp_path / "typo",
            chinese_root=None,
            out_dir=tmp_path / "out",
            tenant_id="acme",
            effective_date="2026-08-30",
            limit=None,
        )
    with pytest.raises(SystemExit, match="is not a directory"):
        build(
            english_root=english,
            chinese_root=tmp_path / "typo",
            out_dir=tmp_path / "out",
            tenant_id="acme",
            effective_date="2026-08-30",
            limit=None,
        )


def test_a_corpus_with_no_documents_aborts(tmp_path: Path) -> None:
    # Globs that match nothing, or a rule set that excludes everything, must
    # not leave an empty directory that a later step indexes as a corpus.
    from tools.build_distractor_corpus import build

    english = tmp_path / "docs"
    english.mkdir()
    chinese = tmp_path / "zh-tw"
    _write(chinese / "notes.md", "# Notes\n\nBody.\n")

    with pytest.raises(SystemExit, match="no documents"):
        build(
            english_root=english,
            chinese_root=chinese,
            out_dir=tmp_path / "out",
            tenant_id="acme",
            effective_date="2026-08-30",
            limit=None,
            chinese_globs=("day-9*.md",),
        )


def test_a_non_positive_limit_aborts(tmp_path: Path) -> None:
    # `admitted[:-1]` silently drops the last document instead of failing,
    # which would be a corpus one document short of its own manifest name.
    from tools.build_distractor_corpus import build

    english = tmp_path / "docs"
    _write(english / "clean.md", "# Clean\n\nBody.\n")

    with pytest.raises(SystemExit, match="--limit must be positive"):
        build(
            english_root=english,
            chinese_root=None,
            out_dir=tmp_path / "out",
            tenant_id="acme",
            effective_date="2026-08-30",
            limit=-1,
        )


def test_a_document_with_no_body_aborts(tmp_path: Path) -> None:
    # An empty document indexes as a chunkless, unretrievable member of the
    # corpus: it inflates the document count without adding a distractor.
    from tools.build_distractor_corpus import build

    english = tmp_path / "docs"
    _write(english / "clean.md", "# Clean\n\nBody.\n")
    _write(english / "stub.md", "---\nday: 1\n---\n")

    with pytest.raises(SystemExit, match="no body"):
        build(
            english_root=english,
            chinese_root=None,
            out_dir=tmp_path / "out",
            tenant_id="acme",
            effective_date="2026-08-30",
            limit=None,
        )


def test_main_builds_a_corpus_from_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The operator's actual surface. `action="append"` / `dest` / `default`
    # are wiring that a build()-only test cannot see: a defect there hands
    # build() a None, which is exactly the fail-open case above.
    import json

    from tools.build_distractor_corpus import main

    english = tmp_path / "docs"
    _write(english / "architecture.md", "# Architecture\n\nEnglish body.\n")
    chinese = tmp_path / "zh-tw"
    _write(chinese / "day-01-a.md", "# One\n\nBody one.\n")
    _write(chinese / "day-13-b.md", "# Thirteen\n\nBody thirteen.\n")
    _write(chinese / "day-27-unpublished.md", "# Unpublished\n\nBody.\n")
    _write(chinese / "notes.md", "# Notes\n\nBody.\n")
    out_dir = tmp_path / "out"

    main(
        [
            "--english-root", str(english),
            "--chinese-root", str(chinese),
            "--chinese-glob", "day-0*.md",
            "--chinese-glob", "day-1*.md",
            "--tenant-id", "acme",
            "--effective-date", "2026-08-30",
            "--out-dir", str(out_dir),
        ]
    )

    assert sorted(path.name for path in (out_dir / "acme").iterdir()) == [
        "draft-day-01-a.md",
        "draft-day-13-b.md",
        "labdocs-architecture.md",
    ]
    data = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    # Both patterns arrived, in order: proof the repeatable flag appends.
    assert data["source_roots"][1]["name_globs"] == ["day-0*.md", "day-1*.md"]
    assert data["document_count"] == 3
    assert "documents: 3" in capsys.readouterr().out


def test_main_refuses_a_chinese_root_without_a_glob(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tools.build_distractor_corpus import main

    english = tmp_path / "docs"
    _write(english / "clean.md", "# Clean\n\nBody.\n")
    chinese = tmp_path / "zh-tw"
    _write(chinese / "day-27-unpublished.md", "# Unpublished\n\nBody.\n")
    out_dir = tmp_path / "out"

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--english-root", str(english),
                "--chinese-root", str(chinese),
                "--tenant-id", "acme",
                "--effective-date", "2026-08-30",
                "--out-dir", str(out_dir),
            ]
        )

    assert excinfo.value.code == 2
    assert "--chinese-glob" in capsys.readouterr().err
    assert not out_dir.exists()


def test_main_without_a_chinese_root_needs_no_glob(tmp_path: Path) -> None:
    from tools.build_distractor_corpus import main

    english = tmp_path / "docs"
    _write(english / "clean.md", "# Clean\n\nBody.\n")
    out_dir = tmp_path / "out"

    main(
        [
            "--english-root", str(english),
            "--tenant-id", "acme",
            "--effective-date", "2026-08-30",
            "--out-dir", str(out_dir),
        ]
    )

    assert [path.name for path in (out_dir / "acme").iterdir()] == ["labdocs-clean.md"]
