"""Recorrido local PAPER GRVT con cotizaciones observadas y base temporal."""

from app.derivatives.paper_schema import CURRENT_PAPER_SCHEMA
from tests.paper_schema_fixture import drop_observed_tables
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.derivatives.paper_api import create_paper_perp_router
from app.derivatives.funding import FundingRecord, HOUR_NS
from app.derivatives.paper_store import PaperPerpStore


NOW = 1_800_000_000_000_000_000


def harness(path: Path):
    with PaperPerpStore(path).connect():  # Base heredada para pruebas de operativa manual.
        pass
    state = {"now": NOW, "bid": "99", "ask": "101", "mark": "100",
             "event_ns": None, "offline": False, "rule_calls": 0, "ticker_calls": 0,
             "spec_calls": 0, "spec_changes": {}, "spec_offline": False,
             "rule_offline": False, "rule_changes": {}, "rule_delay_ns": 0}

    class Source:
        known_pairs = ("BTC/USDT:PERP",)

        def _instrument(self, symbol):
            return "BTC_USDT_Perp" if symbol in self.known_pairs else None

        def get_ticker(self, symbol):
            state["ticker_calls"] += 1
            if state["offline"]:
                return None
            return {"instrument": "BTC_USDT_Perp", "event_time_ns": state["event_ns"] or state["now"],
                    "mark_price": state["mark"], "bid": state["bid"], "ask": state["ask"]}

        def get_margin_rules(self, symbol):
            state["rule_calls"] += 1
            if state["offline"] or state["rule_offline"]:
                return None
            fetched_at = state["now"]
            state["now"] += state["rule_delay_ns"]
            return {"instrument": "BTC_USDT_Perp", "fetched_at_ns": fetched_at,
                    "max_position_size": "1000", "risk_brackets": [
                        {"tier": 1, "notional_floor": "0", "notional_cap": "600000",
                         "maintenance_margin_rate": "0.01", "initial_margin_rate": "0.02",
                         "max_leverage": 50, "cumulative_maintenance_amount": "0"},
                    ]} | state["rule_changes"]

        def get_instrument_spec(self, symbol):
            state["spec_calls"] += 1
            if state["offline"] or state["spec_offline"]:
                return None
            return {"instrument": "BTC_USDT_Perp", "kind": "PERPETUAL", "venues": ["ORDERBOOK"],
                    "base_decimals": 3, "tick_size": "0.01", "min_size": "0.01",
                    "min_notional": "20", "max_position_size": "1000",
                    "fetched_at_ns": state["now"]} | state["spec_changes"]

    app = FastAPI()
    app.include_router(create_paper_perp_router(path, adapter_factory=Source,
                                                 clock_ns=lambda: state["now"]))
    return TestClient(app), state


def create_account(client, **changes):
    body = {"name": "Prueba GRVT", "initial_balance": "1000",
            "max_position_notional": "500", "max_total_notional": "800",
            "max_leverage": "5", "max_positions": 2,
            "fee_rate": "0.001", "slippage_bps": "0"}
    body.update(changes)
    response = client.post("/paper/perp/accounts", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def open_position(client, account_id, **changes):
    body = {"request_id": str(uuid4()), "symbol": "BTC/USDT:PERP", "side": "LONG",
            "size": "1", "leverage": "2"}
    body.update(changes)
    return client.post(f"/paper/perp/accounts/{account_id}/positions", json=body), body


def add_funding_anchor(db, position_id, first_funding_time, interval_hours=1):
    db.execute("INSERT INTO paper_perp_funding_anchors VALUES (?,?,?,?,?,?,?)",
               (position_id, first_funding_time - interval_hours * HOUR_NS,
                "0.01", "100", interval_hours, "test-fixture",
                "2026-10-24T00:00:00+00:00"))


def test_long_open_observe_close_replay_and_reopen(tmp_path):
    path = tmp_path / "paper-perp.db"
    client, state = harness(path)
    account = create_account(client)
    response, request = open_position(client, account["id"])
    assert response.status_code == 201, response.text
    opened = response.json()
    position = opened["position"]
    assert position["entry_price"] == "101"
    assert Decimal(position["entry_fee"]) == Decimal("0.101")
    assert Decimal(position["collateral"]) == Decimal("50.601")
    assert Decimal(opened["account"]["available_cash"]) == Decimal("949.399")
    assert position["margin_rules"]["instrument"] == "BTC_USDT_Perp"
    assert position["instrument_spec_checked"] is True
    assert position["instrument_spec"]["min_size"] == "0.01"
    assert position["funding_paid"] == "0"
    assert position["mode"] == "PAPER_MANUAL"

    state["now"] += 1_000_000_000
    state.update(bid="109", ask="111", mark="110")
    observed = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position['id']}/observe")
    assert observed.status_code == 200
    assert Decimal(observed.json()["unrealized_pnl"]) == Decimal("9")
    assert Decimal(observed.json()["isolated_equity"]) == Decimal("59.5")
    assert observed.json()["maintenance_status"] == "ABOVE_MAINTENANCE_ON_MARK"
    assert Decimal(observed.json()["maintenance_requirement"]) == Decimal("1.1")
    assert Decimal(observed.json()["margin_buffer"]) == Decimal("58.4")
    assert observed.json()["maintenance_mark_model"] == "ISOLATED_STATIC_SNAPSHOT_NOT_LIQUIDATION_PRICE"
    assert Decimal(observed.json()["estimated_maintenance_mark"]) < Decimal("110")
    assert observed.json()["liquidation_status"] == "NOT_DETERMINED_NO_CONTINUOUS_MARK_OR_FUNDING"

    close_id = str(uuid4())
    closed_response = client.post(
        f"/paper/perp/accounts/{account['id']}/positions/{position['id']}/close",
        json={"request_id": close_id})
    assert closed_response.status_code == 200, closed_response.text
    closed = closed_response.json()
    assert closed["position"]["exit_price"] == "109"
    assert Decimal(closed["position"]["realized_pnl"]) == Decimal("7.79")
    assert Decimal(closed["account"]["available_cash"]) == Decimal("1007.79")
    assert closed["account"]["open_positions"] == 0

    state["offline"] = True
    assert client.post(f"/paper/perp/accounts/{account['id']}/positions", json=request).json() == opened
    assert client.post(
        f"/paper/perp/accounts/{account['id']}/positions/{position['id']}/close",
        json={"request_id": close_id}).json() == closed
    assert len(client.get(f"/paper/perp/accounts/{account['id']}/events").json()) == 3
    assert state["ticker_calls"] == 3  # apertura, observación, cierre; no reintentos
    assert state["spec_calls"] == 1

    reopened, _ = harness(path)
    assert reopened.get(f"/paper/perp/accounts/{account['id']}").json() == closed["account"]
    assert len(reopened.get(f"/paper/perp/accounts/{account['id']}/positions").json()) == 1
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_PAPER_SCHEMA


