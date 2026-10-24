"""Public scoring-run export stays free of dollars, order ids, and account fields."""

import json
import sys
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

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
    def test_full_precision_calibration_uses_payload_rows_only(self):
        rows = [
            {"p_up": 0.9, "label_up_net30": True, "p_up_source": "payload"},
            {"p_up": 0.2, "label_up_net30": False, "p_up_source": "payload"},
            {"p_up": 0.5, "label_up_net30": True, "p_up_source": "thesis_2dp"},
            {"p_up": None, "label_up_net30": True, "p_up_source": "payload"},
        ]
        full = export_runs.full_precision_calibration(rows)
        self.assertEqual(full["n_scored"], 2)
        self.assertAlmostEqual(full["brier"], (0.1 ** 2 + 0.2 ** 2) / 2, places=6)
        self.assertEqual(set(full), {"n_scored", "logloss", "brier"})
        main = export_runs.calibrate(rows)
        self.assertEqual(main["n_scored"], 3)
        self.assertIsNone(export_runs.full_precision_calibration([{"p_up": 0.5, "label_up_net30": True}]))

    def test_calibration_covers_all_predictions_not_just_exported(self):
        runs, predictions, versions = dirty_rows()
        extra = []
        for index in range(2500):
            extra.append({
                "run_id": "run-old-%d" % (index % 7),
                "symbol": "BTC",
                "bar_time": "2026-09-01T%02d:00:00Z" % (index % 24),
                "p_up": None if index % 3 == 0 else 0.6,
                "label_up_net30": index % 2 == 0,
                "resolved_at": "2026-09-02T00:00:00Z",
            })
        extra.append({"run_id": "run-old-0", "symbol": "ETH", "p_up": 0.4, "label_up_net30": None})
        every = predictions + extra
        calls = []

        def read_rows(source, columns, order, limit):
            calls.append((source, tuple(columns), limit))
            if source in ("runs_public",):
                return runs
            if source == "run_predictions_public":
                rows = every if "p_up" in columns and len(columns) <= len(export_runs.CALIBRATION_COLUMNS) else every
                return rows[:limit]
            if source == "model_versions_public":
                return versions
            raise export_runs.SourceMissing(source)

        old_limit = export_runs.PREDICTION_LIMIT
        export_runs.PREDICTION_LIMIT = 10
        try:
            bundle, _ = export_runs.collect(read_rows, "2026-10-11T17:00:00Z")
        finally:
            export_runs.PREDICTION_LIMIT = old_limit
        calibration = bundle["calibration"]
        expected_resolved = sum(1 for row in every if isinstance(row.get("label_up_net30"), bool))
        expected_scored = sum(1 for row in every if isinstance(row.get("label_up_net30"), bool) and row.get("p_up") is not None)
        self.assertEqual(calibration["scope"], "all_predictions")
        self.assertEqual(calibration["n_resolved"], expected_resolved)
        self.assertEqual(calibration["n_scored"], expected_scored)
        self.assertEqual(calibration["n_unscored"], expected_resolved - expected_scored)
        self.assertEqual(sum(slot["n"] for slot in calibration["bins"]), expected_scored)
        self.assertLessEqual(len(bundle["predictions"]), 10)
        self.assertGreater(calibration["n_resolved"], len(bundle["predictions"]))
        self.assertTrue(any(limit == export_runs.CALIBRATION_LIMIT for _, _, limit in calls))


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
        self.assertEqual(calibration["n_scored"], 3)
        self.assertAlmostEqual(calibration["brier"], 0.24, places=6)
        occupied = [slot for slot in calibration["bins"] if slot["n"]]
        self.assertEqual(sorted(slot["n"] for slot in occupied), [1, 2])
        high = next(slot for slot in calibration["bins"] if slot["lo"] == 0.8)
        self.assertEqual(high["n"], 2)
        self.assertAlmostEqual(high["label_rate"], 0.5, places=6)

    def test_model_version_name_from_the_view_is_published(self):
        self.assertIn("model_version_name", export_runs.RUN_COLUMNS)
        self.assertIn("model_version_name", export_runs.CSV_COLUMNS)
        run = {"id": "r1", "model_version_name": "lgbm_v1", "backend": "ml", "generated_at": "2026-10-11T00:00:00Z"}
        bundle = export_runs.build_bundle([run], [], [], "2026-10-11T01:00:00Z")
        self.assertEqual(bundle["runs"][0]["model_version_name"], "lgbm_v1")
        self.assertEqual(bundle["runs"][0]["model_version"], "lgbm_v1")
        self.assertIn(",lgbm_v1,", export_runs.bundle_csv(bundle))
        runs, predictions, versions = dirty_rows()
        bundle = export_runs.build_bundle(runs, predictions, versions, "2026-10-11T17:00:00Z")
        self.assertEqual(bundle["runs"][0]["model_version_name"], "candidateone")

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
            self.assertIn("data/predictions.json", commit)

    def test_stale_predictions_fail_and_a_fresh_export_keeps_the_artifact_time(self):
        artifact_at = "2026-10-17T12:36:56Z"
        older = "2026-10-12T22:43:49Z"
        self.assertTrue(export_runs.predictions_are_stale(older, artifact_at))
        self.assertTrue(export_runs.predictions_are_stale(None, artifact_at))
        self.assertFalse(export_runs.predictions_are_stale(artifact_at, artifact_at))
        self.assertFalse(export_runs.predictions_are_stale(artifact_at, "2026-10-17T12:36:56+00:00"))
        self.assertFalse(export_runs.predictions_are_stale(artifact_at, None))
        chosen, source = export_runs.choose_newer_signal(
            {"generated_at": older, "candidates": [{"symbol": "BTC", "side": "flat"}]},
            {"generated_at": artifact_at, "candidates": [{"symbol": "AAVE", "side": "flat"}]},
        )
        self.assertEqual(source, export_runs.ARTIFACT_TABLE)
        self.assertEqual(chosen["generated_at"], artifact_at)
        row = {
            "generated_at": artifact_at,
            "mode": "live",
            "payload": {
                "generated_at": artifact_at,
                "mode": "live",
                "backend": "ml",
                "model_version": "lgbm_no_dow_calibrated",
                "last_bar_time": "2026-10-17T11:00:00Z",
                "book_usd": 731,
                "candidates": [
                    {
                        "symbol": "AAVE",
                        "side": "flat",
                        "size_usd": 12.5,
                        "p_up": 0.5056231734627226,
                        "conviction": 0.1556,
                        "bar_time": "2026-10-17T11:00:00Z",
                        "expires_at": "2026-10-18T12:36:56Z",
                        "thesis": "cost $1",
                        "order_id": "private-order",
                    }
                ],
            },
        }
        signal = export_runs.signal_document(row)
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            stale_path = folder / "stale.json"
            stale_path.write_text(json.dumps({"generated_at": older, "predictions": []}), encoding="utf-8")
            failed = export_runs.main(
                ["--require-fresh", "--predictions", str(stale_path), "--newest-artifact-at", artifact_at]
            )
            self.assertEqual(failed, 1)
            with self.assertRaises(RuntimeError):
                export_runs.publish_predictions(
                    folder,
                    {"generated_at": older, "candidates": []},
                    source=export_runs.ARTIFACT_TABLE,
                    newest_artifact_at=artifact_at,
                )
            out = folder / "fresh"
            signal_path = folder / "signal.json"
            signal_path.write_text(json.dumps(signal), encoding="utf-8")
            wrote = export_runs.main(
                [
                    "--from-signal",
                    str(signal_path),
                    "--out",
                    str(out),
                    "--newest-artifact-at",
                    artifact_at,
                    "--source",
                    export_runs.ARTIFACT_TABLE,
                ]
            )
            self.assertEqual(wrote, 0)
            written = json.loads((out / "predictions.json").read_text(encoding="utf-8"))
            self.assertEqual(written["generated_at"], artifact_at)
            self.assertEqual(written["source"], export_runs.ARTIFACT_TABLE)
            self.assertEqual(written["predictions"][0]["symbol"], "AAVE")
            self.assertEqual(written["predictions"][0]["bar_time"], "2026-10-17T11:00:00Z")
            blob = json.dumps(written)
            self.assertNotIn("$", blob)
            self.assertNotIn("usd", blob.lower())
            self.assertNotIn("order_id", blob)
            self.assertEqual(export_runs.public_violations(out / "predictions.json"), [])
            fresh = export_runs.main(
                ["--require-fresh", "--predictions", str(out / "predictions.json"), "--newest-artifact-at", artifact_at]
            )
            self.assertEqual(fresh, 0)


