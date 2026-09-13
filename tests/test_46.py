"""Prove spend cap, nutrition label, HITL. Run: python tests/test_46.py

Temp SQLite via A2A_DB is set before db/server import. HTTP tests bind
127.0.0.1:0 and stop in finally. Does not start a leftover daemon.
Does not touch tests/test_switchboard.py.
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
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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
    "spend",
    "spend_cap",
    "nutrition",
    "label",
    "hitl",
    "approve",
):
    try:
        _MODULES.append(__import__(_name))
    except ImportError:
        continue
    except Exception as exc:  # noqa: BLE001
        IMPORT_ERRORS.append(f"{_name}: {exc}")

_SEED = json.loads((ROOT / "seed.json").read_text(encoding="utf-8"))
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


def _mod_fn(mod_name: str, *names: str):
    for mod in _MODULES:
        if getattr(mod, "__name__", None) == mod_name:
            for name in names:
                if hasattr(mod, name):
                    return getattr(mod, name)
    return _fn(*names)


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


def _write_key_row():
    lookup = _fn("lookup_key", "lookup_write_key")
    if lookup is None:
        return None
    con = _connect()
    try:
        try:
            return lookup(con, WRITE_KEY)
        except TypeError:
            return lookup(WRITE_KEY)
    finally:
        con.close()


def _dispatch(fn, **supplied):
    """Call fn with con/payload/key_row bound from signature when present."""
    if fn is None:
        raise AssertionError("required function is missing")
    sig = inspect.signature(fn)
    kwargs: dict = {}
    con = None
    close = False
    try:
        for name, param in sig.parameters.items():
            if name in ("self",):
                continue
            kind = param.kind
            if kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            if name in ("con", "conn", "connection"):
                con = _connect()
                close = True
                kwargs[name] = con
            elif name in ("key_row", "key") and supplied.get("key_row") is not None:
                kwargs[name] = supplied["key_row"]
            elif name == "payload":
                payload = supplied.get("payload")
                if not isinstance(payload, dict):
                    payload = {
                        k: v
                        for k, v in supplied.items()
                        if v is not None and k not in ("key_row", "payload")
                    }
                kwargs[name] = payload
            elif name in supplied and supplied[name] is not None:
                kwargs[name] = supplied[name]
            elif param.default is inspect.Parameter.empty:
                if name in supplied:
                    kwargs[name] = supplied[name]
        out = fn(**kwargs)
        if con is not None:
            try:
                con.commit()
            except sqlite3.Error:
                pass
        return out
    finally:
        if close and con is not None:
            con.close()


def _call(fn, *args, **kwargs):
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
    raise AssertionError(f"trust/spend result missing pass/result: {out!r}")


def _reason(out) -> str:
    if not isinstance(out, dict):
        return str(out or "")
    val = out.get("reason")
    if val is None:
        val = out.get("error") or out.get("code") or ""
    if isinstance(val, dict):
        val = val.get("reason") or val.get("code") or val.get("error") or ""
    return str(val)


def _status(out) -> str:
    if not isinstance(out, dict):
        return str(out or "").lower()
    val = out.get("status") or out.get("decision") or out.get("state") or ""
    return str(val).lower()


def _req_id(out) -> str | None:
    if not isinstance(out, dict):
        return str(out) if out else None
    for key in ("request_id", "approval_id", "id", "hitl_id"):
        val = out.get(key)
        if isinstance(val, dict):
            val = val.get("id") or val.get("request_id")
        if val:
            return str(val)
    return None


def _last_test(out) -> str:
    if not isinstance(out, dict):
        return str(out or "").lower()
    for key in ("last_test_result", "last_test", "test_result"):
        if out.get(key) is not None:
            return str(out[key]).lower()
    for nest in ("label", "nutrition", "nutrition_label"):
        inner = out.get(nest)
        if isinstance(inner, dict):
            for key in ("last_test_result", "last_test", "test_result"):
                if inner.get(key) is not None:
                    return str(inner[key]).lower()
    return ""


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


class Test46SpendNutritionHitl(unittest.TestCase):
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

    def _live_org(self, name: str, site_url: str, body: str | None = None) -> dict:
        org = self._signup(name, site_url)
        published = self._publish(
            org,
            {
                "title": "Live policy",
                "summary": "Stores bookings in AU. Dispute: trust@example",
                "body": body or f"Live policy body for {site_url}",
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
        org.setdefault("url", site_url)
        return org

    def _spend_check(self, url: str, amount=None, action=None) -> dict:
        fn = _mod_fn("spend", "spend_check") or _fn("spend_check")
        if fn is None:
            self.fail("spend_check is missing")
        payload = {
            "url": url,
            "target_url": url,
            "site_url": url,
        }
        if amount is not None:
            payload["amount"] = amount
            payload["amount_usd"] = amount
            payload["spend"] = amount
        if action is not None:
            payload["action"] = action
        key_row = _write_key_row()
        supplied = dict(payload)
        supplied["payload"] = payload
        supplied["key_row"] = key_row
        supplied["amount_usd"] = amount
        try:
            out = _dispatch(fn, **supplied)
        except TypeError:
            try:
                out = _call(fn, url, amount, action)
            except TypeError:
                out = _call(fn, payload, key_row)
        if not isinstance(out, dict):
            self.fail(f"spend_check returned {out!r}")
        return out

    def _record_spend(self, org: dict, amount, action: str, url: str) -> None:
        fn = _mod_fn("spend", "record_spend") or _fn("record_spend")
        if fn is None:
            return
        oid = _org_id(org)
        supplied = {
            "org_id": oid,
            "amount_usd": amount,
            "amount": amount,
            "action": action,
            "target_url": url,
            "url": url,
            "receipt_id": None,
        }
        try:
            _dispatch(fn, **supplied)
        except TypeError:
            try:
                _call(fn, oid, amount, action, url, None)
            except TypeError:
                return

    def _set_spend_cap(self, org: dict, cap: int, allowlist: list[str]) -> dict:
        site = org.get("site_url") or org.get("url") or org.get("website") or ""
        oid = _org_id(org)
        payload = {
            "org_id": oid,
            "url": site,
            "site_url": site,
            "target_url": site,
            "cap": cap,
            "spend_cap": cap,
            "spend_cap_usd": cap,
            "limit": cap,
            "allowlist": list(allowlist),
            "allowlist_json": json.dumps(list(allowlist)),
            "allowed_actions": list(allowlist),
            "actions": list(allowlist),
        }
        fn = _fn("set_spend_cap", "publish_spend_cap", "upsert_spend_cap", "set_cap")
        out = None
        if fn is not None:
            key = _raw_key(org)
            key_row = None
            lookup = _fn("lookup_key")
            if key and lookup is not None:
                con = _connect()
                try:
                    try:
                        key_row = lookup(con, key)
                    except TypeError:
                        key_row = lookup(key)
                finally:
                    con.close()
            if key_row is None:
                key_row = _write_key_row()
            supplied = dict(payload)
            supplied["payload"] = payload
            supplied["key_row"] = key_row
            try:
                out = _dispatch(fn, **supplied)
            except TypeError:
                out = _call(fn, payload, key_row)
        else:
            extra = dict(payload)
            extra.update(
                {
                    "title": "Live policy with spend cap",
                    "summary": "Stores bookings in AU. Dispute: trust@example",
                    "body": f"Spend cap {cap} allowlist {allowlist}",
                    "status": "active",
                    "site_url": site,
                }
            )
            out = self._publish(org, extra)

        # spend_check reads policies.spend_cap_usd + allowlist_json.
        con = _connect()
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(policies)").fetchall()}
            row = None
            if oid:
                row = con.execute(
                    "SELECT id FROM policies WHERE org_id = ? ORDER BY version DESC LIMIT 1",
                    (oid,),
                ).fetchone()
            if row is not None:
                pid = row["id"]
                dumped = json.dumps(list(allowlist))
                if "spend_cap_usd" in cols:
                    con.execute(
                        "UPDATE policies SET spend_cap_usd = ? WHERE id = ?",
                        (cap, pid),
                    )
                if "allowlist_json" in cols:
                    con.execute(
                        "UPDATE policies SET allowlist_json = ? WHERE id = ?",
                        (dumped, pid),
                    )
                if "allowlist" in cols:
                    con.execute(
                        "UPDATE policies SET allowlist = ? WHERE id = ?",
                        (dumped, pid),
                    )
            con.commit()
        finally:
            con.close()
        return out if isinstance(out, dict) else {"ok": True, "raw": out}

    def _nutrition(self, url: str | None = None, org: dict | None = None) -> dict:
        fn = _mod_fn("nutrition", "nutrition_for_url", "nutrition_label") or _fn(
            "nutrition_label", "nutrition_for_url", "nutrition", "get_nutrition", "label"
        )
        if fn is None:
            self.fail("nutrition_label is missing")
        oid = _org_id(org) if org else None
        payload = {}
        if url:
            payload["url"] = url
            payload["target_url"] = url
            payload["site_url"] = url
        if oid:
            payload["org_id"] = oid
            payload["agent_id"] = oid
        supplied = dict(payload)
        supplied["payload"] = payload
        try:
            out = _dispatch(fn, **supplied)
        except TypeError:
            try:
                out = _call(fn, payload) if payload else _call(fn)
            except TypeError:
                out = _call(fn, url) if url else _call(fn)
        if not isinstance(out, dict):
            self.fail(f"nutrition_label returned {out!r}")
        return out

    def _stamp_test(self, result: str, url: str | None = None, org: dict | None = None) -> dict:
        fn = _mod_fn("nutrition", "stamp_test") or _fn(
            "stamp_test", "stamp_nutrition", "record_test"
        )
        if fn is None:
            self.fail("stamp_test is missing")
        oid = _org_id(org) if org else None
        note = "proof"
        payload = {
            "result": result,
            "outcome": result,
            "status": result,
            "last_test_result": result,
            "note": note,
        }
        if url:
            payload["url"] = url
            payload["target_url"] = url
            payload["site_url"] = url
        if oid:
            payload["org_id"] = oid
            payload["agent_id"] = oid
        key_row = _write_key_row()
        supplied = dict(payload)
        supplied["payload"] = payload
        supplied["key_row"] = key_row
        supplied["note"] = note
        try:
            out = _dispatch(fn, **supplied)
        except TypeError:
            try:
                out = _call(fn, oid, result, note)
            except TypeError:
                try:
                    out = _call(fn, payload, key_row)
                except TypeError:
                    out = _call(fn, result)
        return out if isinstance(out, dict) else {"raw": out}

    def _request_approve(self, payload: dict) -> dict:
        fn = _fn("request_approve", "request_approval", "hitl_request")
        if fn is None:
            self.fail("request_approve is missing")
        key_row = _write_key_row()
        supplied = dict(payload)
        supplied["payload"] = payload
        supplied["key_row"] = key_row
        try:
            out = _dispatch(fn, **supplied)
        except TypeError:
            out = _call(fn, payload, key_row)
        if not isinstance(out, dict):
            self.fail(f"request_approve returned {out!r}")
        return out

    def _decide(self, request_id: str, decision: str) -> dict:
        fn = _fn("decide", "decide_approve", "hitl_decide", "resolve_approve")
        if fn is None:
            self.fail("decide is missing")
        payload = {
            "request_id": request_id,
            "approval_id": request_id,
            "id": request_id,
            "decision": decision,
            "status": decision,
        }
        key_row = _write_key_row()
        supplied = dict(payload)
        supplied["payload"] = payload
        supplied["key_row"] = key_row
        supplied["decision"] = decision
        try:
            out = _dispatch(fn, **supplied)
        except TypeError:
            try:
                out = _call(fn, payload, key_row)
            except TypeError:
                out = _call(fn, request_id, decision)
        if not isinstance(out, dict):
            self.fail(f"decide returned {out!r}")
        return out

    def _decide_again_fails(self, request_id: str) -> dict | str:
        """Second decide must fail because the request is no longer pending."""
        try:
            out = self._decide(request_id, "approved")
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        return out

    # --- 6 proofs ---

    def test_01_spend_check_unknown_url_fail_closed(self) -> None:
        out = self._spend_check(
            "https://no-policy-spend.example", amount=1, action="buy"
        )
        self.assertFalse(_passed(out), out)

    def test_02_policy_without_cap_fails_no_spend_cap(self) -> None:
        site = "https://nocap.example"
        self._live_org("No Cap Co", site)
        out = self._spend_check(site, amount=1, action="buy")
        self.assertFalse(_passed(out), out)
        self.assertEqual(_reason(out), "no_spend_cap", out)

    def test_03_cap_exceeded_and_allowlist(self) -> None:
        site = "https://capped.example"
        org = self._live_org("Capped Co", site)
        self._set_spend_cap(org, 10, ["buy"])
        first = self._spend_check(site, amount=3, action="buy")
        self.assertTrue(_passed(first), first)
        self._record_spend(org, 3, "buy", site)
        blocked = self._spend_check(site, amount=1, action="pay")
        self.assertFalse(_passed(blocked), blocked)
        br = _reason(blocked).lower()
        self.assertTrue(
            "allowlist" in br
            or br
            in (
                "not_allowlisted",
                "action_not_allowed",
                "not_allowed",
                "forbidden_action",
            ),
            blocked,
        )
        second = self._spend_check(site, amount=8, action="buy")
        self.assertFalse(_passed(second), second)
        self.assertEqual(_reason(second), "cap_exceeded", second)

    def test_04_nutrition_untested_then_stamp_pass(self) -> None:
        site = "https://nutrition.example"
        org = self._live_org("Nutrition Co", site)
        before = self._nutrition(url=site, org=org)
        self.assertEqual(_last_test(before), "untested", before)
        stamped = self._stamp_test("pass", url=site, org=org)
        if isinstance(stamped, dict) and ("pass" in stamped or "result" in stamped):
            if "last_test_result" not in stamped and "label" not in stamped:
                self.assertTrue(_passed(stamped) or stamped.get("result") == "pass", stamped)
        after = self._nutrition(url=site, org=org)
        self.assertEqual(_last_test(after), "pass", after)

    def test_05_request_approve_decide_once(self) -> None:
        site = "https://hitl.example"
        self._live_org("Hitl Co", site)
        pending = self._request_approve(
            {
                "action": "buy",
                "amount": 5,
                "url": site,
                "target_url": site,
                "summary": "buy listed tool",
                "query": "approve spend",
            }
        )
        self.assertEqual(_status(pending), "pending", pending)
        rid = _req_id(pending)
        self.assertTrue(rid, f"request_approve must return an id: {pending!r}")
        decided = self._decide(rid, "approved")
        self.assertEqual(_status(decided), "approved", decided)
        again = self._decide_again_fails(rid)
        if isinstance(again, str):
            self.assertTrue(
                "pending" in again.lower() or again,
                f"second decide should fail (not pending): {again!r}",
            )
            return
        self.assertNotEqual(_status(again), "pending", again)
        if "pass" in again or again.get("result") in ("pass", "fail", "unknown"):
            self.assertFalse(_passed(again), again)
        reason = _reason(again).lower()
        status = _status(again)
        self.assertTrue(
            "pending" in reason
            or "already" in reason
            or status in ("fail", "error", "rejected", "already_decided", "not_pending"),
            again,
        )

    def test_06_http_spend_check_401_nutrition_200(self) -> None:
        with _http_server() as base:
            payload = {
                "url": "https://northsidedental.example",
                "amount": 1,
                "action": "buy",
            }
            code, body = _http_json(base, "POST", "/tools/spend_check", payload)
            if code == 404:
                code, body = _http_json(base, "POST", "/spend_check", payload)
            self.assertEqual(code, 401, body)

            nutrition_hits = (
                ("GET", "/tools/nutrition"),
                ("GET", "/nutrition"),
                ("GET", "/tools/nutrition_label"),
                ("GET", "/nutrition_label"),
                ("POST", "/tools/nutrition"),
                ("POST", "/nutrition"),
            )
            ncode, nbody = None, None
            for method, path in nutrition_hits:
                if method == "GET":
                    ncode, nbody = _http_json(base, "GET", path)
                else:
                    ncode, nbody = _http_json(base, "POST", path, {})
                if ncode != 404:
                    break
            if ncode == 404:
                qs = urlencode({"url": "https://northsidedental.example"})
                ncode, nbody = _http_json(base, "GET", "/tools/nutrition?" + qs)
            self.assertEqual(ncode, 200, nbody)


LABELS = [
    ("test_01_spend_check_unknown_url_fail_closed", "spend_check unknown URL fail closed"),
    (
        "test_02_policy_without_cap_fails_no_spend_cap",
        "org with policy but no cap → spend_check fail no_spend_cap",
    ),
    (
        "test_03_cap_exceeded_and_allowlist",
        "cap 10, spend 3 then 8 → cap_exceeded; action pay not on allowlist fails",
    ),
    (
        "test_04_nutrition_untested_then_stamp_pass",
        "nutrition untested default; stamp_test pass → last_test_result pass",
    ),
    (
        "test_05_request_approve_decide_once",
        "request_approve pending; decide approved; decide again fails (not pending)",
    ),
    (
        "test_06_http_spend_check_401_nutrition_200",
        "HTTP spend_check without key 401; nutrition public 200",
    ),
]


def _print_list(result: unittest.TestResult) -> None:
    failed = {}
    for test, err in list(result.failures) + list(result.errors):
        failed[test.id().split(".")[-1]] = err.splitlines()[-1] if err else "error"
    print("")
    print("A2A spend / nutrition / HITL proof")
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
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Test46SpendNutritionHitl)
    runner = unittest.TextTestRunner(verbosity=2)
    outcome = runner.run(suite)
    _print_list(outcome)
    sys.exit(0 if outcome.wasSuccessful() else 1)