def test_short_uses_bid_to_open_ask_to_close_and_stop_allows_exit(tmp_path):
    client, state = harness(tmp_path / "short.db")
    account = create_account(client)
    response, _ = open_position(client, account["id"], side="SHORT")
    assert response.status_code == 201, response.text
    position = response.json()["position"]
    assert position["entry_price"] == "99"
    stopped = client.post(f"/paper/perp/accounts/{account['id']}/stop")
    assert stopped.status_code == 200 and stopped.json()["stopped_at"]
    blocked, _ = open_position(client, account["id"])
    assert blocked.status_code == 422
    state["now"] += 1_000_000_000
    state.update(bid="88", ask="90", mark="89")
    closed = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position['id']}/close",
                         json={"request_id": str(uuid4())})
    assert closed.status_code == 200, closed.text
    assert closed.json()["position"]["exit_price"] == "90"
    assert Decimal(closed.json()["position"]["realized_pnl"]) == Decimal("8.811")
    assert closed.json()["account"]["stopped_at"]


def test_same_tick_close_has_complete_zero_duration_funding(tmp_path):
    path = tmp_path / "same-tick-close.db"
    client, _ = harness(path)
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    assert opened.status_code == 201
    position = opened.json()["position"]
    closed = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position['id']}/close",
                         json={"request_id": str(uuid4())})
    assert closed.status_code == 200, closed.text
    result = closed.json()["position"]
    assert result["closed_at_ns"] == position["opened_at_ns"]
    assert result["funding_checked_through_ns"] == result["closed_at_ns"]
    assert result["funding_coverage"] == "CLOSED_COMPLETE"
    assert result["funding_paid"] == "0"
    reopened, _ = harness(path)
    response = reopened.get(f"/paper/perp/accounts/{account['id']}/positions")
    assert response.status_code == 200
    assert response.json()[0]["funding_coverage"] == "CLOSED_COMPLETE"


def test_funding_ledger_mismatch_blocks_balance_and_new_paper_actions(tmp_path):
    path = tmp_path / "funding-mismatch.db"
    client, state = harness(path)
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    assert opened.status_code == 201
    position_id = opened.json()["position"]["id"]
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET funding_paid='1' WHERE id=?", (position_id,))
    prefix = f"/paper/perp/accounts/{account['id']}"
    assert client.get(prefix).status_code == 422
    assert client.get("/paper/perp/accounts").status_code == 422
    assert client.get(prefix + "/positions").status_code == 422
    assert client.get(prefix + "/valuations").status_code == 422
    before = len(client.get(prefix + "/events").json())
    state["now"] += 1_000_000_000
    assert client.post(prefix + f"/positions/{position_id}/observe").status_code == 422
    assert client.post(prefix + f"/positions/{position_id}/close",
                       json={"request_id": str(uuid4())}).status_code == 422
    assert open_position(client, account["id"])[0].status_code == 422
    assert len(client.get(prefix + "/events").json()) == before
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET funding_paid='0' WHERE id=?", (position_id,))
    assert client.get(prefix).status_code == 200


