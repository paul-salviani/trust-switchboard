"""Handshake — "Are you on Trust Switch?"

Bot meets bot (or a product is about to use a vendor). Ask the question.
Record the answer. Business proceeds.

This is NOT trust_check. trust_check answers "may I recommend this to a
human?" and fails closed. handshake answers "I am about to deal with this
bot/vendor — are they on Trust Switch?" and, while activation is off,
always lets business proceed. Nobody is on Trust Switch yet, so an honest
"no" must not block anyone. The "no" is the data. The question is the seed.

Two fields land on every handshake:
  other_bot_trusted_per_trust_switch: yes | no
  trust_switch_activation:            off | on

activation off  -> proceed True always (observe mode, the default)
activation on   -> proceed only when the other side is on Trust Switch
"""
from __future__ import annotations

import os
import sqlite3
import uuid

from db import now
import policy

QUESTION = "Are you on Trust Switch?"

ACTIVATION_VALUES = ("off", "on")


def _clean(v, limit: int = 500) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return s[:limit]


def default_activation() -> str:
    """Server-wide default. Env A2A_ACTIVATION=on turns enforcement on."""
    v = (os.environ.get("A2A_ACTIVATION") or "off").strip().lower()
    return v if v in ACTIVATION_VALUES else "off"


def _activation_from(payload: dict) -> str:
    raw = payload.get("activation")
    if raw is None:
        raw = payload.get("trust_switch_activation")
    if isinstance(raw, bool):
        return "on" if raw else "off"
    if raw is None:
        return default_activation()
    v = str(raw).strip().lower()
    if v in ("on", "true", "1", "enforce", "yes"):
        return "on"
    if v in ("off", "false", "0", "observe", "no"):
        return "off"
    return default_activation()


def _their_answer(payload: dict) -> str | None:
    raw = payload.get("their_answer")
    if raw is None:
        raw = payload.get("answer")
    if isinstance(raw, bool):
        return "yes" if raw else "no"
    return _clean(raw, 200)


def ask_line(root: str) -> str:
    """The line one bot says to another. Paste it into the message."""
    return (
        f"{QUESTION} If not: publish a policy at {root} and other AIs can "
        f"verify you before they recommend you. Free. No account to read."
    )


def handshake(con: sqlite3.Connection, payload: dict, root: str = "") -> dict:
    payload = payload if isinstance(payload, dict) else {}
    raw_url = _clean(payload.get("url") or payload.get("target_url"), 2000) or ""
    domain = policy.extract_domain(raw_url) if raw_url else ""
    asker = _clean(payload.get("asker") or payload.get("caller_id"), 200)
    context = _clean(payload.get("context"), 500)
    their_answer = _their_answer(payload)
    activation = _activation_from(payload)

    org = policy.org_by_url(con, raw_url) if raw_url else None
    pol = policy.latest_policy(con, org["id"]) if org else None
    fixture = policy._is_fixture(org, domain)
    on_switch = bool(
        org is not None
        and pol is not None
        and pol["status"] == "active"
        and org["attested"]
        and not fixture
    )
    ph = pol["policy_hash"] if (on_switch and pol is not None) else None

    if not raw_url:
        reason = "no_url"
    elif on_switch:
        reason = "on_trust_switch"
    elif org is None or pol is None:
        reason = "not_on_trust_switch"
    elif fixture:
        reason = "fixture"
    elif pol["status"] != "active":
        reason = "policy_not_active"
    elif not org["attested"]:
        reason = "not_attested"
    else:
        reason = "not_on_trust_switch"

    trusted = "yes" if on_switch else "no"
    proceed = True if activation == "off" else on_switch

    if on_switch:
        speak = f"{org['name']} is on Trust Switch. Policy hash {ph}. Proceeding."
    elif activation == "off":
        speak = (
            f"{domain or 'That bot'} is not on Trust Switch yet. Noted. "
            f"Proceeding — Trust Switch activation is off."
        )
    else:
        speak = (
            f"{domain or 'That bot'} is not on Trust Switch. "
            f"Trust Switch activation is on, so I will not proceed."
        )

    hs_id = "hs_" + uuid.uuid4().hex[:16]
    created_at = now()
    con.execute(
        """INSERT INTO handshakes
           (id, url, domain, asker, context, their_answer, on_trust_switch,
            activation, proceed, reason, policy_hash, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            hs_id,
            raw_url,
            domain or None,
            asker,
            context,
            their_answer,
            1 if on_switch else 0,
            activation,
            1 if proceed else 0,
            reason,
            ph,
            created_at,
        ),
    )
    con.commit()

    return {
        "handshake_id": hs_id,
        "question": QUESTION,
        "url": raw_url,
        "domain": domain or None,
        "asker": asker,
        "context": context,
        "their_answer": their_answer,
        "on_trust_switch": on_switch,
        "other_bot_trusted_per_trust_switch": trusted,
        "trust_switch_activation": activation,
        "proceed": proceed,
        "reason": reason,
        "policy_hash": ph,
        "org": policy._org_public(org) if on_switch else None,
        "speak": speak,
        "ask_them": ask_line(root or os.environ.get("A2A_PUBLIC_URL") or "https://170.64.146.185.sslip.io"),
        "asked_at": created_at,
    }


def handshake_stats(con: sqlite3.Connection, limit: int = 50) -> dict:
    """Who is being asked about, and how often. The "no"s are demand."""
    limit = max(1, min(int(limit or 50), 500))
    total = con.execute("SELECT COUNT(*) FROM handshakes").fetchone()[0]
    on = con.execute(
        "SELECT COUNT(*) FROM handshakes WHERE on_trust_switch = 1"
    ).fetchone()[0]
    proceeded = con.execute(
        "SELECT COUNT(*) FROM handshakes WHERE proceed = 1"
    ).fetchone()[0]
    blocked = con.execute(
        "SELECT COUNT(*) FROM handshakes WHERE proceed = 0"
    ).fetchone()[0]
    asked_rows = con.execute(
        """SELECT domain, COUNT(*) AS n, MAX(created_at) AS last_at,
                  SUM(on_trust_switch) AS on_switch
           FROM handshakes
           WHERE domain IS NOT NULL
           GROUP BY domain
           ORDER BY n DESC, last_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    recent_rows = con.execute(
        """SELECT id, url, domain, asker, context, their_answer,
                  on_trust_switch, activation, proceed, reason, created_at
           FROM handshakes
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return {
        "total": int(total),
        "on_trust_switch": int(on),
        "not_on_trust_switch": int(total) - int(on),
        "proceeded": int(proceeded),
        "blocked": int(blocked),
        "default_activation": default_activation(),
        "most_asked": [
            {
                "domain": r["domain"],
                "asks": int(r["n"]),
                "on_trust_switch": bool(r["on_switch"]),
                "last_at": r["last_at"],
            }
            for r in asked_rows
        ],
        "recent": [
            {
                "handshake_id": r["id"],
                "url": r["url"],
                "domain": r["domain"],
                "asker": r["asker"],
                "context": r["context"],
                "their_answer": r["their_answer"],
                "other_bot_trusted_per_trust_switch": "yes" if r["on_trust_switch"] else "no",
                "trust_switch_activation": r["activation"],
                "proceed": bool(r["proceed"]),
                "reason": r["reason"],
                "asked_at": r["created_at"],
            }
            for r in recent_rows
        ],
    }
