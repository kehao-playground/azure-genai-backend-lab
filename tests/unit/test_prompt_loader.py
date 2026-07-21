"""Prompt templates are startup-validated assets: any malformed template must
raise PromptTemplateError so build fails fast, before the first request."""

from pathlib import Path

import pytest

from azgenai_lab.prompts.loader import PromptTemplate, PromptTemplateError, load_prompt


def _write(tmp_path: Path, name: str, content: str) -> Path:
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
    assert template == PromptTemplate(
        name="sample",
        version=2,
        description="test template",
        text="You are a test assistant.\nSecond line.",
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