def test_open_collateral_must_match_margin_and_original_event(tmp_path):
    path = tmp_path / "open-collateral-mismatch.db"
    client, _ = harness(path)
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    assert opened.status_code == 201
    position = opened.json()["position"]
    position_id = position["id"]
    url = f"/paper/perp/accounts/{account['id']}"
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET collateral='1' WHERE id=?", (position_id,))
    assert client.get(url).status_code == 422
    assert open_position(client, account["id"])[0].status_code == 422
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET collateral='51.601' WHERE id=?", (position_id,))
    assert client.get(url).status_code == 422  # Cubre margen, pero no coincide con OPEN.
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET collateral=? WHERE id=?",
                   (position["collateral"], position_id))
        event = db.execute("SELECT id,details_json FROM paper_perp_events WHERE position_id=? AND kind='OPEN'",
                           (position_id,)).fetchone()
        details = json.loads(event["details_json"])
        details["fee"] = "0"
        db.execute("UPDATE paper_perp_events SET details_json=? WHERE id=?",
                   (json.dumps(details), event["id"]))
    assert client.get(url).status_code == 422
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_events SET details_json=? WHERE id=?",
                   (event["details_json"], event["id"]))
    assert client.get(url).status_code == 200


def test_open_collateral_validation_accepts_dividing_by_three(tmp_path):
    client, _ = harness(tmp_path / "open-repeating-margin.db")
    account = create_account(client)
    opened, _ = open_position(client, account["id"], leverage="3")
    assert opened.status_code == 201, opened.text
    assert client.get(f"/paper/perp/accounts/{account['id']}").status_code == 200


def test_funding_event_amount_and_coverage_are_verified_on_read(tmp_path):
    path = tmp_path / "funding-event-mismatch.db"
    client, _ = harness(path)
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position_id = opened.json()["position"]["id"]
    funding_time = opened.json()["position"]["opened_at_ns"] + 1
    event_id = str(uuid4())
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET funding_paid='1',funding_checked_through_ns=? WHERE id=?",
                   (funding_time, position_id))
        add_funding_anchor(db, position_id, funding_time)
        db.execute("INSERT INTO paper_perp_funding_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (event_id, account["id"], position_id, str(uuid4()), funding_time,
                    "1", "100", 1, "1", "test-fixture", "2026-10-23T00:00:00+00:00"))
    url = f"/paper/perp/accounts/{account['id']}"
    assert client.get(url).status_code == 200
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_funding_events SET payment='2' WHERE id=?", (event_id,))
    assert client.get(url).status_code == 422
    # Alterar a la vez evento y acumulado no debe ocultar un cargo inventado.
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET funding_paid='2' WHERE id=?", (position_id,))
    assert client.get(url).status_code == 422
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET funding_paid='1' WHERE id=?", (position_id,))
        db.execute("UPDATE paper_perp_funding_events SET payment='1',rate_percent='2' WHERE id=?", (event_id,))
    assert client.get(url).status_code == 422
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_funding_events SET rate_percent='1' WHERE id=?", (event_id,))
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_funding_events SET payment='NaN' WHERE id=?", (event_id,))
    assert client.get(url).status_code == 422
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_funding_events SET payment='1' WHERE id=?", (event_id,))
        db.execute("UPDATE paper_perp_positions SET funding_checked_through_ns=? WHERE id=?",
                   (funding_time - 1, position_id))
    assert client.get(url).status_code == 422


def test_funding_ledger_rejects_missing_interval_and_false_coverage(tmp_path):
    path = tmp_path / "funding-gap.db"
    client, _ = harness(path)
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position_id = opened.json()["position"]["id"]
    first = opened.json()["position"]["opened_at_ns"] + 1
    second = first + HOUR_NS
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET funding_paid='3',funding_checked_through_ns=? WHERE id=?",
                   (second, position_id))
        add_funding_anchor(db, position_id, first)
        for event_id, event_time, rate, payment in (("funding-first", first, "1", "1"),
                                                     ("funding-second", second, "2", "2")):
            db.execute("INSERT INTO paper_perp_funding_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (event_id, account["id"], position_id, str(uuid4()), event_time,
                        rate, "100", 1, payment, "test-fixture", "2026-10-24T00:00:00+00:00"))
    url = f"/paper/perp/accounts/{account['id']}"
    assert client.get(url).status_code == 200
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_funding_events SET funding_time_ns=? WHERE id='funding-second'",
                   (second + HOUR_NS,))
        db.execute("UPDATE paper_perp_positions SET funding_checked_through_ns=? WHERE id=?",
                   (second + HOUR_NS, position_id))
    assert client.get(url).status_code == 422
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_funding_events SET funding_time_ns=? WHERE id='funding-second'",
                   (second,))
    assert client.get(url).status_code == 422  # Cobertura dice haber pasado la siguiente liquidación.
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET funding_checked_through_ns=? WHERE id=?",
                   (second, position_id))
    assert client.get(url).status_code == 200


