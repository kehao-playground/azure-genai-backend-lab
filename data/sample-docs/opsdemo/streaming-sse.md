---
doc_id: streaming-sse
title: Streaming Terminal Events
doc_type: runbook
tenant_id: opsdemo
effective_date: 2026-08-01
allowed_groups: []
---

# Streaming Terminal Events

## The event vocabulary

Streaming endpoints use Server-Sent Events with an owned vocabulary; the
upstream provider's event names never reach clients. `message.delta`
carries one increment of output text. `message.done` is the sole success
terminal, carrying `status` (`completed` or `incomplete`) and, when
incomplete, an `incomplete_reason`. `error` is the sole failure terminal,
carrying the same error envelope used everywhere else in the API.

## Exactly one terminal event

A normally closed stream ends with exactly one terminal event —
`message.done` or `error` — and nothing is emitted after it. A client that
reaches end-of-stream without having seen a terminal event must treat that
as a failure rather than a silent success.

## Unknown event names are additive

Clients must ignore event names they do not recognize. New event types are
added to the vocabulary over time without breaking existing clients, so
an unrecognized name is not itself an error condition.
