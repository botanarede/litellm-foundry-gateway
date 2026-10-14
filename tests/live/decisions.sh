#!/usr/bin/env bash
# Smoke-test decision models (Profile D) on both LiteLLM decision routes against a gateway port.
# Prints each question, the options offered and the option the model chose, for human review.
set -u

PORT=${1:-4000}
MODEL=${2:-Decision-1}
GATEWAY_ENV="${GATEWAY_ENV:-$HOME/.config/litellm-foundry-gateway/gateway.env}"
KEY="$(grep -m 1 '^LITELLM_MASTER_KEY=' "$GATEWAY_ENV" | cut -d= -f2-)"
if [ -z "$KEY" ]; then
  printf 'Error: LITELLM_MASTER_KEY is unavailable in the gateway environment file.\n' >&2
  exit 2
fi

# Each case: label | route | JSON body. Bodies use the documented System One and OpenAI decision shapes.
# The key travels in the environment, not argv, so other local users cannot read it from the process list.
GATEWAY_KEY="$KEY" python3 - "$PORT" "$MODEL" <<'EOF'
import json, os, sys, time, urllib.request, urllib.error

port, model, key = sys.argv[1], sys.argv[2], os.environ["GATEWAY_KEY"]

CASES = [
    ("ticket routing (choice)", "/v1/systemone", "engineering", {
        "state": "The API returns 500 on every call since this morning.",
        "questions": {"team": {"type": "choice",
            "instructions": "Which team should handle this ticket?",
            "criteria": {"billing": "Charges, invoices and refunds",
                         "engineering": "Bugs, errors and outages",
                         "support": "How-to and account questions"}}}}),
    ("refund promise (noul)", "/v1/systemone", True, {
        "state": "I refunded your $200. You don't need to contact billing.",
        "questions": {"promises_refund": {"type": "noul",
            "instructions": "Does this reply promise the customer a refund?",
            "criteria": {"true": "Commits to returning money",
                         "false": "Makes no commitment about money"}}}}),
    ("incident severity (score)", "/v1/systemone", 3, {
        "state": "Checkout is down for all EU customers since 09:00.",
        "questions": {"severity": {"type": "score",
            "instructions": "How severe is this incident?",
            "criteria": ["Cosmetic", "Minor", "Major", "Critical"]}}}),
    ("model routing (choice, OpenAI shape)", "/v1/decisions", "fast_model", {
        "input": "Rename the variable 'cnt' to 'count' in one Python file.",
        "questions": [{"type": "choice", "name": "route",
            "instructions": "Which model should handle this coding request?",
            "choices": [{"value": "fast_model", "description": "Small, cheap model for trivial edits"},
                        {"value": "reasoning_model", "description": "Expensive model for complex design work"}]}]}),
]

failures = 0
for label, route, expected, body in CASES:
    body = dict(body, model=model)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{route}", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        print(f"FAIL  {label}: HTTP {exc.code}")
        failures += 1
        continue
    ms = (time.perf_counter() - start) * 1000
    answers = payload.get("answers")
    # System One returns a dict keyed by question name; the OpenAI shape returns a list.
    answer = next(iter(answers.values())) if isinstance(answers, dict) else answers[0]
    kind = answer.get("type")
    if kind in ("noul", "predicate"):
        # System One reports P(true) under "noul"; the OpenAI shape uses "probability".
        p = answer.get("noul", answer.get("probability"))
        chosen = p is not None and p >= 0.5
    elif kind == "score":
        s = answer.get("score")
        chosen = round(s) if isinstance(s, (int, float)) else s
    else:
        chosen = answer.get("choice")
    ok = chosen == expected
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {label} [{route}] {ms:.0f} ms")
    print(f"      expected={expected!r} chosen={chosen!r} raw={json.dumps(answer, sort_keys=True)[:300]}")

print(f"RESULT: {len(CASES) - failures}/{len(CASES)} PASS")
sys.exit(1 if failures else 0)
EOF
