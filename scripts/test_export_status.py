"""Failure stamps stay on meta.json and do not rewrite KPI numbers."""

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_kpi


class ExportStatusTests(unittest.TestCase):
    def test_stamp_keeps_kpi_numbers_and_last_fetch(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp)
            summary = [{"sleeve": "crypto", "as_of": "2026-09-27T23:48:06+00:00", "running_pnl_frac": 0.079233}]
            export_kpi.write_json(target / "kpi_summary.json", summary)
            export_kpi.write_json(
                target / "meta.json",
                {
                    "source": "supabase",
                    "fetched_at": "2026-09-28T01:08:27Z",
                    "row_counts": {"kpi_summary": 3},
                },
            )
            export_kpi.stamp_export_failure(
                target,
                "error",
                "Export failed: disk full. KPI numbers were left unchanged.",
            )
            kpi = json.loads((target / "kpi_summary.json").read_text(encoding="utf-8"))
            meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(kpi, summary)
        self.assertEqual(meta["fetched_at"], "2026-09-28T01:08:27Z")
        self.assertEqual(meta["export_status"], "error")
        self.assertEqual(meta["row_counts"]["kpi_summary"], 3)
        self.assertIn("disk full", meta["export_error"])
        self.assertTrue(meta["export_attempted_at"])

    def test_failure_messages_do_not_invent_numbers_or_keep_secrets(self):
        status, message = export_kpi.failure_message(OSError(28, "No space left on device"))
        self.assertEqual(status, "error")
        self.assertIn("disk full", message)
        self.assertNotIn("0.079233", message)

        status, message = export_kpi.failure_message(OSError(30, "Read-only file system"))
        self.assertEqual(status, "error")
        self.assertIn("read-only", message)

        status, message = export_kpi.failure_message(RuntimeError("cannot execute INSERT in a read-only transaction"))
        self.assertIn("read-only", message)

        leaked = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
        status, message = export_kpi.failure_message(RuntimeError(f"REST kpi_summary HTTP 401: {leaked}"))
        self.assertEqual(status, "error")
        self.assertNotIn(leaked, message)
        self.assertNotIn("eyJ", message)
        self.assertIn("KPI numbers were left unchanged", message)

    def test_read_only_and_disk_full_name_the_frozen_snapshot(self):
        frozen = "2026-09-27T23:48:06+00:00"
        status, message, warehouse = export_kpi.classify_failure(
            RuntimeError("INSERT rejected: read-only transaction (25006). kpi_sleeve_snapshots was not updated."),
            frozen,
        )
        self.assertEqual(status, "error")
        self.assertEqual(warehouse, "read-only")
        self.assertIn("warehouse read-only — snapshot frozen at", message)
        self.assertIn(frozen, message)

        status, message, warehouse = export_kpi.classify_failure(
            RuntimeError("INSERT rejected: database disk full (53100). Snapshots were not updated."),
            frozen,
        )
        self.assertEqual(warehouse, "disk-full")
        self.assertIn("warehouse disk full — snapshot frozen at", message)
        self.assertNotIn("323.77", message)

    def test_missing_credentials_stamp_meta_only(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp)
            summary = [{"sleeve": "crypto", "as_of": "2026-09-27T23:48:06+00:00"}]
            trades = [{"sleeve": "crypto", "ticker": "QNT", "pnl_frac": 0.013266}]
            for name, payload in (
                ("kpi_summary.json", summary),
                ("kpi_trades_scrubbed.json", trades),
                ("models_oos.json", {"rows": []}),
                ("models.json", {"models": []}),
                ("meta.json", {"source": "supabase", "fetched_at": "2026-09-28T01:08:27Z"}),
            ):
                export_kpi.write_json(target / name, payload)
            env = {key: value for key, value in os.environ.items() if key not in {"SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_DB_URL"}}
            with mock.patch.object(export_kpi, "DATA", target), mock.patch.dict(os.environ, env, clear=True):
                code = export_kpi.main([])
            kpi = json.loads((target / "kpi_summary.json").read_text(encoding="utf-8"))
            tape = json.loads((target / "kpi_trades_scrubbed.json").read_text(encoding="utf-8"))
            meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(kpi, summary)
        self.assertEqual(tape, trades)
        self.assertEqual(meta["fetched_at"], "2026-09-28T01:08:27Z")
        self.assertEqual(meta["export_status"], "stale")
        self.assertIn("unset", meta["export_error"])


