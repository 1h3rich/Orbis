"""Financiación pública GRVT: estimación, asentamiento PAPER y cobertura."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.derivatives.funding import FundingHistoryError, FundingRecord, HOUR_NS, estimate_funding
from app.derivatives.paper_api import create_paper_perp_router
from app.marketdata.adapters.grvt import GrvtAdapter, GrvtHistoryError


NATIVE = "BTC_USDT_Perp"
SYMBOL = "BTC/USDT:PERP"
OPEN = 2000 * HOUR_NS + HOUR_NS // 2
END = 2003 * HOUR_NS + HOUR_NS // 2


def funding(hour, *, rate="0.01", mark="1000", interval=1, instrument=NATIVE):
    return {"instrument": instrument, "funding_time": str(hour * HOUR_NS),
            "funding_rate": rate, "mark_price": mark,
            "funding_interval_hours": interval}


def position(side="LONG"):
    return {"id": "p", "instrument": NATIVE, "opened_at_ns": OPEN,
            "closed_at_ns": END, "size": "2", "side": side}


def test_signed_funding_uses_percent_points_and_settlement_mark():
    records = [FundingRecord.from_payload(funding(hour), NATIVE) for hour in range(2000, 2004)]
    long = estimate_funding(position(), records, END)
    short = estimate_funding(position("SHORT"), records, END)
    assert Decimal(long["estimated_funding_paid"]) == Decimal("0.6")
    assert Decimal(short["estimated_funding_paid"]) == Decimal("-0.6")
    assert [row["funding_time_ns"] for row in long["settlements"]] == [hour * HOUR_NS for hour in (2001, 2002, 2003)]
    assert all(Decimal(row["estimated_payment"]) == Decimal("0.2") for row in long["settlements"])
    assert long["status"] == "HISTORICAL_ESTIMATE_NOT_BOOKED"


def test_no_settlement_yet_is_zero_only_with_previous_anchor():
    current = dict(position(), closed_at_ns=None)
    end = 2000 * HOUR_NS + 3 * HOUR_NS // 4
    record = FundingRecord.from_payload(funding(2000), NATIVE)
    estimate = estimate_funding(current, [record], end)
    assert estimate["settlements"] == []
    assert estimate["estimated_funding_paid"] == "0"


@pytest.mark.parametrize("rows,end,reason", [
    ([funding(2001), funding(2002), funding(2003)], END, "anterior"),
    ([funding(2000), funding(2002), funding(2003)], END, "Hueco"),
    ([funding(2000), funding(2001), funding(2001), funding(2003)], END, "duplicadas"),
    ([funding(2000), funding(2001)], END, "Falta una liquidación"),
    ([funding(2000), funding(2001), funding(2002)], 2002 * HOUR_NS, "coinciden"),
])
def test_uncertain_funding_is_not_reported_as_zero(rows, end, reason):
    records = [FundingRecord.from_payload(row, NATIVE) for row in rows]
    with pytest.raises(FundingHistoryError, match=reason):
        estimate_funding(dict(position(), closed_at_ns=end), records, end)


@pytest.mark.parametrize("change", [
    {"instrument": "ETH_USDT_Perp"}, {"funding_rate": 0.01},
    {"funding_rate": "NaN"}, {"funding_time": "nan"},
    {"mark_price": "0"}, {"funding_interval_hours": 2},
])
def test_funding_record_rejects_imprecise_or_unsafe_data(change):
    with pytest.raises((FundingHistoryError, ValueError)):
        FundingRecord.from_payload(funding(2001) | change, NATIVE)


def test_reverse_pagination_fetches_anchor_and_preserves_public_values():
    calls = []

    def post(url, body):
        calls.append((url, dict(body)))
        if body["cursor"] == "":
            return {"result": [funding(2003), funding(2002)], "next": "older"}
        assert body["cursor"] == "older"
        return {"result": [funding(2001), funding(2000)], "next": ""}

    adapter = GrvtAdapter(http_post=post)
    rows = adapter.get_funding_history(SYMBOL, OPEN, END)
    assert [int(row["funding_time"]) // HOUR_NS for row in rows] == [2003, 2002, 2001, 2000]
    assert calls[0] == ("https://market-data.grvt.io/full/v1/funding", {
        "instrument": NATIVE, "start_time": str(OPEN - 8 * HOUR_NS),
        "end_time": str(END), "limit": 1000, "cursor": "", "agg_type": "FUNDING_INTERVAL"})


@pytest.mark.parametrize("pages,reason", [
    ([{"result": [funding(2003), funding(2002)], "next": ""}], "sin ancla"),
    ([{"result": [funding(2003)], "next": "x"},
      {"result": [funding(2003), funding(2000)], "next": ""}], "desordenada"),
    ([{"result": [funding(2003, rate=0.01)], "next": ""}], "no textuales"),
    ([{"result": [funding(2003, instrument="ETH_USDT_Perp")], "next": ""}], "otro instrumento"),
])
def test_funding_adapter_fails_closed_on_partial_or_wrong_pages(pages, reason):
    responses = iter(pages)
    adapter = GrvtAdapter(http_post=lambda _url, _body: next(responses))
    with pytest.raises(GrvtHistoryError, match=reason):
        adapter.get_funding_history(SYMBOL, OPEN, END)


def test_api_estimates_funding_without_changing_paper_balance(tmp_path):
    state = {"now": OPEN, "funding_available": True}

    class Source:
        known_pairs = (SYMBOL,)

        def _instrument(self, _symbol):
            return NATIVE

        def get_ticker(self, _symbol):
            return {"instrument": NATIVE, "event_time_ns": state["now"], "mark_price": "1000",
                    "bid": "999", "ask": "1001"}

        def get_margin_rules(self, _symbol):
            return {"instrument": NATIVE, "fetched_at_ns": state["now"],
                    "max_position_size": "1000", "risk_brackets": [{
                        "tier": 1, "notional_floor": "0", "notional_cap": "600000",
                        "maintenance_margin_rate": "0.01", "initial_margin_rate": "0.02",
                        "max_leverage": 50, "cumulative_maintenance_amount": "0"}]}

        def get_instrument_spec(self, _symbol):
            return {"instrument": NATIVE, "kind": "PERPETUAL", "venues": ["ORDERBOOK"],
                    "base_decimals": 3, "tick_size": "0.01", "min_size": "0.01",
                    "min_notional": "20", "max_position_size": "1000", "fetched_at_ns": state["now"]}

        def get_funding_history(self, _symbol, start, end):
            assert start == OPEN and end == state["now"]
            if not state["funding_available"]:
                raise GrvtHistoryError("Proveedor no disponible")
            return [funding(hour) for hour in range(2003, 1999, -1)]

    app = FastAPI()
    app.include_router(create_paper_perp_router(tmp_path / "paper.db", adapter_factory=Source,
                                                 clock_ns=lambda: state["now"]))
    client = TestClient(app)
    assert client.post("/paper/perp/create", json={"passphrase": "funding paper encrypted test password"}).status_code == 200
    account = client.post("/paper/perp/accounts", json={
        "name": "Funding PAPER", "initial_balance": "10000", "max_position_notional": "5000",
        "max_total_notional": "8000", "max_leverage": "5", "max_positions": 2,
        "fee_rate": "0", "slippage_bps": "0"}).json()
    opened = client.post(f"/paper/perp/accounts/{account['id']}/positions", json={
        "request_id": str(uuid4()), "symbol": SYMBOL, "side": "LONG", "size": "2", "leverage": "2"})
    assert opened.status_code == 201, opened.text
    before = client.get(f"/paper/perp/accounts/{account['id']}").json()
    state["now"] = END
    endpoint = f"/paper/perp/accounts/{account['id']}/positions/{opened.json()['position']['id']}/funding-estimate"
    estimate = client.get(endpoint)
    assert estimate.status_code == 200, estimate.text
    assert Decimal(estimate.json()["estimated_funding_paid"]) == Decimal("0.6")
    assert client.get(f"/paper/perp/accounts/{account['id']}").json() == before
    assert [row["kind"] for row in client.get(f"/paper/perp/accounts/{account['id']}/events").json()] == ["OPEN"]
    state["funding_available"] = False
    assert client.get(endpoint).status_code == 503


def _settlement_client(tmp_path):
    state = {"now": OPEN, "available": True, "rate_override": None, "history_calls": 0}

    class Source:
        known_pairs = (SYMBOL,)

        def _instrument(self, _symbol):
            return NATIVE

        def get_ticker(self, _symbol):
            return {"instrument": NATIVE, "event_time_ns": state["now"],
                    "mark_price": "1000", "bid": "999", "ask": "1001"}

        def get_margin_rules(self, _symbol):
            return {"instrument": NATIVE, "fetched_at_ns": state["now"],
                    "max_position_size": "1000", "risk_brackets": [{
                        "tier": 1, "notional_floor": "0", "notional_cap": "600000",
                        "maintenance_margin_rate": "0.01", "initial_margin_rate": "0.02",
                        "max_leverage": 50, "cumulative_maintenance_amount": "0"}]}

        def get_instrument_spec(self, _symbol):
            return {"instrument": NATIVE, "kind": "PERPETUAL", "venues": ["ORDERBOOK"],
                    "base_decimals": 3, "tick_size": "0.01", "min_size": "0.01",
                    "min_notional": "20", "max_position_size": "1000", "fetched_at_ns": state["now"]}

        def get_funding_history(self, _symbol, start, end):
            state["history_calls"] += 1
            assert start == OPEN and end <= state["now"]
            if not state["available"]:
                raise GrvtHistoryError("Histórico incompleto")
            last_hour = end // HOUR_NS
            return [funding(hour, rate=state["rate_override"] if hour == 2002 and state["rate_override"]
                            else "0.01") for hour in range(last_hour, 1999, -1)]

    app = FastAPI()
    app.include_router(create_paper_perp_router(tmp_path / "funding-paper.db", adapter_factory=Source,
                                                 clock_ns=lambda: state["now"]))
    client = TestClient(app)
    assert client.post("/paper/perp/create", json={"passphrase": "funding settlement encrypted password"}).status_code == 200
    account = client.post("/paper/perp/accounts", json={
        "name": "Funding PAPER", "initial_balance": "10000", "max_position_notional": "5000",
        "max_total_notional": "8000", "max_leverage": "5", "max_positions": 2,
        "fee_rate": "0", "slippage_bps": "0"}).json()
    return client, state, account


def _open_settlement_position(client, account, side="LONG"):
    response = client.post(f"/paper/perp/accounts/{account['id']}/positions", json={
        "request_id": str(uuid4()), "symbol": SYMBOL, "side": side, "size": "2", "leverage": "2"})
    assert response.status_code == 201, response.text
    return response.json()["position"]


def _settle(client, account, position, request_id=None):
    return client.post(f"/paper/perp/accounts/{account['id']}/positions/{position['id']}/funding-settle",
                       json={"request_id": request_id or str(uuid4())})


def test_public_funding_books_once_and_changes_paper_equity_and_close(tmp_path):
    client, state, account = _settlement_client(tmp_path)
    position = _open_settlement_position(client, account)
    state["now"] = 2002 * HOUR_NS + HOUR_NS // 2
    endpoint = f"/paper/perp/accounts/{account['id']}/positions/{position['id']}/observe"
    before_booking = client.post(endpoint)
    assert before_booking.status_code == 200
    request_id = str(uuid4())
    first = _settle(client, account, position, request_id)
    assert first.status_code == 200, first.text
    assert first.json()["new_settlements"] == 2
    assert Decimal(first.json()["new_payment"]) == Decimal("0.4")
    assert first.json()["position"]["funding_coverage"] == "PARTIAL"
    assert Decimal(first.json()["position"]["funding_paid"]) == Decimal("0.4")
    calls = state["history_calls"]
    assert _settle(client, account, position, request_id).json() == first.json()
    assert state["history_calls"] == calls
    observation = client.post(endpoint)
    assert observation.status_code == 200, observation.text
    assert Decimal(observation.json()["isolated_equity"]) == Decimal(position["collateral"]) - Decimal("2.4")
    assert observation.json()["event_id"] != before_booking.json()["event_id"]

    state["now"] = END
    second = _settle(client, account, position)
    assert second.status_code == 200, second.text
    assert second.json()["new_settlements"] == 1
    assert Decimal(second.json()["new_payment"]) == Decimal("0.2")
    closed = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position['id']}/close",
                         json={"request_id": str(uuid4())})
    assert closed.status_code == 200, closed.text
    assert Decimal(closed.json()["position"]["realized_pnl"]) == Decimal("-4.6")
    assert Decimal(closed.json()["account"]["available_cash"]) == Decimal("9995.4")
    assert closed.json()["position"]["funding_coverage"] == "CLOSED_COMPLETE"
    events = client.get(f"/paper/perp/accounts/{account['id']}/events").json()
    assert [item["kind"] for item in events].count("FUNDING_PAPER") == 3
    assert [Decimal(item["details"]["payment"]) for item in events if item["kind"] == "FUNDING_PAPER"] == [
        Decimal("0.2"), Decimal("0.2"), Decimal("0.2")]
    assert _settle(client, account, position).json()["new_settlements"] == 0


def test_closed_short_funding_credit_corrects_realized_pnl_after_close(tmp_path):
    client, state, account = _settlement_client(tmp_path)
    position = _open_settlement_position(client, account, "SHORT")
    state["now"] = END
    closed = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position['id']}/close",
                         json={"request_id": str(uuid4())}).json()
    assert Decimal(closed["position"]["realized_pnl"]) == Decimal("-4")
    assert closed["position"]["funding_coverage"] == "UNASSESSED"
    booked = _settle(client, account, position)
    assert booked.status_code == 200, booked.text
    assert Decimal(booked.json()["position"]["funding_paid"]) == Decimal("-0.6")
    assert Decimal(booked.json()["realized_pnl_delta"]) == Decimal("0.6")
    assert Decimal(booked.json()["position"]["realized_pnl"]) == Decimal("-3.4")
    assert booked.json()["position"]["funding_coverage"] == "CLOSED_COMPLETE"
    assert Decimal(client.get(f"/paper/perp/accounts/{account['id']}").json()["available_cash"]) == Decimal("9996.6")


def test_incomplete_or_changed_history_never_books_a_partial_payment(tmp_path):
    client, state, account = _settlement_client(tmp_path)
    position = _open_settlement_position(client, account)
    state["now"] = 2002 * HOUR_NS + HOUR_NS // 2
    state["available"] = False
    assert _settle(client, account, position).status_code == 503
    assert client.get(f"/paper/perp/accounts/{account['id']}/positions").json()[0]["funding_paid"] == "0"
    state["available"] = True
    assert _settle(client, account, position).status_code == 200
    state["now"] = END
    state["rate_override"] = "0.02"
    changed = _settle(client, account, position)
    assert changed.status_code == 503 and "contradice" in changed.text
    saved = client.get(f"/paper/perp/accounts/{account['id']}/positions").json()[0]
    assert Decimal(saved["funding_paid"]) == Decimal("0.4")
    events = client.get(f"/paper/perp/accounts/{account['id']}/events").json()
    assert len([item for item in events if item["kind"] == "FUNDING_PAPER"]) == 2


def test_concurrent_settlement_requests_share_one_set_of_funding_events(tmp_path):
    client, state, account = _settlement_client(tmp_path)
    position = _open_settlement_position(client, account)
    state["now"] = END
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: _settle(client, account, position), range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert sorted(response.json()["new_settlements"] for response in responses) == [0, 0, 0, 3]
    events = client.get(f"/paper/perp/accounts/{account['id']}/events").json()
    assert len([item for item in events if item["kind"] == "FUNDING_PAPER"]) == 3
    assert Decimal(client.get(f"/paper/perp/accounts/{account['id']}/positions").json()[0]["funding_paid"]) == Decimal("0.6")


def test_funding_settlement_rejects_external_origin_before_touching_ledger(tmp_path):
    client, state, account = _settlement_client(tmp_path)
    position = _open_settlement_position(client, account)
    state["now"] = END
    response = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position['id']}/funding-settle",
                           headers={"Origin": "https://example.org"}, json={"request_id": str(uuid4())})
    assert response.status_code == 403
    assert state["history_calls"] == 0
    assert client.get(f"/paper/perp/accounts/{account['id']}/positions").json()[0]["funding_paid"] == "0"
