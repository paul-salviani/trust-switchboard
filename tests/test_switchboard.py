"""Prove the A2A ranking law. Run: python tests/test_switchboard.py

Temp SQLite via A2A_DB is set before db/server import. HTTP tests bind
127.0.0.1:0 and stop in finally. Does not start a leftover daemon.
"""
from __future__ import annotations

import atexit
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_BOOT_DIR = tempfile.mkdtemp(prefix="a2a_boot_")
os.environ["A2A_DB"] = str(Path(_BOOT_DIR) / "a2a.sqlite")

IMPORT_ERRORS: list[str] = []
_MODULES: list[object] = []
for _name in (
    "db",
    "policy",
    "recommend",
    "tape",
    "auth",
    "geo",
    "uniqueness",
    "mcp",
    "server",
):
    try:
        _MODULES.append(__import__(_name))
    except ImportError:
        continue
    except Exception as exc:  # noqa: BLE001
        IMPORT_ERRORS.append(f"{_name}: {exc}")

_SEED = json.loads((ROOT / "seed.json").read_text(encoding="utf-8"))
SEED_DOMAINS = {
    (o.get("domain") or "").lower()
    for o in (_SEED.get("orgs") or [])
    if o.get("domain")
}
WRITE_KEY = _SEED.get("write_key") or ""

_LIVE_HTTP: list[ThreadingHTTPServer] = []


def _shutdown_http() -> None:
    for httpd in list(_LIVE_HTTP):
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass
    _LIVE_HTTP.clear()


atexit.register(_shutdown_http)


def _fn(*names: str):
    for name in names:
        for mod in _MODULES:
            if hasattr(mod, name):
                return getattr(mod, name)
    return None


def _bind_db(path: Path) -> None:
    os.environ["A2A_DB"] = str(path)
    for mod in _MODULES:
        if hasattr(mod, "DB_PATH"):
            mod.DB_PATH = path


def _connect() -> sqlite3.Connection:
    opener = _fn("db")
    if opener is not None:
        try:
            con = opener()
            if isinstance(con, sqlite3.Connection):
                con.row_factory = sqlite3.Row
                return con
        except TypeError:
            pass
    con = sqlite3.connect(os.environ["A2A_DB"])
    con.row_factory = sqlite3.Row
    return con


def _call(fn, *args, **kwargs):
    """Call fn(con, ...) or fn(...) depending on signature."""
    if fn is None:
        raise AssertionError("required function is missing")
    con = _connect()
    try:
        try:
            out = fn(con, *args, **kwargs)
        except TypeError:
            out = fn(*args, **kwargs)
        try:
            con.commit()
        except sqlite3.Error:
            pass
        return out
    finally:
        con.close()


def _passed(out) -> bool:
    if not isinstance(out, dict):
        raise AssertionError(f"expected dict, got {type(out).__name__}: {out!r}")
    if "pass" in out:
        return bool(out["pass"])
    result = out.get("result")
    if result in ("pass", "fail", "unknown"):
        return result == "pass"
    raise AssertionError(f"trust result missing pass/result: {out!r}")


def _matches(out) -> list:
    if not isinstance(out, dict):
        raise AssertionError(f"whats_good_for expected dict, got {out!r}")
    if "matches" in out:
        return list(out.get("matches") or [])
    if "items" in out:
        return list(out.get("items") or [])
    raise AssertionError(f"whats_good_for missing matches: {out!r}")


def _domain_of(row: dict) -> str:
    domain = (row.get("domain") or "").lower().removeprefix("www.")
    if domain:
        return domain
    extract = _fn("extract_domain")
    url = row.get("website") or row.get("url") or row.get("site_url") or ""
    if extract is not None:
        try:
            return (extract(url) or "").lower()
        except Exception:
            pass
    return url.lower()


def _contains_domain(obj, domain: str) -> bool:
    needle = domain.lower()
    if obj is None:
        return False
    if isinstance(obj, str):
        return needle in obj.lower()
    if isinstance(obj, dict):
        return any(_contains_domain(v, domain) for v in obj.values()) or any(
            needle in str(k).lower() for k in obj
        )
    if isinstance(obj, (list, tuple, set)):
        return any(_contains_domain(v, domain) for v in obj)
    return needle in str(obj).lower()


