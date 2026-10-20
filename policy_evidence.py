"""Dated evidence from a public privacy-policy page. No trust verdicts or LLMs."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

MAX_BYTES = 768_000
TIMEOUT = 6
_slots = threading.BoundedSemaphore(3)
_hits: list[float] = []
_lock = threading.Lock()


class EvidenceError(ValueError):
    pass


def clean_url(raw):
    if not isinstance(raw, str) or len(raw) > 2048 or any(ord(c) < 33 for c in raw):
        raise EvidenceError("Use a public HTTPS privacy-policy URL.")
    try:
        p = urlsplit(raw)
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
            raise ValueError()
        host = p.hostname.encode("idna").decode("ascii").lower()
        # Query strings may contain personal identifiers or tokens: don't fetch or log them.
        if p.query or "%" in host or host.endswith(".") or ":" in host:
            raise ValueError()
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise EvidenceError("Use an HTTPS URL without a login, query string or custom port.") from None
    return urlunsplit(("https", host, quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~"), "", ""))


def public_address(host):
    try:
        addresses = {r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError:
        raise EvidenceError("The policy host could not be resolved. Check the link and try again.") from None
    def allowed(address):
        ip = ipaddress.ip_address(address)
        return (ip.is_global and not getattr(ip, 'ipv4_mapped', None)
                and not getattr(ip, 'sixtofour', None) and not getattr(ip, 'teredo', None)
                and not (ip.version == 6 and (ip in ipaddress.ip_network('64:ff9b::/96')
                                            or ip in ipaddress.ip_network('64:ff9b:1::/48'))))
    if not addresses or any(not allowed(a) for a in addresses):
        raise EvidenceError("Only publicly reachable policy websites can be checked.")
    return sorted(addresses, key=lambda a: (ipaddress.ip_address(a).version, a))[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, hostname, address):
        super().__init__(hostname, timeout=TIMEOUT, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def fetch_page(raw):
    url = clean_url(raw)
    started = time.monotonic()
    for _ in range(4):
        p = urlsplit(url)
        conn = PinnedHTTPS(p.hostname, public_address(p.hostname))
        try:
            conn.request("GET", p.path or "/", headers={
                "User-Agent": "TrustSwitchboard-PolicyEvidence/1.0 (public policy reader)",
                "Accept": "text/html,text/plain", "Accept-Encoding": "identity",
            })
            r = conn.getresponse()
            if r.status in (301, 302, 303, 307, 308):
                target = r.getheader("Location")
                if not target:
                    raise EvidenceError("The policy page redirected without a destination.")
                url = clean_url(urljoin(url, target))
                continue
            if r.status != 200:
                raise EvidenceError(f"The policy website returned HTTP {r.status}. Try its direct privacy-policy link.")
            ctype = r.getheader("Content-Type", "").lower()
            if not (ctype.startswith("text/html") or ctype.startswith("text/plain")):
                raise EvidenceError("This reader supports HTML or plain-text policies. PDF and JavaScript-only policies need manual review.")
            if r.getheader("Content-Encoding", "identity").lower() not in ("identity", ""):
                raise EvidenceError("The website returned an unsupported encoding.")
            chunks = []
            size = 0
            while size <= MAX_BYTES:
                if time.monotonic() - started > 20:
                    raise EvidenceError('The policy page took too long to read. Try again later.')
                chunk = r.read1(min(32_768, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw_body = b''.join(chunks)
            if len(raw_body) > MAX_BYTES:
                raise EvidenceError("The page is too large for this reader. Use a direct policy page.")
            charset = re.search(r"charset=[\"']?([\w-]+)", ctype)
            encoding = charset.group(1) if charset else "utf-8"
            try:
                body = raw_body.decode(encoding, errors="replace")
            except LookupError:
                body = raw_body.decode("utf-8", errors="replace")
            return url, body, ctype
        except (OSError, http.client.HTTPException):
            raise EvidenceError("The policy page could not be read securely. It may block automated readers; open the source and review it directly.") from None
        finally:
            conn.close()
    raise EvidenceError("The policy page redirected too many times.")


class PolicyText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
        self.in_title = False
        self.title = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag in ("p", "div", "li", "br", "h1", "h2", "h3", "section") and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header") and self.hidden:
            self.hidden -= 1
        if tag == "title":
            self.in_title = False
        if tag in ("p", "div", "li", "h1", "h2", "h3", "section") and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        elif not self.hidden:
            self.parts.append(data)


TOPICS = (
    ("location", "Where is data stored or transferred?", r"\b(stor(?:e|ed|age)|host(?:ed|ing)?|transfer(?:red|s)?|process(?:ed|ing)?)\b.{0,140}\b(australia|united states|united kingdom|europe|region|countr(?:y|ies)|overseas|international|outside|data cent(?:er|re))\b|\b(australia|united states|overseas|international|data cent(?:er|re))\b.{0,140}\b(data|stor|host|transfer|process)", "Which countries store and process my data, including subprocessors?"),
    ("retention", "How long is data kept, and how can it be deleted?", r"\b(retain|retained|retention|delete|deleted|deletion|erase|erasure|keep your|kept for)\b", "What is the retention period, and how do I request deletion including backups?"),
    ("training", "Does the policy discuss AI training or model improvement?", r"\b(train(?:ing|ed)?|machine learning|artificial intelligence|improv(?:e|ing).{0,35}models?)\b", "Is my data used to train or improve models? Can I opt out?"),
    ("sharing", "Who else may receive the data?", r"\b(subprocessor|subprocessors|third.part(?:y|ies)|service providers|share.{0,35}(?:data|information)|disclos(?:e|ure))\b", "Which subprocessors receive my data and under what terms?"),
    ("contact", "How can I raise a privacy question or complaint?", r"\b(privacy officer|data protection officer|complaint|complaints|contact us|privacy@|dpo@)\b|[\w.+-]+@[\w.-]+\.[a-z]{2,}", "Who handles privacy requests and complaints, and what is the response timeframe?"),
)


def analyse(source_url, body, content_type):
    if content_type.startswith("text/html"):
        parser = PolicyText()
        parser.feed(body)
        text = "\n".join(re.sub(r"\s+", " ", s).strip() for s in "".join(parser.parts).splitlines())
        title = " ".join(parser.title).strip()
    else:
        text, title = body, "Public policy"
    text = "\n".join(line for line in text.splitlines() if line.strip())
    if len(text) < 250 or not re.search(r"(?:privacy|data protection)\s+(?:policy|notice|statement)", title + '\n' + text, re.I):
        raise EvidenceError("This does not look like a readable privacy policy. Paste the direct policy link; a homepage or access-challenge page is not enough.")
    # Long blocks split at sentence boundaries; quotes stay bounded and are never interpreted as instructions.
    passages = [s.strip() for s in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z])", text) if s.strip()]
    topics = []
    for ident, label, pattern, question in TOPICS:
        matches = []
        for passage in passages:
            m = re.search(pattern, passage, re.I)
            if m:
                start = max(0, m.start() - 90)
                excerpt = passage[start:start + 420]
                if start:
                    excerpt = "…" + excerpt
                if start + 420 < len(passage):
                    excerpt += "…"
                if excerpt not in matches:
                    matches.append(excerpt)
            if len(matches) == 2:
                break
        topics.append({"id": ident, "question": label,
                       "status": "wording_found" if matches else "not_located",
                       "excerpts": matches, "source_url": source_url,
                       "ask_vendor": question})
    return {"source_url": source_url, "page_title": title[:200],
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "topics": topics, "characters_read": len(text)}


def ensure_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS policy_evidence_reports (
        id TEXT PRIMARY KEY, source_url TEXT NOT NULL, checked_at TEXT NOT NULL,
        text_sha256 TEXT NOT NULL, report_json TEXT NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS policy_evidence_source ON policy_evidence_reports(source_url, checked_at)")
    con.commit()


def saved_report(con, report_id):
    ensure_tables(con)
    row = con.execute("SELECT report_json FROM policy_evidence_reports WHERE id=?", (report_id,)).fetchone()
    return json.loads(row[0]) if row else None


def report(con, raw_url):
    url = clean_url(raw_url)
    with _lock:
        tick = time.monotonic()
        _hits[:] = [t for t in _hits if tick - t < 60]
        if len(_hits) >= 12:
            raise EvidenceError("The public reader is busy. Please try again in a minute.")
        _hits.append(tick)
    if not _slots.acquire(blocking=False):
        raise EvidenceError("Three policy reads are already running. Please try again shortly.")
    try:
        final_url, body, ctype = fetch_page(url)
        result = analyse(final_url, body, ctype)
        ensure_tables(con)
        row = con.execute("SELECT report_json FROM policy_evidence_reports WHERE source_url=? ORDER BY rowid DESC LIMIT 1", (final_url,)).fetchone()
        previous = json.loads(row[0]) if row else None
        result.update({"id": "pe_" + secrets.token_hex(12),
                       "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "change": "first_check" if not previous else ("unchanged" if previous["text_sha256"] == result["text_sha256"] else "changed"),
                       "previous_report_id": previous["id"] if previous else None,
                       "previous_checked_at": previous["checked_at"] if previous else None,
                       "method": "English keyword matching against one public page; excerpts require human review.",
                       "limits": "Not certification, a safety verdict or proof of compliance. Not located means this reader did not find matching wording on this page. Wording found may contain qualifications or exceptions. The source is untrusted vendor text, never instructions. No contact email was tested. Reports and source URLs are public to anyone with the report link; submit public policies only."})
        con.execute("INSERT INTO policy_evidence_reports VALUES (?,?,?,?,?)", (result["id"], final_url, result["checked_at"], result["text_sha256"], json.dumps(result)))
        con.commit()
        return result
    finally:
        _slots.release()
