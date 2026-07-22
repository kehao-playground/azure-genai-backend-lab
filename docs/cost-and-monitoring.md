# Cost and Monitoring

Day 9 makes token cost a backend contract instead of a monthly surprise. Day 27 extends this with Application Insights.

## Metering over estimation

The application never estimates token counts with a local tokenizer. Every Responses API result carries a `usage` block — the same numbers the invoice is built from — and the backend propagates them:

- into the API contract (`usage` on `/chat` responses and on the `message.done` terminal event),
- into the logs, one line per billed call, joinable with the prompt-attribution line (Day 8) on `correlation_id`:

```text
llm usage input_tokens=1234 output_tokens=210 total_tokens=1444 correlation_id=…
```

`input_tokens` dominates conversation cost: with `store=False` the full replay context is resent every turn, so input grows roughly quadratically over a conversation's life. That is the number the budget guardrail exists for.

## The two guardrails

| Mechanism | Bound | Failure mode when it fires |
|---|---|---|
| `max_output_tokens` per call (`LLM_MAX_OUTPUT_TOKENS`, default 1000) | one reply | `incomplete`/`max_output_tokens` — client keeps the partial text (Day 6) |
| Per-conversation lifetime budget (`CONVERSATION_TOKEN_BUDGET`, default 50000) | one conversation | `429 token_budget_exceeded` before inference — costs nothing upstream |

The budget is a post-paid ledger: billed usage accumulates atomically with each committed turn, and the check runs between turns. A single turn can therefore overshoot the line by up to one call's worth of tokens — bounded by `max_output_tokens` plus the history that turn replays. Reject-vs-truncate-vs-degrade: this backend rejects at the conversation level and truncates at the reply level; degrade (switching to a cheaper model near the line) is deliberately out of scope.

## Known gaps (disclosed, not hidden)

- A failed turn (upstream error, discarded `content_filter` text, disconnect) is billed upstream but never enters the ledger — turn-commit semantics (Day 7) win over billing completeness.
- The ledger is per conversation, not per user: there is no identity until Day 19. Per-user and per-feature quotas belong behind authentication.
- Log lines are attribution, not metrics: aggregation (spend per day, per prompt version) is a Cost Management / Application Insights job (Day 27), not grep's.

The subscription-level backstop is an Azure Cost Management budget alert — application guardrails bound the burn rate; the budget alert bounds the blast radius.
