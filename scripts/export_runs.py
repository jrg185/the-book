#!/usr/bin/env python3
"""Export public scoring runs for the Book.

Reads only safe columns from the public views. Falls back to the same
columns on the base tables when a view is not installed yet. Dollar
amounts, fill_trade_id, order ids, and account fields are not written.

A missing or empty relation is logged. It does not fail Export KPI.

  data/runs.json
  data/runs.csv
"""

from __future__ import annotations

import csv
import hashlib
import datetime as dt
import io
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RUN_KEY_LABEL = "the-book/run-key/v1"
VERSION_KEY_LABEL = "the-book/model-version-key/v1"


def public_key(value, label: str, prefix: str = "k"):
    """Short non-reversible key for a private id. Never the raw id."""
    text = as_text(value)
    if not text:
        return None
    # The prefix keeps the key from reading as an account-number-like token.
    return prefix + "_" + hashlib.sha256((label + ":" + text).encode("utf-8")).hexdigest()[:12]


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# Recent window for the public file. Bounds the snapshot; not a live balance.
RUN_LIMIT = 500
PREDICTION_LIMIT = 20_000
VERSION_LIMIT = 200
BIN_COUNT = 10
PAGE_SIZE = 1000

RUN_SOURCES = ("runs_public", "scoring_runs")
PREDICTION_SOURCES = ("run_predictions_public", "run_predictions")
VERSION_SOURCES = ("model_versions_public", "model_versions")

RUN_COLUMNS = (
    "id",
    "model_version_id",
    "model_version",
    "backend",
    "mode",
    "generated_at",
    "last_bar_time",
    "anomaly",
)
PREDICTION_COLUMNS = (
    "run_id",
    "symbol",
    "p_up",
    "side",
    "conviction",
    "bar_time",
    "expires_at",
    "fwd_ret_8h",
    "label_up_net30",
    "resolved_at",
    "traded",
)
VERSION_COLUMNS = (
    "id",
    "name",
    "parent",
    "status",
    "oos_metrics",
    "trained_at",
    "promoted_at",
    "created_at",
)

CSV_COLUMNS = (
    "kind",
    "id",
    "run_id",
    "model_version",
    "model_version_id",
    "backend",
    "mode",
    "generated_at",
    "last_bar_time",
    "anomaly",
    "symbol",
    "p_up",
    "side",
    "conviction",
    "bar_time",
    "expires_at",
    "fwd_ret_8h",
    "label_up_net30",
    "resolved_at",
    "traded",
    "name",
    "parent",
    "status",
    "oos_metrics",
    "trained_at",
    "promoted_at",
    "created_at",
    "bin_lo",
    "bin_hi",
    "n",
    "mean_p_up",
    "label_rate",
    "logloss",
    "brier",
    "n_resolved",
)

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_RAW_TOKENS = ("fill_trade_id", "order_id", "account_id", "account_number", "rhs_account")
_TRUE = {"true", "t", "1", "yes", "y"}
_FALSE = {"false", "f", "0", "no", "n"}


class SourceMissing(Exception):
    pass


class ColumnMissing(Exception):
    def __init__(self, column: str | None, detail: str):
        super().__init__(detail)
        self.column = column


def forbidden_key(key: object) -> bool:
    text = str(key)
    lowered = text.lower()
    if "$" in text:
        return True
    if "usd" in lowered or "account" in lowered:
        return True
    if "order_id" in lowered or "fill_trade_id" in lowered:
        return True
    return False


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise RuntimeError(f"refusing unsafe sql identifier {name!r}")
    return name


def _redact(detail: object, secrets: list[str]) -> str:
    text = str(detail)
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "[redacted]")
    return text[:240]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def as_text(value) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(value).strip().replace("$", "")
    return text or None


