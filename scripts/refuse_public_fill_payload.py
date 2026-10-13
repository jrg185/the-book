#!/usr/bin/env python3
"""Fail closed if a public Actions event carries a fills document.

workflow_dispatch inputs and repository_dispatch client_payload are stored
on the run. This public repo's logs and event payloads must not include
Robinhood fills. Desk upserts with scripts/upsert_fills_direct.py, then
dispatches Export KPI with no payload.

This script never prints the event body, order ids, or fill fields.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REFUSE = (
    "This workflow does not accept a fills payload. "
    "Run scripts/upsert_fills_direct.py, then dispatch Export KPI with no inputs "
    "and rh-fill with no client_payload."
)


def nonempty(value) -> bool:
    if isinstance(value, dict):
        return any(nonempty(item) for item in value.values())
    if isinstance(value, list):
        return any(nonempty(item) for item in value)
    return bool(str(value or "").strip())


def load_event(path: str) -> dict:
    if not path:
        return {}
    file = Path(path)
    if not file.is_file():
        return {}
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        sys.exit("workflow event could not be read. Fills were not accepted.")
    return payload if isinstance(payload, dict) else {}


def refuse(event_name: str, event: dict) -> str | None:
    name = (event_name or "").strip()
    if name == "workflow_dispatch" and nonempty(event.get("inputs")):
        return REFUSE
    if name == "repository_dispatch" and nonempty(event.get("client_payload")):
        return REFUSE
    return None


def main() -> int:
    reason = refuse(os.environ.get("GITHUB_EVENT_NAME") or "", load_event(os.environ.get("GITHUB_EVENT_PATH") or ""))
    if reason:
        print(reason, file=sys.stderr)
        return 1
    print(
        "No fill payload on this run. kpi_trades is updated by "
        "upsert_fills_direct.py or the hourly Robinhood poll."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
