"""Day 18 live smoke: /chat then /agent share one conversation.

Two modes against two server configs (a single config cannot honestly prove
both: a budget low enough to trip 429 on the second turn would also break
the history flow):

    # mode=full — default server env (pick --backend to match the server)
    uv run python tools/agent_endpoint_smoke.py --backend real --base-url http://127.0.0.1:8000

    # mode=budget — server started with CONVERSATION_TOKEN_BUDGET=1
    uv run python tools/agent_endpoint_smoke.py --backend real --mode budget

mode=full asserts (exit non-zero on any failure; --backend is mandatory):
  1. a /chat turn opens a conversation and plants a marker;
  2. an /agent turn in the same conversation reproduces the marker WITHOUT
     the task restating it (--backend real), or carries the fake's
     history=2 wiring marker (--backend fake) — content proof, not just 200;
  3. a follow-up /chat turn retrieves the marker back out of the agent's
     committed answer (real) / shows the grown history count (fake);
  4. a different group set gets 404 (same shape as unknown);
  5. an unknown conversation id gets 404.
The marker lives in memory only — never in the printed capture.

mode=budget asserts:
  6. after one committed turn, the next /agent turn is HTTP 429 with
     error.code == "token_budget_exceeded" in the JSON envelope and NO
     Retry-After header (the budget does not replenish).

Evidence discipline: print only status codes, ids, token counts and
durations — never document content (Day 17 capture rules; note date,
region, api-version and tier in the capture).
"""

import argparse
import sys
import time

import httpx

HEADERS = {"X-Tenant-Id": "smoke", "X-User-Id": "smoke-user"}


def _budget_mode(client: httpx.Client, checks: list[tuple[str, bool, str]]) -> None:
    seed = client.post("/api/v1/chat", json={"message": "Seed one committed turn."})
    checks.append(("budget seed turn 200", seed.status_code == 200, f"got {seed.status_code}"))
    cid = seed.json().get("conversation_id", "")
    blocked = client.post(
        "/api/v1/agent", json={"task": "one more", "conversation_id": cid}
    )
    checks.append(("budget gate 429", blocked.status_code == 429, f"got {blocked.status_code}"))
    content_type = blocked.headers.get("content-type", "")
    body = blocked.json() if content_type.startswith("application/json") else {}
    checks.append(
        ("budget error code", body.get("error", {}).get("code") == "token_budget_exceeded",
         f"got {body.get('error')}")
    )
    checks.append(
        ("no Retry-After", "retry-after" not in blocked.headers, "Retry-After present")
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mode", choices=("full", "budget"), default="full")
    # Explicit, never auto-detected: misclassifying a real environment as
    # fake would silently downgrade the history proof to structure checks.
    parser.add_argument("--backend", choices=("fake", "real"), required=True)
    args = parser.parse_args()
    checks: list[tuple[str, bool, str]] = []
    client = httpx.Client(base_url=args.base_url, headers=HEADERS, timeout=120)

    if args.mode == "budget":
        _budget_mode(client, checks)
        failed = [c for c in checks if not c[1]]
        for name, ok, detail in checks:
            print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
        return 1 if failed else 0

    started = time.perf_counter()
    # History-awareness is proven with a marker the model can only know from
    # the prior turn: the agent task deliberately never restates it. The
    # marker is compared in memory only and MUST NOT be printed to the
    # capture. Fake backend: adapters echo structural markers instead.
    marker = f"cobalt-{int(started * 1000) % 100000}"
    chat = client.post(
        "/api/v1/chat",
        json={"message": f"Remember this exact code and repeat it when asked: {marker}"},
    )
    checks.append(("chat turn 200", chat.status_code == 200, f"got {chat.status_code}"))
    cid = chat.json().get("conversation_id", "")

    agent = client.post(
        "/api/v1/agent",
        json={
            "task": "What exact code were you asked to remember? Reply with the code only.",
            "conversation_id": cid,
        },
    )
    body = agent.json() if agent.status_code == 200 else {}
    checks.append(("agent turn 200", agent.status_code == 200, f"got {agent.status_code}"))
    checks.append(
        ("agent turn committed usage", bool(body.get("usage")), "usage missing")
    )
    if args.backend == "real":
        checks.append(
            ("agent answer carries prior-turn marker", marker in body.get("answer", ""),
             "marker absent from agent answer")
        )
    else:
        checks.append(
            ("fake agent saw history", "history=2" in body.get("answer", ""),
             f"marker absent (status={body.get('status')})")
        )

    follow = client.post(
        "/api/v1/chat",
        json={"message": "Repeat the code one more time.", "conversation_id": cid},
    )
    checks.append(("follow-up chat 200", follow.status_code == 200, f"got {follow.status_code}"))
    if args.backend == "real":
        checks.append(
            ("follow-up chat sees agent turn", marker in follow.json().get("message", ""),
             "marker absent from follow-up reply")
        )
    else:
        checks.append(
            (
                "fake follow-up sees agent turn in history",
                "history=4" in follow.json().get("message", ""),
                "history count marker absent",
            )
        )

    mismatch = client.post(
        "/api/v1/agent",
        json={"task": "peek", "conversation_id": cid},
        headers={"X-Tenant-Id": "smoke", "X-User-Id": "smoke-user", "X-Group-Ids": "other"},
    )
    checks.append(
        ("scope mismatch 404", mismatch.status_code == 404, f"got {mismatch.status_code}")
    )

    unknown = client.post(
        "/api/v1/agent", json={"task": "peek", "conversation_id": "never-issued"}
    )
    checks.append(("unknown id 404", unknown.status_code == 404, f"got {unknown.status_code}"))

    duration = time.perf_counter() - started
    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    print(f"conversation_id={cid} total_duration_s={duration:.1f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