def as_float(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def as_bool(value):
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def as_traded(value) -> bool:
    return as_bool(value) is True


def q6(value):
    if value is None:
        return None
    return float(format(value, ".6f"))


def scrub_metrics(value, depth: int = 0):
    if depth > 6:
        return None
    if isinstance(value, str):
        text = value.replace("$", "").strip()
        if text[:1] in "{[":
            try:
                return scrub_metrics(json.loads(text), depth + 1)
            except json.JSONDecodeError:
                return text or None
        return text or None
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return as_float(value)
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if forbidden_key(key):
                continue
            name = str(key).replace("$", "")
            if not name or forbidden_key(name):
                continue
            clean[name] = scrub_metrics(item, depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [scrub_metrics(item, depth + 1) for item in value]
    return None


def project_run(row: dict, names: dict) -> dict | None:
    version_id = as_text(row.get("model_version_id"))
    version = as_text(row.get("model_version")) or as_text(row.get("model_name"))
    if not version and version_id:
        version = names.get(version_id)
    public = {
        "id": public_key(row.get("id"), RUN_KEY_LABEL, "run"),
        "model_version_id": public_key(version_id, VERSION_KEY_LABEL, "mv"),
        "model_version": version,
        "backend": as_text(row.get("backend")),
        "mode": as_text(row.get("mode")),
        "generated_at": as_text(row.get("generated_at")),
        "last_bar_time": as_text(row.get("last_bar_time")),
        "anomaly": as_bool(row.get("anomaly")),
    }
    if not any(value is not None for value in public.values()):
        return None
    return public


def project_prediction(row: dict) -> dict | None:
    public = {
        "run_id": public_key(row.get("run_id"), RUN_KEY_LABEL, "run"),
        "symbol": as_text(row.get("symbol")),
        "p_up": as_float(row.get("p_up")),
        "side": as_text(row.get("side")),
        "conviction": as_float(row.get("conviction")),
        "bar_time": as_text(row.get("bar_time")),
        "expires_at": as_text(row.get("expires_at")),
        "fwd_ret_8h": as_float(row.get("fwd_ret_8h")),
        "label_up_net30": as_bool(row.get("label_up_net30")),
        "resolved_at": as_text(row.get("resolved_at")),
        "traded": as_traded(row.get("traded")),
    }
    if not public["run_id"] and not public["symbol"]:
        return None
    return public


def project_version(row: dict) -> dict | None:
    metrics = row.get("oos_metrics")
    public = {
        "id": public_key(row.get("id"), VERSION_KEY_LABEL, "mv"),
        "name": as_text(row.get("name")),
        "parent": as_text(row.get("parent")),
        "status": as_text(row.get("status")),
        "oos_metrics": None if metrics is None else scrub_metrics(metrics),
        "trained_at": as_text(row.get("trained_at")),
        "promoted_at": as_text(row.get("promoted_at")),
        "created_at": as_text(row.get("created_at")),
    }
    if not public["id"] and not public["name"]:
        return None
    return public


def calibrate(predictions: list) -> dict:
    resolved = []
    for row in predictions:
        label = row.get("label_up_net30")
        probability = row.get("p_up")
        if not isinstance(label, bool) or probability is None:
            continue
        probability = min(1.0, max(0.0, float(probability)))
        resolved.append((probability, 1.0 if label else 0.0))
    if not resolved:
        return {"n_resolved": 0, "logloss": None, "brier": None, "bins": []}
    epsilon = 1e-15
    logloss = 0.0
    brier = 0.0
    bins = [
        {"lo": index / BIN_COUNT, "hi": (index + 1) / BIN_COUNT, "n": 0, "sum_p": 0.0, "sum_y": 0.0}
        for index in range(BIN_COUNT)
    ]
    for probability, outcome in resolved:
        clipped = min(1 - epsilon, max(epsilon, probability))
        logloss += -(outcome * math.log(clipped) + (1 - outcome) * math.log(1 - clipped))
        brier += (probability - outcome) ** 2
        index = BIN_COUNT - 1 if probability >= 1 else int(probability * BIN_COUNT)
        slot = bins[index]
        slot["n"] += 1
        slot["sum_p"] += probability
        slot["sum_y"] += outcome
    public_bins = []
    for slot in bins:
        count = slot["n"]
        public_bins.append(
            {
                "lo": slot["lo"],
                "hi": slot["hi"],
                "n": count,
                "mean_p_up": None if count == 0 else q6(slot["sum_p"] / count),
                "label_rate": None if count == 0 else q6(slot["sum_y"] / count),
            }
        )
    return {
        "n_resolved": len(resolved),
        "logloss": q6(logloss / len(resolved)),
        "brier": q6(brier / len(resolved)),
        "bins": public_bins,
    }


def build_bundle(runs, predictions, versions, fetched_at: str) -> dict:
    public_versions = []
    names = {}
    for row in versions or []:
        if not isinstance(row, dict):
            continue
        public = project_version(row)
        if not public:
            continue
        public_versions.append(public)
        raw_id = as_text(row.get("id"))
        if raw_id and public.get("name"):
            names[raw_id] = public["name"]
    public_versions.sort(key=lambda row: (row.get("name") or "", row.get("id") or ""))

    public_runs = []
    for row in runs or []:
        if not isinstance(row, dict):
            continue
        public = project_run(row, names)
        if public:
            public_runs.append(public)
    public_runs.sort(key=lambda row: row.get("id") or "")
    public_runs.sort(key=lambda row: row.get("generated_at") or "", reverse=True)
    public_runs = public_runs[:RUN_LIMIT]
    run_ids = {row["id"] for row in public_runs if row.get("id")}

    public_predictions = []
    for row in predictions or []:
        if not isinstance(row, dict):
            continue
        public = project_prediction(row)
        if not public:
            continue
        if run_ids and public.get("run_id") not in run_ids:
            continue
        public_predictions.append(public)
    public_predictions.sort(key=lambda row: (row.get("symbol") or "", row.get("run_id") or ""))
    public_predictions.sort(key=lambda row: row.get("bar_time") or "", reverse=True)

    return {
        "source": "supabase",
        "fetched_at": fetched_at,
        "runs": public_runs,
        "predictions": public_predictions,
        "model_versions": public_versions,
        "calibration": calibrate(public_predictions),
    }


def empty_bundle() -> dict:
    return {
        "source": "empty",
        "fetched_at": None,
        "runs": [],
        "predictions": [],
        "model_versions": [],
        "calibration": {"n_resolved": 0, "logloss": None, "brier": None, "bins": []},
    }


def _csv_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), allow_nan=False).replace("$", "")
    if isinstance(value, float):
        return format(value, ".8g")
    return as_text(value) or ""


