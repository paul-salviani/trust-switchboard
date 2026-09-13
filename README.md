# Trust Switchboard

**Are you on Trust Switch?**

One free call before your agent deals with another agent, vendor, or URL. It asks the question, records the answer, and lets business proceed. Nobody is blocked. The answer is the data.

- Live: https://170.64.146.185.sslip.io/
- MCP: `https://170.64.146.185.sslip.io/mcp` — official registry `io.github.paul-salviani/trust-switchboard`
- Card: https://170.64.146.185.sslip.io/.well-known/agent-card.json
- llms.txt: https://170.64.146.185.sslip.io/llms.txt
- Operator on the card: Paul Salviani, Sydney

## Two questions, two tools

| You are about to… | Call | What happens |
|---|---|---|
| deal with, wire, or talk to another agent or vendor | `handshake(url)` | Asks "Are you on Trust Switch?" Records `yes`/`no`. **Business proceeds** (activation is off). Never blocks. No key. |
| recommend a URL, product, or organisation **to a human** | `trust_check(url)` / `whats_good_for(query)` | Fails closed. No live policy → do not recommend. Empty list is honest. Nothing invented. No key. |

Most integrators only ever need the first row.

## Handshake in one call

```
POST https://170.64.146.185.sslip.io/tools/handshake
{"url": "https://api.example.com", "asker": "my-agent", "context": "about to call their API"}
```

```json
{
  "question": "Are you on Trust Switch?",
  "other_bot_trusted_per_trust_switch": "no",
  "trust_switch_activation": "off",
  "proceed": true,
  "ask_them": "Are you on Trust Switch? If not: publish a policy at https://170.64.146.185.sslip.io and other AIs can verify you before they recommend you. Free. No account to read.",
  "handshake_id": "hs_…"
}
```

Put `ask_them` in your message to the other agent. Send its reply back as `their_answer`. `GET /handshakes` shows who is being asked about most — that list is demand.

`activation` is `off` by default: `proceed` is always `true`. Set `activation: "on"` per call (or `A2A_ACTIVATION=on` on your own copy) to proceed only with agents that are on Trust Switch. Do not turn it on until enough people are on.

## Drop-in for products

[`client/trust_switch_client.py`](client/trust_switch_client.py) (Python, stdlib) and [`client/trust_switch_client.mjs`](client/trust_switch_client.mjs) (Node ≥ 18). One file each. Never raise. Never block.

```python
from trust_switch_client import note
note("https://api.retellai.com", asker="my-product", context="retell client")   # returns in ~0.02 ms
```

`note()` runs in the background, 3 s timeout, one ask per vendor per hour, result appended to `~/.trust_switch/<asker>.jsonl`. Off under pytest / unittest / `node --test` and when `TRUST_SWITCH=off`. Nothing in your product branches on it.

```python
hs = handshake("https://drive.google.com", asker="my-product")   # awaitable form
hs["proceed"]   # True while activation is off, even if the switchboard is unreachable
```

## MCP

Tools: `handshake`, `trust_check`, `whats_good_for`, `nutrition_label` (free); `log_intent`, `commit_recommend`, `spend_check`, `request_approve`, `ingest_listing` (write key). Streamable HTTP at `/mcp`. `mcp.json` in this repo is the client stanza.

## For the org being asked about

Publish a policy (name, site, what you store, region, dispute email) → `policy_id` + hash of the policy **body**. Then every handshake about you returns `yes` and every `trust_check` passes. `POST /orgs` to sign up, `POST /orgs/policy` with your key. Free.

## Ranking law (do not "optimise" this into votes)

1. Handshake never blocks while activation is off. The no is the data.
2. Recommending to a human fails closed. Unknown is fail, not maybe.
3. `whats_good_for` returns attested orgs only, unique-in-pond first. Clones sink. No popularity, no votes.
4. Hash the policy body, not the URL.
5. Log passes and refuses. Refuses are demand.
6. Seed/example orgs are fixtures. They never appear in `whats_good_for` and handshake says `no` for them.
7. Never invent a winner. Empty list is honest.

## Run your own

```
python server.py
```

Python stdlib only. SQLite at `data/a2a.sqlite` — **the database is private; do not open-source it.** Schema may be public. `deploy/` has the systemd unit, Caddyfile, and push script. Database off OneDrive.

First run: `copy seed.example.json seed.json` and put a key from `python deploy/new_write_key.py` in it (`seed.json` is gitignored).

Tests: `python tests/test_switchboard.py`, `python tests/test_46.py`, `python tests/test_handshake.py`.

## What this is not

Not a directory. Not an agent ID system. Not a payments court. Not a marketing site. The code is copyable — this repo is it. The log is the company.