def test_funding_ledger_rejects_omitted_first_settlement(tmp_path):
    path = tmp_path / "funding-first-omitted.db"
    client, _ = harness(path)
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position = opened.json()["position"]
    first = position["opened_at_ns"] + HOUR_NS - 1
    second = first + 4 * HOUR_NS
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET funding_paid='3',funding_checked_through_ns=? WHERE id=?",
                   (second, position["id"]))
        add_funding_anchor(db, position["id"], first)
        for event_id, event_time, rate, payment, interval in (
                ("first", first, "1", "1", 1), ("second", second, "2", "2", 4)):
            db.execute("INSERT INTO paper_perp_funding_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (event_id, account["id"], position["id"], str(uuid4()), event_time,
                        rate, "100", interval, payment, "test-fixture", "2026-10-24T00:00:00+00:00"))
    url = f"/paper/perp/accounts/{account['id']}"
    assert client.get(url).status_code == 200
    with PaperPerpStore(path).connect() as db:
        db.execute("DELETE FROM paper_perp_funding_events WHERE id='first'")
        db.execute("UPDATE paper_perp_positions SET funding_paid='2' WHERE id=?", (position["id"],))
    assert client.get(url).status_code == 422


def test_funding_ledger_rejects_unbacked_zero_event_coverage(tmp_path):
    path = tmp_path / "funding-empty-false-coverage.db"
    client, _ = harness(path)
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position = opened.json()["position"]
    url = f"/paper/perp/accounts/{account['id']}"
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET funding_checked_through_ns=? WHERE id=?",
                   (position["opened_at_ns"] + 9 * HOUR_NS, position["id"]))
    assert client.get(url).status_code == 422


def test_funding_anchor_rejects_missing_event_within_eight_hours(tmp_path):
    path = tmp_path / "funding-anchor-gap.db"
    client, _ = harness(path)
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position = opened.json()["position"]
    with PaperPerpStore(path).connect() as db:
        db.execute("INSERT INTO paper_perp_funding_anchors VALUES (?,?,?,?,?,?,?)",
                   (position["id"], position["opened_at_ns"] - 1,
                    "0.01", "100", 1, "test-fixture", "2026-10-24T00:00:00+00:00"))
        db.execute("UPDATE paper_perp_positions SET funding_checked_through_ns=? WHERE id=?",
                   (position["opened_at_ns"] + 2 * HOUR_NS, position["id"]))
    assert client.get(f"/paper/perp/accounts/{account['id']}").status_code == 422


def test_closed_result_mismatch_blocks_paper_balance_and_new_positions(tmp_path):
    path = tmp_path / "closed-result-mismatch.db"
    client, state = harness(path)
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    assert opened.status_code == 201
    position_id = opened.json()["position"]["id"]
    state["now"] += 1_000_000_000
    state.update(bid="109", ask="111", mark="110")
    prefix = f"/paper/perp/accounts/{account['id']}"
    closed = client.post(prefix + f"/positions/{position_id}/close",
                         json={"request_id": str(uuid4())})
    assert closed.status_code == 200
    booked = closed.json()["position"]["realized_pnl"]
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET realized_pnl='999' WHERE id=?", (position_id,))
    assert client.get(prefix).status_code == 422
    assert client.get(prefix + "/positions").status_code == 422
    assert open_position(client, account["id"])[0].status_code == 422
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET realized_pnl=? WHERE id=?", (booked, position_id))
        db.execute("UPDATE paper_perp_positions SET uncovered_loss='1' WHERE id=?", (position_id,))
    assert client.get(prefix).status_code == 422
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET uncovered_loss='0' WHERE id=?", (position_id,))
    assert client.get(prefix).status_code == 200
    # Precio y P/L alterados de forma coherente entre sí aún contradicen el
    # evento CLOSE original, que no cambia por ajustes de funding posteriores.
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET exit_price='110',realized_pnl='8.79' WHERE id=?",
                   (position_id,))
    assert client.get(prefix).status_code == 422
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_positions SET exit_price='109',realized_pnl=? WHERE id=?",
                   (booked, position_id))
    assert client.get(prefix).status_code == 200
    with PaperPerpStore(path).connect() as db:
        event = db.execute("SELECT id,details_json FROM paper_perp_events WHERE position_id=? AND kind='CLOSE'",
                           (position_id,)).fetchone()
        details = json.loads(event["details_json"])
        details["fill_price"] = "110"
        db.execute("UPDATE paper_perp_events SET details_json=? WHERE id=?",
                   (json.dumps(details), event["id"]))
    assert client.get(prefix).status_code == 422
    with PaperPerpStore(path).connect() as db:
        db.execute("UPDATE paper_perp_events SET details_json=? WHERE id=?",
                   (event["details_json"], event["id"]))
        db.execute("""INSERT INTO paper_perp_events
                      SELECT ?,account_id,position_id,kind,request_id,observed_at_ns,
                             recorded_at,details_json FROM paper_perp_events WHERE id=?""",
                   (str(uuid4()), event["id"]))
    assert client.get(prefix).status_code == 422