def bundle_csv(bundle: dict) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()

    def emit(kind: str, fields: dict) -> None:
        row = {column: "" for column in CSV_COLUMNS}
        row["kind"] = kind
        for key, value in fields.items():
            if key in row and not forbidden_key(key):
                row[key] = _csv_cell(value)
        writer.writerow(row)

    for run in bundle.get("runs") or []:
        emit("run", run)
    for prediction in bundle.get("predictions") or []:
        emit("prediction", prediction)
    for version in bundle.get("model_versions") or []:
        emit("model_version", version)
    calibration = bundle.get("calibration") or {}
    emit(
        "calibration",
        {
            "logloss": calibration.get("logloss"),
            "brier": calibration.get("brier"),
            "n_resolved": calibration.get("n_resolved"),
        },
    )
    for slot in calibration.get("bins") or []:
        emit(
            "calibration_bin",
            {
                "bin_lo": slot.get("lo"),
                "bin_hi": slot.get("hi"),
                "n": slot.get("n"),
                "mean_p_up": slot.get("mean_p_up"),
                "label_rate": slot.get("label_rate"),
            },
        )
    return buffer.getvalue()


def bundle_json(bundle: dict) -> str:
    return json.dumps(bundle, indent=2, allow_nan=False) + "\n"


def _walk_keys(value, found: list) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if forbidden_key(key):
                found.append(str(key))
            _walk_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_keys(item, found)


def text_violations(text: str) -> list[str]:
    found = []
    if "$" in text:
        found.append("$")
    lowered = text.lower()
    for token in _RAW_TOKENS:
        if token in lowered:
            found.append(token)
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            _walk_keys(json.loads(text), found)
        except json.JSONDecodeError:
            found.append("invalid-json")
    elif text.strip():
        header = text.splitlines()[0]
        for cell in next(csv.reader([header])):
            if forbidden_key(cell):
                found.append(cell)
    return list(dict.fromkeys(found))


def public_violations(path: Path) -> list[str]:
    return text_violations(path.read_text(encoding="utf-8"))


