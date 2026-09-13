#!/usr/bin/env python3
"""Hit a running A2A. Does not start a server. Exit 0 only if card + health + fail-closed hold."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
fails = []


def get(path: str) -> tuple[int, bytes]:
    req = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(path: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(base + path, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return e.code, parsed


code, raw = get("/health")
if code != 200:
    fails.append(f"health {code}")
else:
    health = json.loads(raw.decode("utf-8"))
    if not health.get("ok"):
        fails.append(f"health not ok {health}")

code, raw = get("/.well-known/agent-card.json")
if code != 200:
    fails.append(f"agent-card {code}")
else:
    card = json.loads(raw.decode("utf-8"))
    names = {s.get("id") or s.get("name") for s in card.get("skills") or []}
    for need in ("trust_check", "whats_good_for", "log_intent"):
        if need not in names:
            fails.append(f"card missing skill {need}")

code, raw = get("/llms.txt")
text = raw.decode("utf-8", errors="replace")
if code != 200 or not text.startswith("# Trust Switchboard") or "before every recommend" not in text.lower():
    fails.append("llms.txt")

code, body = post("/tools/trust_check", {"url": "https://no-such-org-for-smoke.example"})
if code != 200 or body.get("pass") is True or body.get("result") == "pass":
    fails.append(f"fail-closed {code} {body}")

code, body = post("/listings", {"name": "x", "url": "https://x.example"})
if code != 401:
    fails.append(f"listings unkeyed {code}")

if fails:
    print("SMOKE FAIL")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("SMOKE PASS", base)
