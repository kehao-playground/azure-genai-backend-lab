# Two Assertion Layers, One Exit Code

The Day 28 evaluation design splits every case's assertions across two
separate JSON keys — `deterministic` and `judged` — and gives only the
first a path to the process exit code. This diagram makes the missing
edge visible: judged-layer outcomes flow into the report, and the report
has no edge into `gate_exit_code`, whose signature takes only the
deterministic results map. The split is structural, not disciplinary — a
typo cannot promote a judged assertion into a gating one, and there is no
parameter through which a judged verdict could reach the gate. Setup
failures exit `2` before any verdict exists, so a run that never
evaluated the gate cannot look green. See
[evaluation.md](../evaluation.md#3-two-layers-and-only-one-of-them-owns-the-exit-code).

This English diagram is the semantic companion to the article's published
figure; `eval-two-layers.zh-tw.mmd` is the zh-TW publication source. Keep
the two in the same topology.

```mermaid
flowchart TB
    case["One case in tools/eval_cases.json<br/>deterministic and judged are two keys,<br/>not two values of one field"]

    subgraph detside["Deterministic layer: repeats give the same answer"]
        det["deterministic<br/>status . must_cite . citations_subset_of . must_not_cite"]
        gate["gate_exit_code(results)<br/>no judged parameter in the signature"]
        det --> gate
    end

    subgraph judside["Judged layer: re-judging the same answer can flip"]
        jud["judged<br/>expected_facts . forbidden_facts . rubric"]
        judge["pass B generation, then pass C judge x5<br/>the model returns fact ids only;<br/>the verdict is derived in code"]
        report["Report: per-repeat outcome sequence<br/>NOT MEASURED when it cannot be measured"]
        jud --> judge --> report
    end

    case --> det
    case --> jud

    ok["exit 0<br/>the gate ran, everything passed"]
    bad["exit 1<br/>the gate ran, the thing under test has a problem"]
    gate --> ok
    gate --> bad

    setup["Setup failure<br/>invalid dataset . corpus will not load . missing credentials"]
    two["exit 2<br/>the thing under test was never tested"]
    setup --> two

    report -. "no such edge (structurally)" .-x gate
```
