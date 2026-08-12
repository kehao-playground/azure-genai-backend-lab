# Builder: uv and the build graph stay here; the runtime stage copies only
# the finished virtualenv.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/

# UV_PYTHON_DOWNLOADS=0: the venv must be built on the interpreter the
# runtime stage ships (both stages share the python:3.13-slim base).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependency layer first: src/ changes must not invalidate it. README.md
# lives in this second COPY, not the one above: --no-install-project does
# not read it (confirmed by building this layer without it), but the
# second sync below installs the project itself, and pyproject.toml's
# `readme = "README.md"` makes that sync need the file to exist.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# --no-editable installs the project (including azgenai_lab/prompts/*.md)
# into site-packages, so the runtime stage needs .venv and nothing else.
COPY src/ src/
COPY README.md ./
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app
# Root-owned on purpose: the runtime user cannot rewrite its own code.
COPY --from=builder /app/.venv /app/.venv

# Fake search is the default, and it seeds the agent's index from the
# bundled sample corpus at startup (services/agent_tools.py). That corpus is
# repo data, not wheel content, so the image ships it explicitly and points
# the loader at it: the module-relative default cannot find it once the
# project is installed non-editable (Day 23; see docs/docker.md).
COPY data/sample-docs /app/data/sample-docs
ENV SAMPLE_DOCS_DIR=/app/data/sample-docs

USER app
EXPOSE 8000

# Local docker/compose observability only: Container Apps and Kubernetes run
# their own probes against the same /health (see docs/docker.md).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

# Exec form keeps uvicorn at PID 1 (SIGTERM reaches it directly). 20s bounds
# request drain only; lifespan shutdown runs after it, inside the runtime's
# stop grace (docker stop -t 30 locally, 30s on Container Apps).
CMD ["/app/.venv/bin/uvicorn", "azgenai_lab.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
