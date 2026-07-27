# Search index schema

`index-schema.json` is **generated**. Do not edit it by hand.

The schema is defined in `src/azgenai_lab/models/search_index.py` and rendered by:

```bash
uv run python tools/export_index_schema.py
```

CI regenerates it and fails on drift, the same way it guards `docs/openapi/openapi.yaml`.

The JSON is the body of a [Create or Update
Index](https://learn.microsoft.com/en-us/rest/api/searchservice/indexes/create-or-update) request.
It is not applied to a live service in this milestone; Day 13 creates the ephemeral service that
consumes it.
