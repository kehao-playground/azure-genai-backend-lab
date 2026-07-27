---
doc_id: oncall-runbook
title: On-Call Runbook
doc_type: runbook
tenant_id: globex
effective_date: 2026-03-10
---

# On-Call Runbook

This runbook tells the on-call engineer what to do when paged, who to
escalate to, and how to respond to the alerts most commonly seen in
production. It assumes the reader has production access already and is
starting from the paging notification itself.

## Paging policy

Pages are generated automatically from the monitoring system when a metric
crosses a threshold for more than five consecutive minutes. The on-call
engineer must acknowledge a page within 10 minutes; an unacknowledged page
automatically re-pages the secondary on-call and, after a further 10
minutes, the engineering manager. Acknowledging a page means the engineer
has seen it and is actively investigating, not that the issue is resolved.

### Related documentation

See the escalation path below for what happens after a page goes
unacknowledged, and the service SLA for the response-time targets a page
is ultimately measured against.

## Escalation path

If the on-call engineer cannot resolve an incident within 30 minutes of
acknowledging it, they must escalate to the secondary on-call by name in
the incident channel, not merely by re-triggering the page. If the incident
is customer-impacting and unresolved after 60 minutes, the engineering
manager and the on-call lead for the affected service must both be paged
directly, and a customer-facing status update must be posted regardless of
whether the root cause has been identified yet.

### Related documentation

See the paging policy above for how an incident reaches this stage, and
the common alerts below for the playbooks referenced during escalation.

## Common alerts

### High error rate

This alert fires when the 5xx response rate for a service exceeds 2% over
a five-minute window. The first step is to check the service's recent
deployment history, since a fresh deployment is the most common cause. If
a deployment correlates with the alert, roll it back before investigating
further. If no recent deployment correlates, check upstream dependency
health next.

### Elevated latency

This alert fires when the p99 latency for a service exceeds its configured
threshold for five consecutive minutes. Elevated latency is frequently a
symptom of a downstream dependency slowing down rather than a problem in
the alerting service itself, so the runbook directs the engineer to check
downstream dependencies before assuming the paged service is at fault.

### Queue backlog

This alert fires when a message queue's depth grows faster than its
configured consumers can drain it. The engineer should first confirm that
consumers are healthy and processing messages at all, then check whether
the backlog is a temporary spike from an upstream burst or a sustained
trend that indicates a consumer has stalled.
