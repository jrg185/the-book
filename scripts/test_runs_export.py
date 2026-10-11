"""Public scoring-run export stays free of dollars, order ids, and account fields."""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_runs

ROOT = Path(__file__).resolve().parents[1]


def dirty_rows():
    run = {
        "id": "run-1",
        "model_version_id": "ver-1",
        "backend": "ml",
        "mode": "live",
        "generated_at": "2026-10-11T16:00:00Z",
        "last_bar_time": "2026-10-11T15:00:00Z",
        "anomaly": False,
        "fill_trade_id": "private-fill",
        "order_id": "private-order",
        "account_id": "private-account",
        "account_number": "123456",
        "pnl_usd": 12.5,
        "note": "cost $4",
    }
    older = {
        "id": "run-0",
        "model_version_id": "ver-1",
        "backend": "rules",
        "mode": "shadow",
        "generated_at": "2026-10-10T16:00:00Z",
        "last_bar_time": "2026-10-10T15:00:00Z",
        "anomaly": True,
    }
    predictions = [
        {
            "run_id": "run-1",
            "symbol": "BTC",
            "p_up": 0.8,
            "side": "long",
            "conviction": 0.2,
            "bar_time": "2026-10-11T15:00:00Z",
            "expires_at": "2026-10-11T23:00:00Z",
            "fwd_ret_8h": 0.01,
            "label_up_net30": True,
            "resolved_at": "2026-10-11T23:00:00Z",
            "traded": "true",
            "fill_trade_id": "ft",
            "order_id": "oid",
            "realized_pnl_usd": 3,
            "account_number": "999",
        },
        {
            "run_id": "run-1",
            "symbol": "ETH",
            "p_up": 0.8,
            "side": "short",
            "conviction": 0.1,
            "bar_time": "2026-10-11T15:00:00Z",
            "expires_at": "2026-10-11T23:00:00Z",
            "fwd_ret_8h": -0.02,
            "label_up_net30": False,
            "resolved_at": "2026-10-11T23:00:00Z",
            "traded": 0,
        },
        {
            "run_id": "run-0",
            "symbol": "SOL",
            "p_up": 0.2,
            "side": "flat",
            "conviction": 0,
            "bar_time": "2026-10-10T15:00:00Z",
            "expires_at": "2026-10-10T23:00:00Z",
            "fwd_ret_8h": 0,
            "label_up_net30": False,
            "resolved_at": "2026-10-10T23:00:00Z",
            "traded": None,
        },
    ]
    version = {
        "id": "ver-1",
        "name": "candidate$one",
        "parent": None,
        "status": "candidate",
        "oos_metrics": {"auc": 0.53, "logloss": 0.69, "fee_usd": 1.25, "note": "cost $1"},
        "trained_at": "2026-10-01T00:00:00Z",
        "promoted_at": None,
        "fill_trade_id": "nope",
        "order_id": "nope",
        "account_id": "nope",
    }
    return [run, older], predictions, [version]


