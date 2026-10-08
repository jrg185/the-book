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

import export_kpi
import recon_kpi_realized as recon
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
    assert fee["reasons"] == ["fee_not_netted"]
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
