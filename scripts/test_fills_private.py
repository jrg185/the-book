"""Fills must not ride a public Actions input, and logs must not print order ids."""

from __future__ import annotations

import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import sync_rh_kpi_trades as sync
import upsert_fills_direct

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
ORDER_ID = "11111111-1111-4111-8111-111111111111"
# Echoing a workflow input into the public log. A mention of the mapper is not an echo.
ECHO_INPUT = re.compile(
    r"(?i)("
    r"echo\b[^\n]*(\$\{\{\s*github\.event|github\.event\.inputs|sync_rh_json|SYNC_RH_JSON|client_payload)"
    r"|\$\{\{\s*github\.event\.inputs"
    r"|\$\{\{\s*toJson\(github\.event"
    r"|\$\{\{\s*toJSON\(github\.event"
    r"|print\([^\n]{0,240}(github\.event\.inputs|SYNC_RH_JSON|sync_rh_json)"
    r")"
)


def _fill(order_id: str = ORDER_ID) -> dict:
    return {
        "id": order_id,
        "currency_code": "GRT",
        "side": "buy",
        "state": "filled",
        "cumulative_quantity": "100",
        "average_price": "0.05",
        "fee": "0.01",
        "created_at": "2026-09-28T18:00:00Z",
    }


class FillsPrivateTests(unittest.TestCase):
    def test_workflow_and_scripts_do_not_echo_inputs(self):
        targets = [
            ROOT / ".github" / "workflows" / "export-kpi.yml",
            ROOT / "scripts" / "export-kpi.yml",
        ]
        targets.extend(sorted((ROOT / "scripts").glob("*.py")))
        targets.extend(sorted((ROOT / "scripts").glob("*.sh")))
        targets.extend(sorted((ROOT / ".github" / "workflows").glob("*.yml")))
        seen = set()
        for path in targets:
            if path in seen or not path.is_file() or path.name == "test_fills_private.py":
                continue
            seen.add(path)
            text = path.read_text(encoding="utf-8")
            match = ECHO_INPUT.search(text)
            self.assertIsNone(match, f"{path} echoes a workflow input: {match.group(0) if match else ''}")
        for relative in (".github/workflows/export-kpi.yml", "scripts/export-kpi.yml"):
            workflow = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("sync_rh_json", workflow, relative)
            self.assertNotIn("github.event.inputs", workflow, relative)
            self.assertIn("does not accept a fills payload", workflow, relative)
            self.assertIn("test_fills_private.py", workflow, relative)
        script = (ROOT / "scripts" / "upsert_fills_direct.py").read_text(encoding="utf-8")
        for line in script.splitlines():
            if "print(" in line:
                self.assertNotIn("order_id", line)
                self.assertIsNone(UUID_RE.search(line), line)

    def test_direct_upsert_prints_counts_and_is_idempotent(self):
        stored: list[dict] = []

        def load_trades(_env):
            return [dict(row) for row in stored], "rest"

        def upsert_rows(_env, rows, _source):
            for row in rows:
                stored.append(dict(row))

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "fills.json"
            path.write_text(json.dumps({"results": [_fill()]}), encoding="utf-8")
            env = {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "test-service-role",
                "SUPABASE_DB_URL": "",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch.object(sync, "load_trades", load_trades),
                mock.patch.object(sync, "upsert_rows", upsert_rows),
                mock.patch.object(sync, "remember_cursor"),
            ):
                out = io.StringIO()
                err = io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    first = upsert_fills_direct.main([str(path)])
                    second = upsert_fills_direct.main([str(path)])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertIn("upserted=1", text)
        self.assertIn("upserted=0", text)
        self.assertNotIn(ORDER_ID, text)
        self.assertIsNone(UUID_RE.search(text), text)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["order_id"], ORDER_ID)
        self.assertEqual(stored[0]["ticker"], "GRT")

    def test_direct_upsert_error_does_not_print_the_order_id(self):
        bad = _fill()
        bad["asset_class"] = "option"
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "fills.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            env = {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "test-service-role",
                "SUPABASE_DB_URL": "",
            }
            with (
                mock.patch.dict(os.environ, env, clear=False),
                mock.patch.object(sync, "load_trades", return_value=([], "rest")),
                mock.patch.object(sync, "remember_cursor"),
            ):
                out = io.StringIO()
                err = io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = upsert_fills_direct.main([str(path)])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("not crypto or equities", text)
        self.assertNotIn(ORDER_ID, text)
        self.assertIsNone(UUID_RE.search(text), text)
        redacted = sync.public_sync_message(f"filled order {ORDER_ID} side 'nope'")
        self.assertNotIn(ORDER_ID, redacted)
        self.assertIn("[redacted]", redacted)


class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8") if body is not None else b""

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


