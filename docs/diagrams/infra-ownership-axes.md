# Infrastructure Ownership Axes

Handing infrastructure to a template is two decisions, not one — this diagram
walks one object through both. Axis 1 (provisioning ownership) asks three
questions: a type can declare it, the properties you care about are not
resource functions or generated secrets, and changes are previewable with
`what-if`. Fail any and creation stays in scripts; pass all three and create +
update move to Bicep while teardown stays in scripts — where
[`main.bicep`](../../infra/bicep/main.bicep) sits today. Axis 2 (full lifecycle
ownership) only starts after a live deployment-stack test on that resource
type, and then asks whether `actionOnUnmanage` can express the teardown and
whether ownership is traceable. Nothing in this lab is across axis 2; the
conditions and the evidence behind each cell are in
[infra-evolution.md](../infra-evolution.md#the-two-axes). The side note on the
default matters most: `actionOnUnmanage` defaults to detach, so a wrong guess
does not break anything — the resource you think is gone keeps running and
keeps billing.

This English diagram is the semantic companion to the article's published
figure. The publication PNG is rendered from the localized source
[`infra-ownership-axes.zh-tw.mmd`](infra-ownership-axes.zh-tw.mmd) in this
same directory — both sources are public and canonical here; the planning repo
stores only the rendered PNG snapshot. Changes to either file must keep the
two topologies identical.

```mermaid
flowchart TD
    S["an infra object"] --> A1{"axis 1: provisioning ownership<br/>(a) a type can declare it?<br/>(b) properties not from resource functions / generated secrets?<br/>(c) changes previewable (what-if)?"}
    A1 -->|"any condition fails"| K1["create + update stay in scripts"]
    A1 -->|"all three hold"| B["create + update move to Bicep; teardown stays in scripts<br/>(main.bicep: the AOAI account + two deployments)"]
    B --> A2{"axis 2: full lifecycle ownership<br/>prerequisite: a live deployment-stack test on that type<br/>(d) actionOnUnmanage can express its teardown?<br/>(e) ownership traceable?"}
    A2 -->|"untested, or a condition fails"| K2["teardown stays in scripts<br/>(this lab today: everything)"]
    A2 -->|"tested + both hold"| C["full lifecycle to a deployment stack<br/>(this lab today: nothing)"]

    K1 -.- n1["e.g. Entra client secrets,<br/>the search index schema, GitHub"]
    A1 -.- n2["Graph objects pass (a)<br/>but have no what-if — stopped by (c)"]
    A2 -.- n3["default actionOnUnmanage = detach:<br/>guess wrong and the resource you<br/>think is gone keeps billing"]

    style B fill:#d3f0d8,stroke:#2e7d32
    style n1 fill:#f8f9fa,stroke:#adb5bd,stroke-dasharray:4
    style n2 fill:#f8f9fa,stroke:#adb5bd,stroke-dasharray:4
    style n3 fill:#f8f9fa,stroke:#adb5bd,stroke-dasharray:4
```
