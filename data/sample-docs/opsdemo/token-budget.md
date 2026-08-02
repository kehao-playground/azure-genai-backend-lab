---
doc_id: token-budget
title: Conversation Token Budget
doc_type: runbook
tenant_id: opsdemo
effective_date: 2026-08-01
allowed_groups: []
---

# Conversation Token Budget

## How the budget works

Each conversation has a lifetime budget measured in provider-reported
tokens. The ledger accumulates with each committed turn, and the check
runs before inference: an exhausted conversation is rejected before any
money is spent upstream.

## What a 429 means

When a conversation's spent tokens reach its budget, the next request is
rejected with HTTP 429 and error code `token_budget_exceeded`. The budget
does not replenish and there is no Retry-After. The remedy is to start a
new conversation.

## Known accounting gap

A failed turn may have incurred billable processing upstream but leaves no
ledger trace — turn-commit semantics win over accounting completeness. The
authoritative spend record is Azure Cost Management, not this ledger.