def test_marked_maintenance_warning_does_not_claim_grvt_liquidation(tmp_path):
    client, state = harness(tmp_path / "maintenance.db")
    account = create_account(client)
    long_response, _ = open_position(client, account["id"])
    long_id = long_response.json()["position"]["id"]
    state["now"] += 1_000_000_000
    state.update(bid="49", ask="51", mark="50")
    observed = client.post(f"/paper/perp/accounts/{account['id']}/positions/{long_id}/observe")
    assert observed.status_code == 200, observed.text
    risk = observed.json()
    assert risk["maintenance_status"] == "AT_OR_BELOW_MAINTENANCE_ON_MARK"
    assert Decimal(risk["maintenance_requirement"]) == Decimal("0.5")
    assert Decimal(risk["margin_buffer"]) < 0
    assert risk["liquidation_status"] == "NOT_DETERMINED_NO_CONTINUOUS_MARK_OR_FUNDING"
    assert client.get(f"/paper/perp/accounts/{account['id']}/positions").json()[0]["closed_at_ns"] is None
    assert client.get(f"/paper/perp/accounts/{account['id']}/events").json()[-1]["details"]["margin_rules"]["risk_brackets"][0]["cumulative_maintenance_amount"] == "0"


def test_observe_fails_closed_without_current_margin_rules(tmp_path):
    client, state = harness(tmp_path / "margin-unavailable.db")
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position_id = opened.json()["position"]["id"]
    state["now"] += 1_000_000_000
    state["rule_offline"] = True
    response = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position_id}/observe")
    assert response.status_code == 503
    assert [row["kind"] for row in client.get(f"/paper/perp/accounts/{account['id']}/events").json()] == ["OPEN"]


def test_observation_does_not_store_a_quote_aged_during_rule_fetch(tmp_path):
    client, state = harness(tmp_path / "rules-delay.db")
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position_id = opened.json()["position"]["id"]
    state["now"] += 1_000_000_000
    state["rule_delay_ns"] = 16_000_000_000
    response = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position_id}/observe")
    assert response.status_code == 503
    assert [row["kind"] for row in client.get(f"/paper/perp/accounts/{account['id']}/events").json()] == ["OPEN"]


def test_observation_is_once_per_quote_and_rejects_older_market_time(tmp_path):
    client, state = harness(tmp_path / "observations-ordered.db")
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position_id = opened.json()["position"]["id"]
    endpoint = f"/paper/perp/accounts/{account['id']}/positions/{position_id}/observe"

    state["now"] += 2_000_000_000
    state.update(bid="109", ask="111", mark="110")
    first = client.post(endpoint)
    assert first.status_code == 200, first.text
    repeated = client.post(endpoint)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == first.json()
    assert [row["kind"] for row in client.get(f"/paper/perp/accounts/{account['id']}/events").json()] == ["OPEN", "OBSERVE"]

    state["now"] += 1_000_000_000
    state.update(bid="112", ask="114", mark="113")
    newer = client.post(endpoint)
    assert newer.status_code == 200, newer.text
    state["now"] += 1_000_000_000
    state.update(event_ns=NOW + 2_500_000_000, bid="111", ask="113", mark="112")
    older = client.post(endpoint)
    assert older.status_code == 422
    assert [row["kind"] for row in client.get(f"/paper/perp/accounts/{account['id']}/events").json()] == ["OPEN", "OBSERVE", "OBSERVE"]


