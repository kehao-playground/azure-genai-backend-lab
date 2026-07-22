# Cost and Monitoring

Day 9 makes token cost a backend contract instead of a monthly surprise. Day 27 extends this with Application Insights.

## Metering over estimation

The application never estimates token counts with a local tokenizer. Every Responses API result carries a `usage` block — provider-reported, request-level metering — and the backend propagates it:

- into the API contract (`usage` on `/chat` responses and on the `message.done` terminal event),
- into the logs, one line per call that returned a usage-bearing terminal (non-streaming success, stream `completed`/`incomplete`), joinable with the prompt-attribution line (Day 8) on `correlation_id`:

```text
llm usage input_tokens=1234 output_tokens=210 reasoning_tokens=128 total_tokens=1444 correlation_id=…
```

This usage signal is for attribution and guardrails, not accounting: it is not an invoice record, and no per-request 1:1 reconciliation against Cost Management meters is implied. With reasoning models, hidden reasoning tokens (`usage.output_tokens_details.reasoning_tokens`, kept as `reasoning_tokens`) are billed as output — short exchanges can be output-dominated for exactly that reason. Input becomes the dominant term only as conversations grow: with `store=False` the full replay context is resent every turn, so *cumulative* input spend grows quadratically with turn count. That asymptote is what the per-conversation budget exists for.

## The two guardrails

| Mechanism | Bound | Failure mode when it fires |
|---|---|---|
| `max_output_tokens` per call (`LLM_MAX_OUTPUT_TOKENS`, default 1000) | one reply | `incomplete`/`max_output_tokens` on both endpoints — streaming via `message.done`, non-streaming via the `status`/`incomplete_reason` response fields; the client keeps the partial text (Day 6 rule) |
| Per-conversation lifetime budget (`CONVERSATION_TOKEN_BUDGET`, default 50000) | one conversation | `429 token_budget_exceeded` before inference — costs nothing upstream |

The budget is a post-paid ledger: billed usage accumulates atomically with each committed turn, and the check runs between turns. A single turn can therefore overshoot the line by up to one call's worth of tokens — bounded by `max_output_tokens` plus the history that turn replays. Reject-vs-truncate-vs-degrade: this backend rejects at the conversation level and truncates at the reply level; degrade (switching to a cheaper model near the line) is deliberately out of scope.

## Known gaps (disclosed, not hidden)

- A failed turn (upstream error, discarded `content_filter` text, disconnect) is billed upstream but never enters the ledger — turn-commit semantics (Day 7) win over billing completeness. The same paths produce no `llm usage` log line either (there is no usage-bearing terminal to read): a missing line is not zero cost, and reconciliation belongs to Cost Management.
- The ledger is per conversation, not per user: there is no identity until Day 19. Per-user and per-feature quotas belong behind authentication.
- Log lines are attribution, not metrics: aggregation (spend per day, per prompt version) is a Cost Management / Application Insights job (Day 27), not grep's.

The subscription-level backstop is an Azure Cost Management budget alert — a **delayed detection/notification mechanism, not a spending cap**: per Microsoft's documentation, crossing a budget threshold triggers notifications only; resources are not stopped and consumption continues, and cost data itself lags hours behind usage. Application guardrails bound the burn rate; the budget alert tells you (late) that something got past them. An actual automated stop would require an action-group automation with its own failure modes — out of scope here.