def _as_hash(val) -> str:
    if isinstance(val, dict):
        val = val.get("policy_hash") or val.get("hash") or val.get("digest")
    if not val:
        raise AssertionError(f"no policy hash in {val!r}")
    return str(val)


def _policy_hash(body: str, policy_url: str):
    fn = _fn("policy_hash")
    if fn is None:
        raise AssertionError("policy_hash is missing")
    sig = inspect.signature(fn)
    mapping = {
        "body": body,
        "policy_url": policy_url,
        "url": policy_url,
        "policy_json": body,
        "text": body,
        "title": "Policy",
        "summary": "Stores bookings in AU.",
        "stores_what": "bookings",
        "data_region": "AU",
        "dispute_email": "trust@hash.example",
        "org_id": "org_hash_probe",
        "version": 1,
    }
    kwargs = {}
    con = None
    try:
        for name, param in sig.parameters.items():
            if name in ("self",):
                continue
            if name in ("con", "conn", "connection"):
                con = _connect()
                kwargs[name] = con
            elif name in mapping:
                kwargs[name] = mapping[name]
            elif param.default is inspect.Parameter.empty:
                kwargs[name] = None
        return _as_hash(fn(**kwargs))
    except TypeError:
        try:
            return _as_hash(
                fn(
                    "Policy",
                    "Stores bookings in AU.",
                    body,
                    "bookings",
                    "AU",
                    "trust@hash.example",
                )
            )
        except TypeError:
            return _as_hash(fn(body, policy_url))
    finally:
        if con is not None:
            con.close()


def _org_id(obj) -> str | None:
    if not isinstance(obj, dict):
        return str(obj) if obj else None
    for key in ("org_id", "id", "org"):
        val = obj.get(key)
        if isinstance(val, dict):
            val = val.get("id") or val.get("org_id")
        if val:
            return str(val)
    return None


def _raw_key(obj) -> str | None:
    if not isinstance(obj, dict):
        return None
    for key in ("api_key", "key", "publish_key", "org_key", "write_key"):
        if obj.get(key):
            return str(obj[key])
    return None


@contextmanager
def _http_server():
    handler = _fn("Handler")
    if handler is None:
        raise AssertionError("HTTP Handler is missing")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    _LIVE_HTTP.append(httpd)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass
        if httpd in _LIVE_HTTP:
            _LIVE_HTTP.remove(httpd)
        thread.join(timeout=3)


