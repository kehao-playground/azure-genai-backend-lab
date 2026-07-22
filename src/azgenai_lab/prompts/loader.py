"""Prompt templates: versioned files with YAML front matter (Day 8).

A prompt is a production asset with its own lifecycle — it does not live in
transcripts or replay history (Day 7), so its identity (name) and revision
(version) must be self-describing. Validation is strict and happens at load
time; build_chat_service loads at startup, so a malformed template kills the
app before it can serve a request (fail-fast, Day 5 convention).

Format (all four fields required):

    ---
    name: default_chat        # must equal the filename stem
    version: 1                # int, bump on every semantic change
    description: ...
    changelog:
      - "v1: initial"
    ---
    <prompt text>

No template engine and no variables — YAGNI until a real variable need
appears (revisit with RAG context assembly, Day 11).
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

_PROMPTS_DIR = Path(__file__).parent
_DELIMITER = "---"
_KNOWN_FIELDS = frozenset({"name", "version", "description", "changelog"})


class PromptTemplateError(Exception):
    """A template is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class PromptTemplate:
    """``name@version`` is the human-readable release label a person chooses;
    ``sha256`` is content provenance computed from the bytes — a forgotten
    version bump cannot lie about what text was actually sent upstream."""

    name: str
    version: int
    description: str
    text: str
    sha256: str


def load_prompt(name: str, base_dir: Path = _PROMPTS_DIR) -> PromptTemplate:
    path = base_dir / f"{name}.md"
    if not path.is_file():
        raise PromptTemplateError(f"prompt template not found: {path}")
    raw = path.read_text(encoding="utf-8")

    if not raw.startswith(_DELIMITER + "\n"):
        raise PromptTemplateError(f"{path.name}: missing YAML front matter block")
    try:
        front, _, body = raw.removeprefix(_DELIMITER + "\n").partition("\n" + _DELIMITER + "\n")
    except ValueError as exc:  # pragma: no cover - partition never raises; belt only
        raise PromptTemplateError(f"{path.name}: malformed front matter") from exc
    if not body and "\n" + _DELIMITER + "\n" not in raw:
        raise PromptTemplateError(f"{path.name}: missing YAML front matter close")

    try:
        meta = yaml.safe_load(front)
    except yaml.YAMLError as exc:
        raise PromptTemplateError(f"{path.name}: invalid YAML front matter: {exc}") from exc
    if not isinstance(meta, dict):
        raise PromptTemplateError(f"{path.name}: front matter must be a YAML mapping")

    unknown_fields = sorted(set(meta) - _KNOWN_FIELDS)
    if unknown_fields:
        raise PromptTemplateError(
            f"{path.name}: unknown front matter field(s): {', '.join(unknown_fields)}"
        )

    for field in ("name", "version", "description", "changelog"):
        if field not in meta:
            raise PromptTemplateError(f"{path.name}: front matter missing field: {field}")
    if not isinstance(meta["name"], str) or not isinstance(meta["description"], str):
        raise PromptTemplateError(f"{path.name}: name and description must be strings")
    # bool is an int subclass; a literal true/false version is a mistake.
    if not isinstance(meta["version"], int) or isinstance(meta["version"], bool):
        raise PromptTemplateError(f"{path.name}: version must be an integer")
    if meta["version"] < 1:
        raise PromptTemplateError(f"{path.name}: version must be >= 1")
    if not meta["name"].strip():
        raise PromptTemplateError(f"{path.name}: name must not be empty")
    if not meta["description"].strip():
        raise PromptTemplateError(f"{path.name}: description must not be empty")
    if not isinstance(meta["changelog"], list) or not all(
        isinstance(entry, str) for entry in meta["changelog"]
    ):
        raise PromptTemplateError(f"{path.name}: changelog must be a list of strings")
    if not meta["changelog"]:
        raise PromptTemplateError(f"{path.name}: changelog must not be empty")
    if meta["name"] != path.stem:
        raise PromptTemplateError(
            f"{path.name}: front matter name {meta['name']!r} does not match filename"
        )

    text = body.strip()
    if not text:
        raise PromptTemplateError(f"{path.name}: prompt body is empty")

    return PromptTemplate(
        name=meta["name"],
        version=meta["version"],
        description=meta["description"],
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
