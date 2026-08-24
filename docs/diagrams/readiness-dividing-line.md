# The Checklist's Dividing Line

The Day 29 readiness checklist separates machine-checkable lines from
question lines without a `[verifiable]` tag: a line is machine-checkable
if and only if a runnable command follows it. This diagram shows where
that line actually runs — not between topics, but between requirements
whose subject is a **property of the code** and requirements whose
subject is a **decision made by a person**. Code-side lines carry a
command that was run against this tree (or are honestly marked as
verifiable only against a deployed app); person-side lines become
questions that must ask for a date and a name, because a question that
cannot be answered with either is an empty phrase wearing a
requirement's clothes. See
[production-readiness-checklist.md](../production-readiness-checklist.md#how-the-two-layers-are-separated).

This English diagram is the semantic companion to the article's published
figure; `readiness-dividing-line.zh-tw.mmd` is the zh-TW publication
source. Keep the two in the same topology.

```mermaid
flowchart TB
    req["One requirement on the checklist"]
    q{"What is its subject?"}
    req --> q

    subgraph codeside["A property of the code"]
        cmd["✅ a runnable command follows —<br/>clear pass/fail, non-zero exit on violation,<br/>actually run this round against this tree"]
        live["⚑ verifiable, but only against<br/>a deployed app"]
    end

    subgraph personside["A decision made by a person"]
        question["✎ written as a question<br/>the reader answers themselves"]
        dated{"Does it ask for<br/>a date and a name?"}
        good["A qualified question:<br/>&quot;who approved it, and on what date?&quot;"]
        empty["An empty phrase —<br/>&quot;ensure retention is configured&quot; —<br/>rewrite it"]
        question --> dated
        dated -->|yes| good
        dated -->|no| empty
    end

    q -->|"the code"| cmd
    q -->|"the code, but it needs<br/>a running deployment"| live
    q -->|"a person"| question

    oos["The requirement asks for an organization<br/>or a real user population"]
    na["Out of scope for a lab —<br/>the only such line is on-call"]
    q -->|"(rare)"| oos --> na

    cmd -.- warn["No third state between these:<br/>a command not run this round<br/>must not pose as machine-checkable"]

    style cmd fill:#d3f0d8,stroke:#2e7d32
    style good fill:#d3e5f0,stroke:#1565c0
    style empty fill:#fde2e0,stroke:#c62828
    style warn fill:#f8f9fa,stroke:#adb5bd,stroke-dasharray:4
```
