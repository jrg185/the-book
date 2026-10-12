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


if __name__ == "__main__":
    unittest.main()
