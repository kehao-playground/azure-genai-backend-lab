# Three Timings of One Streamed Call

One `/chat/stream` request produces three numbers that are routinely confused
for each other. This diagram exists to make their containment relationship
visible: the httpx transport span ends when response headers return at 1.067 s,
while generation continues until 2.568 s — so reading the transport span as
"how long the model took" under-reports this request by more than half. The
1.343 s between headers and the first content chunk is, strictly, a gap during
which no content byte had arrived: that response reported 64 reasoning tokens,
so reasoning happened in there, but the measurement does not decompose the
gap's ownership between model reasoning, service-side scheduling and network
delivery.

The measurements are one live call (gpt-5-mini, japaneast, 2026-08-21). A
second deployed measurement showed the same direction at a more extreme ratio
(transport span covering 11% of generation); the two are not a controlled
comparison, so only the direction is claimed. See
[observability.md](../observability.md#streaming).

Bar colors carry no status semantics — the two solid bars are the two spans,
the grey segment is the headers-to-first-chunk gap. Nothing in this diagram
failed.

This English diagram is the semantic companion to the article's published
figure; `streaming-latency.zh-tw.mmd` is the zh-TW publication source. Keep the
two in the same topology.

```mermaid
gantt
    title Three timings of one /chat/stream call (gpt-5-mini, japaneast, 2026-08-21)
    dateFormat x
    axisFormat %S.%L s
    tickInterval 500millisecond
    section semantic span
        chat chat-mini (whole generation, 2.568 s) : active, 0, 2568
        first content chunk (TTFB 2.410 s)         : milestone, 2410, 0
    section httpx span
        request sent -> response headers (1.067 s)          : 0, 1067
        headers back, first chunk pending (1.343 s gap)     : done, 1067, 2410
```
