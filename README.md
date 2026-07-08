# Azure GenAI Backend Lab

A Python/FastAPI lab for building production-minded Azure GenAI backend applications.

This repository accompanies a bilingual content series:

- Traditional Chinese iThome Ironman series
- English technical site built with Astro and hosted on Cloudflare Pages
- English-first GitHub reference implementation with Traditional Chinese companion notes

## Goals

This project demonstrates how backend engineers can build GenAI applications that are not just demos, but production-minded systems:

- Azure OpenAI-backed chat APIs
- Streaming responses
- RAG with Azure AI Search
- Microsoft Agent Framework for Python
- API management and security boundaries
- Observability and audit logging
- Containerized deployment to Azure Container Apps
- Testing, BDD scenarios, OpenAPI specs, sequence diagrams, and state models

## Current Status

This repository starts as a skeleton and evolves with the article series.

Implemented in the initial skeleton:

- FastAPI app skeleton with `/api/v1` routing, error envelope, and correlation ID middleware
- Health endpoint
- pytest unit test skeleton
- behave BDD skeleton
- OpenAPI export script (committed contract with CI drift check)
- Docs structure
- Astro site skeleton
- Working minimal Dockerfile

## Quick Start

```bash
uv sync
uv run uvicorn azgenai_lab.main:app --reload
```

Open:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/docs
```

## Run Tests

```bash
uv run pytest
uv run behave
uv run ruff check .
uv run mypy
```

## Export OpenAPI

```bash
uv run python tools/export_openapi.py
```

Output:

```text
docs/openapi/openapi.yaml
```

## Documentation

- [API Conventions](docs/api-conventions.md)
- [Architecture](docs/architecture.md)
- [Architecture Style](docs/architecture-style.md)
- [Testing Strategy](docs/testing-strategy.md)
- [Production Readiness Checklist](docs/production-readiness-checklist.md)
- [Traditional Chinese Companion Notes](docs/zh-tw/README.md)

## License

MIT