SELL_ID = "22222222-2222-4222-8222-222222222222"


def _stored_buy() -> dict:
    return {
        "sleeve": "crypto", "ticker": "GRT", "side": "buy", "qty": "100",
        "avg_price": "0.05", "timestamp_et": "2026-09-28T14:00:00-04:00",
        "why": None, "pnl_trade_usd": "0", "order_id": ORDER_ID, "fee_usd": "0",
    }


def _sell_fill() -> dict:
    fill = _fill(SELL_ID)
    fill.update({"side": "sell", "average_price": "0.07", "fee": "0", "created_at": "2026-09-29T18:00:00Z"})
    return fill


class RestBackendTests(unittest.TestCase):
    ENV = {
        "SIGNALS_SUPABASE_SERVICE_ROLE_KEY": "signals-test-key",
        "SUPABASE_SERVICE_ROLE_KEY": "other-project-key",
        "SUPABASE_DB_URL": "postgres://placeholder",
        "SUPABASE_URL": "",
    }

    def test_env_prefers_signals_key_and_drops_placeholder_db_url(self):
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            env = sync.env_values()
        self.assertEqual(env["SUPABASE_SERVICE_ROLE_KEY"], "signals-test-key")
        self.assertEqual(env["SUPABASE_DB_URL"], "")
        self.assertIn("bsnqwgbshwszbjncglqx", sync.DEFAULT_URL)

    def test_valid_db_url(self):
        self.assertTrue(sync.valid_db_url("postgresql://u:pw@db.example.co:5432/postgres"))
        for bad in ("", "postgres://placeholder", "https://x.co", "postgres://u@h/db",
                    "postgresql://u:[YOUR-PASSWORD]@h/db", "not a url"):
            self.assertFalse(sync.valid_db_url(bad), bad)

    def test_sync_upserts_over_rest_with_merge_duplicates(self):
        calls = []

        def urlopen(request, timeout=0):
            calls.append(request)
            if request.get_method() == "GET":
                return _Resp([_stored_buy()])
            return _Resp(None)

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "fills.json"
            path.write_text(json.dumps([_sell_fill()]), encoding="utf-8")
            with (
                mock.patch.dict(os.environ, self.ENV, clear=False),
                mock.patch.object(sync.urllib.request, "urlopen", urlopen),
                mock.patch.object(sync, "apply_migration", side_effect=AssertionError("no psycopg")),
                mock.patch.object(sync, "remember_cursor"),
            ):
                out = io.StringIO()
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    code = upsert_fills_direct.main([str(path)])
        self.assertEqual(code, 0)
        self.assertIn("warehouse=rest", out.getvalue())
        self.assertIsNone(UUID_RE.search(out.getvalue()))
        posts = [c for c in calls if c.get_method() == "POST"]
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertTrue(post.full_url.startswith(sync.DEFAULT_URL + "/rest/v1/kpi_trades?on_conflict=order_id"))
        self.assertIn("resolution=merge-duplicates", post.get_header("Prefer"))
        self.assertEqual(post.get_header("Apikey"), "signals-test-key")
        body = json.loads(post.data.decode("utf-8"))
        self.assertEqual(body[0]["order_id"], SELL_ID)
        self.assertEqual(sync.dec(body[0]["pnl_trade_usd"]), sync.dec("2"))

    def test_dry_run_reads_rest_and_reprices_without_ids(self):
        rows = [_stored_buy(), {**_stored_buy(), "side": "sell", "avg_price": "0.07",
                                "timestamp_et": "2026-09-29T14:00:00-04:00",
                                "pnl_trade_usd": "2", "order_id": SELL_ID}]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "fills.json"
            path.write_text(json.dumps([_sell_fill()]), encoding="utf-8")
            env = {"SUPABASE_SERVICE_ROLE_KEY": "k", "SUPABASE_DB_URL": "", "SUPABASE_URL": ""}
            with mock.patch.object(sync, "fetch_trades_rest", return_value=rows):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = upsert_fills_direct.main([str(path), "--dry-run", "--reprice"], env=env)
                plain = io.StringIO()
                with redirect_stdout(plain):
                    upsert_fills_direct.main([str(path), "--dry-run"], env=env)
        self.assertEqual(code, 0)
        self.assertIn("upserted=1", out.getvalue())
        self.assertIn("pnl=2", out.getvalue())
        self.assertIn("upserted=0", plain.getvalue())
        self.assertIsNone(UUID_RE.search(out.getvalue() + plain.getvalue()))

    def test_public_message_redacts_signals_key(self):
        with mock.patch.dict(os.environ, self.ENV, clear=False):
            self.assertNotIn("signals-test-key", sync.public_sync_message("bad signals-test-key"))


if __name__ == "__main__":
    unittest.main()