class TapeLedgerTests(unittest.TestCase):
    def test_committed_scrubbed_tape_gets_a_running_ledger(self):
        from decimal import Decimal

        path = Path(__file__).resolve().parents[1] / "data" / "kpi_trades_scrubbed.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        bare = []
        for row in raw:
            bare.append(
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"running_pnl_frac", "running_balance_frac"}
                }
            )
        out = export_kpi.attach_running_ledger(bare)
        crypto = sorted(
            (row for row in out if row.get("sleeve") == "crypto"),
            key=lambda row: row["timestamp_et"],
        )
        self.assertGreater(len(crypto), 1)
        last = crypto[-1]
        self.assertIsNotNone(last["running_pnl_frac"])
        self.assertIsNotNone(last["running_balance_frac"])
        self.assertAlmostEqual(last["running_balance_frac"], 1 + last["running_pnl_frac"], places=6)
        committed = [
            row for row in raw if row.get("sleeve") == "crypto"
        ]
        committed.sort(key=lambda row: row["timestamp_et"])
        # Expected tip is the last committed crypto row by timestamp_et.
        tip = committed[-1]
        self.assertEqual(last["ticker"], tip["ticker"])
        self.assertEqual(last["side"], tip["side"])
        self.assertEqual(last["timestamp_et"], tip["timestamp_et"])
        self.assertAlmostEqual(
            last["running_pnl_frac"],
            float(export_kpi.q6(Decimal(str(tip["running_pnl_frac"])))),
            places=6,
        )
        self.assertAlmostEqual(
            last["running_balance_frac"],
            float(export_kpi.q6(Decimal(str(tip["running_balance_frac"])))),
            places=6,
        )
        self.assertEqual(tip["running_pnl_frac"], last["running_pnl_frac"])
        self.assertEqual(tip["running_balance_frac"], last["running_balance_frac"])
        long_why = "n" * 240
        kept = export_kpi.attach_running_ledger(
            [
                {
                    "sleeve": "crypto",
                    "timestamp_et": "2026-09-27T00:00:00+00:00",
                    "ticker": "W",
                    "side": "buy",
                    "pnl_frac_of_book": 0,
                    "why": long_why,
                }
            ]
        )
        self.assertEqual(kept[0]["why"], long_why)
        self.assertEqual(len(kept[0]["why"]), 240)