def test_open_valuation_expires_without_rewriting_grvt_paper_history(tmp_path):
    client, state = harness(tmp_path / "valuation-age.db")
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position_id = opened.json()["position"]["id"]
    url = f"/paper/perp/accounts/{account['id']}/valuations"
    first_snapshot = client.get(url)
    assert first_snapshot.headers["Cache-Control"] == "no-store"
    assert first_snapshot.json()[position_id] == {
        "status": "NO_OBSERVATION", "observed_at_ns": None,
        "remaining_ms": 0, "details": None, "last_risk_type": None,
    }
    state["now"] += 1_000_000_000
    state.update(bid="109", ask="111", mark="110")
    observed = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position_id}/observe")
    assert observed.status_code == 200, observed.text
    event_ns = observed.json()["observed_at_ns"]
    fresh = client.get(url).json()[position_id]
    assert fresh["status"] == "FRESH"
    assert fresh["observed_at_ns"] == event_ns
    assert fresh["remaining_ms"] == 15000
    assert fresh["details"]["unrealized_pnl"] == "9"

    state["now"] += 15_000_000_000
    assert client.get(url).json()[position_id]["status"] == "FRESH"
    state["now"] += 1_000_000_000
    stale = client.get(url).json()[position_id]
    assert stale["status"] == "STALE"
    assert stale["observed_at_ns"] == event_ns
    assert stale["details"] is None
    assert stale["remaining_ms"] == 0
    assert client.get(f"/paper/perp/accounts/{account['id']}/events").json()[-1]["details"]["unrealized_pnl"] == "9"
    state["now"] = event_ns - 1
    assert client.get(url).json()[position_id]["status"] == "STALE"

    state["now"] = event_ns + 17_000_000_000
    state.update(bid="112", ask="114", mark="113")
    assert client.post(f"/paper/perp/accounts/{account['id']}/positions/{position_id}/observe").status_code == 200
    assert client.get(url).json()[position_id]["details"]["mark_price"] == "113"
    with PaperPerpStore(tmp_path / "valuation-age.db").connect() as db:
        funding_time = state["now"]
        db.execute("UPDATE paper_perp_positions SET funding_paid='1',funding_checked_through_ns=? WHERE id=?",
                   (funding_time, position_id))
        add_funding_anchor(db, position_id, funding_time)
        db.execute("""INSERT INTO paper_perp_funding_events VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (str(uuid4()), account["id"], position_id, str(uuid4()), funding_time,
                    "1", "100", 1, "1", "browser-fixture", "2026-10-23T00:00:00+00:00"))
    outdated = client.get(url).json()[position_id]
    assert outdated["status"] == "OUTDATED_BOOKING" and outdated["details"] is None
    state["now"] += 1_000_000_000
    state.update(bid="0.9", ask="1.1", mark="1")
    assert client.post(f"/paper/perp/accounts/{account['id']}/positions/{position_id}/observe").status_code == 200
    assert client.get(url).json()[position_id]["details"]["maintenance_status"] == "AT_OR_BELOW_MAINTENANCE_ON_MARK"
    state["now"] += 16_000_000_000
    old_risk = client.get(url).json()[position_id]
    assert old_risk["status"] == "STALE" and old_risk["details"] is None
    assert old_risk["last_risk_type"] == "OBSERVED_MAINTENANCE_BREACH"
    closed = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position_id}/close",
                         json={"request_id": str(uuid4())})
    assert closed.status_code == 200, closed.text
    assert client.get(url).json() == {}


def test_concurrent_or_conflicting_observations_do_not_corrupt_history(tmp_path):
    client, state = harness(tmp_path / "observations-concurrent.db")
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position_id = opened.json()["position"]["id"]
    endpoint = f"/paper/perp/accounts/{account['id']}/positions/{position_id}/observe"
    state["now"] += 1_000_000_000
    state.update(bid="109", ask="111", mark="110")
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post(endpoint), range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["event_id"] for response in responses}) == 1
    assert [row["kind"] for row in client.get(f"/paper/perp/accounts/{account['id']}/events").json()] == ["OPEN", "OBSERVE"]

    state.update(bid="110", ask="112", mark="111")
    conflict = client.post(endpoint)
    assert conflict.status_code == 422
    assert len(client.get(f"/paper/perp/accounts/{account['id']}/events").json()) == 2


def test_close_cannot_be_backdated_before_last_observation(tmp_path):
    client, state = harness(tmp_path / "close-ordered.db")
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position_id = opened.json()["position"]["id"]
    state["now"] += 2_000_000_000
    state.update(bid="109", ask="111", mark="110")
    observed = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position_id}/observe")
    assert observed.status_code == 200, observed.text

    state["now"] += 1_000_000_000
    state.update(event_ns=NOW + 1_000_000_000, bid="108", ask="110", mark="109")
    rejected = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position_id}/close",
                           json={"request_id": str(uuid4())})
    assert rejected.status_code == 422
    assert client.get(f"/paper/perp/accounts/{account['id']}/positions").json()[0]["closed_at_ns"] is None
    assert [row["kind"] for row in client.get(f"/paper/perp/accounts/{account['id']}/events").json()] == ["OPEN", "OBSERVE"]

    state["event_ns"] = None
    closed = client.post(f"/paper/perp/accounts/{account['id']}/positions/{position_id}/close",
                         json={"request_id": str(uuid4())})
    assert closed.status_code == 200, closed.text


def test_stale_quotes_invalid_limits_and_idempotency_conflict_fail_closed(tmp_path):
    client, state = harness(tmp_path / "reject.db")
    account = create_account(client)
    state["now"] += 16_000_000_000
    state["event_ns"] = NOW
    stale, _ = open_position(client, account["id"])
    assert stale.status_code == 503
    state["event_ns"] = None
    state["now"] += 1_000_000_000
    too_large, _ = open_position(client, account["id"], size="10")
    assert too_large.status_code == 422
    too_much_leverage, _ = open_position(client, account["id"], leverage="6")
    assert too_much_leverage.status_code == 422
    float_value, _ = open_position(client, account["id"], size=0.1)
    assert float_value.status_code == 422
    foreign_symbol, _ = open_position(client, account["id"], symbol="BTC/USDT")
    assert foreign_symbol.status_code == 422

    first_request = str(uuid4())
    first, _ = open_position(client, account["id"], request_id=first_request, side="SHORT")
    assert first.status_code == 201
    conflict, _ = open_position(client, account["id"], request_id=first_request, side="LONG")
    assert conflict.status_code == 409
    assert len(client.get(f"/paper/perp/accounts/{account['id']}/positions").json()) == 1
    assert client.get(f"/paper/perp/accounts/{account['id']}/events", headers={
        "Origin": "https://outside.invalid"}).status_code == 403


def test_concurrent_opens_cannot_exceed_position_limit(tmp_path):
    client, state = harness(tmp_path / "concurrent.db")
    account = create_account(client, max_positions=1)

    def attempt():
        response, _ = open_position(client, account["id"])
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(lambda _: attempt(), range(2)))
    assert codes == [201, 422]
    assert len(client.get(f"/paper/perp/accounts/{account['id']}/positions").json()) == 1
    assert len(client.get(f"/paper/perp/accounts/{account['id']}/events").json()) == 1


def test_instrument_spec_is_publicly_visible_but_rechecked_on_open(tmp_path):
    client, state = harness(tmp_path / "instrument.db")
    account = create_account(client)
    spec = client.get("/paper/perp/instrument?symbol=BTC%2FUSDT%3APERP")
    assert spec.status_code == 200
    assert spec.json()["size_step"] == "0.001"
    assert spec.json()["min_size"] == "0.01"
    assert client.get("/paper/perp/instrument?symbol=BTC%2FUSDT").status_code == 422
    assert client.get("/paper/perp/instrument?symbol=BTC%2FUSDT%3APERP",
                      headers={"Origin": "https://outside.invalid"}).status_code == 403
    for size, expected in (("0.001", "mínimo"), ("0.0105", "múltiplo"),
                           ("0.1", "Nocional inferior")):
        response, _ = open_position(client, account["id"], size=size)
        assert response.status_code == 422 and expected in response.text
    assert client.get(f"/paper/perp/accounts/{account['id']}/positions").json() == []
    state["spec_offline"] = True
    failed, _ = open_position(client, account["id"])
    assert failed.status_code == 503
    assert client.get(f"/paper/perp/accounts/{account['id']}/positions").json() == []
    state["spec_offline"] = False
    state["spec_changes"] = {"fetched_at_ns": NOW - 61_000_000_000}
    stale, _ = open_position(client, account["id"])
    assert stale.status_code == 503
    state["spec_changes"] = {}
    valid, _ = open_position(client, account["id"])
    assert valid.status_code == 201, valid.text
    events = client.get(f"/paper/perp/accounts/{account['id']}/events").json()
    assert events[0]["details"]["instrument_spec"]["size_step"] == "0.001"


def test_aggregate_instrument_size_cannot_exceed_grvt_public_max(tmp_path):
    client, state = harness(tmp_path / "aggregate.db")
    state["spec_changes"] = {"max_position_size": "1.5"}
    account = create_account(client, initial_balance="100000", max_position_notional="50000",
                             max_total_notional="100000", max_leverage="2")
    first, _ = open_position(client, account["id"], size="1")
    second, _ = open_position(client, account["id"], size="1")
    assert first.status_code == 201
    assert second.status_code == 422 and "agregada" in second.text
    assert len(client.get(f"/paper/perp/accounts/{account['id']}/positions").json()) == 1


def test_version_one_paper_db_migrates_without_reinterpreting_existing_position(tmp_path):
    path = tmp_path / "paper-v1.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            PRAGMA user_version=1;
            CREATE TABLE paper_perp_accounts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, initial_balance TEXT NOT NULL,
                max_position_notional TEXT NOT NULL, max_total_notional TEXT NOT NULL,
                max_leverage TEXT NOT NULL, max_positions INTEGER NOT NULL,
                fee_rate TEXT NOT NULL, slippage_bps TEXT NOT NULL,
                created_at TEXT NOT NULL, stopped_at TEXT);
            CREATE TABLE paper_perp_positions (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES paper_perp_accounts(id),
                symbol TEXT NOT NULL, instrument TEXT NOT NULL, side TEXT NOT NULL,
                size TEXT NOT NULL, leverage TEXT NOT NULL, collateral TEXT NOT NULL,
                entry_price TEXT NOT NULL, entry_mark TEXT NOT NULL, entry_fee TEXT NOT NULL,
                opened_at_ns INTEGER NOT NULL, opened_recorded_at TEXT NOT NULL,
                margin_rules_json TEXT NOT NULL, margin_rules_at_ns INTEGER NOT NULL,
                closed_at_ns INTEGER, exit_price TEXT, exit_mark TEXT,
                exit_fee TEXT, realized_pnl TEXT, uncovered_loss TEXT);
            INSERT INTO paper_perp_accounts VALUES
                ('a','Legada','1000','500','800','5',2,'0.001','0','2026-01-01',NULL);
            INSERT INTO paper_perp_positions VALUES
                ('p','a','BTC/USDT:PERP','BTC_USDT_Perp','LONG','1','2','50.6',
                 '101','100','0.1',1800000000000000000,'2026-01-01','{}',
                 1800000000000000000,NULL,NULL,NULL,NULL,NULL,NULL);
        """)
    store = PaperPerpStore(path)
    position = store.position("a", "p")
    assert position["size"] == "1" and position["instrument_spec"] is None
    assert position["instrument_spec_checked"] is False
    assert Decimal(store.account("a")["available_cash"]) == Decimal("949.4")
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_PAPER_SCHEMA
        assert db.execute("SELECT COUNT(*) FROM paper_perp_positions").fetchone()[0] == 1