def write_public(target: Path, bundle: dict) -> None:
    json_text = bundle_json(bundle)
    csv_text = bundle_csv(bundle)
    problems = text_violations(json_text) + text_violations(csv_text)
    if problems:
        raise RuntimeError("scoring runs export blocked by public scrub: " + ", ".join(problems))
    target.mkdir(parents=True, exist_ok=True)
    (target / "runs.json").write_text(json_text, encoding="utf-8")
    (target / "runs.csv").write_text(csv_text, encoding="utf-8")


def ensure_placeholder(target: Path) -> None:
    json_path = target / "runs.json"
    csv_path = target / "runs.csv"
    if json_path.is_file() and csv_path.is_file():
        return
    write_public(target, empty_bundle())
    print("scoring runs: wrote an empty public snapshot")


def named_column(detail: str, columns) -> str | None:
    for column in sorted(columns, key=len, reverse=True):
        if re.search(rf"['\"]{re.escape(column)}['\"]", detail):
            return column
        lowered = detail.lower()
        if re.search(rf"\b{re.escape(column)}\b", detail) and (
            "does not exist" in lowered or "could not find" in lowered
        ):
            return column
    return None


def raise_for_http(source: str, code: int, detail: str, columns) -> None:
    lowered = detail.lower()
    if code == 404 or "pgrst205" in lowered or "42p01" in lowered:
        raise SourceMissing(source)
    if "42703" in lowered or "pgrst204" in lowered or (code == 400 and "column" in lowered):
        raise ColumnMissing(named_column(detail, columns), detail)
    if "does not exist" in lowered and "column" not in lowered:
        raise SourceMissing(source)
    if code in (401, 403):
        raise SourceMissing(source)
    raise RuntimeError(f"REST {source} HTTP {code}: {detail[:180]}")


def fetch_rest(base_url: str, key: str, source: str, columns, order: str | None, limit: int) -> list:
    rows: list = []
    offset = 0
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "agentic-sleeves-kpi-export",
    }
    while len(rows) < limit:
        batch = min(PAGE_SIZE, limit - len(rows))
        params = {
            "select": ",".join(_ident(column) for column in columns),
            "limit": str(batch),
            "offset": str(offset),
        }
        if order:
            params["order"] = order
        url = base_url.rstrip("/") + "/rest/v1/" + urllib.parse.quote(_ident(source)) + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise_for_http(source, exc.code, _redact(detail, [key]), columns)
        payload = json.loads(body)
        if not isinstance(payload, list):
            raise RuntimeError(f"REST {source} did not return a row list")
        rows.extend(row for row in payload if isinstance(row, dict))
        if len(payload) < batch:
            break
        offset += len(payload)
    if len(rows) >= limit:
        print(f"scoring runs: {source} capped at {limit} rows")
    return rows


def fetch_db(db_url: str, source: str, columns, order: str | None, limit: int) -> list:
    try:
        import psycopg
        from psycopg.errors import UndefinedColumn, UndefinedTable
    except ImportError as exc:
        raise SourceMissing(source) from exc
    selected = ", ".join(_ident(column) for column in columns)
    sql = f"select {selected} from public.{_ident(source)}"
    if order:
        column, direction = order.split(".", 1)
        sql += f" order by {_ident(column)} {'desc' if direction.startswith('desc') else 'asc'} nulls last"
    sql += " limit %s"
    try:
        with psycopg.connect(db_url, connect_timeout=20) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (int(limit),))
                names = [desc.name for desc in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]
    except UndefinedTable:
        raise SourceMissing(source) from None
    except UndefinedColumn as exc:
        column = getattr(getattr(exc, "diag", None), "column_name", None)
        if column not in columns:
            column = named_column(str(exc), columns)
        raise ColumnMissing(column, str(exc)) from None


def make_reader(base_url: str, key: str | None, db_url: str | None):
    missing_rest: set[str] = set()
    secrets = [key or "", db_url or ""]

    def read_rows(source: str, columns, order: str | None, limit: int) -> list:
        if key and source not in missing_rest:
            try:
                return fetch_rest(base_url, key, source, columns, order, limit)
            except ColumnMissing:
                raise
            except SourceMissing:
                missing_rest.add(source)
                if not db_url:
                    raise
                print(f"scoring runs: REST {source} missing; trying SUPABASE_DB_URL")
            except Exception as exc:
                missing_rest.add(source)
                print(f"scoring runs: REST {source} skipped: {_redact(exc, secrets)}")
                if not db_url:
                    raise SourceMissing(source) from None
        if db_url:
            try:
                return fetch_db(db_url, source, columns, order, limit)
            except ColumnMissing:
                raise
            except SourceMissing:
                raise
            except Exception as exc:
                print(f"scoring runs: SQL {source} skipped: {_redact(exc, secrets)}")
                raise SourceMissing(source) from None
        raise SourceMissing(source)

    return read_rows


