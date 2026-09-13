"""Trust Switch drop-in for products (Salviani OS, LEA, Anastasia, EA, bots).

One file. No dependencies. Never raises. Never blocks business.

    from trust_switch_client import handshake

    hs = handshake("https://drive.google.com", asker="salviani-os", context="tenant connects Drive")
    if hs["proceed"]:
        ...connect Drive...
    log(hs["other_bot_trusted_per_trust_switch"], hs["trust_switch_activation"])

Talking to another bot? Put hs["ask_them"] in your message to it, then send
its reply back:

    handshake(url, asker="salviani-os", their_answer=reply_text)

For products that must never be slowed or stopped, use note():

    from trust_switch import note
    note("https://api.retellai.com", asker="salviani-os", context="retell client")

note() returns immediately. Background thread, 3 s timeout, one ask per
vendor per hour per process, result appended to a local JSONL log
(TRUST_SWITCH_LOG, default ~/.trust_switch/<asker>.jsonl). Off under
unittest/pytest and when TRUST_SWITCH=off. Nothing branches on it.

Rules baked in:
- activation off (default): proceed is always True. The "no" is the data.
- activation on: proceed only when they are on Trust Switch.
- switchboard unreachable: proceed True when activation is off (observe mode
  must never take a product down), False when on. reason = "unreachable".
- 3 second timeout. Set TRUST_SWITCH_URL to point elsewhere; TRUST_SWITCH_ACTIVATION=on to enforce.

trust_check (recommend-to-a-human gate) is a different question and still
fails closed. Do not use this file for that.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_URL = "https://170.64.146.185.sslip.io"
QUESTION = "Are you on Trust Switch?"


def _base() -> str:
    return (os.environ.get("TRUST_SWITCH_URL") or DEFAULT_URL).rstrip("/")


def _activation(explicit: str | bool | None) -> str:
    raw = explicit if explicit is not None else os.environ.get("TRUST_SWITCH_ACTIVATION")
    if isinstance(raw, bool):
        return "on" if raw else "off"
    v = (str(raw or "").strip().lower())
    return "on" if v in ("on", "true", "1", "enforce", "yes") else "off"


def ask_line(base: str | None = None) -> str:
    root = (base or _base())
    return (
        f"{QUESTION} If not: publish a policy at {root} and other AIs can "
        f"verify you before they recommend you. Free. No account to read."
    )


def _offline(url: str, asker: str | None, context: str | None, activation: str, err: str) -> dict:
    proceed = activation == "off"
    return {
        "handshake_id": None,
        "question": QUESTION,
        "url": url,
        "asker": asker,
        "context": context,
        "their_answer": None,
        "on_trust_switch": False,
        "other_bot_trusted_per_trust_switch": "no",
        "trust_switch_activation": activation,
        "proceed": proceed,
        "reason": "unreachable",
        "policy_hash": None,
        "org": None,
        "speak": (
            "Trust Switch unreachable. Proceeding - activation is off."
            if proceed
            else "Trust Switch unreachable and activation is on. Not proceeding."
        ),
        "ask_them": ask_line(),
        "error": err,
    }


def handshake(
    url: str,
    asker: str | None = None,
    context: str | None = None,
    their_answer: str | bool | None = None,
    activation: str | bool | None = None,
    timeout: float = 3.0,
) -> dict:
    """Ask the switchboard "Are you on Trust Switch?" about url. Never raises."""
    act = _activation(activation)
    body = {"url": url, "activation": act}
    if asker:
        body["asker"] = asker
    if context:
        body["context"] = context
    if their_answer is not None:
        body["their_answer"] = ("yes" if their_answer else "no") if isinstance(their_answer, bool) else str(their_answer)
    req = urllib.request.Request(
        _base() + "/tools/handshake",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"trust-switch-client/{asker or 'unknown'}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
        return _offline(url, asker, context, act, f"{type(e).__name__}: {e}")
    if not isinstance(out, dict) or "proceed" not in out:
        return _offline(url, asker, context, act, "bad response")
    return out


# ---- fire-and-forget for products -------------------------------------------

_SEEN: dict[tuple[str, str], float] = {}
_SEEN_LOCK = threading.Lock()
_DEDUPE_SECONDS = 3600.0
_QUEUE: "queue.Queue[tuple[str, str | None, str | None]]" = queue.Queue(maxsize=256)
_WORKER: threading.Thread | None = None


def enabled() -> bool:
    """Off when Paul says off, and off under test runners so tests never touch the network."""
    if (os.environ.get("TRUST_SWITCH") or "").strip().lower() in ("off", "0", "false", "no"):
        return False
    if "pytest" in sys.modules or "unittest" in sys.modules:
        return False
    return True


def _domain(url: str) -> str:
    try:
        host = urlparse(url if "://" in url else "https://" + url).hostname or ""
    except ValueError:
        host = ""
    return host.lower()


def _log_path(asker: str | None) -> Path:
    custom = os.environ.get("TRUST_SWITCH_LOG")
    if custom:
        return Path(custom)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (asker or "product"))
    return Path.home() / ".trust_switch" / f"{safe}.jsonl"


def _append_log(asker: str | None, row: dict) -> None:
    try:
        p = _log_path(asker)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _worker() -> None:
    while True:
        url, asker, context = _QUEUE.get()
        try:
            out = handshake(url, asker=asker, context=context)
            _append_log(asker, {
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "url": url,
                "asker": asker,
                "context": context,
                "other_bot_trusted_per_trust_switch": out.get("other_bot_trusted_per_trust_switch"),
                "trust_switch_activation": out.get("trust_switch_activation"),
                "proceed": out.get("proceed"),
                "reason": out.get("reason"),
                "handshake_id": out.get("handshake_id"),
            })
        except Exception:
            pass
        finally:
            _QUEUE.task_done()


def _ensure_worker() -> None:
    global _WORKER
    if _WORKER is None or not _WORKER.is_alive():
        _WORKER = threading.Thread(target=_worker, name="trust-switch-note", daemon=True)
        _WORKER.start()


def note(url: str, asker: str | None = None, context: str | None = None) -> None:
    """Ask in the background. Returns at once. Never raises. Never blocks. Nothing depends on it."""
    try:
        if not enabled() or not url:
            return
        key = (_domain(url), context or "")
        now = time.monotonic()
        with _SEEN_LOCK:
            last = _SEEN.get(key)
            if last is not None and now - last < _DEDUPE_SECONDS:
                return
            _SEEN[key] = now
        _ensure_worker()
        _QUEUE.put_nowait((url, asker, context))
    except Exception:
        pass


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    print(json.dumps(handshake(target, asker="cli"), indent=2))
