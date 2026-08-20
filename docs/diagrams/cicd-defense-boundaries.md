# CI/CD Defense Boundaries

One push to `main`, one deploy, and every control on the way — each annotated
with what it does **not** prove. The main chain is the path a commit actually
travels through [`ci.yml`](../../.github/workflows/ci.yml); the dashed side
notes are each layer's limit, as stated in
[ci-cd.md §4](../ci-cd.md#4-layered-controls-and-each-layers-limit). A layer's
name is always broader than its guarantee: `needs` proves the automated gates
were green, not that anyone read the diff; the approval gate does not stop the
pre-approval image push ([§2](../ci-cd.md#2-two-identities-and-the-residual-approval-does-not-remove));
the deploy identity's OIDC subject carries no ref, so "only `main` deploys"
comes from two separate layers — this workflow's own job-level `if` and the
environment's deployment branch policy, which holds even for a workflow that
omits that condition ([§3](../ci-cd.md#3-subject-binding-what-it-stops-and-what-it-does-not));
and the revision poll is a failure detector, leaving the digest/revision
read-backs plus the exact-body `/health` probe to establish that a deployment
succeeded ([§11](../ci-cd.md#11-open-questions-settled-only-by-the-live-session)).

This English diagram is the semantic companion to the article's published
figure. The publication PNG is rendered from the localized source
[`cicd-defense-boundaries.zh-tw.mmd`](cicd-defense-boundaries.zh-tw.mmd) in
this same directory — both sources are public and canonical here; the planning
repo stores only the rendered PNG snapshot. Changes to either file must keep
the two topologies identical.

```mermaid
flowchart TD
    P["push to main"] --> G["gates: python / site / image<br/>(every push to main + every PR)"]
    G -->|"needs: all three green"| Q{"main? DEPLOY_ENABLED?"}
    Q --> E["environment: production<br/>branch policy + required reviewer"]
    E -->|"a human approves"| F["freshness guard<br/>is github.sha still main's HEAD?"]
    F --> O["OIDC token exchange<br/>subject = environment:production"]
    O --> D["az containerapp update<br/>--image @digest"]
    D --> H["digest / revision read-back<br/>+ exact /health body"]

    G -.- gN["does not prove: anyone read the diff<br/>ceiling = fake-CLI fidelity"]
    E -.- eN["does not stop: pre-approval push to ACR<br/>self-approval &ne; two-person review"]
    F -.- fN["answers &quot;still fresh?&quot;<br/>not &quot;was it reviewed?&quot;"]
    O -.- oN["subject carries no ref<br/>main-only = job if + branch policy (two layers)"]
    D -.- dN["runningState: failure detector only<br/>success from read-backs + /health (image-pull failure untested)"]

    style H fill:#d3f0d8,stroke:#2e7d32
    style gN fill:#f8f9fa,stroke:#adb5bd,stroke-dasharray:4
    style eN fill:#f8f9fa,stroke:#adb5bd,stroke-dasharray:4
    style fN fill:#f8f9fa,stroke:#adb5bd,stroke-dasharray:4
    style oN fill:#f8f9fa,stroke:#adb5bd,stroke-dasharray:4
    style dN fill:#f8f9fa,stroke:#adb5bd,stroke-dasharray:4
```
