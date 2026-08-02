---
doc_id: error-contract
title: Error Contract
doc_type: runbook
tenant_id: opsdemo
effective_date: 2026-08-01
allowed_groups: []
---

# Error Contract

## The error envelope

Every HTTP error response, across every endpoint, shares one JSON shape:
`{"error": {"code", "message"}, "correlation_id"}`. Clients never need to
parse more than one error shape. The envelope is produced by a single
exception handler, never by a framework's default error body.

## The correlation id header

Each request carries an `X-Correlation-Id` response header. If the caller
sent one on the request, it is echoed back; otherwise a new one is
generated. The same id appears inside every error body's `correlation_id`
field, so a support engineer can join a client-reported failure to server
logs.

## 4xx and 5xx both use it

Validation failures, authentication failures, not-found responses, and
server errors all use the same envelope shape — only `code` and `message`
change. There is no separate "validation error" body format; a 422 wraps
the framework's default error detail into the same envelope with code
`validation_error`.