def read_with_columns(read_rows, source: str, columns, order: str | None, limit: int) -> list:
    cols = list(columns)
    while cols:
        order_column = order.split(".", 1)[0] if order else None
        use_order = order if order_column in cols else None
        try:
            return read_rows(source, cols, use_order, limit)
        except ColumnMissing as exc:
            column = exc.column if exc.column in cols else None
            if column is None:
                if len(cols) == 1:
                    raise SourceMissing(source) from exc
                cols = cols[:-1]
                continue
            cols = [item for item in cols if item != column]
    raise SourceMissing(source)


def read_first(read_rows, sources, columns, order: str | None, limit: int):
    notes = []
    for source in sources:
        try:
            rows = read_with_columns(read_rows, source, columns, order, limit)
            return source, rows, notes
        except SourceMissing as exc:
            notes.append(str(exc) or source)
    return None, None, notes


def collect(read_rows, fetched_at: str):
    run_source, runs, run_notes = read_first(read_rows, RUN_SOURCES, RUN_COLUMNS, "generated_at.desc", RUN_LIMIT)
    pred_source, predictions, pred_notes = read_first(
        read_rows, PREDICTION_SOURCES, PREDICTION_COLUMNS, "bar_time.desc", PREDICTION_LIMIT
    )
    version_source, versions, version_notes = read_first(
        read_rows, VERSION_SOURCES, VERSION_COLUMNS, "name.asc", VERSION_LIMIT
    )
    missing = []
    if run_source is None:
        missing.extend(run_notes or list(RUN_SOURCES))
    if pred_source is None:
        missing.extend(pred_notes or list(PREDICTION_SOURCES))
    if version_source is None:
        missing.extend(version_notes or list(VERSION_SOURCES))
    if run_source is None and pred_source is None and version_source is None:
        return None, missing
    bundle = build_bundle(runs or [], predictions or [], versions or [], fetched_at)
    bundle["sources"] = {
        "runs": run_source,
        "predictions": pred_source,
        "model_versions": version_source,
    }
    return bundle, missing


def publish(target: Path, base_url: str, key: str | None = None, db_url: str | None = None, *, now: str | None = None, read_rows=None) -> None:
    """Write the public runs snapshot. Never raises; a miss is a log line."""
    fetched_at = now or utc_now()
    secrets = [key or "", db_url or ""]
    try:
        if read_rows is None:
            if not key and not db_url:
                print("scoring runs export skipped: no Supabase credential")
                ensure_placeholder(target)
                return
            read_rows = make_reader(base_url, key, db_url)
        bundle, missing = collect(read_rows, fetched_at)
        if bundle is None:
            print("scoring runs export skipped: " + ", ".join(missing))
            ensure_placeholder(target)
            return
        for note in missing:
            print(f"scoring runs source skipped: {note}")
        write_public(target, bundle)
    except Exception as exc:
        print(f"scoring runs export skipped: {_redact(exc, secrets)}", file=sys.stderr)
        try:
            ensure_placeholder(target)
        except Exception as write_exc:
            print(f"scoring runs export skipped: {_redact(write_exc, secrets)}", file=sys.stderr)
        return
    calibration = bundle["calibration"]
    print(
        "scoring runs exported "
        f"runs={len(bundle['runs'])} "
        f"predictions={len(bundle['predictions'])} "
        f"model_versions={len(bundle['model_versions'])} "
        f"resolved={calibration['n_resolved']}"
    )


def main(argv: list[str] | None = None) -> int:
    del argv
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip() or None
    db_url = (os.environ.get("SUPABASE_DB_URL") or "").strip() or None
    base_url = (os.environ.get("SUPABASE_URL") or "https://bsnqwgbshwszbjncglqx.supabase.co").strip()
    publish(DATA, base_url, key, db_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
