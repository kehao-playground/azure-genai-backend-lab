"""Prompt templates are startup-validated assets: any malformed template must
raise PromptTemplateError so build fails fast, before the first request."""

import hashlib
from pathlib import Path

import pytest

from azgenai_lab.prompts.loader import PromptTemplate, PromptTemplateError, load_prompt


def _write(tmp_path: Path, name: str, content: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return tmp_path


VALID = """---
name: sample
version: 2
description: test template
changelog:
  - "v2: second"
  - "v1: initial"
---
You are a test assistant.
Second line.
"""


def test_loads_valid_template(tmp_path: Path) -> None:
    base = _write(tmp_path, "sample", VALID)
    template = load_prompt("sample", base_dir=base)
    text = "You are a test assistant.\nSecond line."
    assert template == PromptTemplate(
        name="sample",
        version=2,
        description="test template",
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def test_default_chat_ships_and_loads() -> None:
    template = load_prompt("default_chat")
    assert template.name == "default_chat"
    assert template.version >= 1
    assert template.text


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PromptTemplateError, match="not found"):
        load_prompt("nope", base_dir=tmp_path)


def test_missing_front_matter_raises(tmp_path: Path) -> None:
    base = _write(tmp_path, "bare", "just text, no front matter\n")
    with pytest.raises(PromptTemplateError, match="front matter"):
        load_prompt("bare", base_dir=base)


def test_bad_yaml_raises(tmp_path: Path) -> None:
    base = _write(tmp_path, "bad", "---\nname: [unclosed\n---\nbody\n")
    with pytest.raises(PromptTemplateError, match="YAML"):
        load_prompt("bad", base_dir=base)


@pytest.mark.parametrize("missing", ["name", "version", "description", "changelog"])
def test_missing_field_raises(tmp_path: Path, missing: str) -> None:
    fields = {
        "name": "name: partial",
        "version": "version: 1",
        "description": "description: d",
        "changelog": 'changelog:\n  - "v1: initial"',
    }
    del fields[missing]
    content = "---\n" + "\n".join(fields.values()) + "\n---\nbody\n"
    base = _write(tmp_path, "partial", content)
    with pytest.raises(PromptTemplateError, match=missing):
        load_prompt("partial", base_dir=base)


def test_non_int_version_raises(tmp_path: Path) -> None:
    content = VALID.replace("version: 2", "version: '2'")
    base = _write(tmp_path, "sample", content)
    with pytest.raises(PromptTemplateError, match="version"):
        load_prompt("sample", base_dir=base)


def test_name_filename_mismatch_raises(tmp_path: Path) -> None:
    base = _write(tmp_path, "other", VALID)  # front matter says "sample"
    with pytest.raises(PromptTemplateError, match="filename"):
        load_prompt("other", base_dir=base)


def test_empty_body_raises(tmp_path: Path) -> None:
    content = VALID.split("---\n")[0] + "---\n" + VALID.split("---\n")[1] + "---\n\n"
    base = _write(tmp_path, "sample", content)
    with pytest.raises(PromptTemplateError, match="empty"):
        load_prompt("sample", base_dir=base)


def test_different_body_text_produces_different_sha256(tmp_path: Path) -> None:
    base_a = _write(tmp_path / "a", "sample", VALID)
    other = VALID.replace("You are a test assistant.", "You are a different assistant.")
    base_b = _write(tmp_path / "b", "sample", other)
    a = load_prompt("sample", base_dir=base_a)
    b = load_prompt("sample", base_dir=base_b)
    assert a.sha256 != b.sha256


def test_identical_body_text_produces_same_sha256(tmp_path: Path) -> None:
    base_a = _write(tmp_path / "a", "sample", VALID)
    base_b = _write(tmp_path / "b", "sample", VALID)
    a = load_prompt("sample", base_dir=base_a)
    b = load_prompt("sample", base_dir=base_b)
    assert a.sha256 == b.sha256
    assert len(a.sha256) == 64


def test_unknown_front_matter_field_raises(tmp_path: Path) -> None:
    content = VALID.replace(
        "description: test template", "description: test template\nextra_field: nope"
    )
    base = _write(tmp_path, "sample", content)
    with pytest.raises(PromptTemplateError, match="extra_field"):
        load_prompt("sample", base_dir=base)


def test_version_zero_raises(tmp_path: Path) -> None:
    content = VALID.replace("version: 2", "version: 0")
    base = _write(tmp_path, "sample", content)
    with pytest.raises(PromptTemplateError, match="version"):
        load_prompt("sample", base_dir=base)


def test_negative_version_raises(tmp_path: Path) -> None:
    content = VALID.replace("version: 2", "version: -1")
    base = _write(tmp_path, "sample", content)
    with pytest.raises(PromptTemplateError, match="version"):
        load_prompt("sample", base_dir=base)


def test_empty_name_raises(tmp_path: Path) -> None:
    content = VALID.replace("name: sample", 'name: "   "')
    base = _write(tmp_path, "sample", content)
    with pytest.raises(PromptTemplateError, match="name"):
        load_prompt("sample", base_dir=base)


def test_empty_description_raises(tmp_path: Path) -> None:
    content = VALID.replace("description: test template", 'description: "   "')
    base = _write(tmp_path, "sample", content)
    with pytest.raises(PromptTemplateError, match="description"):
        load_prompt("sample", base_dir=base)


def test_empty_changelog_list_raises(tmp_path: Path) -> None:
    content = VALID.replace('changelog:\n  - "v2: second"\n  - "v1: initial"', "changelog: []")
    base = _write(tmp_path, "sample", content)
    with pytest.raises(PromptTemplateError, match="changelog"):
        load_prompt("sample", base_dir=base)
