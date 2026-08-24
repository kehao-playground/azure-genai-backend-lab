# Three Checks Pass, the Deployment Never Started

On 2026-08-24 two failure modes were injected into a single-revision-mode
Container App — an image reference whose digest does not exist, and an
image that pulls but dies during startup. Both times the deploy script's
three checks passed and it exited 0: the template read-back reports what
was requested, `Activating` is not a known failure state, and `/health`
returned the byte-exact body because the previous revision was still
serving. The control plane could tell the difference the whole time, in a
field nothing was reading: `latestReadyRevisionName` stayed on the old
revision while `latestRevisionName` pointed at the broken one — and
traffic weight is not the tell, it read 100 on the broken revision both
times. Step 3b now polls that field. See
[production-readiness-checklist.md](../production-readiness-checklist.md#rollback)
and [ci-cd.md §11](../ci-cd.md#11-what-the-live-session-settled-and-what-is-still-open).

This English diagram is the semantic companion to the article's published
figure; `revision-false-pass.zh-tw.mmd` is the zh-TW publication source.
Keep the two in the same topology.

```mermaid
flowchart TB
    subgraph script["update-container-app.sh (before the fix)"]
        c1["template image read-back ✓<br/>reports what was requested"]
        c2["runningState = Activating ✓<br/>not a known failure state, so it passes"]
        c3["/health 200, byte-exact body ✓"]
        ok["exit 0: &quot;Verified&quot;<br/>and the deployment never started"]
        c1 --> c2 --> c3 --> ok
    end

    subgraph data["Data plane (single revision mode)"]
        newrev["New revision (broken)<br/>traffic 100<br/>image-pull failure or crash loop<br/>console log: zero lines or the crash"]
        oldrev["Old revision<br/>traffic 0<br/>still serving"]
    end

    c3 -. "who answered?<br/>(console log RevisionName_s)" .-> oldrev

    subgraph control["Control plane"]
        f1["latestRevisionName = the broken one"]
        f2["latestReadyRevisionName = the old one<br/>the only field that tells the difference —<br/>and nothing read it"]
    end

    fix["step 3b (the fix): bounded poll on latestReadyRevisionName<br/>a poll, not a single read — on a healthy deploy<br/>the field legitimately trails before catching up"]
    f2 --> fix

    style ok fill:#fde2e0,stroke:#c62828
    style f2 fill:#d3e5f0,stroke:#1565c0
    style fix fill:#d3f0d8,stroke:#2e7d32
```