def _http_json(base: str, method: str, path: str, body=None, key: str | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(base + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-API-Key", key)
    try:
        with urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
            return resp.status, parsed
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return exc.code, parsed
    except URLError as exc:
        raise AssertionError(f"HTTP {method} {path} failed: {exc}") from exc


class TestSwitchboard(unittest.TestCase):
    def setUp(self) -> None:
        if IMPORT_ERRORS:
            self.fail("import failed: " + "; ".join(IMPORT_ERRORS))
        if not _MODULES:
            self.fail("neither db nor server could be imported")
        self._tmp = tempfile.TemporaryDirectory(prefix="a2a_test_")
        self._db_path = Path(self._tmp.name) / "a2a.sqlite"
        _bind_db(self._db_path)
        init_db = _fn("init_db")
        if init_db is None:
            self.fail("init_db is missing")
        init_db()

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def _trust(self, url: str) -> dict:
        fn = _fn("trust_check")
        if fn is None:
            self.fail("trust_check is missing")
        out = _call(fn, url)
        if not isinstance(out, dict):
            self.fail(f"trust_check returned {out!r}")
        return out

    def _whats(self, query: str) -> dict:
        fn = _fn("whats_good_for")
        if fn is None:
            self.fail("whats_good_for is missing")
        out = _call(fn, query)
        if not isinstance(out, dict):
            self.fail(f"whats_good_for returned {out!r}")
        return out

    def _signup(self, name: str, site_url: str) -> dict:
        fn = _fn("signup_org")
        if fn is None:
            self.fail("signup_org is missing")
        payload = {
            "org_name": name,
            "name": name,
            "site_url": site_url,
            "website": site_url,
        }
        con = _connect()
        try:
            attempts = (
                lambda: fn(con, payload),
                lambda: fn(payload),
                lambda: fn(con, name=name, site_url=site_url),
                lambda: fn(name=name, site_url=site_url),
                lambda: fn(con, org_name=name, site_url=site_url),
                lambda: fn(org_name=name, site_url=site_url),
            )
            last = None
            for attempt in attempts:
                try:
                    out = attempt()
                    con.commit()
                    if not isinstance(out, dict):
                        out = {"id": out, "site_url": site_url}
                    return out
                except TypeError as exc:
                    last = exc
                    continue
            self.fail(f"signup_org signature not accepted: {last}")
        finally:
            con.close()

    def _publish(self, org: dict, payload: dict) -> dict:
        fn = _fn("publish_policy")
        if fn is None:
            self.fail("publish_policy is missing")
        key = _raw_key(org)
        con = _connect()
        try:
            key_row = None
            lookup = _fn("lookup_key")
            if key and lookup is not None:
                try:
                    key_row = lookup(con, key)
                except TypeError:
                    key_row = lookup(key)
            if key_row is None:
                self.fail("publish_policy needs the org publish key from signup_org")
            out = fn(con, payload, key_row)
            con.commit()
            return out if isinstance(out, dict) else {"policy_id": out}
        finally:
            con.close()

    def _attested_org(self, name: str, site_url: str, body: str) -> dict:
        """signup_org path if present; otherwise insert an attested non-seed org."""
        if _fn("signup_org") is not None:
            org = self._signup(name, site_url)
            published = self._publish(
                org,
                {
                    "title": "Live policy",
                    "summary": "Stores bookings in AU. Dispute: trust@example",
                    "body": body,
                    "status": "active",
                    "attested": True,
                    "policy_url": site_url.rstrip("/") + "/policy.json",
                    "stores_what": "bookings",
                    "data_region": "AU",
                    "dispute_email": "trust@example",
                    "site_url": site_url,
                    "org_name": name,
                },
            )
            org.update(published)
            org.setdefault("site_url", site_url)
            return org

        domain = site_url.split("://", 1)[-1].split("/", 1)[0].lower().removeprefix("www.")
        oid = "org_" + uuid.uuid4().hex[:12]
        hasher = _fn("policy_hash")
        ph = None
        if hasher is not None:
            try:
                ph = _policy_hash(body, site_url + "/policy.json")
            except Exception:
                ph = None
        if not ph:
            import hashlib

            ph = hashlib.sha256(body.encode("utf-8")).hexdigest()
        con = _connect()
        try:
            con.execute(
                """INSERT INTO orgs (id, name, domain, website, category, attested, is_seed, created_at)
                   VALUES (?,?,?,?,?,?,?,datetime('now'))""",
                (oid, name, domain, site_url, "ai-tools", 1, 0),
            )
            con.execute(
                """INSERT INTO policies
                   (id, org_id, version, title, summary, body, status, policy_hash, published_at)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
                (
                    f"pol_{oid}_v1",
                    oid,
                    1,
                    "Live policy",
                    "Stores bookings in AU.",
                    body,
                    "active",
                    ph,
                ),
            )
            con.commit()
        except sqlite3.OperationalError as exc:
            self.fail(f"could not insert attested org: {exc}")
        finally:
            con.close()
        return {"id": oid, "org_id": oid, "site_url": site_url}

    def _ingest(self, payload: dict) -> dict:
        fn = _fn("ingest_listing")
        if fn is None:
            self.fail("ingest_listing is missing")
        out = _call(fn, payload)
        if not isinstance(out, dict):
            self.fail(f"ingest_listing returned {out!r}")
        return out

    # --- 15 proofs ---

    def test_01_northside_trust_check_passes(self) -> None:
        out = self._trust("https://northsidedental.example")
        self.assertTrue(_passed(out), out)

    def test_02_dentist_matches_empty(self) -> None:
        out = self._whats("dentist")
        self.assertEqual(_matches(out), [], out)

    def test_03_whats_good_for_omits_seed_and_local(self) -> None:
        queries = (
            "dentist",
            "gp",
            "informed consent",
            "insurer report",
            "leftover tiles",
            "youtube",
            "legal pack",
        )
        leaked = []
        for query in queries:
            for row in _matches(self._whats(query)):
                domain = _domain_of(row)
                if row.get("is_seed") in (1, True, "1"):
                    leaked.append((query, row.get("id"), "is_seed", domain))
                if domain.endswith(".local"):
                    leaked.append((query, row.get("id"), ".local", domain))
                if domain in SEED_DOMAINS:
                    leaked.append((query, row.get("id"), "seed_domain", domain))
        self.assertEqual(leaked, [])

    def test_04_post_listings_without_key_401(self) -> None:
        with _http_server() as base:
            code, body = _http_json(
                base,
                "POST",
                "/listings",
                {"name": "No Key Tool", "url": "https://nokey.example", "pond": "ai-tools"},
            )
            self.assertEqual(code, 401, body)

    def test_05_log_intent_without_key_401(self) -> None:
        with _http_server() as base:
            payload = {
                "query": "dentist",
                "action": "recommend",
                "target_url": "https://northsidedental.example",
            }
            code, body = _http_json(base, "POST", "/tools/log_intent", payload)
            if code == 404:
                code, body = _http_json(base, "POST", "/log_intent", payload)
            self.assertEqual(code, 401, body)

    def test_06_signup_publish_attested_trust_passes(self) -> None:
        site = "https://livepolicy.example"
        org = self._signup("Live Policy Co", site)
        self._publish(
            org,
            {
                "title": "Live policy",
                "summary": "Stores bookings in AU. Dispute: trust@livepolicy.example",
                "body": "We store appointment emails in AU. Dispute: trust@livepolicy.example",
                "status": "active",
                "attested": True,
                "policy_url": site + "/policy.json",
                "stores_what": "bookings",
                "data_region": "AU",
                "dispute_email": "trust@livepolicy.example",
                "site_url": site,
                "org_name": "Live Policy Co",
            },
        )
        oid = _org_id(org)
        con = _connect()
        try:
            if oid:
                row = con.execute("SELECT attested FROM orgs WHERE id = ?", (oid,)).fetchone()
                if row is not None:
                    self.assertTrue(int(row["attested"]), "org is not attested after publish")
            else:
                row = con.execute(
                    "SELECT attested FROM orgs WHERE domain = ?",
                    ("livepolicy.example",),
                ).fetchone()
                self.assertIsNotNone(row, "signup_org did not persist livepolicy.example")
                self.assertTrue(int(row["attested"]))
        finally:
            con.close()
        out = self._trust(site)
        self.assertTrue(_passed(out), out)

    def test_07_unique_ranks_above_clone_in_pond(self) -> None:
        body = "We store MCP telemetry in AU. Dispute: trust@ranked.example"
        org = self._attested_org("Ranked Co", "https://ranked.example", body)
        oid = _org_id(org)
        unique_text = "Balloon kite telemetry MCP for high-altitude weather"
        clone_text = unique_text
        unique = self._ingest(
            {
                "name": unique_text,
                "url": "https://unique-kite.example",
                "one_liner": unique_text,
                "pond": "ai-tools",
                "org_id": oid,
            }
        )
        clone = self._ingest(
            {
                "name": clone_text,
                "url": "https://clone-kite.example",
                "one_liner": clone_text,
                "pond": "ai-tools",
                "org_id": oid,
            }
        )
        out = self._whats("kite telemetry")
        rows = _matches(out)
        uid = unique.get("id")
        cid = clone.get("id")
        by_id = {r.get("id"): r for r in rows}
        self.assertIn(uid, by_id, out)
        self.assertIn(cid, by_id, out)
        u_score = int(by_id[uid].get("unique_score") or 0)
        c_score = int(by_id[cid].get("unique_score") or 0)
        self.assertGreater(
            u_score,
            c_score,
            f"unique_score unique={u_score} clone={c_score} matches={rows}",
        )

    def test_08_policy_hash_independent_of_url(self) -> None:
        body = "We store appointment emails in AU. Dispute: trust@hash.example"
        h1 = _policy_hash(body, "https://hash-a.example/policy.json")
        h2 = _policy_hash(body, "https://hash-b.example/legal/policy")
        self.assertEqual(h1, h2)
        self.assertTrue(h1)

    def test_09_fail_closed_unknown_logged(self) -> None:
        url = "https://no-policy-unknown.example"
        out = self._trust(url)
        self.assertFalse(_passed(out), out)
        con = _connect()
        try:
            rows = con.execute(
                "SELECT * FROM checks WHERE url = ? OR domain = ?",
                (url, "no-policy-unknown.example"),
            ).fetchall()
            self.assertTrue(rows, "fail-closed must log a checks row")
        finally:
            con.close()

    def test_10_demand_includes_failed_domain(self) -> None:
        url = "https://needed-missing.example"
        self._trust(url)
        fn = _fn("demand")
        if fn is None:
            self.fail("demand() is missing")
        out = _call(fn)
        self.assertTrue(
            _contains_domain(out, "needed-missing.example"),
            f"demand() did not include needed-missing.example: {out!r}",
        )

    def test_11_log_click_stores_a_row(self) -> None:
        fn = _fn("log_click")
        if fn is None:
            self.fail("log_click is missing")
        url = "https://clicked.example/item"
        payload = {"item_url": url, "receipt_id": "rcp_test_click"}
        con = _connect()
        try:
            attempts = (
                lambda: fn(con, payload),
                lambda: fn(payload),
                lambda: fn(con, item_url=url, receipt_id="rcp_test_click"),
                lambda: fn(item_url=url, receipt_id="rcp_test_click"),
                lambda: fn(con, url),
                lambda: fn(url),
            )
            last = None
            called = False
            for attempt in attempts:
                try:
                    attempt()
                    con.commit()
                    called = True
                    break
                except TypeError as exc:
                    last = exc
                    continue
            if not called:
                self.fail(f"log_click signature not accepted: {last}")
            n = con.execute(
                "SELECT COUNT(*) AS c FROM clicks WHERE item_url = ?", (url,)
            ).fetchone()["c"]
            self.assertGreater(n, 0, "log_click did not store a clicks row")
        finally:
            con.close()

    def test_12_agent_card_has_three_skills(self) -> None:
        fn = _fn("agent_card")
        card = None
        if fn is not None:
            try:
                card = fn("http://127.0.0.1:8787")
            except TypeError:
                class _H:
                    headers = {"Host": "127.0.0.1:8787", "X-Forwarded-Proto": "http"}

                try:
                    card = fn(_H())
                except TypeError:
                    card = None
        if card is None:
            with _http_server() as base:
                code, card = _http_json(base, "GET", "/.well-known/agent-card.json")
                self.assertEqual(code, 200, card)
        skills = card.get("skills") or card.get("capabilities") or []
        ids = set()
        if isinstance(skills, dict):
            ids = set(skills)
        else:
            for s in skills:
                if isinstance(s, str):
                    ids.add(s)
                elif isinstance(s, dict):
                    ids.add(s.get("id") or s.get("name"))
        for name in ("trust_check", "whats_good_for", "log_intent"):
            self.assertIn(name, ids, f"agent card missing skill {name}: {card}")
        op = card.get("operator") or {}
        self.assertEqual(op.get("name"), "Paul Salviani", card)
        self.assertEqual(op.get("location"), "Sydney", card)
        self.assertNotIn("email", op, "do not invent operator email")
        org = (card.get("provider") or {}).get("organization") or ""
        self.assertIn("Paul Salviani", org)
        self.assertIn("Sydney", org)

    def test_13_llms_txt_before_every_recommend(self) -> None:
        fn = _fn("llms_txt")
        text = None
        if fn is not None:
            try:
                text = fn("http://127.0.0.1:8787")
            except TypeError:
                class _H:
                    headers = {"Host": "127.0.0.1:8787", "X-Forwarded-Proto": "http"}

                try:
                    text = fn(_H())
                except TypeError:
                    text = None
        if text is None:
            with _http_server() as base:
                req = Request(base + "/llms.txt", method="GET")
                with urlopen(req, timeout=5) as resp:
                    text = resp.read().decode("utf-8")
        self.assertIsInstance(text, str)
        self.assertTrue(text.lstrip().startswith("# Trust Switchboard"), text[:80])
        self.assertIn("before every recommend", text.lower())
        self.assertIn("## Operator", text)
        self.assertIn("Paul Salviani", text)
        self.assertIn("Sydney", text)
        self.assertIn("Email not on the card until the operator publishes one.", text)
        self.assertNotIn("mailto:", text)

    def test_14_commit_recommend_unknown_fail_receipt(self) -> None:
        fn = _fn("commit_recommend")
        if fn is None:
            self.fail("commit_recommend is missing")
        url = "https://unknown-commit.example"
        payload = {
            "url": url,
            "target_url": url,
            "query": "unknown",
            "action": "recommend",
        }
        con = _connect()
        try:
            lookup = _fn("lookup_key")
            key_row = lookup(con, WRITE_KEY) if lookup is not None else None
            if key_row is None:
                self.fail("commit_recommend needs the seed write key")
            out = fn(con, payload, key_row)
            con.commit()
        finally:
            con.close()
        self.assertFalse(_passed(out), out)
        rid = out.get("receipt_id") or out.get("receipt")
        if isinstance(rid, dict):
            rid = rid.get("id") or rid.get("receipt_id")
        self.assertTrue(rid, f"commit_recommend must return a receipt: {out!r}")

    def test_15_revoked_or_inactive_latest_fails(self) -> None:
        seed_fail = self._trust("https://quickfixaligners.example")
        self.assertFalse(_passed(seed_fail), seed_fail)

        if _fn("signup_org") is None:
            return
        site = "https://later-revoked.example"
        org = self._signup("Later Revoked Co", site)
        self._publish(
            org,
            {
                "title": "Live then pulled",
                "summary": "Active first.",
                "body": "Active policy body for later-revoked.example",
                "status": "active",
                "attested": True,
                "site_url": site,
            },
        )
        live = self._trust(site)
        self.assertTrue(_passed(live), live)
        self._publish(
            org,
            {
                "title": "Revoked",
                "summary": "Pulled.",
                "body": "Revoked policy body for later-revoked.example",
                "status": "revoked",
                "site_url": site,
            },
        )
        dead = self._trust(site)
        self.assertFalse(_passed(dead), dead)

    def test_16_operator_email_only_when_env_set(self) -> None:
        geo = None
        for mod in _MODULES:
            if hasattr(mod, "operator"):
                geo = mod
                break
        if geo is None:
            self.fail("geo.operator missing")
        old = os.environ.pop("A2A_OPERATOR_EMAIL", None)
        try:
            bare = geo.operator()
            self.assertEqual(bare.get("name"), "Paul Salviani")
            self.assertEqual(bare.get("location"), "Sydney")
            self.assertNotIn("email", bare)
            os.environ["A2A_OPERATOR_EMAIL"] = "buyer@example.test"
            with_mail = geo.operator()
            self.assertEqual(with_mail.get("email"), "buyer@example.test")
            card = geo.agent_card("https://example.test")
            self.assertEqual(card["operator"]["email"], "buyer@example.test")
            txt = geo.llms_txt("https://example.test")
            self.assertIn("mailto:buyer@example.test", txt)
        finally:
            if old is None:
                os.environ.pop("A2A_OPERATOR_EMAIL", None)
            else:
                os.environ["A2A_OPERATOR_EMAIL"] = old

    def test_17_crawler_doors(self) -> None:
        with _http_server() as base:
            code, catalog = _http_json(base, "GET", "/.well-known/ai-catalog.json")
            self.assertEqual(code, 200, catalog)
            self.assertEqual(catalog.get("specVersion"), "1.0")
            urls = {e.get("url") for e in (catalog.get("entries") or [])}
            self.assertTrue(any(str(u).endswith("/.well-known/agent-card.json") for u in urls), catalog)
            self.assertTrue(any(str(u).endswith("/mcp/server-card") for u in urls), catalog)
            code, card = _http_json(base, "GET", "/mcp/server-card")
            self.assertEqual(code, 200, card)
            remotes = card.get("remotes") or []
            self.assertTrue(remotes)
            self.assertEqual(remotes[0].get("type"), "streamable-http")
            self.assertTrue(str(remotes[0].get("url") or "").endswith("/mcp"))
            req = Request(base + "/.well-known/llms.txt", method="GET")
            with urlopen(req, timeout=5) as resp:
                text = resp.read().decode("utf-8")
            self.assertTrue(text.lstrip().startswith("# Trust Switchboard"), text[:80])
            req = Request(base + "/", method="HEAD")
            with urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("llms.txt", resp.headers.get("Link") or "")
                self.assertEqual(resp.read(), b"")
            req = Request(base + "/robots.txt", method="GET")
            with urlopen(req, timeout=5) as resp:
                robots = resp.read().decode("utf-8")
            self.assertIn("/for-agents.txt", robots)
            self.assertIn("ai-catalog.json", robots)
            self.assertIn("Sitemap:", robots)

    def test_18_source_is_code_not_the_log(self) -> None:
        with _http_server() as base:
            req = Request(base + "/source", method="GET")
            with urlopen(req, timeout=5) as resp:
                index = resp.read().decode("utf-8")
            self.assertIn("The CODE is copyable", index)
            self.assertIn("schema.sql", index)
            self.assertNotIn("/source/seed.json", index)
            self.assertNotIn("/source/.env", index)
            self.assertNotIn("- [seed.json]", index)
            code, schema = _http_json(base, "GET", "/source/schema.sql")
            self.assertEqual(code, 200)
            text = schema if isinstance(schema, str) else str(schema)
            self.assertIn("CREATE TABLE", text)
            code, missing = _http_json(base, "GET", "/source/seed.json")
            self.assertEqual(code, 404, missing)
            code, env = _http_json(base, "GET", "/source/.env")
            self.assertEqual(code, 404, env)
            req = Request(base + "/source.tar.gz", method="GET")
            with urlopen(req, timeout=5) as resp:
                blob = resp.read()
            self.assertGreater(len(blob), 100)
            self.assertEqual(blob[:2], b"\x1f\x8b")


LABELS = [
    ("test_01_northside_trust_check_passes", "trust_check https://northsidedental.example PASSES"),
    ("test_02_dentist_matches_empty", 'whats_good_for("dentist") matches=[]'),
    ("test_03_whats_good_for_omits_seed_and_local", "whats_good_for omits is_seed and .local"),
    ("test_04_post_listings_without_key_401", "POST /listings without key → 401"),
    ("test_05_log_intent_without_key_401", "log_intent without key → 401"),
    ("test_06_signup_publish_attested_trust_passes", "signup_org + publish_policy active → attested pass"),
    ("test_07_unique_ranks_above_clone_in_pond", "unique_score unique > clone in pond ai-tools"),
    ("test_08_policy_hash_independent_of_url", "policy_hash independent of URL"),
    ("test_09_fail_closed_unknown_logged", "fail closed unknown + checks row"),
    ("test_10_demand_includes_failed_domain", "demand() includes failed domain"),
    ("test_11_log_click_stores_a_row", "log_click stores a row"),
    ("test_12_agent_card_has_three_skills", "agent card skills trust_check, whats_good_for, log_intent"),
    ("test_13_llms_txt_before_every_recommend", 'llms.txt starts "# Trust Switchboard" and "before every recommend"'),
    ("test_14_commit_recommend_unknown_fail_receipt", "commit_recommend unknown → pass False + receipt"),
    ("test_15_revoked_or_inactive_latest_fails", "revoked/inactive latest policy → fail"),
    ("test_16_operator_email_only_when_env_set", "operator email only when A2A_OPERATOR_EMAIL is set"),
    ("test_17_crawler_doors", "ai-catalog, MCP card, well-known llms, HEAD Link, robots"),
    ("test_18_source_is_code_not_the_log", "GET /source CODE only — no seed.json, no .env"),
]


def _print_list(result: unittest.TestResult) -> None:
    failed = {}
    for test, err in list(result.failures) + list(result.errors):
        failed[test.id().split(".")[-1]] = err.splitlines()[-1] if err else "error"
    print("")
    print("A2A ranking-law proof")
    if IMPORT_ERRORS:
        print("IMPORT ERRORS: " + "; ".join(IMPORT_ERRORS))
    for i, (name, label) in enumerate(LABELS, 1):
        if name in failed:
            status = "FAIL"
            extra = " — " + failed[name]
        else:
            status = "PASS"
            extra = ""
        print(f"{i:02d} {status}  {label}{extra}")
    n_fail = len(result.failures) + len(result.errors)
    print(f"{result.testsRun} run, {n_fail} fail")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestSwitchboard)
    runner = unittest.TextTestRunner(verbosity=2)
    outcome = runner.run(suite)
    _print_list(outcome)
    sys.exit(0 if outcome.wasSuccessful() else 1)