def _desc_key(value):
    if value is None:
        return (0,)
    return (1, tuple(-ord(char) for char in str(value)))


def prediction_sort_key(row):
    """PREDICTION_ORDER: bar_time DESC NULLS FIRST, run_id DESC, symbol DESC."""
    return (
        _desc_key(row.get("bar_time")),
        _desc_key(row.get("run_id")),
        _desc_key(row.get("symbol")),
    )


def _split_filter_args(text):
    parts = []
    buf = []
    depth = 0
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if quoted:
            buf.append(char)
            if char == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    buf.append(text[index + 1])
                    index += 2
                    continue
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            buf.append(char)
        elif char == "(":
            depth += 1
            buf.append(char)
        elif char == ")":
            depth -= 1
            buf.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
        index += 1
    if buf:
        parts.append("".join(buf))
    return parts


def _unquote_filter(value):
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def _match_condition(row, text):
    for op in ("not.is", "is", "lte", "gte", "lt", "gt", "eq"):
        token = "." + op + "."
        found = text.find(token)
        if found < 0:
            continue
        column = text[:found]
        value = _unquote_filter(text[found + len(token):])
        actual = row.get(column)
        if op == "is":
            return actual is None if value == "null" else False
        if op == "not.is":
            return actual is not None if value == "null" else False
        if actual is None:
            return False
        left = str(actual)
        if op == "eq":
            return left == value
        if op == "lt":
            return left < value
        if op == "gt":
            return left > value
        if op == "lte":
            return left <= value
        if op == "gte":
            return left >= value
    raise AssertionError("unparsed filter condition " + text)


