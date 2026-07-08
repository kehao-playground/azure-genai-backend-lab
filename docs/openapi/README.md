# OpenAPI

The OpenAPI contract is exported from the FastAPI application and committed. CI fails if the committed file drifts from the code.

```bash
uv run python tools/export_openapi.py
```

Output:

```text
docs/openapi/openapi.yaml
```

Re-run the export in any PR that changes routes or schemas.