class ScorecardTests(unittest.TestCase):
    def test_day_kill_scales_with_running_balance(self):
        summary = [
            {
                "sleeve": "crypto",
                "as_of": "2026-10-01T00:00:00Z",
                "day_kill_pct": -0.1,
                "day_target_pct": 0.025,
                "kill_headroom_frac": 0,
            }
        ]
        low = export_kpi.kill_block(summary, 719)
        high = export_kpi.kill_block(summary, 1438)
        self.assertEqual(low["day_kill_pct"], -0.1)
        self.assertEqual(low["day_kill_usd"], export_kpi.rail_usd(-0.1, 719))
        self.assertEqual(high["day_kill_usd"], export_kpi.rail_usd(-0.1, 1438))
        self.assertEqual(high["day_kill_usd"], export_kpi.js_round_cents(low["day_kill_usd"] * 2))
        self.assertNotEqual(low["day_kill_usd"], export_kpi._cents(-0.1))
        self.assertNotIn("seed_usd", low)
        self.assertIsNone(export_kpi.kill_block(summary, None)["day_kill_usd"])
        root = Path(__file__).resolve().parents[1]
        live = json.loads((root / "data" / "live_book.json").read_text(encoding="utf-8"))
        published_summary = json.loads((root / "data" / "kpi_summary.json").read_text(encoding="utf-8"))
        card = json.loads((root / "data" / "model_scorecard.json").read_text(encoding="utf-8"))
        crypto = next(row for row in published_summary if row["sleeve"] == "crypto")
        balance = export_kpi.published_running_balance(live)
        frac = export_kpi.as_fraction(crypto["day_kill_pct"])
        self.assertEqual(card["kill"]["day_kill_pct"], frac)
        self.assertTrue(export_kpi.usd_equal(card["kill"]["running_balance_usd"], balance))
        self.assertTrue(export_kpi.usd_equal(live["running_balance_usd"], balance))
        self.assertTrue(export_kpi.usd_equal(card["kill"]["day_kill_usd"], export_kpi.rail_usd(frac, balance)))
        self.assertTrue(export_kpi.usd_equal(card["kill"]["day_target_usd"], export_kpi.rail_usd(crypto["day_target_pct"], balance)))
        self.assertTrue(
            export_kpi.usd_equal(
                live["kill_remaining_usd"],
                export_kpi.kill_remaining_usd(balance, live.get("day_pnl_usd"), live.get("unrealized_pnl_usd")),
            )
        )
        self.assertTrue(export_kpi.usd_equal(card["kill"]["kill_headroom_usd"], live["kill_remaining_usd"]))
        day_clear = live.get("day_pnl_usd", 0) >= 0
        open_clear = (live.get("unrealized_pnl_usd") or 0) >= 0
        if day_clear and open_clear:
            self.assertTrue(export_kpi.usd_equal(live["kill_remaining_usd"], abs(card["kill"]["day_kill_usd"])))

    def test_half_cent_day_kill_is_restamped_from_the_written_balance(self):
        # 712.05 × 10% is a half cent. 712.1 × 10% is not. The two balances
        # are a cent apart at the tenth, and only the first splits the signed
        # product round from the shared kill round.
        edge = 712.05
        nearby = 712.1
        frac = -0.1
        self.assertTrue(export_kpi.usd_differ(edge, nearby))
        shared = export_kpi.rail_usd(frac, edge)
        signed = export_kpi.js_round_cents(frac * edge)
        self.assertNotEqual(shared, signed)
        self.assertEqual(abs(shared), export_kpi.kill_remaining_usd(edge, 0, 0))
        self.assertEqual(export_kpi.rail_usd(frac, nearby), export_kpi.js_round_cents(frac * nearby))
        summary = [
            {
                "sleeve": "crypto",
                "as_of": "2026-10-17T00:00:00Z",
                "day_kill_pct": frac,
                "day_target_pct": 0.025,
                "kill_headroom_frac": 0,
            }
        ]
        with TemporaryDirectory() as tmp:
            target = Path(tmp)
            export_kpi.write_json(target / "kpi_summary.json", summary)
            stale = export_kpi.kill_block(summary, nearby)
            stale["day_kill_usd"] = signed
            stale["running_balance_usd"] = nearby
            export_kpi.write_json(target / "model_scorecard.json", {"sleeve": "crypto", "kill": stale})
            book = {
                "running_balance_usd": edge,
                "day_pnl_usd": 0,
                "unrealized_pnl_usd": 0,
                "kill_remaining_usd": 1,
                "holdings": [{"ticker": "USD", "sleeve": "crypto", "value_usd": nearby}],
                "positions": [],
            }
            export_kpi.write_json(target / "live_book.json", book)
            written = json.loads((target / "live_book.json").read_text(encoding="utf-8"))
            export_kpi.stamp_shared_kill(target, written)
            card = json.loads((target / "model_scorecard.json").read_text(encoding="utf-8"))
            self.assertEqual(card["kill"]["running_balance_usd"], written["running_balance_usd"])
            self.assertNotEqual(card["kill"]["running_balance_usd"], nearby)
            self.assertEqual(card["kill"]["day_kill_usd"], export_kpi.rail_usd(frac, written["running_balance_usd"]))
            self.assertEqual(
                abs(card["kill"]["day_kill_usd"]),
                export_kpi.kill_remaining_usd(written["running_balance_usd"], 0, 0),
            )
            self.assertNotEqual(card["kill"]["day_kill_usd"], signed)
            self.assertEqual(card["kill"]["kill_headroom_usd"], export_kpi.kill_remaining_usd(edge, 0, 0))
            # The export write path: a stale scorecard is restamped from the
            # running balance refresh just wrote, not from the previous tenth.
            export_kpi.write_json(
                target / "model_scorecard.json",
                {"sleeve": "crypto", "kill": stale},
            )
            export_kpi.write_json(
                target / "live_book.json",
                {
                    "running_balance_usd": nearby,
                    "day_pnl_usd": 0,
                    "unrealized_pnl_usd": 0,
                    "kill_remaining_usd": 1,
                    "holdings": [{"ticker": "USD", "sleeve": "crypto", "value_usd": edge}],
                    "positions": [],
                },
            )
            refreshed = export_kpi.refresh_live_book(target, fetch=False, cash=None)
            self.assertEqual(refreshed["running_balance_usd"], edge)
            restamped = json.loads((target / "model_scorecard.json").read_text(encoding="utf-8"))
            self.assertEqual(restamped["kill"]["running_balance_usd"], refreshed["running_balance_usd"])
            self.assertEqual(restamped["kill"]["day_kill_usd"], export_kpi.rail_usd(frac, refreshed["running_balance_usd"]))
            self.assertNotEqual(restamped["kill"]["day_kill_usd"], signed)
            self.assertEqual(
                restamped["kill"]["kill_headroom_usd"],
                refreshed["kill_remaining_usd"],
            )

    def test_committed_crypto_scorecard_matches_exported_json(self):
        root = Path(__file__).resolve().parents[1]
        summary = json.loads((root / "data" / "kpi_summary.json").read_text(encoding="utf-8"))
        trades = json.loads((root / "data" / "kpi_trades_scrubbed.json").read_text(encoding="utf-8"))
        models = json.loads((root / "data" / "models.json").read_text(encoding="utf-8"))
        oos = json.loads((root / "data" / "models_oos.json").read_text(encoding="utf-8"))
        meta = json.loads((root / "data" / "meta.json").read_text(encoding="utf-8"))
        live = json.loads((root / "data" / "live_book.json").read_text(encoding="utf-8"))
        balance = export_kpi.published_running_balance(live)
        built = export_kpi.build_model_scorecard(
            summary,
            trades,
            models,
            oos,
            signal_linkage=meta.get("signal_linkage"),
            running_balance=balance,
        )
        committed = json.loads((root / "data" / "model_scorecard.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, built)
        self.assertEqual(built["live_backend"]["id"], "rules")
        self.assertEqual(built["live_backend"]["cli"], "--backend rules")
        self.assertEqual(built["live_backend"]["promoted_model"], "lgbm")
        self.assertIs(built["live_backend"]["promoted_in_use"], False)
        fills = built["closed_fills"]
        for key in ("wins", "losses", "flats", "decided", "deduped"):
            self.assertIn(key, fills)
            self.assertIsInstance(fills[key], int)
            self.assertNotIsInstance(fills[key], bool)
            self.assertGreaterEqual(fills[key], 0)
        # Flat zeros are excluded from decided, same as sleeve Win %.
        self.assertEqual(fills["wins"] + fills["losses"], fills["decided"])
        self.assertEqual(fills["dedupe"], "scrubbed-rows")
        self.assertIsInstance(fills["expectancy_usd"], (int, float))
        self.assertNotIsInstance(fills["expectancy_usd"], bool)
        self.assertEqual(fills["wins"], committed["closed_fills"]["wins"])
        self.assertEqual(fills["losses"], committed["closed_fills"]["losses"])
        self.assertEqual(fills["expectancy_usd"], committed["closed_fills"]["expectancy_usd"])
        fees = built["fee_drag"]
        self.assertEqual(fees["status"], "known")
        for key in ("fee_usd", "sell_fee_usd", "fee_frac"):
            self.assertIsInstance(fees[key], (int, float))
            self.assertNotIsInstance(fees[key], bool)
            self.assertGreaterEqual(fees[key], 0)
            self.assertEqual(fees[key], committed["fee_drag"][key])
        self.assertIsInstance(fees["n"], int)
        self.assertNotIsInstance(fees["n"], bool)
        self.assertGreaterEqual(fees["n"], 0)
        self.assertEqual(fees["n"], committed["fee_drag"]["n"])
        self.assertIn("Measured live fee", fees["note"])
        self.assertIn("T24d", fees["note"])
        link = built["signal_linkage"]
        self.assertEqual(link["status"], "known")
        for key in ("artifact_count", "outcome_count"):
            self.assertIsInstance(link[key], int)
            self.assertNotIsInstance(link[key], bool)
            self.assertGreaterEqual(link[key], 0)
            self.assertEqual(link[key], committed["signal_linkage"][key])
        stamp = link.get("last_generated_at")
        self.assertIsInstance(stamp, str)
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertEqual(stamp, committed["signal_linkage"]["last_generated_at"])
        self.assertEqual(built["oos"]["fee_bps"], 30)
        self.assertIn("95 bps/leg", built["oos"]["note"])
        self.assertIn("190 RT", built["oos"]["note"])
        self.assertIn("T24d will set FEE_BPS", built["oos"]["note"])
        self.assertEqual([row["model"] for row in built["oos"]["models"]], ["rules", "logistic", "lgbm"])
        self.assertEqual(built["oos"]["models"][2]["promoted"], True)
        crypto = next(row for row in summary if row["sleeve"] == "crypto")
        self.assertEqual(built["kill"]["kill_headroom_stored"], crypto["kill_headroom_frac"])
        self.assertEqual(built["kill"]["kill_headroom_frac"], export_kpi.as_fraction(crypto["kill_headroom_frac"]))
        self.assertEqual(built["kill"]["kill_headroom_usd"], committed["kill"]["kill_headroom_usd"])
        self.assertEqual(
            built["kill"]["kill_headroom_usd"],
            export_kpi.rail_usd(built["kill"]["kill_headroom_frac"], balance),
        )
        self.assertEqual(built["kill"]["day_kill_pct"], crypto["day_kill_pct"])
        self.assertEqual(built["kill"]["day_target_pct"], crypto["day_target_pct"])
        self.assertEqual(built["kill"]["day_kill_usd"], export_kpi.rail_usd(crypto["day_kill_pct"], balance))
        self.assertEqual(built["kill"]["day_target_usd"], export_kpi.rail_usd(crypto["day_target_pct"], balance))
        self.assertNotIn("seed_usd", built["kill"])
        self.assertTrue(export_kpi.usd_equal(built["kill"]["running_balance_usd"], live["running_balance_usd"]))
        self.assertTrue(
            export_kpi.usd_equal(
                live["kill_remaining_usd"],
                export_kpi.kill_remaining_usd(balance, live.get("day_pnl_usd"), live.get("unrealized_pnl_usd")),
            )
        )
        self.assertTrue(export_kpi.usd_equal(built["kill"]["kill_headroom_usd"], live["kill_remaining_usd"]))
        blob = json.dumps(built)
        self.assertNotIn('"order_id"', blob)
        self.assertNotIn("should-not-survive", blob)
        for row in built["oos"]["models"]:
            self.assertNotIn("sleeve_ir_vs_spy", row)
            self.assertIn("ir_vs_btc", row)

    def test_write_bundle_emits_scorecard_without_order_ids(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp)
            export_kpi.write_json(
                target / "models.json",
                {
                    "as_of": "2026-09-28T00:16:00Z",
                    "note": "The CLI stays --backend rules.",
                    "models": [{"sleeve": "crypto", "used": "stays --backend rules"}],
                },
            )
            export_kpi.write_json(
                target / "models_oos.json",
                {
                    "updated_at": "2026-09-28T00:16:00Z",
                    "rows": [
                        {
                            "sleeve": "crypto",
                            "model": "lgbm",
                            "promoted": True,
                            "auc": 0.53,
                            "after_cost_mean": 0.0028,
                            "sleeve_ir_vs_spy": 0.1113,
                            "n_long": 3,
                        }
                    ],
                },
            )
            export_kpi.write_bundle(
                target,
                {
                    "kpi_summary": [
                        {
                            "sleeve": "crypto",
                            "as_of": "2026-10-01T00:00:00Z",
                            "kill_headroom_frac": 1.1,
                            "day_kill_pct": -0.1,
                            "day_target_pct": 0.025,
                        }
                    ],
                    "kpi_trades_scrubbed": [
                        {
                            "sleeve": "crypto",
                            "side": "sell",
                            "pnl_frac_of_book": 0.01,
                            "order_id": "should-not-survive",
                        }
                    ],
                    "meta": {"fetched_at": "2026-10-01T00:00:00Z"},
                    "models_oos": None,
                },
            )
            card = json.loads((target / "model_scorecard.json").read_text(encoding="utf-8"))
        self.assertEqual(card["live_backend"]["id"], "rules")
        self.assertEqual(card["closed_fills"]["wins"], 1)
        self.assertEqual(card["closed_fills"]["deduped"], 0)
        self.assertEqual(card["fee_drag"]["status"], "unknown")
        blob = json.dumps(card)
        self.assertNotIn("should-not-survive", blob)
        self.assertNotIn('"order_id"', blob)
        self.assertNotIn("order_id", blob)


class PublishedIdentifierTests(unittest.TestCase):
    def test_published_data_has_no_order_or_account_identifiers(self):
        root = Path(__file__).resolve().parents[1] / "data"
        problems = export_kpi.published_identifier_problems(root)
        self.assertEqual(problems, [])
        scorecard = json.loads((root / "model_scorecard.json").read_text(encoding="utf-8"))
        blob = json.dumps(scorecard)
        self.assertNotIn("order_id", blob)
        self.assertNotIn("fill_trade_id", blob)
        self.assertNotIn("account_number", blob)
        self.assertNotIn("rhs_account_number", blob)
        self.assertNotIn("order_id_unique", scorecard)
        self.assertNotIn("order_id_available", scorecard.get("closed_fills", {}))

    def test_scanner_flags_keys_order_uuids_and_account_numbers(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "bad.json").write_text(
                json.dumps(
                    {
                        "order_id": "6ab90000-0000-4000-8000-000000000004",
                        "nested": {
                            "fill_trade_id": "fill-1",
                            "Account": "desk",
                            "account_number": "123456789",
                            "rhs_account_number": "RHACCT1234",
                        },
                        "why": "sync order 6ab90000-0000-4000-8000-000000000099",
                        "ref": "123456789",
                    }
                ),
                encoding="utf-8",
            )
            (folder / "bad.csv").write_text(
                "ticker,order_id,note\nBTC,6ab90000-0000-4000-8000-0000000000bb,held\n",
                encoding="utf-8",
            )
            (folder / "ok.json").write_text(
                json.dumps(
                    {
                        "sleeve": "crypto",
                        "win_rate": 0.484848,
                        "id": "rules",
                        "model_run": "run_6ab900000000",
                        "redacted": "00000000-0000-4000-8000-000000000001",
                    }
                ),
                encoding="utf-8",
            )
            problems = export_kpi.published_identifier_problems(folder)
        blob = "\n".join(problems)
        for name in ("order_id", "fill_trade_id", "Account", "account_number", "rhs_account_number"):
            self.assertIn(name, blob)
        self.assertIn("order uuid", blob)
        self.assertIn("ref", blob)
        self.assertIn("account number", blob)
        self.assertIn("bad.csv", blob)
        self.assertNotIn("ok.json", blob)

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "note.json").write_text(
                json.dumps({"note": "book RH-ACCT-99 stays private"}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("RH_AGENTIC_ACCOUNT", None)
                self.assertEqual(export_kpi.published_identifier_problems(folder), [])
            with mock.patch.dict(os.environ, {"RH_AGENTIC_ACCOUNT": "RH-ACCT-99"}):
                flagged = export_kpi.published_identifier_problems(folder)
        self.assertEqual(flagged, ["note.json: note: account number"])
        source = Path(export_kpi.__file__).read_text(encoding="utf-8")
        self.assertIn('os.environ.get("RH_AGENTIC_ACCOUNT")', source)
        self.assertNotIn("RH-ACCT-99", source)


if __name__ == "__main__":
    unittest.main()
