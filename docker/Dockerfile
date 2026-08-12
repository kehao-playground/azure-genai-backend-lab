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

# Dependency layer first: src/ changes must not invalidate it.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# --no-editable installs the project (including azgenai_lab/prompts/*.md)
# into site-packages, so the runtime stage needs .venv and nothing else.
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app
# Root-owned on purpose: the runtime user cannot rewrite its own code.
COPY --from=builder /app/.venv /app/.venv

# The default fake-mode agent tools seed the ops-assistant index from
# bundled sample documents at startup (services/agent_tools.py), and
# document_loader.SAMPLE_DOCS_DIR resolves that path relative to its own
# module location on disk. Under this --no-editable install that lands
# under the venv's stdlib directory, not /app -- this copy is not project
# code, it is what that resolution already expects for this venv layout,
# and it is what makes the zero-env-var "docker run" experience work at
# all (see docs/docker.md).
COPY data/sample-docs /app/.venv/lib/python3.13/data/sample-docs

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