def test_version_three_paper_db_migrates_funding_without_losing_positions(tmp_path):
    path = tmp_path / "paper-v3.db"
    client, _ = harness(path)
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position_id = opened.json()["position"]["id"]
    with sqlite3.connect(path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN indicator_state_json")
        db.execute("DROP TABLE paper_signal_candles")
        db.execute("DROP TABLE paper_perp_funding_anchors")
        db.execute("DROP TABLE paper_perp_funding_legacy")
        db.execute("DROP TABLE paper_signal_actions")
        db.execute("DROP INDEX ux_paper_signal_auto_active")
        for column in ("execution_mode", "auto_size", "auto_leverage", "position_id", "risk_last_quote_ns", "source_run_id"):
            db.execute(f"ALTER TABLE paper_signal_sessions DROP COLUMN {column}")
        db.execute("ALTER TABLE paper_perp_positions DROP COLUMN origin_session_id")
        db.execute("DROP TABLE paper_perp_funding_events")
        db.execute("ALTER TABLE paper_perp_positions DROP COLUMN funding_checked_through_ns")
        db.execute("ALTER TABLE paper_perp_positions DROP COLUMN funding_paid")
        db.execute("PRAGMA user_version=3")
    migrated = PaperPerpStore(path)
    position = migrated.position(account["id"], position_id)
    assert position["funding_paid"] == "0" and position["funding_coverage"] == "UNASSESSED"
    assert position["entry_price"] == opened.json()["position"]["entry_price"]
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_PAPER_SCHEMA
        assert db.execute("SELECT COUNT(*) FROM paper_perp_positions").fetchone()[0] == 1


def test_version_six_funding_is_marked_legacy_until_public_anchor_is_rechecked(tmp_path):
    path = tmp_path / "paper-v6-funding.db"
    client, _ = harness(path)
    account = create_account(client)
    opened, _ = open_position(client, account["id"])
    position = opened.json()["position"]
    with sqlite3.connect(path) as db:
        drop_observed_tables(db)
        db.execute("DROP TABLE paper_signal_all_candles")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN indicator_state_json")
        db.execute("DROP TABLE paper_signal_candles")
        db.execute("UPDATE paper_perp_positions SET funding_checked_through_ns=? WHERE id=?",
                   (position["opened_at_ns"] + 1, position["id"]))
        db.execute("DROP TABLE paper_perp_funding_anchors")
        db.execute("DROP TABLE paper_perp_funding_legacy")
        db.execute("ALTER TABLE paper_signal_sessions DROP COLUMN source_run_id")
        db.execute("PRAGMA user_version=6")
    recovered = PaperPerpStore(path)
    assert recovered.account(account["id"])["id"] == account["id"]
    assert recovered.position(account["id"], position["id"])["funding_anchor_status"] == "LEGACY_UNANCHORED"
    with recovered.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_PAPER_SCHEMA
        assert db.execute("SELECT position_id FROM paper_perp_funding_legacy").fetchone()[0] == position["id"]
    record = FundingRecord(position["instrument"], position["opened_at_ns"] - 1,
                           Decimal("0.01"), Decimal("100"), 1)
    result = recovered.settle_funding(account["id"], position["id"], str(uuid4()),
                                      recovered.digest({"test": "anchor-recheck"}),
                                      [record], position["opened_at_ns"] + 2)
    assert result["new_settlements"] == 0
    assert result["position"]["funding_anchor_status"] == "PUBLIC_ANCHORED"
    with recovered.connect() as db:
        assert db.execute("SELECT funding_time_ns FROM paper_perp_funding_anchors").fetchone()[0] == record.time_ns
        assert db.execute("SELECT COUNT(*) FROM paper_perp_funding_legacy").fetchone()[0] == 0
    assert recovered.account(account["id"])["id"] == account["id"]


def test_concurrent_initialization_runs_one_migration(tmp_path):
    path = tmp_path / "paper-concurrent.db"
    def initialize():
        with PaperPerpStore(path).connect():
            pass
        return PaperPerpStore(path).accounts()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: initialize(), range(8)))
    assert results == [[]] * 8
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_PAPER_SCHEMA
