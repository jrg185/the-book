"""Synthetic FIFO, fee-net, recon, and dry-run tests.

Fixtures are invented. They are not warehouse rows and not broker figures.
Nothing here asserts a value published under data/.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import account_book
import book_seeds
import export_kpi
import recon_kpi_realized as recon
import refresh_kpi_snapshots as refresh
import sync_rh_kpi_trades as sync

BUY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
FULL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
REOPEN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3"
PARTIAL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4"
LATER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa5"
TAIL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa6"


def _row(ticker, side, qty, price, stamp, order_id, fee="0", sleeve="crypto", trade_id=None):
    return {
        "id": trade_id or order_id or f"null-{ticker}-{stamp}",
        "sleeve": sleeve,
        "ticker": ticker,
        "side": side,
        "qty": qty,
        "avg_price": price,
        "timestamp_et": stamp,
        "order_id": order_id,
        "fee_usd": fee,
        "pnl_trade_usd": "0",
    }


def _pnl(rows, order_id) -> Decimal:
    for row in sync.replay_rows(rows):
        if sync.order_id_of(row) == order_id:
            assert not row.get("replay_skip")
            return Decimal(row["pnl_trade_usd"])
    raise AssertionError(order_id)


def test_self_test_fifo_hook():
    sync.fifo_self_checks()


def test_qnt_shaped_fifo_ignores_legacy_twins_and_closed_lots():
    rows = [
        _row("QNT", "buy", "4", "10", "2026-01-01T00:00:00+00:00", BUY, trade_id="id-buy"),
        _row("QNT", "buy", "4", "10", "2026-01-01T00:00:01+00:00", None, trade_id="id-buy-legacy"),
        _row("QNT", "sell", "4", "14", "2026-01-02T00:00:00+00:00", FULL, "1", trade_id="id-full"),
        _row("QNT", "sell", "4", "14", "2026-01-02T00:00:01+00:00", None, "1", trade_id="id-full-legacy"),
        _row("QNT", "buy", "6", "20", "2026-01-03T00:00:00+00:00", REOPEN, trade_id="id-reopen"),
        _row("QNT", "sell", "2", "25", "2026-01-04T00:00:00+00:00", PARTIAL, "0.40", trade_id="id-partial"),
        _row("QNT", "buy", "3", "30", "2026-01-05T00:00:00+00:00", LATER, trade_id="id-later"),
        _row("QNT", "sell", "5", "28", "2026-01-06T00:00:00+00:00", TAIL, "0.10", trade_id="id-tail"),
    ]
    # (14-10)*4 - 1 = 15. A phantom twin buy would leave inventory and change this.
    assert _pnl(rows, FULL) == Decimal("15")
    # (25-20)*2 - 0.40 = 9.6. Closed lots at 10 must not be in the book.
    assert _pnl(rows, PARTIAL) == Decimal("9.6")
    # 4 @ 20 and 1 @ 30, then the fee: 32 - 2 - 0.10 = 29.9. A blend would differ.
    assert _pnl(rows, TAIL) == Decimal("29.9")
    lots = sync.lots_after(rows[:-1])[("crypto", "QNT")]
    assert [(lot["qty"], lot["px"]) for lot in lots] == [
        (Decimal("4"), Decimal("20")),
        (Decimal("3"), Decimal("30")),
    ]
    skipped = [row["id"] for row in sync.replay_rows(rows) if row.get("replay_skip") == "legacy_duplicate"]
    assert skipped == ["id-buy-legacy", "id-full-legacy"]


def test_average_versus_fifo_partial_sell():
    older = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
    newer = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
    half = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3"
    rows = [
        _row("AVG", "buy", "2", "10", "2026-01-01T00:00:00+00:00", older),
        _row("AVG", "buy", "2", "30", "2026-01-01T01:00:00+00:00", newer),
        _row("AVG", "sell", "2", "20", "2026-01-01T02:00:00+00:00", half, "0"),
    ]
    # Oldest lot is 10. Weighted average of 10 and 30 would realize 0.
    assert _pnl(rows, half) == Decimal("20")


def test_fee_net_and_market_maker_zero_fee():
    buy = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
    sell = "cccccccc-cccc-4ccc-8ccc-ccccccccccc2"
    assert _pnl(
        [
            _row("FEE", "buy", "1", "10", "2026-01-01T00:00:00+00:00", buy),
            _row("FEE", "sell", "1", "12", "2026-01-01T01:00:00+00:00", sell, "0.25"),
        ],
        sell,
    ) == Decimal("1.75")
    maker = "cccccccc-cccc-4ccc-8ccc-ccccccccccc3"
    assert _pnl(
        [
            _row("MM", "buy", "1", "10", "2026-01-01T00:00:00+00:00", buy),
            _row("MM", "sell", "1", "12", "2026-01-01T01:00:00+00:00", maker, "0"),
        ],
        maker,
    ) == Decimal("2")


def test_fee_comes_from_payload_not_a_rate():
    rows = sync.rows_from_orders(
        [
            {
                "id": "dddddddd-dddd-4ddd-8ddd-ddddddddddd1",
                "currency_code": "FEE",
                "side": "buy",
                "state": "filled",
                "cumulative_quantity": "10",
                "average_price": "10",
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "dddddddd-dddd-4ddd-8ddd-ddddddddddd2",
                "currency_code": "FEE",
                "side": "sell",
                "state": "filled",
                "cumulative_quantity": "10",
                "average_price": "11",
                "fees": [{"fee_data": {"fee_amount": "1.50"}}],
                "created_at": "2026-01-01T01:00:00Z",
            },
        ],
        [],
    )
    sell = rows[-1]
    assert sell["fee_usd"] == "1.5"
    # Gross 10. Net 8.5. A 0.95% rate on 110 would not be 1.50.
    assert Decimal(sell["pnl_trade_usd"]) == Decimal("8.5")


def test_duplicate_order_id_is_replayed_once():
    oid = "abababab-abab-4aba-8aba-abababababab"
    rows = [
        _row("DUP", "buy", "1", "10", "2026-01-01T00:00:00+00:00", oid, trade_id="first"),
        _row("DUP", "buy", "1", "10", "2026-01-01T00:00:05+00:00", oid, trade_id="second"),
    ]
    priced = sync.replay_rows(rows)
    assert priced[1]["replay_skip"] == "duplicate_order_id"
    lots = sync.lots_after(rows)[("crypto", "DUP")]
    assert len(lots) == 1
    assert lots[0]["qty"] == Decimal("1")


def test_oversell_guard_still_raises():
    rows = [
        _row("AAA", "buy", "1", "2", "2026-01-01T00:00:00+00:00", BUY),
        _row("AAA", "sell", "2", "3", "2026-01-01T01:00:00+00:00", FULL),
    ]
    try:
        sync.replay_rows(rows)
    except sync.SyncError as exc:
        assert "exceeds open" in str(exc)
    else:
        raise AssertionError("oversell did not fail")


def test_dry_run_without_seed_labels_sells_and_does_not_crash():
    orders = [
        {
            "id": "ffffffff-ffff-4fff-8fff-fffffffffff1",
            "currency_code": "AAA",
            "side": "sell",
            "state": "filled",
            "cumulative_quantity": "100",
            "average_price": "5",
            "fee": "0.20",
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    rows = sync.dry_run_rows(orders, None)
    assert rows[0]["pnl_trade_usd"] is None
    assert rows[0]["pnl_label"] == "not priced in dry-run"
    seeded = sync.dry_run_rows(
        [
            {
                "id": "ffffffff-ffff-4fff-8fff-fffffffffff2",
                "currency_code": "AAA",
                "side": "sell",
                "state": "filled",
                "cumulative_quantity": "4",
                "average_price": "5",
                "fee": "0.20",
                "created_at": "2026-09-28T11:00:00Z",
            }
        ],
        [
            {
                "sleeve": "crypto",
                "ticker": "AAA",
                "side": "buy",
                "qty": "10",
                "avg_price": "2",
                "timestamp_et": "2026-09-28T09:00:00+00:00",
                "order_id": "11111111-1111-4111-8111-111111111111",
                "fee_usd": "0",
                "pnl_trade_usd": "0",
            }
        ],
    )
    assert Decimal(seeded[0]["pnl_trade_usd"]) == Decimal("11.8")
    assert "pnl_label" not in seeded[0]


def _book():
    return [
        _row("XYZ", "buy", "1", "10", "2026-02-01T15:00:00+00:00", "11111111-1111-4111-8111-111111111111", trade_id="buy-1"),
        _row(
            "XYZ",
            "buy",
            "1",
            "10",
            "2026-02-01T15:00:01+00:00",
            None,
            trade_id="buy-legacy",
        ),
        {
            **_row(
                "XYZ",
                "sell",
                "1",
                "12",
                "2026-02-01T16:00:00+00:00",
                "22222222-2222-4222-8222-222222222222",
                "0.25",
                trade_id="sell-1",
            ),
            "pnl_trade_usd": "2",
        },
    ]


def _orders():
    return [
        {
            "id": "22222222-2222-4222-8222-222222222222",
            "currency_code": "XYZ",
            "side": "sell",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "12",
            "fee": "0.25",
            "created_at": "2026-02-01T16:00:00Z",
        }
    ]


def _pnl_row(gain, stamp="2026-02-01T16:03:00Z"):
    return {
        "symbol": "XYZ-USD",
        "quantity": "1",
        "realized_gain": gain,
        "price": "11.98",
        "timestamp": stamp,
        "order_id": "22222222-2222-4222-8222-222222222222",
    }


def test_recon_flags_fee_gap_and_labels_price_precision():
    # FIFO net = (12-10)*1 - 0.25 = 1.75. RH gross 2.02, net 1.77. Residual 0.02.
    report = recon.reconcile(_book(), [_pnl_row("2.02")], _orders())
    assert report["counts"]["matched"] == 1
    assert report["counts"]["legacy_duplicates"] == 1
    sell = report["matched_sells"][0]
    assert Decimal(sell["warehouse_pnl"]) == Decimal("2")
    assert Decimal(sell["rh_gross"]) == Decimal("2.02")
    assert Decimal(sell["fee"]) == Decimal("0.25")
    assert Decimal(sell["rh_net"]) == Decimal("1.77")
    assert Decimal(sell["diff"]) == Decimal("0.23")
    assert sell["flagged"] is True
    assert sell["fee_source"] == "rh_order"
    assert sell["residual_class"] == "rh_price_precision"
    assert report["price_precision"][0]["kpi_trades_id"] == "sell-1"
    assert report["legacy_duplicates"][0]["kpi_trades_id"] == "buy-legacy"
    assert report["legacy_duplicates"][0]["class"] == "legacy_duplicate"


def test_recon_matches_inside_a_few_minutes_and_rejects_a_far_print():
    near = recon.reconcile(_book(), [_pnl_row("2", "2026-02-01T16:04:00Z")], _orders())
    assert near["counts"]["matched"] == 1
    assert near["matched_sells"][0]["residual_class"] == "method_agrees"
    assert near["matched_sells"][0]["flagged"] is True
    far = recon.reconcile(_book(), [_pnl_row("1.75", "2026-02-01T16:20:00Z")], _orders())
    assert far["counts"]["matched"] == 0
    assert far["unmatched_sells"][0]["reason"] == "no_pnl_row"


def test_recon_without_orders_uses_the_warehouse_fee():
    report = recon.reconcile(_book(), [_pnl_row("2.02")], None)
    sell = report["matched_sells"][0]
    assert sell["fee_source"] == "warehouse_fee_usd"
    assert Decimal(sell["fee"]) == Decimal("0.25")
    assert Decimal(sell["rh_net"]) == Decimal("1.77")
    assert sell["residual_class"] == "rh_price_precision"


def test_recon_market_maker_zero_fee_order():
    rows = [
        _row("MM", "buy", "1", "10", "2026-03-01T15:00:00+00:00", "33333333-3333-4333-8333-333333333333", trade_id="mm-buy"),
        {
            **_row(
                "MM",
                "sell",
                "1",
                "12",
                "2026-03-01T16:00:00+00:00",
                "44444444-4444-4444-8444-444444444444",
                "0",
                trade_id="mm-sell",
            ),
            "pnl_trade_usd": "2",
        },
    ]
    orders = [
        {
            "id": "44444444-4444-4444-8444-444444444444",
            "currency_code": "MM",
            "side": "sell",
            "state": "filled",
            "cumulative_quantity": "1",
            "average_price": "12",
            "fee": "0",
            "created_at": "2026-03-01T16:00:00Z",
        }
    ]
    pnl = [{"symbol": "MM-USD", "quantity": "1", "realized_gain": "2", "timestamp": "2026-03-01T16:01:00Z"}]
    report = recon.reconcile(rows, pnl, orders)
    sell = report["matched_sells"][0]
    assert Decimal(sell["fee"]) == Decimal("0")
    assert Decimal(sell["rh_net"]) == Decimal("2")
    assert sell["flagged"] is False
    assert sell["residual_class"] == "method_agrees"


def test_plan_backfill_tags_fee_average_phantom_and_legacy():
    rows = [
        _row("FEE", "buy", "1", "10", "2026-04-01T15:00:00+00:00", "55555555-5555-4555-8555-555555555551", trade_id="fee-buy"),
        _row("FEE", "buy", "1", "10", "2026-04-01T15:00:01+00:00", None, trade_id="fee-legacy-buy"),
        {
            **_row(
                "FEE",
                "sell",
                "1",
                "12",
                "2026-04-01T16:00:00+00:00",
                "55555555-5555-4555-8555-555555555552",
                "0.25",
                trade_id="fee-sell",
            ),
            "pnl_trade_usd": "2",
        },
        _row("AVG", "buy", "2", "10", "2026-04-02T15:00:00+00:00", "55555555-5555-4555-8555-555555555553", trade_id="avg-buy-1"),
        _row("AVG", "buy", "2", "30", "2026-04-02T15:05:00+00:00", "55555555-5555-4555-8555-555555555554", trade_id="avg-buy-2"),
        {
            **_row(
                "AVG",
                "sell",
                "2",
                "20",
                "2026-04-02T16:00:00+00:00",
                "55555555-5555-4555-8555-555555555555",
                "0",
                trade_id="avg-sell",
            ),
            "pnl_trade_usd": "0",
        },
        _row("PH", "buy", "1", "10", "2026-04-03T15:00:00+00:00", "55555555-5555-4555-8555-555555555556", trade_id="ph-buy"),
        _row("PH", "buy", "1", "10", "2026-04-03T15:00:01+00:00", None, trade_id="ph-legacy-buy"),
        {
            **_row(
                "PH",
                "sell",
                "1",
                "12",
                "2026-04-03T16:00:00+00:00",
                "55555555-5555-4555-8555-555555555557",
                "0",
                trade_id="ph-sell-1",
            ),
            "pnl_trade_usd": "2",
        },
        _row("PH", "buy", "1", "30", "2026-04-03T17:00:00+00:00", "55555555-5555-4555-8555-555555555558", trade_id="ph-reopen"),
        {
            **_row(
                "PH",
                "sell",
                "1",
                "32",
                "2026-04-03T18:00:00+00:00",
                "55555555-5555-4555-8555-555555555559",
                "0",
                trade_id="ph-sell-2",
            ),
            "pnl_trade_usd": "22",
        },
        {
            **_row(
                "FEE",
                "sell",
                "1",
                "12",
                "2026-04-01T16:00:01+00:00",
                None,
                "0.25",
                trade_id="fee-legacy-sell",
            ),
            "pnl_trade_usd": "2",
        },
    ]
    # The legacy sell twin is one second after the real FEE sell. Same qty and price.
    plan = recon.plan_backfill(rows)
    by_id = {row["kpi_trades_id"]: row for row in plan["rows"]}
    fee = by_id["fee-sell"]
    assert fee["action"] == "UPDATE"
    # Stored pnl already matches FIFO gross. The gap is only the fee, so the
    # plan tags it and the live sync does not rewrite the stored row.
    assert fee["reasons"] == ["late_fee_not_netted"]
    seed = export_kpi.LEDGER_SEEDS["crypto"]
    assert fee["old_pnl_frac_of_book"] == format(export_kpi.q6(Decimal("2") / seed), "f")
    assert fee["new_pnl_frac_of_book"] == format(export_kpi.q6(Decimal("1.75") / seed), "f")
    assert "order_id" not in fee
    avg = by_id["avg-sell"]
    assert avg["action"] == "UPDATE"
    assert avg["reasons"] == ["avg_cost_not_fifo"]
    assert Decimal(avg["new_pnl_frac_of_book"]) == export_kpi.q6(Decimal("20") / seed)
    phantom = by_id["ph-sell-2"]
    assert phantom["action"] == "UPDATE"
    assert "phantom_lots_from_legacy_duplicates" in phantom["reasons"]
    assert by_id["fee-legacy-buy"]["action"] == "EXCLUDE_LEGACY_DUPLICATE_PENDING_APPROVAL"
    assert by_id["ph-legacy-buy"]["action"] == "EXCLUDE_LEGACY_DUPLICATE_PENDING_APPROVAL"
    assert by_id["fee-legacy-sell"]["action"] == "EXCLUDE_LEGACY_DUPLICATE_PENDING_APPROVAL"
    assert by_id["fee-legacy-sell"]["new_pnl_frac_of_book"] is None
    text = json.dumps(plan)
    assert "55555555-5555-4555-8555-555555555552" not in text
    assert plan["note"] == "PLANNED ONLY - do not apply until Eng and Wags approve."


def test_fee_backfill_never_touches_stored_pnl(monkeypatch):
    """A late fee fills fee_usd only. REST body and SQL both omit pnl_trade_usd."""
    sell_id = "22222222-2222-4222-8222-222222222222"
    buy_id = "11111111-1111-4111-8111-111111111111"
    priced = sync.map_orders(
        [
            {
                "id": sell_id,
                "currency_code": "AAA",
                "side": "sell",
                "state": "filled",
                "cumulative_quantity": "4",
                "average_price": "5",
                "fee": "0.20",
                "created_at": "2026-09-28T11:00:00Z",
            }
        ]
    )
    stored = [
        {
            "order_id": buy_id,
            "sleeve": "crypto",
            "ticker": "AAA",
            "side": "buy",
            "qty": "10",
            "avg_price": "2",
            "timestamp_et": "2026-09-28T09:00:00+00:00",
            "fee_usd": "0.10",
            "pnl_trade_usd": "0",
        },
        {
            "order_id": sell_id,
            "sleeve": "crypto",
            "ticker": "AAA",
            "side": "sell",
            "qty": "4",
            "avg_price": "5",
            "timestamp_et": "2026-09-28T11:00:00+00:00",
            "fee_usd": "0",
            "pnl_trade_usd": "12",
        },
    ]
    patches = sync.fee_backfill_for(priced, stored)
    assert patches == [{"order_id": sell_id, "fee_usd": "0.2"}]
    assert "pnl_trade_usd" not in patches[0]

    captured = []

    def fake_rest(base_url, key, path, method="GET", body=None, extra_headers=None):
        captured.append({"method": method, "path": path, "body": body})
        return [{"order_id": sell_id, "fee_usd": "0.2", "pnl_trade_usd": "12"}]

    monkeypatch.setattr(sync, "rest_call", fake_rest)
    stuffed = {"order_id": sell_id, "fee_usd": "0.2", "pnl_trade_usd": "11.8"}
    written = sync.apply_fee_backfill_rest("https://example.supabase.co", "key", [stuffed])
    assert written == 1
    assert captured[0]["method"] == "PATCH"
    assert captured[0]["body"] == {"fee_usd": "0.2"}
    assert "pnl_trade_usd" not in captured[0]["body"]

    executed = []

    class FakeCursor:
        rowcount = 1

        def execute(self, sql, params):
            executed.append((sql, dict(params)))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(sync, "connect_db", lambda db_url: FakeConn())
    sync.apply_fee_backfill_db("postgres://example", [stuffed])
    sql, params = executed[0]
    assert "pnl_trade_usd" not in sql.lower()
    assert set(params) == {"order_id", "fee_usd"}
    assert "pnl_trade_usd" not in sync.FEE_BACKFILL_SQL.lower()


def test_late_fee_is_classified_instead_of_rewritten():
    reasons = recon._reasons(
        stored=Decimal("2"),
        proposed=Decimal("1.75"),
        fee=Decimal("0.25"),
        dirty=Decimal("1.75"),
        average=Decimal("2"),
    )
    assert reasons == ["late_fee_not_netted"]
    mixed = recon._reasons(
        stored=Decimal("6"),
        proposed=Decimal("1"),
        fee=Decimal("0.25"),
        dirty=Decimal("1"),
        average=Decimal("1.25"),
    )
    assert "late_fee_not_netted" not in mixed
    assert "fee_not_netted" in mixed


def test_plan_no_change_when_already_within_tolerance():
    rows = [
        _row("OK", "buy", "1", "10", "2026-05-01T15:00:00+00:00", "66666666-6666-4666-8666-666666666661", trade_id="ok-buy"),
        {
            **_row(
                "OK",
                "sell",
                "1",
                "12",
                "2026-05-01T16:00:00+00:00",
                "66666666-6666-4666-8666-666666666662",
                "0",
                trade_id="ok-sell",
            ),
            "pnl_trade_usd": "2",
        },
    ]
    plan = recon.plan_backfill(rows)
    sell = plan["rows"][0]
    assert sell["action"] == "NO_CHANGE"
    assert sell["reasons"] == ["rounding"]
    assert sell["delta_frac"] == "0.000000"


def test_recon_cli_dry_run_writes_only_the_given_paths(tmp_path):
    kpi = tmp_path / "kpi.json"
    pnl = tmp_path / "pnl.json"
    orders = tmp_path / "orders.json"
    report_path = tmp_path / "report.json"
    plan_path = tmp_path / "plan.json"
    kpi.write_text(json.dumps(_book()), encoding="utf-8")
    pnl.write_text(json.dumps({"trades": [_pnl_row("2.02")]}), encoding="utf-8")
    orders.write_text(json.dumps({"results": _orders()}), encoding="utf-8")
    code = recon.main(
        [
            "--kpi-json",
            str(kpi),
            "--rh-pnl-json",
            str(pnl),
            "--rh-orders-json",
            str(orders),
            "--dry-run",
            "--output",
            str(report_path),
            "--plan-backfill",
            "--plan-output",
            str(plan_path),
        ]
    )
    assert code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert report["dry_run"] is True
    assert report["counts"]["flagged"] == 1
    assert plan["note"].startswith("PLANNED ONLY")
    assert plan["counts"]["EXCLUDE_LEGACY_DUPLICATE_PENDING_APPROVAL"] == 1


def test_committed_backfill_diff_is_scrubbed():
    path = ROOT / "reports" / "kpi_realized_backfill_diff.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["note"] == "PLANNED ONLY - do not apply until Eng and Wags approve."
    counts = payload["counts"]
    assert counts["UPDATE"] == 43
    assert counts["NO_CHANGE"] == 10
    assert counts["EXCLUDE_LEGACY_DUPLICATE_PENDING_APPROVAL"] == 7
    allowed = {
        "kpi_trades_id",
        "timestamp_et",
        "ticker",
        "side",
        "old_pnl_frac_of_book",
        "new_pnl_frac_of_book",
        "delta_frac",
        "reasons",
        "action",
    }
    forbidden = {
        "order_id",
        "qty",
        "avg_price",
        "price",
        "fee",
        "fee_usd",
        "pnl_trade_usd",
        "notional_usd",
        "realized_gain",
        "usd",
    }
    assert len(payload["rows"]) == 60
    for row in payload["rows"]:
        assert set(row) == allowed
        assert row["side"] == "sell"
        assert row["action"] in counts
        for key in forbidden:
            assert key not in row
        if row["action"] == "EXCLUDE_LEGACY_DUPLICATE_PENDING_APPROVAL":
            assert row["new_pnl_frac_of_book"] is None
            assert row["delta_frac"] is None
            continue
        old = Decimal(row["old_pnl_frac_of_book"])
        new = Decimal(row["new_pnl_frac_of_book"])
        delta = Decimal(row["delta_frac"])
        assert delta == export_kpi.q6(new - old)
        assert abs(old) < 1
        assert abs(new) < 1


def _acct(
    ticker,
    side,
    qty,
    price,
    stamp,
    pnl="0",
    fee="0",
    notional=None,
    sleeve="crypto",
):
    row = {
        "sleeve": sleeve,
        "ticker": ticker,
        "side": side,
        "qty": qty,
        "avg_price": price,
        "pnl_trade_usd": pnl,
        "fee_usd": fee,
        "timestamp_et": stamp,
        "order_id": f"fixture-{sleeve}-{ticker}-{stamp}-{side}",
    }
    if notional is not None:
        row["notional_usd"] = notional
    return row


def _crypto_flows(fills) -> Decimal:
    total = Decimal("0")
    for fill in fills:
        if str(fill.get("sleeve") or "crypto") != "crypto":
            continue
        qty = Decimal(fill["qty"])
        price = Decimal(fill["avg_price"])
        fee = Decimal(fill["fee_usd"])
        notional = Decimal(fill["notional_usd"]) if fill.get("notional_usd") not in (None, "") else qty * price
        if fill["side"] == "buy":
            total -= notional + fee
        else:
            total += notional - fee
    return total


def _funded_cash(fills, seed: Decimal) -> dict:
    """Cash that matches the combined seed plus crypto trade flows. Split across USD and USDC."""
    total = seed + _crypto_flows(fills)
    usdc = Decimal("3.25")
    return {"USD": format(total - usdc, "f"), "USDC": format(usdc, "f")}


def _sample_book():
    """Three buys, one partial sell, one full close. Cent notionals. Not a live book."""
    sells_aaa = (Decimal("11") - Decimal("10")) * Decimal("4") - Decimal("0.10")
    sells_bbb = (Decimal("6") - Decimal("5")) * Decimal("2") - Decimal("0.05")
    fills = [
        _acct("AAA", "buy", "4", "10", "2026-04-01T15:00:00Z", fee="0.40", notional="40.01"),
        _acct("BBB", "buy", "2", "5", "2026-04-01T15:01:00Z", fee="0.25", notional="10.00"),
        _acct("AAA", "buy", "4", "12", "2026-04-01T15:02:00Z", fee="0.48", notional="48.02"),
        _acct("AAA", "sell", "4", "11", "2026-04-01T16:00:00Z", pnl=format(sells_aaa, "f"), fee="0.10", notional="43.99"),
        _acct("BBB", "sell", "2", "6", "2026-04-01T16:05:00Z", pnl=format(sells_bbb, "f"), fee="0.05", notional="12.00"),
    ]
    marks = {("crypto", "AAA"): Decimal("13")}
    return fills, marks


def test_account_balance_is_cash_plus_lots_and_buy_fees_hit_realized():
    fills, marks = _sample_book()
    seed = book_seeds.current_seeds()["combined"]
    cash = _funded_cash(fills, seed)
    reading = account_book.statement(fills, marks, cash, seed)
    lots = Decimal("4") * Decimal("13")
    assert reading["balance_exact"] == account_book.cash_total(cash) + lots
    assert reading["realized_exact"] + reading["unrealized_exact"] == reading["running_exact"]
    assert reading["running_exact"] == reading["balance_exact"] - seed
    assert reading["realized_cents"] + reading["unrealized_cents"] == reading["running_cents"]
    assert reading["buy_fees"] == Decimal("0.40") + Decimal("0.25") + Decimal("0.48")
    assert reading["open_buy_fees"] == Decimal("0.48")
    assert reading["closed_buy_fees"] == Decimal("0.65")
    assert reading["realized_exact"] < reading["trade_pnl"]
    assert reading["trade_pnl"] - reading["realized_exact"] == reading["buy_fees"] + reading["rounding"]
    positions = export_kpi.card_positions(fills, marks)
    exact = sum(
        (
            Decimal(row["qty"]) * (Decimal(row["mark"]) - Decimal(row["avg_cost"]))
            for row in positions
            if row["sleeve"] == "crypto"
        ),
        Decimal("0"),
    )
    assert reading["unrealized_exact"] == exact
    for row in positions:
        mark = marks[(row["sleeve"], row["ticker"])]
        assert Decimal(row["mark"]) == mark
        assert Decimal(row["qty"]) * mark == Decimal(row["qty"]) * Decimal(row["mark"])


def test_refresh_and_export_share_one_balance():
    fills, marks = _sample_book()
    seed = book_seeds.current_seeds()["combined"]
    cash = _funded_cash(fills, seed)
    rows, _opens = refresh.build_rows(fills, marks, {}, "2026-04-02T00:00:00Z", cash=cash)
    combined = {row["sleeve"]: row for row in rows}["combined"]
    positions = export_kpi.card_positions(fills, marks)
    identity = account_book.position_identity(positions, cash, seed)
    assert identity is not None
    assert export_kpi.usd_equal(combined["running_balance_usd"], identity["balance_cents"])
    assert export_kpi.usd_equal(combined["running_pnl_usd"], identity["running_cents"])
    assert export_kpi.usd_equal(combined["realized_pnl_usd"], identity["realized_cents"])
    assert export_kpi.usd_equal(combined["unrealized_pnl_usd"], identity["unrealized_cents"])
    assert "not the account" in {row["sleeve"]: row for row in rows}["crypto"]["notes"]


def test_average_cost_full_close_gap_sums_to_the_cent_sliver():
    fifo_gross = Decimal("8")
    fee = Decimal("0.16")
    sliver = Decimal("0.004321")
    stored = fifo_gross - fee - sliver
    fills = [
        _acct("ZZZ", "buy", "4", "2", "2026-04-03T15:00:00Z", fee=format(fee, "f"), notional="8"),
        _acct(
            "ZZZ",
            "sell",
            "4",
            "4",
            "2026-04-03T18:00:00Z",
            pnl=format(stored, "f"),
            fee=format(fee, "f"),
            notional="16",
        ),
    ]
    seed = book_seeds.current_seeds()["combined"]
    cash = _funded_cash(fills, seed)
    reading = account_book.decompose(fills, {}, cash, seed)
    assert abs(reading["gap"] - reading["components_sum"]) < Decimal("1e-6")
    assert reading["basis_gap"] == -sliver
    assert reading["buy_fees"] == fee
    # Flat book: a second quote set does not move an open mark.
    same = account_book.decompose(fills, {}, cash, seed, snapshot_marks={})
    assert same["mark_drift"] == 0
    assert abs(same["gap"] - same["components_sum"]) < Decimal("1e-6")


def test_open_mark_drift_is_in_the_gap():
    fills, marks = _sample_book()
    seed = book_seeds.current_seeds()["combined"]
    cash = _funded_cash(fills, seed)
    other = {("crypto", "AAA"): Decimal("13.5")}
    reading = account_book.decompose(fills, marks, cash, seed, snapshot_marks=other)
    assert reading["mark_drift"] != 0
    assert abs(reading["gap"] - reading["components_sum"]) < Decimal("1e-6")


def test_cash_lines_usd_only_usdc_only_and_a_missing_drop(capsys):
    assert account_book.cash_total({"USD": "6"}) == Decimal("6")
    assert account_book.cash_total({"USDC": "4.5"}) == Decimal("4.5")
    assert account_book.cash_total({}) is None
    assert account_book.cash_total(None) is None
    fills, marks = _sample_book()
    usd_only = account_book.statement(fills, marks, {"USD": "20"})
    usdc_only = account_book.statement(fills, marks, {"USDC": "20"})
    assert usd_only["cash"] == Decimal("20")
    assert usdc_only["cash"] == Decimal("20")
    prior = {
        "combined": {
            "as_of": "2026-03-01T00:00:00Z",
            "realized_pnl_usd": "1.25",
            "unrealized_pnl_usd": "0.50",
            "running_pnl_usd": "1.75",
            "running_balance_usd": "9.25",
        }
    }
    rows, _opens = refresh.build_rows(fills, marks, prior, "2026-04-04T00:00:00Z")
    combined = {row["sleeve"]: row for row in rows}["combined"]
    assert combined["running_balance_usd"] == "9.250000"
    assert combined["realized_pnl_usd"] == "1.250000"
    assert combined["as_of"] == "2026-03-01T00:00:00Z"
    assert "cash drop absent" in capsys.readouterr().err
    assert "not a seed+realized+unrealized fallback" in combined["notes"]
    try:
        account_book.statement(fills, marks, None)
    except account_book.AccountBookError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("a missing cash drop built a book")


def test_failed_robinhood_cash_is_missing_and_unset_keys_use_the_drop(monkeypatch, tmp_path, capsys):
    (tmp_path / "rh_cash.json").write_text(json.dumps({"USD": 4, "USDC": 1}), encoding="utf-8")

    def boom(_env=None):
        raise RuntimeError("Robinhood cash read failed. Holdings were not refreshed.")

    monkeypatch.setattr(export_kpi, "load_rh_cash", boom)
    cash, origin = account_book.read_cash({"RH_API_KEY": "k", "RH_BASE64_PRIVATE_KEY": "p"}, tmp_path)
    assert cash is None and origin is None
    assert "treating cash as missing" in capsys.readouterr().err

    monkeypatch.setattr(export_kpi, "load_rh_cash", lambda _env=None: None)
    cash, origin = account_book.read_cash({}, tmp_path)
    assert origin == "rh_cash.json"
    assert account_book.cash_total(cash) == Decimal("5")


def test_equities_close_stays_out_of_the_account_residual():
    fills, marks = _sample_book()
    closed = list(fills) + [
        _acct("QCOM", "buy", "2", "100", "2026-04-01T15:10:00Z", sleeve="equities", notional="200"),
        _acct(
            "QCOM",
            "sell",
            "2",
            "110",
            "2026-04-01T16:10:00Z",
            pnl="20",
            sleeve="equities",
            notional="220",
        ),
    ]
    seed = book_seeds.current_seeds()["combined"]
    cash = _funded_cash(fills, seed)
    base = account_book.statement(fills, marks, _funded_cash(fills, seed), seed)
    reading = account_book.statement(closed, marks, cash, seed)
    assert reading["equities_realized"] == Decimal("20")
    assert reading["trade_pnl"] == base["trade_pnl"] + Decimal("20")
    assert reading["residual"] == base["residual"]
    assert abs(reading["residual"]) <= account_book.residual_tolerance()
    assert abs(reading["gap"] - reading["components_sum"]) < Decimal("1e-6")
    assert account_book.format_note(reading).split("residual ", 1)[1] == account_book.format_note(base).split(
        "residual ", 1
    )[1]
    rows, _opens = refresh.build_rows(closed, marks, {}, "2026-04-02T00:00:00Z", cash=cash)
    combined = {row["sleeve"]: row for row in rows}["combined"]
    assert combined["notes"] == account_book.format_note(reading)


def test_account_recon_exits_on_the_tolerance(tmp_path, capsys):
    fills, marks = _sample_book()
    seed = book_seeds.current_seeds()["combined"]
    cash = _funded_cash(fills, seed)
    kpi = tmp_path / "kpi.json"
    cash_path = tmp_path / "cash.json"
    marks_path = tmp_path / "marks.json"
    kpi.write_text(json.dumps(fills), encoding="utf-8")
    cash_path.write_text(json.dumps({key: str(value) for key, value in cash.items()}), encoding="utf-8")
    marks_path.write_text(
        json.dumps({f"{sleeve}:{ticker}": str(price) for (sleeve, ticker), price in marks.items()}),
        encoding="utf-8",
    )
    code = recon.main(
        [
            "--account",
            "--kpi-json",
            str(kpi),
            "--cash-json",
            str(cash_path),
            "--marks-json",
            str(marks_path),
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert "A buy fees" in "\n".join(report["lines"])
    assert abs(Decimal(report["gap_usd"]) - Decimal(report["components_sum_usd"])) < Decimal("1e-6")
    skewed = dict(cash)
    skewed["USD"] = str(Decimal(skewed["USD"]) + Decimal("5"))
    cash_path.write_text(json.dumps(skewed), encoding="utf-8")
    code = recon.main(
        [
            "--account",
            "--kpi-json",
            str(kpi),
            "--cash-json",
            str(cash_path),
            "--marks-json",
            str(marks_path),
            "--tolerance",
            "0.01",
        ]
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "warning: unexplained residual exceeds tolerance" in err


def test_export_reuses_the_refresh_marks(tmp_path, monkeypatch):
    fills, marks = _sample_book()
    path = tmp_path / "marks.json"
    monkeypatch.setenv("KPI_MARKS_PATH", str(path))
    account_book.save_marks(marks, "2026-04-05T00:00:00Z")

    def explode(_keys, _env):
        raise AssertionError("export fetched a second quote set")

    reused, origin = account_book.resolve_or_reuse([("crypto", "AAA")], explode, {})
    assert origin == "refresh"
    assert reused[("crypto", "AAA")] == Decimal("13")
    monkeypatch.delenv("KPI_MARKS_PATH")
    calls = []

    def once(keys, _env):
        calls.append(list(keys))
        return {key: (Decimal("13"), "fixture") for key in keys}

    fetched, origin = account_book.resolve_or_reuse([("crypto", "AAA")], once, {})
    assert origin == "quotes"
    assert calls == [[("crypto", "AAA")]]
    assert fetched[("crypto", "AAA")] == Decimal("13")
    _ = fills


def test_stale_marks_are_not_reused(tmp_path, monkeypatch, capsys):
    path = tmp_path / "marks.json"
    monkeypatch.setenv("KPI_MARKS_PATH", str(path))
    account_book.save_marks({("crypto", "AAA"): Decimal("13")}, "2026-04-05T00:00:00Z")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["written_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(payload), encoding="utf-8")
    calls = []

    def once(keys, _env):
        calls.append(list(keys))
        return {key: (Decimal("9"), "fixture") for key in keys}

    fetched, origin = account_book.resolve_or_reuse([("crypto", "AAA")], once, {})
    assert origin == "quotes"
    assert calls == [[("crypto", "AAA")]]
    assert fetched[("crypto", "AAA")] == Decimal("9")
    assert "stale quote set ignored" in capsys.readouterr().err
