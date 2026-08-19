# Container Apps Teardown Order

`delete-container-app.sh` runs seven steps whose order is a contract, not a
convenience, per [container-apps.md §9](../container-apps.md#9-teardown-ordering-is-a-contract-not-a-convenience):
Azure does not delete a managed identity's role assignments when the identity
is deleted, and the principal id — the handle that ties those assignments to
*this* identity — dies with it (what remains afterwards is a per-scope sweep
for `ObjectType: Unknown` assignments, no longer a lookup). So the principal
id is read back before anything identity-related is touched, and a
fail-closed `--all` read-back stands between deleting the assignments and
deleting the identity. Aborting in the middle is worse than failing outright:
it leaves the identity, its three assignments and the workspace all standing.

This English diagram is the semantic companion to the article's published
figure. The publication PNG is rendered from the localized source
[`aca-teardown-order.zh-tw.mmd`](aca-teardown-order.zh-tw.mmd) in this same
directory — both sources are public and canonical here; the planning repo
stores only the rendered PNG snapshot. Changes to either file must keep the
two topologies identical.

```mermaid
flowchart TB
    subgraph app["First, the resources whose deletion produces shutdowns"]
        S1["1. Delete the Container App<br/>read back: confirmed gone"]
        S2["2. Delete the environment<br/>read back = bounded poll, not one snapshot<br/>(ScheduledForDelete window: 26s, measured 2026-08-17)"]
        S3["3. Delete the Log Analytics workspace<br/>(a name only this run invented)"]
    end
    subgraph mi["Then the identity — the order is the contract"]
        S4["4. Read back the principal id<br/>(it dies with the identity)"]
        S5["5. Delete the role assignment at each scope<br/>ACR / Key Vault / Azure OpenAI"]
        S6{"6. Read back with --all:<br/>assignments held by that principal = 0?"}
        S7["7. Delete the identity<br/>read back: confirmed gone"]
    end
    S1 --> S2 --> S3 --> S4 --> S5 --> S6
    S6 -->|"yes"| S7
    S6 -->|"no: fail closed and stop —<br/>the identity still exists,<br/>so the assignments can still be queried and removed"| STOP["Fix, then rerun"]
    S3 -.->|"skipping 4–6 and deleting the identity directly"| ORPHAN["Orphaned assignments:<br/>Identity not found — no saved principal id,<br/>cleanup falls back to sweeping each scope<br/>for Unknown-type assignments"]
    style S6 fill:#fff3cd,stroke:#b26a00
    style STOP fill:#d3f0d8,stroke:#2e7d32
    style ORPHAN fill:#fde3e0,stroke:#c62828
```

Reading notes:

- Step 6 queries **by assignee alone**, with `--all` — the CLI's default is
  subscription scope only, and all three assignments here are at resource
  scope, so without `--all` the read returns `0` no matter what remains.
- The dashed edge is the counterfactual the contract exists to prevent: it is
  not a path the script can take.