def _match_filter(row, text):
    text = text.strip()
    if text.startswith("or(") and text.endswith(")"):
        return any(_match_filter(row, part) for part in _split_filter_args(text[3:-1]))
    if text.startswith("and(") and text.endswith(")"):
        return all(_match_filter(row, part) for part in _split_filter_args(text[4:-1]))
    return _match_condition(row, text)


def _match_query(row, query):
    if "or" in query:
        text = query["or"][0]
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        return any(_match_filter(row, part) for part in _split_filter_args(text))
    if "and" in query:
        text = query["and"][0]
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        return all(_match_filter(row, part) for part in _split_filter_args(text))
    return True


class _Body:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def fixture_predictions():
    """Ties on bar_time, a null bar_time, and one row past a 4-row window."""
    rows = [
        {"run_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2", "symbol": "ETH", "bar_time": "2026-10-24T06:00:00Z", "p_up": 0.8, "side": "long", "conviction": 0.2, "expires_at": "2026-10-25T06:00:00Z", "fwd_ret_8h": 0.01, "label_up_net30": True, "resolved_at": "2026-10-25T06:00:00Z", "traded": True, "p_up_source": "payload", "features": {"secret": True}},
        {"run_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2", "symbol": "BTC", "bar_time": "2026-10-24T06:00:00Z", "p_up": 0.2, "side": "short", "conviction": 0.1, "expires_at": "2026-10-25T06:00:00Z", "fwd_ret_8h": -0.02, "label_up_net30": False, "resolved_at": "2026-10-25T06:00:00Z", "traded": False, "p_up_source": "payload", "features": {"secret": True}},
        {"run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "symbol": "ETH", "bar_time": "2026-10-24T06:00:00Z", "p_up": 0.55, "side": "flat", "conviction": 0.0, "expires_at": "2026-10-25T06:00:00Z", "fwd_ret_8h": 0.0, "label_up_net30": False, "resolved_at": "2026-10-25T06:00:00Z", "traded": False, "p_up_source": "thesis_2dp"},
        {"run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "symbol": "SOL", "bar_time": "2026-10-24T05:00:00+00:00", "p_up": 0.4, "side": "long", "conviction": 0.05, "expires_at": "2026-10-25T05:00:00Z", "fwd_ret_8h": 0.03, "label_up_net30": True, "resolved_at": "2026-10-25T05:00:00Z", "traded": True, "p_up_source": "payload"},
        {"run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "symbol": "BTC", "bar_time": "2026-10-24T05:00:00+00:00", "p_up": 0.1, "side": "flat", "conviction": 0.0, "expires_at": "2026-10-25T05:00:00Z", "fwd_ret_8h": None, "label_up_net30": None, "resolved_at": None, "traded": False, "p_up_source": "thesis_2dp"},
        {"run_id": "cccccccc-cccc-4ccc-8ccc-ccccccccccc3", "symbol": "SOL", "bar_time": None, "p_up": 0.5, "side": "flat", "conviction": 0.0, "expires_at": None, "fwd_ret_8h": None, "label_up_net30": True, "resolved_at": None, "traded": False, "p_up_source": "payload"},
    ]
    return rows


def _markers(rows):
    return [(row.get("bar_time"), row.get("run_id"), row.get("symbol")) for row in rows]


class _PredictionServer:
    def __init__(self, rows):
        self.ordered = sorted(rows, key=prediction_sort_key)
        self.queries = []
        self.selects = []

    def urlopen(self, request, timeout=30):
        del timeout
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        self.queries.append(query)
        select = query.get("select", [""])[0]
        self.selects.append(select)
        columns = [column for column in select.split(",") if column]
        limit = int(query["limit"][0])
        if "offset" in query:
            start = int(query["offset"][0])
            page = self.ordered[start:start + limit]
        else:
            matched = [row for row in self.ordered if _match_query(row, query)]
            page = matched[:limit]
        projected = [{column: row.get(column) for column in columns} for row in page]
        return _Body(json.dumps(projected).encode("utf-8"))


class PredictionKeysetTests(unittest.TestCase):
    def test_fetch_columns_are_the_union_and_omit_features(self):
        self.assertEqual(
            export_runs.PREDICTION_FETCH_COLUMNS,
            export_runs.PREDICTION_COLUMNS + ("p_up_source",),
        )
        for banned in ("features", "fill_trade_id", "size_frac", "realized_pnl_frac"):
            self.assertNotIn(banned, export_runs.PREDICTION_FETCH_COLUMNS)
        self.assertEqual(export_runs.PREDICTION_SOURCES[0], "run_predictions")
        self.assertNotIn("id", export_runs.PREDICTION_KEY)
        self.assertEqual(export_runs.PREDICTION_KEY, ("bar_time", "run_id", "symbol"))

    def test_keyset_matches_offset_including_ties_nulls_and_exact_pages(self):
        rows = fixture_predictions()
        expected = _markers(sorted(rows, key=prediction_sort_key))
        # Null bar_time sorts first. The 06:00 ETH row sorts ahead of BTC on the same run.
        self.assertEqual(expected[0][0], None)
        self.assertEqual(expected[1][2], "ETH")
        self.assertEqual(expected[1][0], "2026-10-24T06:00:00Z")
        self.assertEqual(expected[2][2], "BTC")
        old_page = export_runs.PAGE_SIZE
        export_runs.PAGE_SIZE = 2
        try:
            offset_server = _PredictionServer(rows)
            keyset_server = _PredictionServer(rows)
            with mock.patch.object(urllib.request, "urlopen", offset_server.urlopen):
                offset_rows = export_runs.fetch_rest(
                    "https://example.invalid",
                    "test-key",
                    "run_predictions",
                    export_runs.PREDICTION_FETCH_COLUMNS,
                    export_runs.PREDICTION_ORDER,
                    len(rows) + 5,
                    paging="offset",
                )
            with mock.patch.object(urllib.request, "urlopen", keyset_server.urlopen):
                keyset_rows = export_runs.fetch_rest(
                    "https://example.invalid",
                    "test-key",
                    "run_predictions",
                    export_runs.PREDICTION_FETCH_COLUMNS,
                    export_runs.PREDICTION_ORDER,
                    len(rows) + 5,
                    paging="keyset",
                )
        finally:
            export_runs.PAGE_SIZE = old_page
        self.assertEqual(_markers(offset_rows), expected)
        self.assertEqual(_markers(keyset_rows), expected)
        self.assertEqual(_markers(offset_rows), _markers(keyset_rows))
        # 6 rows, page size 2: three full pages plus one empty probe.
        self.assertEqual(len(offset_server.queries), export_runs.prediction_get_count(len(rows), len(rows) + 5, 2))
        self.assertEqual(len(keyset_server.queries), len(offset_server.queries))
        self.assertEqual(len(keyset_server.queries), 4)
        self.assertTrue(all("offset" in query for query in offset_server.queries))
        self.assertTrue(all("offset" not in query for query in keyset_server.queries))
        self.assertNotIn("or", keyset_server.queries[0])
        self.assertIn("or", keyset_server.queries[1])
        self.assertIn("symbol.lt.", keyset_server.queries[1]["or"][0])
        for select in keyset_server.selects + offset_server.selects:
            self.assertNotIn("features", select.split(","))
            self.assertIn("p_up_source", select.split(","))
        self.assertTrue(all("features" not in row for row in keyset_rows))

    def test_empty_page_and_exact_page_size(self):
        old_page = export_runs.PAGE_SIZE
        export_runs.PAGE_SIZE = 2
        try:
            empty = _PredictionServer([])
            with mock.patch.object(urllib.request, "urlopen", empty.urlopen):
                rows = export_runs.fetch_rest(
                    "https://example.invalid",
                    "k",
                    "run_predictions",
                    export_runs.PREDICTION_FETCH_COLUMNS,
                    export_runs.PREDICTION_ORDER,
                    10,
                    paging="keyset",
                )
            self.assertEqual(rows, [])
            self.assertEqual(len(empty.queries), 1)
            exact_rows = sorted(fixture_predictions(), key=prediction_sort_key)[:2]
            exact = _PredictionServer(exact_rows)
            with mock.patch.object(urllib.request, "urlopen", exact.urlopen):
                fetched = export_runs.fetch_rest(
                    "https://example.invalid",
                    "k",
                    "run_predictions",
                    export_runs.PREDICTION_FETCH_COLUMNS,
                    export_runs.PREDICTION_ORDER,
                    10,
                    paging="keyset",
                )
        finally:
            export_runs.PAGE_SIZE = old_page
        self.assertEqual(_markers(fetched), _markers(exact_rows))
        self.assertEqual(len(exact.queries), 2)
        self.assertEqual(exact.queries[1].get("or") is not None or exact.queries[1].get("and") is not None, True)

    def test_timestamp_with_plus_roundtrips_the_keyset_filter(self):
        cursor = {
            "bar_time": "2026-10-24T05:00:00+00:00",
            "run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            "symbol": "SOL",
        }
        name, value = export_runs.prediction_page_filter(older_than=cursor)
        self.assertEqual(name, "or")
        self.assertIn('bar_time.lt."2026-10-24T05:00:00+00:00"', value)
        encoded = urllib.parse.urlencode({name: value})
        self.assertIn("%2B00", encoded)
        decoded = urllib.parse.parse_qs(encoded, keep_blank_values=True)
        self.assertIn("+00:00", decoded["or"][0])

    def test_offset_and_keyset_exports_are_byte_identical(self):
        rows = fixture_predictions()
        runs = [
            {"id": row["run_id"], "backend": "ml", "generated_at": "2026-10-24T07:00:00Z"}
            for row in rows
        ]
        # One run id per distinct run. build_bundle keeps every run under RUN_LIMIT.
        seen = []
        unique_runs = []
        for run in runs:
            if run["id"] in seen:
                continue
            seen.append(run["id"])
            unique_runs.append(run)
        versions = [{"id": "ver-1", "name": "lgbm_v1", "status": "promoted"}]
        old_page = export_runs.PAGE_SIZE
        old_limit = export_runs.PREDICTION_LIMIT
        export_runs.PAGE_SIZE = 2
        export_runs.PREDICTION_LIMIT = 4
        try:
            offset_server = _PredictionServer(rows)
            keyset_server = _PredictionServer(rows)
            with mock.patch.object(urllib.request, "urlopen", offset_server.urlopen):
                offset_rows = export_runs.fetch_rest(
                    "https://example.invalid", "k", "run_predictions",
                    export_runs.PREDICTION_FETCH_COLUMNS, export_runs.PREDICTION_ORDER,
                    export_runs.CALIBRATION_LIMIT, paging="offset",
                )
            with mock.patch.object(urllib.request, "urlopen", keyset_server.urlopen):
                keyset_rows = export_runs.fetch_rest(
                    "https://example.invalid", "k", "run_predictions",
                    export_runs.PREDICTION_FETCH_COLUMNS, export_runs.PREDICTION_ORDER,
                    export_runs.CALIBRATION_LIMIT, paging="keyset",
                )
            offset_bytes = _public_bytes(unique_runs, offset_rows, versions)
            keyset_bytes = _public_bytes(unique_runs, keyset_rows, versions)
        finally:
            export_runs.PAGE_SIZE = old_page
            export_runs.PREDICTION_LIMIT = old_limit
        self.assertEqual(offset_bytes, keyset_bytes)
        self.assertEqual(offset_bytes[0], keyset_bytes[0])
        self.assertEqual(offset_bytes[1], keyset_bytes[1])

    def test_one_fetch_derives_the_window_and_calibration(self):
        rows = sorted(fixture_predictions(), key=prediction_sort_key)
        calls = []

        def read_rows(source, columns, order, limit, after=None):
            calls.append((source, tuple(columns), order, limit, after))
            if source == "runs_public":
                return [{"id": row["run_id"], "backend": "ml", "generated_at": "2026-10-24T07:00:00Z"} for row in rows]
            if source == "run_predictions":
                return list(rows)
            if source == "model_versions_public":
                return [{"id": "ver-1", "name": "lgbm_v1", "status": "promoted"}]
            raise export_runs.SourceMissing(source)

        old_limit = export_runs.PREDICTION_LIMIT
        export_runs.PREDICTION_LIMIT = 2
        try:
            bundle, missing = export_runs.collect(read_rows, "2026-10-24T08:00:00Z")
        finally:
            export_runs.PREDICTION_LIMIT = old_limit
        prediction_calls = [call for call in calls if call[0] in export_runs.PREDICTION_SOURCES]
        self.assertEqual(prediction_calls, [(
            "run_predictions",
            export_runs.PREDICTION_FETCH_COLUMNS,
            export_runs.PREDICTION_ORDER,
            export_runs.CALIBRATION_LIMIT,
            None,
        )])
        self.assertEqual(missing, [])
        self.assertEqual(len(bundle["predictions"]), 2)
        self.assertEqual(bundle["calibration"]["scope"], "all_predictions")
        self.assertGreater(bundle["calibration"]["n_resolved"], len(bundle["predictions"]))
        self.assertEqual(bundle["calibration"]["full_precision"]["n_scored"], 4)
        self.assertEqual(bundle["sources"]["predictions"], "run_predictions")
        blob = json.dumps(bundle)
        self.assertNotIn("features", blob)

    def test_incremental_matches_a_full_keyset_scan(self):
        ordered = sorted(fixture_predictions(), key=prediction_sort_key)
        # Prior already holds the null bar and every row from the old watermark down.
        prior = [ordered[0]] + ordered[3:]
        cursor = export_runs.incremental_cursor(prior)
        self.assertEqual(cursor["bar_time"], ordered[3]["bar_time"])
        self.assertEqual(cursor["symbol"], ordered[3]["symbol"])
        old_page = export_runs.PAGE_SIZE
        old_limit = export_runs.PREDICTION_LIMIT
        export_runs.PAGE_SIZE = 2
        export_runs.PREDICTION_LIMIT = 4
        try:
            fresh_server = _PredictionServer(ordered)
            with mock.patch.object(urllib.request, "urlopen", fresh_server.urlopen):
                fresh = export_runs.fetch_rest(
                    "https://example.invalid", "k", "run_predictions",
                    export_runs.PREDICTION_FETCH_COLUMNS, export_runs.PREDICTION_ORDER,
                    export_runs.CALIBRATION_LIMIT, after=cursor, paging="keyset",
                )
            full_server = _PredictionServer(ordered)
            with mock.patch.object(urllib.request, "urlopen", full_server.urlopen):
                full = export_runs.fetch_rest(
                    "https://example.invalid", "k", "run_predictions",
                    export_runs.PREDICTION_FETCH_COLUMNS, export_runs.PREDICTION_ORDER,
                    export_runs.CALIBRATION_LIMIT, paging="keyset",
                )
            merged = export_runs.merge_prediction_rows(prior, fresh)
            self.assertEqual(_markers(merged), _markers(full))
            self.assertLess(len(fresh_server.queries), len(full_server.queries))
            runs = []
            seen = set()
            for row in ordered:
                if row["run_id"] in seen:
                    continue
                seen.add(row["run_id"])
                runs.append({"id": row["run_id"], "backend": "ml", "generated_at": "2026-10-24T07:00:00Z"})
            versions = [{"id": "ver-1", "name": "lgbm_v1", "status": "promoted"}]

            def read_rows(source, columns, order, limit, after=None):
                del columns, order, limit
                if source == "runs_public":
                    return runs
                if source == "model_versions_public":
                    return versions
                if source != "run_predictions":
                    raise export_runs.SourceMissing(source)
                if after is None:
                    return list(ordered)
                name, value = export_runs.prediction_page_filter(newer_than=after)
                return [row for row in ordered if _match_query(row, {name: [value]})]

            incremental, _ = export_runs.collect(read_rows, "2026-10-24T08:00:00Z", prior_predictions=prior)
            complete, _ = export_runs.collect(read_rows, "2026-10-24T08:00:00Z")
        finally:
            export_runs.PAGE_SIZE = old_page
            export_runs.PREDICTION_LIMIT = old_limit
        self.assertEqual(_markers(merged), _markers(ordered))
        self.assertEqual(export_runs.bundle_json(incremental), export_runs.bundle_json(complete))
        self.assertEqual(export_runs.bundle_csv(incremental), export_runs.bundle_csv(complete))

    def test_missing_page_state_is_a_full_scan(self):
        rows = sorted(fixture_predictions(), key=prediction_sort_key)
        calls = []

        def read_rows(source, columns, order, limit, after=None):
            calls.append(after)
            if source == "run_predictions":
                return rows
            if source == "runs_public":
                return [{"id": rows[0]["run_id"], "generated_at": "2026-10-24T07:00:00Z"}]
            if source == "model_versions_public":
                return []
            raise export_runs.SourceMissing(source)

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "runs.json").write_text(json.dumps({"predictions": [{"bar_time": "2026-10-24T06:00:00Z"}]}), encoding="utf-8")
            self.assertIsNone(export_runs.remembered_predictions(folder))
            export_runs.publish(folder, "https://example.invalid", read_rows=read_rows, now="2026-10-24T08:00:00Z")
            stored = export_runs.remembered_predictions(folder)
            self.assertIsNotNone(stored)
            self.assertEqual(_markers(stored), _markers(rows))
            self.assertTrue((folder / ".cache" / "run_predictions_page_state.json").is_file())
            import export_kpi
            self.assertEqual(export_kpi.published_identifier_problems(folder), [])
        self.assertIn(None, calls)

    def test_request_count_before_and_after(self):
        # ~63k rows, page size 1000. The public window stops at 20_000.
        # 63_000 is an exact multiple of the page, so calibration pays one empty GET.
        full = export_runs.prediction_export_gets(63_000)
        self.assertEqual(full, {"before": 84, "after": 64})
        incremental = export_runs.prediction_export_gets(63_000, new_rows=82)
        self.assertEqual(incremental["after"], 1)
        self.assertEqual(export_runs.prediction_get_count(0, 10, 2), 1)
        self.assertEqual(export_runs.prediction_get_count(2, 10, 2), 2)
        self.assertEqual(export_runs.prediction_get_count(4, 4, 2), 2)

    def test_sql_order_and_newer_bound_are_parameterized(self):
        order = export_runs._order_sql(export_runs.PREDICTION_ORDER)
        self.assertIn("bar_time desc nulls first", order)
        self.assertIn("run_id desc", order)
        self.assertIn("symbol desc", order)
        self.assertEqual(export_runs._order_sql("generated_at.desc"), " order by generated_at desc nulls last")
        where, params = export_runs._prediction_newer_sql({
            "bar_time": "2026-10-24T06:00:00Z",
            "run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            "symbol": "BTC",
        })
        self.assertEqual(where.count("%s"), len(params))
        self.assertNotIn("BTC'", where)
        self.assertIn("timestamptz", where)
        self.assertIn("uuid", where)


def _public_bytes(runs, prediction_rows, versions):
    window, calibration_rows = export_runs.derive_prediction_outputs(prediction_rows)
    bundle = export_runs.build_bundle(runs, window, versions, "2026-10-24T08:00:00Z")
    bundle["calibration"] = export_runs.calibrate(calibration_rows)
    bundle["calibration"]["scope"] = "all_predictions"
    bundle["calibration"]["full_precision"] = export_runs.full_precision_calibration(calibration_rows)
    bundle["sources"] = {
        "runs": "runs_public",
        "predictions": "run_predictions",
        "model_versions": "model_versions_public",
    }
    return export_runs.bundle_json(bundle), export_runs.bundle_csv(bundle)


if __name__ == "__main__":
    unittest.main()
