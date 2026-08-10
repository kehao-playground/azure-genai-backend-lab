# Audit Event Exit Paths

Every request that finishes inside the process lands on exactly one exit of this map, per
the in-process classification in [audit-logging.md](../audit-logging.md#exactly-once--delivery):
an authenticated route request produces exactly one route event, a rejected authentication
produces exactly one `auth.rejected`, and two exits deliberately produce **zero** events — a
malformed-JSON body (no identity was ever verified, so an event would be a fabrication) and
an out-of-contract exception (a genuine bug must stay loud, not be reclassified as a
disconnect or a clean outcome).

This English diagram is the semantic companion to the article's published figure. The
publication PNG is rendered from the localized source
[`audit-exit-paths.zh-tw.mmd`](audit-exit-paths.zh-tw.mmd) in this same directory — both
sources are public and canonical here; the planning repo stores only the rendered PNG
snapshot. Changes to either file must keep the two topologies identical.

```mermaid
flowchart TB
    req["HTTP request"] --> parse{"Body parses as JSON?"}
    parse -- "no (422)" --> z1["zero events<br/>identity was never verified —<br/>an event would be a fabrication"]
    parse -- "yes" --> auth{"require_principal<br/>verifies identity"}
    auth -- "401 / 403" --> ar["auth.rejected<br/>exactly one"]
    auth -- "verified" --> val{"Field validation passes?"}
    val -- "no (422)" --> rj["rejected<br/>exactly one"]
    val -- "yes" --> handler["endpoint / service runs"]
    handler -- "2xx" --> s["success<br/>exactly one"]
    handler -- "4xx envelope<br/>(400 / 404 / 429)" --> rj
    handler -- "5xx envelope" --> er["error<br/>exactly one"]
    handler -- "client disconnect<br/>(streaming)" --> dis{"StreamDone<br/>already observed?"}
    dis -- "yes: commit already done" --> s
    dis -- "no" --> er
    handler -- "out-of-contract exception<br/>= genuine bug" --> z2["zero events<br/>original exception propagates"]
    style s fill:#d3f0d8,stroke:#2e7d32
    style rj fill:#fff3cd,stroke:#b26a00
    style ar fill:#fff3cd,stroke:#b26a00
    style er fill:#fde3e0,stroke:#c62828
    style z1 fill:#e2e3e5,stroke:#495057
    style z2 fill:#e2e3e5,stroke:#495057
```

Reading notes:

- The map classifies **outcomes**, not emission sites. The six actual emission points (the
  `/chat` finalizer, `/chat/stream`'s two-phase ownership, `RagService.answer()`'s guarded
  terminals, the `/agent` finalizer, `require_principal`, and the 422 handler) are listed in
  [audit-logging.md](../audit-logging.md#exactly-once--delivery); e.g. the "field validation"
  422 event is emitted by the validation handler, not by the route handler, whose body is
  never entered.
- The disconnect diamond is the commit-truth rule: the store commits **before** the terminal
  frame is yielded, so a disconnect after `StreamDone` was observed keeps the terminal
  outcome — the audit log records commit truth, not delivery truth.
- Gray exits are deliberate zero-event paths, not gaps: they are the two cases the system
  refuses to classify, for opposite reasons (no verified identity to attribute; a bug that
  must not be laundered into a clean outcome).
