"""Prove the handshake law. Run: python tests/test_handshake.py

"Are you on Trust Switch?" — ask, record, proceed. Activation off by default
so an honest "no" never blocks business. Temp SQLite; HTTP on 127.0.0.1:0.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_DIR = tempfile.mkdtemp(prefix="a2a_hs_")
os.environ["A2A_DB"] = str(Path(_DIR) / "a2a.sqlite")
os.environ.pop("A2A_ACTIVATION", None)

import db  # noqa: E402
import handshake  # noqa: E402
import mcp  # noqa: E402
import policy  # noqa: E402
import server  # noqa: E402

_SEED = json.loads((ROOT / "seed.json").read_text(encoding="utf-8"))
_LIVE: list[ThreadingHTTPServer] = []


def _stop_all() -> None:
    for h in list(_LIVE):
        try:
            h.shutdown()
            h.server_close()
        except Exception:
            pass
    _LIVE.clear()


atexit.register(_stop_all)


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(os.environ["A2A_DB"])
    con.row_factory = sqlite3.Row
    return con


def _attest_real_org(con: sqlite3.Connection, domain: str, name: str) -> str:
    """A real (non-seed) attested org with an active policy — 'on Trust Switch'."""
    org_id = "org_" + domain.replace(".", "")[:16]
    con.execute(
        "INSERT OR REPLACE INTO orgs (id, name, domain, website, category, attested, is_seed, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (org_id, name, domain, f"https://{domain}", "test", 1, 0, db.now()),
    )
    body = "We store nothing. Region AU. Disputes: ops@" + domain
    ph = hashlib.sha256(body.encode("utf-8")).hexdigest()
    con.execute(
        "INSERT OR REPLACE INTO policies (id, org_id, version, title, summary, body, status, policy_hash, published_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("pol_" + org_id, org_id, 1, "Policy", "Stores nothing", body, "active", ph, db.now()),
    )
    con.commit()
    return org_id


def _http(base: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(base + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


class HandshakeLaw(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db.init_db()
        con = _con()
        try:
            _attest_real_org(con, "onswitch.test", "On Switch Pty")
        finally:
            con.close()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        _LIVE.append(cls.httpd)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        h, p = cls.httpd.server_address[:2]
        cls.base = f"http://{h}:{p}"

    @classmethod
    def tearDownClass(cls) -> None:
        _stop_all()

    def test_01_unknown_bot_no_but_proceed(self) -> None:
        con = _con()
        try:
            out = handshake.handshake(
                con, {"url": "https://drive.google.com", "asker": "salviani-os", "context": "connect Drive"}
            )
        finally:
            con.close()
        self.assertEqual(out["question"], "Are you on Trust Switch?")
        self.assertFalse(out["on_trust_switch"])
        self.assertEqual(out["other_bot_trusted_per_trust_switch"], "no")
        self.assertEqual(out["trust_switch_activation"], "off")
        self.assertTrue(out["proceed"])
        self.assertEqual(out["reason"], "not_on_trust_switch")
        self.assertIn("Proceeding", out["speak"])
        self.assertIn("Are you on Trust Switch?", out["ask_them"])
        self.assertTrue(out["handshake_id"].startswith("hs_"))

    def test_02_attested_org_yes_and_hash(self) -> None:
        con = _con()
        try:
            out = handshake.handshake(con, {"url": "https://onswitch.test/api"})
        finally:
            con.close()
        self.assertTrue(out["on_trust_switch"])
        self.assertEqual(out["other_bot_trusted_per_trust_switch"], "yes")
        self.assertTrue(out["proceed"])
        self.assertTrue(out["policy_hash"])
        self.assertEqual(out["org"]["name"], "On Switch Pty")

    def test_03_seed_fixture_is_no(self) -> None:
        seed_domain = next(o["domain"] for o in _SEED["orgs"] if o.get("domain"))
        con = _con()
        try:
            out = handshake.handshake(con, {"url": f"https://{seed_domain}"})
        finally:
            con.close()
        self.assertFalse(out["on_trust_switch"])
        self.assertEqual(out["reason"], "fixture")
        self.assertTrue(out["proceed"])

    def test_04_activation_on_blocks_unknown_not_attested(self) -> None:
        con = _con()
        try:
            no = handshake.handshake(con, {"url": "https://stripe.com", "activation": "on"})
            yes = handshake.handshake(con, {"url": "https://onswitch.test", "activation": "on"})
        finally:
            con.close()
        self.assertEqual(no["trust_switch_activation"], "on")
        self.assertFalse(no["proceed"])
        self.assertIn("will not proceed", no["speak"])
        self.assertTrue(yes["proceed"])

    def test_05_env_activation_default(self) -> None:
        os.environ["A2A_ACTIVATION"] = "on"
        try:
            con = _con()
            try:
                out = handshake.handshake(con, {"url": "https://twilio.com"})
            finally:
                con.close()
            self.assertEqual(out["trust_switch_activation"], "on")
            self.assertFalse(out["proceed"])
        finally:
            os.environ.pop("A2A_ACTIVATION", None)

    def test_06_their_answer_recorded_and_stats(self) -> None:
        con = _con()
        try:
            handshake.handshake(
                con, {"url": "https://elevenlabs.io", "asker": "lea", "their_answer": "no"}
            )
            handshake.handshake(con, {"url": "https://elevenlabs.io", "asker": "grok"})
            stats = handshake.handshake_stats(con)
            row = con.execute(
                "SELECT their_answer, asker FROM handshakes WHERE domain='elevenlabs.io' AND asker='lea'"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(row["their_answer"], "no")
        self.assertGreaterEqual(stats["total"], 6)
        self.assertEqual(stats["default_activation"], "off")
        top = {m["domain"]: m["asks"] for m in stats["most_asked"]}
        self.assertEqual(top.get("elevenlabs.io"), 2)
        self.assertTrue(any(r["asker"] == "lea" for r in stats["recent"]))

    def test_07_http_no_key_and_get_handshakes(self) -> None:
        st, out = _http(self.base, "POST", "/tools/handshake", {"url": "https://retellai.com", "asker": "http-test"})
        self.assertEqual(st, 200)
        self.assertEqual(out["other_bot_trusted_per_trust_switch"], "no")
        self.assertTrue(out["proceed"])
        self.assertTrue(out["ask_them"].startswith("Are you on Trust Switch?"))
        self.assertIn(self.base, out["ask_them"])
        st, stats = _http(self.base, "GET", "/handshakes?limit=5")
        self.assertEqual(st, 200)
        self.assertIn("retellai.com", [m["domain"] for m in stats["most_asked"]])
        st, health = _http(self.base, "GET", "/health")
        self.assertGreaterEqual(health.get("handshakes", 0), 1)

    def test_08_mcp_tool_listed_and_callable(self) -> None:
        names = [t["name"] for t in mcp.TOOLS]
        self.assertIn("handshake", names)
        self.assertNotIn("handshake", mcp.KEYED_TOOLS)
        con = _con()
        try:
            data, err = mcp._call_tool(con, "handshake", {"url": "https://cliniko.com", "asker": "mcp"}, None)
        finally:
            con.close()
        self.assertIsNone(err)
        self.assertTrue(data["proceed"])
        self.assertEqual(data["other_bot_trusted_per_trust_switch"], "no")

    def test_09_trust_check_still_fails_closed(self) -> None:
        """Handshake did not soften the recommend gate."""
        con = _con()
        try:
            chk = policy.trust_check(con, "https://drive.google.com")
        finally:
            con.close()
        self.assertFalse(chk["pass"])
        self.assertFalse(chk["recommendable"])

    def test_10_card_and_llms_mention_handshake(self) -> None:
        st, card = _http(self.base, "GET", "/.well-known/agent-card.json")
        self.assertIn("handshake", [s["id"] for s in card["skills"]])
        req = Request(self.base + "/llms.txt")
        with urlopen(req, timeout=10) as r:
            txt = r.read().decode("utf-8")
        self.assertIn("/tools/handshake", txt)


def _print(result: unittest.TestResult, names: list[str]) -> None:
    bad = {t.id().rsplit(".", 1)[-1] for t, _ in result.failures + result.errors}
    if result.errors and any("setUpClass" in t.id() for t, _ in result.errors):
        bad = set(names)
    for i, n in enumerate(names, 1):
        print(f"{i:02d} {'FAIL' if n in bad else 'PASS'}  {n}")
    print(f"{len(names)} run, {len(bad)} fail")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(HandshakeLaw)
    names = [t.id().rsplit(".", 1)[-1] for t in suite]
    outcome = unittest.TextTestRunner(verbosity=0).run(suite)
    _print(outcome, names)
    sys.exit(0 if outcome.wasSuccessful() else 1)
