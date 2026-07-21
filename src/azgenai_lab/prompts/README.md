# Prompts

Prompt templates are Markdown files with a required YAML front matter block:

```yaml
---
name: default_chat        # must equal the filename stem
version: 1                # int; bump on every semantic change
description: one-line purpose
changelog:
  - "v1: initial"
---
```

Rules:

- Loaded and validated at startup by `loader.load_prompt` — a malformed
  template fails the build, never a request.
- `version` is the runtime-visible revision: it is logged on every upstream
  call, so incidents can answer "which prompt produced this?". Git history
  is the release note; the changelog list is the human summary.
- No template engine and no variables until a concrete need appears.
