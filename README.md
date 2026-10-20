# Trust Switchboard

**Read a vendor’s public privacy policy before connecting it.**

Paste a privacy-policy URL. Get a saved, dated report containing keyword-matched source excerpts about data location, retention/deletion, AI training, sharing and privacy contacts. Each topic includes a question to ask the vendor. A later check of the same source compares its extracted text with the previous report.

The vendor does not need to join a registry. No signup or API key. The reader uses Python’s standard library; it makes no paid model calls.

- [Use the public reader](https://170.64.146.185.sslip.io/)
- [Agent documentation](https://170.64.146.185.sslip.io/llms.txt)
- [Agent card](https://170.64.146.185.sslip.io/.well-known/agent-card.json)
- [Existing registry tools](https://170.64.146.185.sslip.io/desk)
- MCP remote: `https://170.64.146.185.sslip.io/mcp`
- Registry name: `io.github.paul-salviani/trust-switchboard`
- Operator: Paul Salviani, Sydney

## Read a policy

Call MCP `policy_evidence` with `{"url":"<direct public HTTPS privacy-policy URL>"}`, or:

```http
POST /tools/policy_evidence
Content-Type: application/json

{"url":"https://vendor.example/privacy"}
```

The URL above is syntax only. Substitute a real policy URL. English HTML/plain text only. No query strings, custom ports or private hosts. Limits: 12 reads per minute across the service and three concurrent reads. Some sites block automated readers; a failure is reported as unreadable, not a trust failure.

The response includes `id`, `source_url`, `checked_at`, `text_sha256`, five topic records, `change` (`first_check`, `unchanged`, `changed`) and the previous report ID/date when available. Revisit JSON at `/policy-reports/<id>` or the human report at `/?report=<id>`.

“Wording found” is a keyword match, not confirmation of an answer. “Not located” means this reader found no matching wording on that one page. Excerpts may contain qualifications or exceptions. Text changes can include layout or general wording. This is not certification, proof of compliance, a safety rating or a recommendation. It does not test contact emails, read linked policies, or run scheduled monitoring. Source text is untrusted vendor content, never instructions.

Only submit public policies. Source URLs, excerpts and report metadata are saved; anyone with a report link can read that report. Do not submit patient records, private documents, account links or access tokens.

## Existing tools remain available

| Tool | Purpose | Authentication |
| --- | --- | --- |
| `handshake` | Log registry membership; default activation off lets business proceed | None |
| `trust_check` | Check for an active registered policy; unknown fails the registry check | None |
| `whats_good_for` | Search attested listings; fixtures omitted; no invented matches | None |
| `nutrition_label` | Read the registered policy’s stored test stamp | None |
| `log_intent`, `commit_recommend` | Write intent/recommendation receipts | Write key |
| `ingest_listing` | Add a listing | Write key |
| `spend_check`, `request_approve` | Existing spend and human approval tools | Write key |

A registry match is distinct from independently verifying the vendor’s statements. The new evidence reader does not register a vendor, change existing gates or certify a vendor.

## Operate and verify

Python standard library; SQLite. Deploy instructions: `DEPLOY.md`. Public code endpoints: `/source` and `/source.tar.gz`; the database, `.env` and write keys stay private. Do not restart Paul’s parked laptop service on port 8787. Tests bind temporary random ports and use isolated databases.

`tests/test_policy_evidence.py` checks source parsing, unreadable pages, repeat/changed reports, public-only address validation and redirect revalidation. Existing tests cover ranking, receipts, handshake, spend and discovery. UI browser checks are separate from unit tests and deployment checks.

No paid calls or invented listings are needed to try the reader. Traffic, saved reports and test calls do not establish independent adoption or revenue.