class RunsExportTests(unittest.TestCase):
    def test_uuid_ids_are_never_published(self):
        raw = "6ab90000-1111-4222-8333-944455556666"
        runs, predictions, versions = dirty_rows()
        runs[0]["id"] = raw
        for row in predictions:
            if row.get("run_id") == "run-1":
                row["run_id"] = raw
        versions[0]["id"] = raw.replace("6ab9", "7bc0")
        runs[0]["model_version_id"] = versions[0]["id"]
        bundle = export_runs.build_bundle(runs, predictions, versions, "2026-10-11T17:00:00Z")
        blob = json.dumps(bundle)
        import export_kpi
        self.assertIsNone(export_kpi.UUID_RE.search(blob))
        self.assertTrue(any(row["run_id"] == bundle["runs"][0]["id"] for row in bundle["predictions"]))
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "runs.json").write_text(json.dumps({"runs": [{"id": raw}]}), encoding="utf-8")
            (folder / "other.json").write_text(json.dumps({"x": "00000000-0000-4000-8000-000000000001"}), encoding="utf-8")
            flagged = export_kpi.published_identifier_problems(folder)
            self.assertTrue(any(item.startswith("runs.json") for item in flagged))
            self.assertFalse(any(item.startswith("other.json") for item in flagged))
            (folder / "other.json").write_text(json.dumps({"x": raw}), encoding="utf-8")
            self.assertTrue(any(item.startswith("other.json") for item in export_kpi.published_identifier_problems(folder)))


    def test_bundle_strips_private_fields_and_scores_resolved_rows(self):
        runs, predictions, versions = dirty_rows()
        bundle = export_runs.build_bundle(runs, predictions, versions, "2026-10-11T17:00:00Z")
        run_key = export_runs.public_key("run-1", export_runs.RUN_KEY_LABEL, "run")
        self.assertEqual(bundle["runs"][0]["id"], run_key)
        self.assertRegex(run_key, r"^run_[0-9a-f]{12}$")
        self.assertNotIn("run-1", json.dumps(bundle))
        self.assertTrue(any(row["run_id"] == run_key for row in bundle["predictions"]))
        self.assertTrue(all(row["run_id"].startswith("run_") for row in bundle["predictions"]))
        self.assertEqual(
            bundle["runs"][0]["model_version_id"],
            bundle["model_versions"][0]["id"],
        )
        self.assertEqual(bundle["runs"][0]["model_version"], "candidateone")
        self.assertIs(bundle["runs"][0]["anomaly"], False)
        self.assertIs(bundle["predictions"][0]["traded"], True)
        self.assertIs(bundle["predictions"][1]["traded"], False)
        traded = [row["traded"] for row in bundle["predictions"]]
        self.assertTrue(all(isinstance(value, bool) for value in traded))
        metrics = bundle["model_versions"][0]["oos_metrics"]
        self.assertEqual(metrics["auc"], 0.53)
        self.assertNotIn("fee_usd", metrics)
        self.assertNotIn("$", metrics["note"])
        blob = json.dumps(bundle)
        self.assertNotIn("$", blob)
        self.assertNotIn("fill_trade_id", blob)
        self.assertNotIn("order_id", blob)
        self.assertNotIn("account", blob.lower())
        self.assertNotIn("usd", blob.lower())
        calibration = bundle["calibration"]
        self.assertEqual(calibration["n_resolved"], 3)
        self.assertAlmostEqual(calibration["brier"], 0.24, places=6)
        occupied = [slot for slot in calibration["bins"] if slot["n"]]
        self.assertEqual(sorted(slot["n"] for slot in occupied), [1, 2])
        high = next(slot for slot in calibration["bins"] if slot["lo"] == 0.8)
        self.assertEqual(high["n"], 2)
        self.assertAlmostEqual(high["label_rate"], 0.5, places=6)

    def test_missing_column_is_dropped_and_missing_relations_do_not_raise(self):
        seen = []

        def read_rows(source, columns, order, limit):
            seen.append((source, list(columns)))
            if source != "runs_public":
                raise export_runs.SourceMissing(source)
            if "model_version" in columns:
                raise export_runs.ColumnMissing("model_version", "column model_version does not exist")
            return [{"id": "r1", "backend": "rules", "generated_at": "2026-10-11T00:00:00Z", "order_id": "drop"}]

        source, rows, _notes = export_runs.read_first(
            read_rows, export_runs.RUN_SOURCES, export_runs.RUN_COLUMNS, "generated_at.desc", 10
        )
        self.assertEqual(source, "runs_public")
        self.assertEqual(rows[0]["id"], "r1")
        self.assertNotIn("model_version", seen[-1][1])

        def missing(_source, _columns, _order, _limit):
            raise export_runs.SourceMissing("absent")

        bundle, notes = export_runs.collect(missing, "2026-10-11T00:00:00Z")
        self.assertIsNone(bundle)
        self.assertTrue(notes)

    def test_publish_skips_missing_views_without_clobbering(self):
        def missing(_source, _columns, _order, _limit):
            raise export_runs.SourceMissing("absent")

        with TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "runs.json").write_text('{"source":"keep"}\n', encoding="utf-8")
            (target / "runs.csv").write_text("kind\n", encoding="utf-8")
            export_runs.publish(target, "https://example.invalid", "key", None, read_rows=missing)
            self.assertEqual((target / "runs.json").read_text(encoding="utf-8"), '{"source":"keep"}\n')
            self.assertEqual((target / "runs.csv").read_text(encoding="utf-8"), "kind\n")

    def test_publish_writes_public_files_from_dirty_rows(self):
        runs, predictions, versions = dirty_rows()

        def read_rows(source, columns, order, limit):
            del columns, order, limit
            if source == "runs_public":
                return runs
            if source == "run_predictions_public":
                return predictions
            if source == "model_versions_public":
                return versions
            raise export_runs.SourceMissing(source)

        with TemporaryDirectory() as tmp:
            target = Path(tmp)
            export_runs.publish(
                target,
                "https://example.invalid",
                read_rows=read_rows,
                now="2026-10-11T17:00:00Z",
            )
            json_path = target / "runs.json"
            csv_path = target / "runs.csv"
            self.assertEqual(export_runs.public_violations(json_path), [])
            self.assertEqual(export_runs.public_violations(csv_path), [])
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertIs(payload["predictions"][0]["traded"], True)
            self.assertIn("p_up", csv_path.read_text(encoding="utf-8").splitlines()[0])

    def test_scanner_flags_private_files(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.json"
            path.write_text('{"order_id":"x","balance_usd":1,"account_id":"a"}\n', encoding="utf-8")
            found = export_runs.public_violations(path)
            self.assertIn("order_id", found)
            self.assertTrue(any("usd" in item.lower() for item in found))
            self.assertTrue(any("account" in item.lower() for item in found))
            dirty = Path(tmp) / "runs.csv"
            dirty.write_text("kind,pnl_usd\nprediction,$5\n", encoding="utf-8")
            csv_found = export_runs.public_violations(dirty)
            self.assertIn("$", csv_found)
            self.assertTrue(any("usd" in item.lower() for item in csv_found))

    def test_committed_runs_files_stay_public(self):
        for name in ("runs.json", "runs.csv"):
            path = ROOT / "data" / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(export_runs.public_violations(path), [], path.read_text(encoding="utf-8")[:240])

    def test_check_job_publishes_and_scans_runs(self):
        exporter = (ROOT / "scripts" / "export_kpi.py").read_text(encoding="utf-8")
        self.assertIn("def publish_scoring_runs", exporter)
        self.assertIn("export_runs.publish", exporter)
        for relative in (".github/workflows/export-kpi.yml", "scripts/export-kpi.yml"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("test_runs_export.py", text)
            commit = text.split("Commit refreshed JSON", 1)[1].split("Publish export failure", 1)[0]
            self.assertIn("data/runs.json", commit)
            self.assertIn("data/runs.csv", commit)


if __name__ == "__main__":
    unittest.main()
