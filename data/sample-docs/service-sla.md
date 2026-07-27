---
doc_id: service-sla
title: Service SLA
doc_type: sla
tenant_id: acme
effective_date: 2026-02-01
---

# Service SLA

This document defines the availability and response-time commitments Acme
makes to customers on paid support plans, along with the exclusions that
limit those commitments. It is the reference support engineers use when a
customer asks whether an incident counts against the SLA.

## Availability targets

Acme publishes a monthly uptime target for each support tier. Uptime is
measured as the percentage of five-minute intervals in a calendar month
during which the platform's public API returned successful responses to
synthetic health checks run from three independent regions.

### Standard tier

Standard tier customers are covered by a 99.5% monthly uptime target. If
uptime falls below this target in a given month, the customer is entitled
to a service credit calculated as a percentage of that month's subscription
fee, applied to the following month's invoice.

### Premium tier

Premium tier customers are covered by a 99.9% monthly uptime target, backed
by a dedicated on-call rotation and a higher service-credit percentage for
any shortfall. Premium tier customers also receive a monthly uptime report
regardless of whether the target was met.

## Response times

Support requests are triaged into severity levels, and each level carries a
first-response target measured from the time the request is submitted
through the support portal or email.

Severity 1 incidents, defined as a complete outage of the customer's
production environment, carry a 30 minute first-response target for Premium
tier and a 2 hour target for Standard tier. Severity 2 incidents, defined as
a significant feature degradation without a full outage, carry a 4 hour
target for Premium tier and a next-business-day target for Standard tier.
Severity 3 and 4 requests, covering minor issues and general questions,
are handled on a best-effort basis without a contractual response target.

## Exclusions

Scheduled maintenance windows, announced at least 5 business days in
advance, do not count against the uptime target. Outages caused by a
customer's own misconfiguration, by a third-party service the customer has
integrated, or by a force-majeure event are also excluded from the uptime
calculation and from response-time credits.
