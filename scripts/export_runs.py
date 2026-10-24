#!/usr/bin/env python3
"""Export public scoring runs for the Book.

Reads only safe columns from the public views, then the same columns on
the base tables when a view is not installed yet. Predictions are read
from public.run_predictions first: the service role bypasses RLS, and the
public view cannot push down order or limit. The features jsonb column is
never selected. Dollar amounts, fill_trade_id, order ids, and account
fields are not written.

A missing or empty relation is logged. It does not fail Export KPI.

  data/runs.json
  data/runs.csv
  data/predictions.json
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
# PostgREST page size for every paged read in this script. Not a live balance.
PAGE_SIZE = 1000
# Calibration reads every prediction (few columns) so aggregates are not cut by
# RUN_LIMIT or PREDICTION_LIMIT. Pages of PAGE_SIZE; this is a runaway guard only.
CALIBRATION_LIMIT = 5_000_000
CALIBRATION_COLUMNS = ("run_id", "bar_time", "symbol", "p_up", "label_up_net30", "resolved_at", "p_up_source")
# p_up_source value for probabilities read straight from the model payload (full precision).
FULL_PRECISION_SOURCE = "payload"


def full_precision_calibration(rows: list):
    """Scores over payload-sourced p_up only. None when the source column is absent."""
    if not any(isinstance(row, dict) and "p_up_source" in row for row in rows):
        return None
    subset = [row for row in rows if isinstance(row, dict) and row.get("p_up_source") == FULL_PRECISION_SOURCE]
    scores = calibrate(subset)
    return {"n_scored": scores["n_scored"], "logloss": scores["logloss"], "brier": scores["brier"]}

RUN_SOURCES = ("runs_public", "scoring_runs")
# Base table first. service_role bypasses RLS, and the public view wraps
# ledger_private.run_predictions_rows(), which materializes every row (including
# features) before PostgREST can apply order or limit. The migration primary
# key is (run_id, symbol); the table has no id column.
PREDICTION_TABLE = "run_predictions"
PREDICTION_SOURCES = (PREDICTION_TABLE, "run_predictions_public")
VERSION_SOURCES = ("model_versions_public", "model_versions")
# Newest bar first, then the primary key, so a tie on bar_time cannot skip or
# repeat a row. nullsfirst matches Postgres DESC default and the bar_time index.
PREDICTION_ORDER = "bar_time.desc.nullsfirst,run_id.desc,symbol.desc"
PREDICTION_KEY = ("bar_time", "run_id", "symbol")
# Not a sibling of data/*.json. That glob is the published-identifier scan, and
# this cache holds raw run_id values. Git does not publish it. Missing → full scan.
PREDICTION_STATE_NAME = ".cache/run_predictions_page_state.json"

RUN_COLUMNS = (
    "id",
    "model_version_id",
    "model_version",
    "model_version_name",
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


def union_columns(*groups) -> tuple:
    """Column names in first-seen order. The prediction fetch is this union."""
    seen = []
    for group in groups:
        for column in group:
            if column not in seen:
                seen.append(column)
    return tuple(seen)


# One read serves the public window and calibration. features is not in either list.
PREDICTION_FETCH_COLUMNS = union_columns(PREDICTION_COLUMNS, CALIBRATION_COLUMNS)
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
    "model_version_name",
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
    version_name = as_text(row.get("model_version_name"))
    version = as_text(row.get("model_version")) or version_name or as_text(row.get("model_name"))
    if not version and version_id:
        version = names.get(version_id)
    public = {
        "id": public_key(row.get("id"), RUN_KEY_LABEL, "run"),
        "model_version_id": public_key(version_id, VERSION_KEY_LABEL, "mv"),
        "model_version": version,
        "model_version_name": version_name or version,
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
    n_resolved = 0
    for row in predictions:
        label = as_bool(row.get("label_up_net30"))
        if not isinstance(label, bool):
            continue
        n_resolved += 1
        probability = as_float(row.get("p_up"))
        if probability is None:
            continue
        probability = min(1.0, max(0.0, float(probability)))
        resolved.append((probability, 1.0 if label else 0.0))
    if not resolved:
        return {"n_resolved": n_resolved, "n_scored": 0, "n_unscored": n_resolved, "logloss": None, "brier": None, "bins": []}
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
        # n_resolved: every prediction whose 8h label is known.
        # n_scored: the resolved ones that carry p_up; logloss, brier, and bins use these.
        "n_resolved": n_resolved,
        "n_scored": len(resolved),
        "n_unscored": n_resolved - len(resolved),
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


_FILTER_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def prediction_get_count(row_count: int, limit: int, page_size: int | None = None) -> int:
    """GETs one paging loop issues.

    A last page that fills the page size while still under ``limit`` costs one
    more GET that comes back empty. ``page_size`` defaults to PAGE_SIZE.
    """
    page = PAGE_SIZE if page_size is None else int(page_size)
    if page <= 0:
        raise RuntimeError("page size must be positive")
    if limit <= 0:
        return 0
    remaining = max(0, int(row_count))
    got = 0
    requests = 0
    while got < limit:
        requests += 1
        batch = min(page, limit - got)
        take = min(batch, remaining)
        remaining -= take
        got += take
        if take < batch:
            break
    return requests


def prediction_export_gets(row_count: int, *, new_rows: int | None = None, page_size: int | None = None) -> dict:
    """Prediction GETs for one export.

    ``before`` is the old pair of offset walks (public window, then calibration).
    ``after`` is one keyset walk of the same rows, or of ``new_rows`` when the
    caller already holds the previous rows.
    """
    before = prediction_get_count(row_count, PREDICTION_LIMIT, page_size) + prediction_get_count(
        row_count, CALIBRATION_LIMIT, page_size
    )
    scanned = row_count if new_rows is None else new_rows
    after = prediction_get_count(scanned, CALIBRATION_LIMIT, page_size)
    return {"before": before, "after": after}


def _filter_value(value) -> str:
    text = as_text(value)
    if not text:
        raise RuntimeError("keyset filter value is empty")
    if _FILTER_BARE.fullmatch(text):
        return text
    return '"' + text.replace('"', '""') + '"'


def _cond(column: str, op: str, value=None) -> str:
    if op in ("is", "not.is"):
        literal = "null" if value is None else _filter_value(value)
        return f"{_ident(column)}.{op}.{literal}"
    return f"{_ident(column)}.{op}.{_filter_value(value)}"


def prediction_cursor(row: dict) -> dict | None:
    """Tie-break key for one prediction. None when the primary key is incomplete."""
    if not isinstance(row, dict):
        return None
    run_id = as_text(row.get("run_id"))
    symbol = as_text(row.get("symbol"))
    if not run_id or not symbol:
        return None
    return {"bar_time": as_text(row.get("bar_time")), "run_id": run_id, "symbol": symbol}


def _older_than_filter(cursor: dict) -> str:
    """Rows that sort after cursor in PREDICTION_ORDER (older page)."""
    bar = cursor.get("bar_time")
    run_id = cursor["run_id"]
    symbol = cursor["symbol"]
    if bar is None:
        return (
            f"(and({_cond('bar_time', 'is')},{_cond('run_id', 'lt', run_id)}),"
            f"and({_cond('bar_time', 'is')},{_cond('run_id', 'eq', run_id)},{_cond('symbol', 'lt', symbol)}),"
            f"{_cond('bar_time', 'not.is')})"
        )
    return (
        f"({_cond('bar_time', 'lt', bar)},"
        f"and({_cond('bar_time', 'eq', bar)},{_cond('run_id', 'lt', run_id)}),"
        f"and({_cond('bar_time', 'eq', bar)},{_cond('run_id', 'eq', run_id)},{_cond('symbol', 'lt', symbol)}))"
    )


def _newer_than_filter(cursor: dict) -> str:
    """Rows that sort before cursor in PREDICTION_ORDER (strictly newer)."""
    bar = cursor.get("bar_time")
    run_id = cursor["run_id"]
    symbol = cursor["symbol"]
    if bar is None:
        return (
            f"(and({_cond('bar_time', 'is')},{_cond('run_id', 'gt', run_id)}),"
            f"and({_cond('bar_time', 'is')},{_cond('run_id', 'eq', run_id)},{_cond('symbol', 'gt', symbol)}))"
        )
    return (
        f"({_cond('bar_time', 'gt', bar)},"
        f"and({_cond('bar_time', 'eq', bar)},{_cond('run_id', 'gt', run_id)}),"
        f"and({_cond('bar_time', 'eq', bar)},{_cond('run_id', 'eq', run_id)},{_cond('symbol', 'gt', symbol)}))"
    )


def prediction_page_filter(*, older_than=None, newer_than=None):
    """PostgREST ``(param, value)`` for a keyset bound. None on the first full page."""
    bounds = []
    if newer_than is not None:
        bounds.append(_newer_than_filter(newer_than))
    if older_than is not None:
        bounds.append(_older_than_filter(older_than))
    if not bounds:
        return None
    if len(bounds) == 1:
        return "or", bounds[0]
    return "and", "(or" + bounds[0] + ",or" + bounds[1] + ")"


def _keyset_ready(source: str, columns, order: str | None) -> bool:
    if source != PREDICTION_TABLE or order != PREDICTION_ORDER:
        return False
    return set(PREDICTION_KEY) <= set(columns)


def _rest_headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "agentic-sleeves-kpi-export",
    }


def _rest_get(base_url: str, key: str, source: str, params: dict, columns) -> list:
    url = (
        base_url.rstrip("/")
        + "/rest/v1/"
        + urllib.parse.quote(_ident(source))
        + "?"
        + urllib.parse.urlencode(params)
    )
    request = urllib.request.Request(url, headers=_rest_headers(key))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise_for_http(source, exc.code, _redact(detail, [key]), columns)
    payload = json.loads(body)
    if not isinstance(payload, list):
        raise RuntimeError(f"REST {source} did not return a row list")
    return payload


def _fetch_rest_offset(base_url: str, key: str, source: str, columns, order: str | None, limit: int) -> list:
    rows: list = []
    offset = 0
    while len(rows) < limit:
        batch = min(PAGE_SIZE, limit - len(rows))
        params = {
            "select": ",".join(_ident(column) for column in columns),
            "limit": str(batch),
            "offset": str(offset),
        }
        if order:
            params["order"] = order
        payload = _rest_get(base_url, key, source, params, columns)
        rows.extend(row for row in payload if isinstance(row, dict))
        if len(payload) < batch:
            break
        offset += len(payload)
    if len(rows) >= limit:
        print(f"scoring runs: {source} capped at {limit} rows")
    return rows


def _fetch_rest_keyset(
    base_url: str,
    key: str,
    source: str,
    columns,
    order: str | None,
    limit: int,
    newer_than=None,
) -> list:
    rows: list = []
    older = None
    seen: set[tuple] = set()
    while len(rows) < limit:
        batch = min(PAGE_SIZE, limit - len(rows))
        params = {
            "select": ",".join(_ident(column) for column in columns),
            "limit": str(batch),
            "order": order or PREDICTION_ORDER,
        }
        bound = prediction_page_filter(older_than=older, newer_than=newer_than)
        if bound:
            params[bound[0]] = bound[1]
        payload = _rest_get(base_url, key, source, params, columns)
        page = [row for row in payload if isinstance(row, dict)]
        rows.extend(page)
        if not page or len(payload) < batch:
            break
        cursor = prediction_cursor(page[-1])
        if cursor is None:
            raise RuntimeError(f"REST {source} keyset page has no run_id and symbol")
        marker = (cursor.get("bar_time"), cursor["run_id"], cursor["symbol"])
        if marker in seen:
            raise RuntimeError(f"REST {source} keyset repeated a cursor")
        seen.add(marker)
        older = cursor
    if len(rows) >= limit:
        print(f"scoring runs: {source} capped at {limit} rows")
    return rows


def fetch_rest(
    base_url: str,
    key: str,
    source: str,
    columns,
    order: str | None,
    limit: int,
    after=None,
    paging: str | None = None,
) -> list:
    """Read one relation.

    ``run_predictions`` uses keyset pages on ``(bar_time, run_id, symbol)``.
    ``after`` is the newest row already on hand; the read returns only rows
    that sort strictly ahead of it. Other relations keep offset pages. A view
    fallback ignores ``after`` and returns the full offset read so the caller
    can still merge.
    """
    use_keyset = paging == "keyset" or (paging is None and _keyset_ready(source, columns, order))
    if paging == "offset":
        use_keyset = False
    if use_keyset:
        if not set(PREDICTION_KEY) <= set(columns):
            raise RuntimeError("keyset paging needs bar_time, run_id, and symbol")
        return _fetch_rest_keyset(base_url, key, source, columns, order, limit, newer_than=after)
    return _fetch_rest_offset(base_url, key, source, columns, order, limit)


def _order_sql(order: str | None) -> str:
    if not order:
        return ""
    parts = []
    for term in order.split(","):
        pieces = [piece for piece in term.strip().split(".") if piece]
        if not pieces:
            continue
        column = _ident(pieces[0])
        direction = "asc"
        nulls = None
        for piece in pieces[1:]:
            lowered = piece.lower()
            if lowered.startswith("desc"):
                direction = "desc"
            elif lowered.startswith("asc"):
                direction = "asc"
            elif lowered == "nullsfirst":
                nulls = "first"
            elif lowered == "nullslast":
                nulls = "last"
        if nulls is None:
            nulls = "last"
        parts.append(f"{column} {direction} nulls {nulls}")
    if not parts:
        return ""
    return " order by " + ", ".join(parts)


def _prediction_newer_sql(cursor: dict) -> tuple[str, list]:
    """SQL for rows strictly newer than cursor. Values stay parameters."""
    bar = cursor.get("bar_time")
    run_id = cursor["run_id"]
    symbol = cursor["symbol"]
    if bar is None:
        sql = (
            "(bar_time is null and run_id > %s::uuid)"
            " or (bar_time is null and run_id = %s::uuid and symbol > %s)"
        )
        return sql, [run_id, run_id, symbol]
    sql = (
        "(bar_time > %s::timestamptz)"
        " or (bar_time = %s::timestamptz and run_id > %s::uuid)"
        " or (bar_time = %s::timestamptz and run_id = %s::uuid and symbol > %s)"
    )
    return sql, [bar, bar, run_id, bar, run_id, symbol]


def fetch_db(db_url: str, source: str, columns, order: str | None, limit: int, after=None) -> list:
    try:
        import psycopg
        from psycopg.errors import UndefinedColumn, UndefinedTable
    except ImportError as exc:
        raise SourceMissing(source) from exc
    selected = ", ".join(_ident(column) for column in columns)
    sql = f"select {selected} from public.{_ident(source)}"
    params: list = []
    if after is not None and source == PREDICTION_TABLE:
        where, where_params = _prediction_newer_sql(after)
        sql += " where " + where
        params.extend(where_params)
    sql += _order_sql(order)
    sql += " limit %s"
    params.append(int(limit))
    try:
        with psycopg.connect(db_url, connect_timeout=20) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
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

    def read_rows(source: str, columns, order: str | None, limit: int, after=None) -> list:
        if key and source not in missing_rest:
            try:
                return fetch_rest(base_url, key, source, columns, order, limit, after=after)
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
                return fetch_db(db_url, source, columns, order, limit, after=after)
            except ColumnMissing:
                raise
            except SourceMissing:
                raise
            except Exception as exc:
                print(f"scoring runs: SQL {source} skipped: {_redact(exc, secrets)}")
                raise SourceMissing(source) from None
        raise SourceMissing(source)

    return read_rows


def _invoke_reader(read_rows, source, cols, use_order, limit, after):
    if after is None:
        return read_rows(source, cols, use_order, limit)
    try:
        return read_rows(source, cols, use_order, limit, after=after)
    except TypeError as exc:
        if "after" not in str(exc):
            raise
        return read_rows(source, cols, use_order, limit)


def read_with_columns(read_rows, source: str, columns, order: str | None, limit: int, after=None) -> list:
    cols = list(columns)
    while cols:
        order_column = order.split(".", 1)[0] if order else None
        use_order = order if order_column in cols else None
        try:
            return _invoke_reader(read_rows, source, cols, use_order, limit, after)
        except ColumnMissing as exc:
            column = exc.column if exc.column in cols else None
            if column is None:
                if len(cols) == 1:
                    raise SourceMissing(source) from exc
                cols = cols[:-1]
                continue
            cols = [item for item in cols if item != column]
    raise SourceMissing(source)


def read_first(read_rows, sources, columns, order: str | None, limit: int, after=None):
    notes = []
    for source in sources:
        try:
            rows = read_with_columns(read_rows, source, columns, order, limit, after=after)
            return source, rows, notes
        except SourceMissing as exc:
            notes.append(str(exc) or source)
    return None, None, notes


def derive_prediction_outputs(rows: list) -> tuple[list, list]:
    """Public window and calibration rows from one ordered fetch."""
    ordered = [row for row in rows or [] if isinstance(row, dict)]
    return ordered[:PREDICTION_LIMIT], ordered


def _prediction_key(row: dict):
    cursor = prediction_cursor(row)
    if cursor is None:
        return None
    return (cursor["run_id"], cursor["symbol"])


def merge_prediction_rows(prior: list, fresh: list) -> list:
    """Null bar times, then rows newer than the watermark, then the rest.

    Both inputs are in PREDICTION_ORDER. Fresh rows replace the same primary key.
    """
    fresh_rows = []
    seen: set[tuple] = set()
    for row in fresh or []:
        if not isinstance(row, dict):
            continue
        key = _prediction_key(row)
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        fresh_rows.append(row)
    nulls = []
    rest = []
    for row in prior or []:
        if not isinstance(row, dict):
            continue
        key = _prediction_key(row)
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        cursor = prediction_cursor(row)
        if cursor and cursor.get("bar_time") is None:
            nulls.append(row)
        else:
            rest.append(row)
    return nulls + fresh_rows + rest


def incremental_cursor(rows: list) -> dict | None:
    """Newest non-null bar_time already on hand.

    Page order is nulls-first, so the first row is not the max bar_time.
    A cache of only null bar times cannot anchor an incremental read.
    """
    for row in rows or []:
        cursor = prediction_cursor(row)
        if cursor and cursor.get("bar_time"):
            return cursor
    return None


def _read_predictions(read_rows, prior):
    prior_rows = [row for row in (prior or []) if isinstance(row, dict)]
    cursor = incremental_cursor(prior_rows) if prior_rows else None
    source, rows, notes = read_first(
        read_rows,
        PREDICTION_SOURCES,
        PREDICTION_FETCH_COLUMNS,
        PREDICTION_ORDER,
        CALIBRATION_LIMIT,
        after=cursor,
    )
    if source is None:
        return None, None, notes
    fetched = list(rows or [])
    if cursor is not None:
        fetched = merge_prediction_rows(prior_rows, fetched)
    mode = "incremental" if cursor is not None else "full"
    print(f"scoring runs: {source} {mode} rows={len(fetched)}")
    return source, fetched, notes


def collect(read_rows, fetched_at: str, *, prior_predictions=None, fetched_out=None):
    run_source, runs, run_notes = read_first(read_rows, RUN_SOURCES, RUN_COLUMNS, "generated_at.desc", RUN_LIMIT)
    pred_source, predictions, pred_notes = _read_predictions(read_rows, prior_predictions)
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
    window: list = []
    calibration_rows = None
    if pred_source is not None:
        window, calibration_rows = derive_prediction_outputs(predictions or [])
    bundle = build_bundle(runs or [], window, versions or [], fetched_at)
    if calibration_rows is not None:
        bundle["calibration"] = calibrate(calibration_rows)
        bundle["calibration"]["scope"] = "all_predictions"
        bundle["calibration"]["full_precision"] = full_precision_calibration(calibration_rows)
        if (
            fetched_out is not None
            and calibration_rows
            and len(calibration_rows) < CALIBRATION_LIMIT
        ):
            fetched_out.extend(calibration_rows)
    else:
        bundle["calibration"]["scope"] = "exported_predictions"
        bundle["calibration"]["full_precision"] = None
    bundle["sources"] = {
        "runs": run_source,
        "predictions": pred_source,
        "model_versions": version_source,
    }
    return bundle, missing


def remembered_predictions(folder: Path) -> list | None:
    """Prior raw prediction rows, or None when this folder has no usable cache."""
    path = folder / PREDICTION_STATE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("complete") is not True:
        return None
    if payload.get("order") != PREDICTION_ORDER:
        return None
    if list(payload.get("columns") or []) != list(PREDICTION_FETCH_COLUMNS):
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) >= CALIBRATION_LIMIT:
        return None
    if not all(isinstance(row, dict) and prediction_cursor(row) for row in rows):
        return None
    return rows


def _plain_state_row(row: dict) -> dict | None:
    """JSON values for the page cache. None when a cell is not a scalar."""
    plain = {}
    for key, value in row.items():
        if isinstance(value, bool) or value is None or isinstance(value, (int, float, str)):
            plain[key] = value
            continue
        if isinstance(value, dt.datetime):
            plain[key] = as_text(value)
            continue
        text = as_text(value)
        if text is None:
            continue
        plain[key] = text
    return plain


def remember_prediction_state(folder: Path, rows: list) -> None:
    """Write the raw page cache. A capped or empty read is not a resume point."""
    if not rows or len(rows) >= CALIBRATION_LIMIT:
        return
    plain = []
    for row in rows:
        if prediction_cursor(row) is None:
            return
        stored = _plain_state_row(row)
        if stored is None or prediction_cursor(stored) is None:
            return
        plain.append(stored)
    payload = {
        "order": PREDICTION_ORDER,
        "columns": list(PREDICTION_FETCH_COLUMNS),
        "complete": True,
        "rows": plain,
    }
    path = folder / PREDICTION_STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


# Latest desk score. The public raw URL of signals/latest.json 404s when that
# repo is private, so the warehouse row is a source, not a hardcoded day.
ARTIFACT_TABLE = "signal_artifacts"
PREDICTION_PUBLIC_FIELDS = ("symbol", "side", "p_up", "conviction", "bar_time", "expires_at")
SIGNAL_DOC_FIELDS = ("generated_at", "mode", "backend", "model_version", "last_bar_time")


def parse_stamp(value) -> dt.datetime | None:
    """UTC timestamp truncated to a second. None when the value is not a time."""
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = as_text(value)
        if not text:
            return None
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0)


def predictions_are_stale(predictions_at, artifact_at) -> bool:
    """True when the predictions stamp is older than the newest artifact.

    A missing artifact stamp is not a comparison. A missing predictions stamp
    is stale once an artifact stamp exists. There is no fixed cutoff date.
    """
    artifact = parse_stamp(artifact_at)
    if artifact is None:
        return False
    predictions = parse_stamp(predictions_at)
    if predictions is None:
        return True
    return predictions < artifact


def require_fresh_predictions(predictions_at, artifact_at) -> None:
    if predictions_are_stale(predictions_at, artifact_at):
        raise RuntimeError(
            "predictions "
            f"{as_text(predictions_at) or 'missing'} are older than the newest signal artifact "
            f"{as_text(artifact_at)}"
        )


def signal_document(row: dict | None) -> dict | None:
    """Signal JSON from a warehouse row or from signals/latest.json itself."""
    if not isinstance(row, dict):
        return None
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = None
    doc = dict(payload) if isinstance(payload, dict) else {}
    if not doc and any(key in row for key in ("candidates", "generated_at", "model_version")):
        doc = dict(row)
    if not doc.get("generated_at") and row.get("generated_at") is not None:
        doc["generated_at"] = row.get("generated_at")
    if not doc.get("mode") and row.get("mode"):
        doc["mode"] = row.get("mode")
    if not doc.get("generated_at") and not isinstance(doc.get("candidates"), list):
        return None
    return doc


def signal_generated_at(doc: dict | None):
    if not isinstance(doc, dict):
        return None
    return doc.get("generated_at")


def choose_newer_signal(github_doc: dict | None, artifact_doc: dict | None):
    """Prefer the document whose generated_at is newer. None when both are empty."""
    github = github_doc if isinstance(github_doc, dict) else None
    artifact = artifact_doc if isinstance(artifact_doc, dict) else None
    if github and artifact:
        if predictions_are_stale(signal_generated_at(github), signal_generated_at(artifact)):
            return artifact, ARTIFACT_TABLE
        return github, "signals/latest.json"
    if artifact:
        return artifact, ARTIFACT_TABLE
    if github:
        return github, "signals/latest.json"
    return None, None


def predictions_document(signal: dict, *, source: str) -> dict:
    """Public predictions for one signal. Dollar keys and private ids stay off."""
    public = {"source": source}
    for key in SIGNAL_DOC_FIELDS:
        if forbidden_key(key):
            continue
        value = as_text(signal.get(key))
        if value is not None:
            public[key] = value
    rows = []
    for cand in signal.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        row = {}
        for key in PREDICTION_PUBLIC_FIELDS:
            if forbidden_key(key):
                continue
            if key in ("p_up", "conviction"):
                row[key] = as_float(cand.get(key))
            else:
                row[key] = as_text(cand.get(key))
        if not row.get("symbol"):
            continue
        rows.append(row)
    rows.sort(key=lambda item: (item.get("symbol") or "", item.get("bar_time") or ""))
    public["predictions"] = rows
    return public


def fetch_latest_signal_artifact(base_url: str, key: str | None, db_url: str | None) -> dict | None:
    """Newest signal_artifacts row as a signal document. None when it cannot be read."""
    if not key and not db_url:
        return None
    columns = ("generated_at", "mode", "payload")
    read_rows = make_reader(base_url, key, db_url)
    rows = None
    for order in ("generated_at.desc", "created_at.desc", None):
        try:
            rows = read_with_columns(read_rows, ARTIFACT_TABLE, columns, order, 1)
            break
        except (SourceMissing, ColumnMissing):
            rows = None
    if not rows:
        return None
    return signal_document(rows[0])


def write_predictions(target: Path, document: dict) -> None:
    text = json.dumps(document, indent=2, allow_nan=False) + "\n"
    problems = text_violations(text)
    if problems:
        raise RuntimeError("predictions export blocked by public scrub: " + ", ".join(problems))
    target.mkdir(parents=True, exist_ok=True)
    (target / "predictions.json").write_text(text, encoding="utf-8")


def publish_predictions(target: Path, signal: dict | None, *, source: str | None = None, newest_artifact_at=None) -> dict | None:
    """Write data/predictions.json from the newest signal. Raises when that file would be stale."""
    if not isinstance(signal, dict):
        require_fresh_predictions(None, newest_artifact_at)
        print("predictions export skipped: no signal artifact")
        return None
    document = predictions_document(signal, source=source or ARTIFACT_TABLE)
    require_fresh_predictions(document.get("generated_at"), newest_artifact_at)
    write_predictions(target, document)
    print(
        "predictions exported "
        f"generated_at={document.get('generated_at')} "
        f"n={len(document.get('predictions') or [])} "
        f"source={document.get('source')}"
    )
    return document


def publish(
    target: Path,
    base_url: str,
    key: str | None = None,
    db_url: str | None = None,
    *,
    now: str | None = None,
    read_rows=None,
    prior_predictions=None,
) -> None:
    """Write the public runs snapshot. Never raises; a miss is a log line.

    A hidden page cache in ``target`` is the prior row set. When it is absent
    the prediction read is a full keyset scan. The cache is not a published file.
    """
    fetched_at = now or utc_now()
    secrets = [key or "", db_url or ""]
    try:
        if read_rows is None:
            if not key and not db_url:
                print("scoring runs export skipped: no Supabase credential")
                ensure_placeholder(target)
                return
            read_rows = make_reader(base_url, key, db_url)
        if prior_predictions is None:
            prior_predictions = remembered_predictions(target)
        fetched_out: list = []
        bundle, missing = collect(
            read_rows,
            fetched_at,
            prior_predictions=prior_predictions,
            fetched_out=fetched_out,
        )
        if bundle is None:
            print("scoring runs export skipped: " + ", ".join(missing))
            ensure_placeholder(target)
            return
        for note in missing:
            print(f"scoring runs source skipped: {note}")
        write_public(target, bundle)
        if fetched_out:
            try:
                remember_prediction_state(target, fetched_out)
            except Exception:
                print("scoring runs: page state not saved")
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


def _read_signal_file(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    document = signal_document(payload) if isinstance(payload, dict) else None
    if document is None:
        raise RuntimeError(f"{path} is not a signal document")
    return document


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-signal",
        type=Path,
        help="Write data/predictions.json from this signal JSON instead of reading the warehouse",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Directory for predictions.json. Defaults to data/",
    )
    parser.add_argument(
        "--newest-artifact-at",
        help="ISO timestamp of the newest signal artifact. The write fails when predictions are older.",
    )
    parser.add_argument(
        "--require-fresh",
        action="store_true",
        help="Exit 1 when --predictions is older than --newest-artifact-at",
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        help="Existing predictions.json to compare with --newest-artifact-at",
    )
    parser.add_argument("--source", default=None, help="Source label stored on the predictions file")
    args = parser.parse_args(argv)
    if args.require_fresh:
        if args.predictions is None:
            print("predictions freshness check needs --predictions", file=sys.stderr)
            return 1
        payload = json.loads(args.predictions.read_text(encoding="utf-8"))
        stamp = payload.get("generated_at") if isinstance(payload, dict) else None
        try:
            require_fresh_predictions(stamp, args.newest_artifact_at)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"predictions fresh generated_at={stamp}")
        return 0
    if args.from_signal is not None:
        signal = _read_signal_file(args.from_signal)
        try:
            publish_predictions(
                args.out or DATA,
                signal,
                source=args.source or "signals/latest.json",
                newest_artifact_at=args.newest_artifact_at,
            )
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip() or None
    db_url = (os.environ.get("SUPABASE_DB_URL") or "").strip() or None
    base_url = (os.environ.get("SUPABASE_URL") or "https://bsnqwgbshwszbjncglqx.supabase.co").strip()
    publish(DATA, base_url, key, db_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
