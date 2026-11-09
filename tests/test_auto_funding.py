"""AUTO PAPER asienta sólo financiación pública verificable y sin duplicados."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import time

import pytest

from app.derivatives.funding import FundingHistoryError, FundingRecord, HOUR_NS
from app.derivatives.funding_worker import AutoFunding, AutoFundingWorker
from app.derivatives.paper_math import InstrumentSpec, MarginRules, PaperPerpError, PerpQuote
from app.derivatives.paper_store import PaperPerpStore
from app.marketdata.adapters.grvt import GrvtHistoryError
from tests.test_signal_monitor import START, STEP, harness


NATIVE = "BTC_USDT_Perp"
SYMBOL = "BTC/USDT:PERP"


def _funding_source(state):
    class Source:
        def get_funding_history(self, symbol, opened_at_ns, end_ns):
            state["calls"] += 1
            assert symbol == SYMBOL and opened_at_ns == (START + 6 * STEP + 1) * 1_000_000_000
            assert end_ns <= state["now"] * 1_000_000_000
            if not state["available"]:
                raise GrvtHistoryError("histórico GRVT incompleto")
            last = end_ns // HOUR_NS
            first = START * 1_000_000_000 // HOUR_NS
            return [{"instrument": NATIVE, "funding_time": str(hour * HOUR_NS),
                     "funding_rate": "0.01", "mark_price": "100",
                     "funding_interval_hours": 1}
                    for hour in range(last, first + 5, -1)]
    return Source


def _open_auto(tmp_path):
    monitor, _, state, paper, _, account, dataset, strategy, _ = harness(tmp_path)
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"],
                             "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 1
    monitor.poll(session["id"])
    position = paper.positions(account["id"])[0]
    assert position["mode"] == "PAPER_AUTO"
    state["available"], state["calls"] = True, 0
    funding = AutoFunding(paper, adapter_factory=_funding_source(state),
                          clock_ns=lambda: state["now"] * 1_000_000_000)
    return monitor, state, paper, account, position, funding


def test_auto_funding_books_short_credit_then_finalizes_closed_position(tmp_path):
    _, state, paper, account, position, funding = _open_auto(tmp_path)
    assert [item["id"] for item in funding.candidates()] == [position["id"]]
    state["now"] = START + 7 * STEP + 30
    booked = funding.poll(position["id"])
    assert booked["poll_result"] == "BOOKED" and booked["new_settlements"] == 1
    assert Decimal(booked["new_payment"]) == Decimal("-0.01")
    assert paper.position(account["id"], position["id"])["funding_paid"] == "-0.01"
    assert paper.position(account["id"], position["id"])["funding_anchor_status"] == "PUBLIC_ANCHORED"
    with paper.connect() as db:
        anchor = db.execute("SELECT * FROM paper_perp_funding_anchors WHERE position_id=?",
                            (position["id"],)).fetchone()
        assert anchor["funding_time_ns"] < position["opened_at_ns"]
        assert anchor["interval_hours"] == 1
        assert anchor["source"] == "grvt-public-funding-interval"
    assert PaperPerpStore(paper.path).position(account["id"], position["id"])["funding_paid"] == "-0.01"
    assert funding.poll(position["id"])["poll_result"] == "WAIT"
    assert len([row for row in paper.events(account["id"]) if row["kind"] == "FUNDING_PAPER"]) == 1

    state["now"] += 1
    quote = PerpQuote(NATIVE, state["now"] * 1_000_000_000,
                      Decimal("100"), Decimal("99"), Decimal("101"))
    result = paper.close_position(account["id"], position["id"], "funding-close",
                                  paper.digest({"test": "funding-close"}), quote)
    assert result["position"]["funding_coverage"] == "PARTIAL"
    final = funding.poll(position["id"])
    assert final["poll_result"] == "VERIFIED_NO_NEW"
    assert paper.position(account["id"], position["id"])["funding_coverage"] == "CLOSED_COMPLETE"
    assert funding.candidates() == []


def test_auto_funding_retries_missing_history_without_zero_or_duplicate(tmp_path):
    _, state, paper, account, position, funding = _open_auto(tmp_path)
    state["now"] = START + 7 * STEP + 30
    state["available"] = False
    worker = AutoFundingWorker(funding, retry_seconds=0.01, idle_seconds=0.01)
    assert worker.run_once()[position["id"]] == "RETRY"
    assert paper.position(account["id"], position["id"])["funding_coverage"] == "UNASSESSED"
    assert paper.position(account["id"], position["id"])["funding_paid"] == "0"
    state["available"] = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(funding.poll, [position["id"], position["id"]]))
    assert sum(item.get("new_settlements", 0) for item in results) == 1
    assert sorted(item["poll_result"] for item in results) in (["BOOKED", "VERIFIED_NO_NEW"],
                                                                 ["BOOKED", "WAIT"])
    assert len([row for row in paper.events(account["id"]) if row["kind"] == "FUNDING_PAPER"]) == 1
    assert paper.position(account["id"], position["id"])["funding_paid"] == "-0.01"


def test_public_anchor_change_cannot_rewrite_booked_paper_history(tmp_path):
    _, state, paper, account, position, funding = _open_auto(tmp_path)
    state["now"] = START + 7 * STEP + 30
    booked = funding.poll(position["id"])
    assert booked["new_settlements"] == 1
    raw = _funding_source(state)().get_funding_history(
        SYMBOL, position["opened_at_ns"], booked["through_ns"])
    records = [FundingRecord.from_payload(item, NATIVE) for item in raw]
    anchor_time = max(item.time_ns for item in records if item.time_ns < position["opened_at_ns"])
    changed = [FundingRecord(item.instrument, item.time_ns, Decimal("0.02"), item.mark,
                             item.interval_hours) if item.time_ns == anchor_time else item
               for item in records]
    with pytest.raises(FundingHistoryError, match="ancla PAPER"):
        paper.settle_funding(account["id"], position["id"], "changed-anchor",
                             paper.digest({"test": "changed-anchor"}), changed,
                             booked["through_ns"])
    assert paper.position(account["id"], position["id"])["funding_paid"] == "-0.01"


def test_closed_auto_position_is_reconciled_after_restart_and_pnl_corrected(tmp_path):
    _, state, paper, account, position, funding = _open_auto(tmp_path)
    state["now"] = START + 7 * STEP + 30
    quote = PerpQuote(NATIVE, state["now"] * 1_000_000_000,
                      Decimal("100"), Decimal("99"), Decimal("101"))
    closed = paper.close_position(account["id"], position["id"], "close-before-funding",
                                  paper.digest({"test": "close-before-funding"}), quote)
    before = Decimal(closed["position"]["realized_pnl"])
    assert closed["position"]["funding_coverage"] == "UNASSESSED"
    recovered = AutoFunding(paper, adapter_factory=_funding_source(state),
                            clock_ns=lambda: state["now"] * 1_000_000_000)
    result = recovered.poll(position["id"])
    after = paper.position(account["id"], position["id"])
    assert result["poll_result"] == "BOOKED" and result["new_payment"] == "-0.01"
    assert after["funding_coverage"] == "CLOSED_COMPLETE"
    assert Decimal(after["realized_pnl"]) - before == Decimal("0.01")
    assert recovered.poll(position["id"])["poll_result"] == "COMPLETE_OR_NOT_AUTO"


def test_same_tick_auto_close_does_not_retry_nonexistent_funding(tmp_path):
    _, state, paper, account, position, funding = _open_auto(tmp_path)
    quote = PerpQuote(NATIVE, position["opened_at_ns"], Decimal("100"),
                      Decimal("99"), Decimal("101"))
    closed = paper.close_position(account["id"], position["id"], "same-tick-funding-close",
                                  paper.digest({"test": "same-tick-funding-close"}), quote)
    assert closed["position"]["funding_coverage"] == "CLOSED_COMPLETE"
    assert funding.candidates() == []
    assert funding.poll(position["id"])["poll_result"] == "COMPLETE_OR_NOT_AUTO"


def test_auto_candidates_reject_false_complete_funding_coverage(tmp_path):
    _, state, paper, account, position, funding = _open_auto(tmp_path)
    state["now"] += 9 * 3600
    quote = PerpQuote(NATIVE, state["now"] * 1_000_000_000,
                      Decimal("100"), Decimal("99"), Decimal("101"))
    closed = paper.close_position(account["id"], position["id"], "close-before-funding-audit",
                                  paper.digest({"test": "close-before-funding-audit"}), quote)
    assert closed["position"]["funding_coverage"] == "UNASSESSED"
    assert [item["id"] for item in funding.candidates()] == [position["id"]]
    with paper.connect() as db:
        db.execute("UPDATE paper_perp_positions SET funding_checked_through_ns=closed_at_ns WHERE id=?",
                   (position["id"],))
    with pytest.raises(PaperPerpError, match="Cobertura de financiación"):
        funding.candidates()


def test_auto_funding_excludes_manual_positions(tmp_path):
    monitor, state, paper, account, _, funding = _open_auto(tmp_path)
    state["now"] = START + 6 * STEP + 2
    other = paper.create_account("Manual", "1000", "500", "800", "5", 2, "0", "0")
    source = monitor.adapter_factory()
    quote = PerpQuote.from_ticker(source.get_ticker(SYMBOL), NATIVE, state["now"] * 1_000_000_000)
    rules = MarginRules.from_payload(source.get_margin_rules(SYMBOL), NATIVE, state["now"] * 1_000_000_000)
    spec = InstrumentSpec.from_payload(source.get_instrument_spec(SYMBOL), NATIVE, state["now"] * 1_000_000_000)
    manual = paper.open_position(other["id"], "manual-funding-test", paper.digest({"manual": True}),
                                 SYMBOL, "LONG", Decimal("1"), Decimal("2"), quote, rules, spec)
    assert manual["position"]["mode"] == "PAPER_MANUAL"
    assert other["id"] != account["id"]
    assert all(item["account_id"] == account["id"] for item in funding.candidates())


def test_background_worker_recovers_public_history_and_books_after_restart(tmp_path):
    _, state, paper, account, position, funding = _open_auto(tmp_path)
    state["now"] = START + 7 * STEP + 30
    state["available"] = False
    worker = AutoFundingWorker(funding, retry_seconds=0.02, idle_seconds=0.02)
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and worker.snapshot()["error_count"] == 0:
            time.sleep(0.01)
        assert worker.snapshot()["error_count"] > 0
        assert paper.position(account["id"], position["id"])["funding_coverage"] == "UNASSESSED"
        state["available"] = True
        worker.wake()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and paper.position(account["id"], position["id"])["funding_paid"] == "0":
            time.sleep(0.01)
        assert paper.position(account["id"], position["id"])["funding_paid"] == "-0.01"
    finally:
        worker.stop()
    assert not worker.snapshot()["running"]
    restarted = AutoFundingWorker(AutoFunding(paper, adapter_factory=_funding_source(state),
                                             clock_ns=lambda: state["now"] * 1_000_000_000),
                                  retry_seconds=0.02, idle_seconds=0.02)
    assert restarted.run_once() == {}  # cobertura abierta aún no vence otra hora
