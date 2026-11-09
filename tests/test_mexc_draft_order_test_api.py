"""Ensayo MEXC explícito: sólo /order/test, nunca asientos ni envío real."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event
import time
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.store import LabStore
from app.live_execution.mexc_instrument import MexcSpotRules
from app.live_execution.mexc_test_order import MexcSpotOrderTestClient
from app.portfolio import api as real_api
from app.portfolio.api import create_real_router
from app.portfolio.domain import EventInput
from app.portfolio.repository import append_event
from app.portfolio.vault import RealVault
from tests.test_mexc_policy_drafts_api import (
    KEY, SECRET, PASSPHRASE, UID, View, definition, draft,
)


class Trade:
    def __init__(self, api_key, secret_key):
        assert (api_key, secret_key) == ("draft-trade-access", "draft-trade-secret")

    def account_uid(self):
        return UID


def setup(tmp_path, *, fail_post=False, view_factory=View, exposure_quote_get=None):
    real_path, lab_path = tmp_path / "real.db", tmp_path / "lab.db"
    version_id = LabStore(lab_path).create_strategy("LONG", definition())["version_id"]
    calls = []

    def test_factory(key, secret):
        assert (key, secret) == ("draft-trade-access", "draft-trade-secret")
        def fake_post(url, body, headers):
            calls.append((url, body, headers))
            if fail_post:
                raise RuntimeError("secret upstream response")
            return {}
        return MexcSpotOrderTestClient(key, secret,
            http_post=fake_post)

    def rules(symbol, *, now):
        return MexcSpotRules(symbol, "BTC", "USDT", now, True, True, 8, 8,
                             Decimal("0.000001"), Decimal("5"), Decimal("1000000"),
                             Decimal("0.001"), None, None)

    def quote(symbol):
        assert symbol == "BTCUSDT"
        return {"symbol": symbol, "bidPrice": "99999", "askPrice": "100000",
                "lastPrice": "100000", "closeTime": int((datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp() * 1000)}

    app = FastAPI()
    app.include_router(create_real_router(
        real_path, lab_path=lab_path, mexc_factory=view_factory,
        mexc_trade_factory=Trade, mexc_order_test_factory=test_factory,
        mexc_rules_fetch=rules, mexc_quote_get=exposure_quote_get or quote))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": PASSPHRASE}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account_id = client.post("/real/accounts", json={"name": "MEXC"}, headers=headers).json()["id"]
    client.post("/real/exchanges/mexc/connect", json={"api_key": KEY, "secret_key": SECRET}, headers=headers)
    assert client.post(f"/real/accounts/{account_id}/mexc-link", json={"confirmation": "VINCULAR"},
                       headers=headers).status_code == 200
    payload = draft(version_id)
    policy = client.post(f"/real/accounts/{account_id}/mexc-policy-drafts", json=payload,
                         headers=headers)
    assert policy.status_code == 200, policy.text
    path = f"/real/accounts/{account_id}/mexc-policy-drafts/{payload['request_id']}/order-test"
    return client, headers, path, calls, real_path


def test_draft_order_test_never_sends_or_books_real_order(tmp_path):
    client, headers, path, calls, real_path = setup(tmp_path)
    body = {"quantity_base": "0.001", "limit_price_quote": "100000",
            "confirmation": "PROBAR_SIN_EJECUTAR", "request_id": str(uuid4())}
    assert client.post(path, json=body, headers=headers).status_code == 409
    assert calls == []
    assert client.post("/real/exchanges/mexc/trade/connect", json={
        "api_key": "draft-trade-access", "secret_key": "draft-trade-secret"},
        headers=headers).status_code == 200
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["accepted_by_test_endpoint"] is True
    assert result.json()["executed"] is False
    assert result.json()["persisted"] is True
    assert result.json()["execution_enabled"] is False
    assert len(calls) == 1 and calls[0][0] == "https://api.mexc.com/api/v3/order/test"
    repeated = client.post(path, json=body, headers=headers)
    assert repeated.status_code == 200 and repeated.json()["replayed_without_post"] is True
    assert len(calls) == 1
    archived = client.get(path.removesuffix("/order-test") + "/order-tests", headers=headers)
    assert archived.status_code == 200 and len(archived.json()) == 1
    assert archived.json()[0]["status"] == "ACCEPTED"
    assert "/api/v3/order?" not in calls[0][0]
    vault = RealVault(real_path)
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_live_spot_intents").scalar_one() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_order_test_attempts").scalar_one() == 1
    vault.lock()
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    assert client.post(path, json=body, headers=headers).status_code == 401
    next_token = client.post("/real/vault/unlock", json={"passphrase": PASSPHRASE}).json()["token"]
    restored = client.get(path.removesuffix("/order-test") + "/order-tests",
                          headers={"X-Orbis-Vault-Token": next_token})
    assert restored.status_code == 200 and restored.json()[0]["status"] == "ACCEPTED"


def test_draft_order_test_fails_closed_on_bounds_and_confirmation(tmp_path):
    client, headers, path, calls, _ = setup(tmp_path)
    assert client.post("/real/exchanges/mexc/trade/connect", json={
        "api_key": "draft-trade-access", "secret_key": "draft-trade-secret"},
        headers=headers).status_code == 200
    base = {"quantity_base": "0.001", "limit_price_quote": "100000",
            "confirmation": "PROBAR_SIN_EJECUTAR", "request_id": str(uuid4())}
    for changed in (
        {"quantity_base": "0.002"}, {"quantity_base": "NaN"},
        {"limit_price_quote": "101000"}, {"confirmation": "EJECUTAR"},
    ):
        result = client.post(path, json={**base, **changed}, headers=headers)
        assert result.status_code in (409, 422), result.text
    assert client.post(path.replace(path.split("/")[-2], str(uuid4())), json=base,
                       headers=headers).status_code == 409
    assert calls == []


def test_uncertain_order_test_is_archived_and_same_id_never_resends(tmp_path):
    client, headers, path, calls, real_path = setup(tmp_path, fail_post=True)
    assert client.post("/real/exchanges/mexc/trade/connect", json={
        "api_key": "draft-trade-access", "secret_key": "draft-trade-secret"},
        headers=headers).status_code == 200
    body = {"request_id": str(uuid4()), "quantity_base": "0.001",
            "limit_price_quote": "100000", "confirmation": "PROBAR_SIN_EJECUTAR"}
    first = client.post(path, json=body, headers=headers)
    assert first.status_code == 422
    assert "secret" not in first.text
    assert len(calls) == 1
    replay = client.post(path, json=body, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["status"] == "NOT_CONFIRMED"
    assert replay.json()["replayed_without_post"] is True
    assert len(calls) == 1
    changed = client.post(path, json={**body, "quantity_base": "0.002"}, headers=headers)
    assert changed.status_code == 409 and len(calls) == 1
    vault = RealVault(real_path)
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT status, client_order_id FROM real_mexc_order_test_attempts").one()
        assert row[0] == "NOT_CONFIRMED"
    vault.lock()


def test_policy_daily_book_risk_is_local_and_not_execution_ready(tmp_path):
    client, headers, path, calls, real_path = setup(tmp_path)
    risk_path = path.removesuffix("/order-test") + "/daily-book-risk"
    account = path.split("/accounts/")[1].split("/")[0]
    assert client.get(risk_path).status_code == 401
    empty = client.get(risk_path, headers=headers)
    assert empty.status_code == 200 and empty.headers["cache-control"] == "no-store"
    assert empty.json()["turnover_quote"] == "0"
    assert empty.json()["base_asset"] == "BTC"
    assert empty.json()["position_quantity_base"] == "0"
    assert empty.json()["average_cost_quote_per_base"] is None
    assert empty.json()["cost_provenance"] == "NO_POSITION_HISTORY"
    assert empty.json()["remaining_book_turnover_quote"] == "200"
    assert empty.json()["book_turnover_limit_reached"] is False
    assert empty.json()["book_realized_loss_limit_reached"] is False
    assert empty.json()["execution_ready"] is False
    empty_revision = empty.json()["book_revision"]
    # La prueba puede correr justo después de medianoche UTC: "ahora - 2 min"
    # pertenecería al día anterior y haría fallar el corte diario sin bug real.
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    yesterday = today - timedelta(days=1) + timedelta(hours=12)
    events_path = f"/real/accounts/{account}/events"
    for payload in (
        {"kind": "CONTRIBUTION", "amount": "1000", "quote_currency": "USDT",
         "occurred_at": yesterday.isoformat()},
        {"kind": "BUY", "asset": "BTC", "quantity": "1", "unit_price": "100",
         "quote_currency": "USDT", "occurred_at": (yesterday + timedelta(minutes=1)).isoformat()},
        {"kind": "SELL", "asset": "BTC", "quantity": "0.5", "unit_price": "90",
         "fee": "1", "quote_currency": "USDT", "occurred_at": today.isoformat()},
    ):
        result = client.post(events_path, json={"request_id": str(uuid4()), **payload}, headers=headers)
        assert result.status_code == 200, result.text
    risk = client.get(risk_path, headers=headers).json()
    assert risk["trade_count"] == 1
    assert risk["turnover_quote"] == "45.0"
    assert risk["realized_pnl_quote"] == "-6.0"
    assert risk["position_quantity_base"] == "0.5"
    assert risk["position_cost_basis_quote"] == "50.0"
    assert risk["average_cost_quote_per_base"] == "100"
    assert risk["cost_provenance"] == "UNLINKED_OR_CHANGED_TRADE"
    assert risk["verified_fill_count"] == 0
    assert risk["remaining_book_turnover_quote"] == "155.0"
    assert risk["book_turnover_limit_reached"] is False
    assert risk["book_realized_loss_limit_reached"] is False
    assert risk["source"] == "REAL_BOOK_ONLY"
    assert risk["book_revision"] != empty_revision
    assert risk["reconciled_with_venue"] is False
    assert risk["execution_ready"] is False
    assert "VENUE_FILL_COVERAGE" in risk["missing"]
    assert calls == []

    for payload in (
        {"kind": "SELL", "asset": "BTC", "quantity": "0.5", "unit_price": "50",
         "fee": "1", "quote_currency": "USDT", "occurred_at": datetime.now(timezone.utc).isoformat()},
        {"kind": "BUY", "asset": "ETH", "quantity": "2", "unit_price": "100",
         "quote_currency": "USDT", "occurred_at": datetime.now(timezone.utc).isoformat()},
    ):
        result = client.post(events_path, json={"request_id": str(uuid4()), **payload}, headers=headers)
        assert result.status_code == 200, result.text
    over = client.get(risk_path, headers=headers).json()
    assert over["turnover_quote"] == "270.0"
    assert over["realized_pnl_quote"] == "-32.0"
    assert over["remaining_book_turnover_quote"] == "0"
    assert over["book_turnover_limit_reached"] is True
    assert over["book_realized_loss_limit_reached"] is True
    assert over["execution_ready"] is False
    assert "UNREALIZED_PNL" in over["missing"]
    assert calls == []
    vault = RealVault(real_path)
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        db.connection().exec_driver_sql(
            "UPDATE real_events SET note = 'asiento alterado' WHERE account_id = ? AND kind = 'BUY'",
            (account,),
        )
        db.commit()
    vault.lock()
    assert client.get(risk_path, headers=headers).status_code == 409


def test_spot_exposure_values_all_assets_but_never_authorizes_trading(tmp_path):
    class ExposureView(View):
        def balances(self):
            return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                    "observed_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                    "balances": [
                        {"asset": "BTC", "free": "0.002", "locked": "0", "total": "0.002"},
                        {"asset": "ETH", "free": "1", "locked": "0", "total": "1"},
                        {"asset": "USDT", "free": "50", "locked": "0", "total": "50"},
                    ]}

        def open_orders(self, symbol):
            return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                    "coverage": "ALL_OPEN_ORDERS_FOR_ONE_SYMBOL", "symbol": symbol,
                    "observed_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                    "orders": []}

    def quotes(symbol):
        assert symbol in ("BTCUSDT", "ETHUSDT")
        bid = "99999" if symbol == "BTCUSDT" else "2000"
        return {"symbol": symbol, "bidPrice": bid, "askPrice": bid,
                "lastPrice": bid, "closeTime": int((datetime.now(timezone.utc)
                                                      - timedelta(seconds=1)).timestamp() * 1000)}

    client, headers, path, posts, _ = setup(
        tmp_path, view_factory=ExposureView, exposure_quote_get=quotes)
    exposure_path = path.removesuffix("/order-test") + "/spot-exposure"
    assert client.get(exposure_path).status_code == 401
    response = client.get(exposure_path, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    observed = response.json()
    assert observed["base_exposure_quote"] == "199.998"
    assert observed["other_assets_exposure_quote"] == "2000.000"
    assert observed["non_quote_exposure_quote"] == "2199.998"
    assert observed["total_spot_quote"] == "2249.998"
    assert observed["remaining_base_headroom_quote"] == "100.002"
    assert observed["base_limit_reached"] is False
    assert [row["asset"] for row in observed["balances"]] == ["BTC", "ETH", "USDT"]
    assert observed["source"] == "MEXC_VIEW_PUBLIC_QUOTES_READ_ONLY"
    assert observed["reconciled_with_book"] is False and observed["execution_ready"] is False
    assert "OTHER_PAIR_RISK" not in observed["missing"]
    assert posts == []

    def missing_quote(symbol):
        if symbol == "ETHUSDT":
            return {**quotes(symbol), "symbol": "WRONGUSDT"}
        return quotes(symbol)

    missing_path = tmp_path / "missing"
    missing_path.mkdir()
    client2, headers2, path2, posts2, _ = setup(
        missing_path, view_factory=ExposureView,
        exposure_quote_get=missing_quote)
    failed = client2.get(path2.removesuffix("/order-test") + "/spot-exposure", headers=headers2)
    assert failed.status_code == 503
    assert posts2 == []


def test_daily_book_risk_reads_one_snapshot_while_other_connection_writes(tmp_path, monkeypatch):
    client, headers, path, _calls, real_path = setup(tmp_path)
    risk_path = path.removesuffix("/order-test") + "/daily-book-risk"
    account = path.split("/accounts/")[1].split("/")[0]
    initial_revision = client.get(risk_path, headers=headers).json()["book_revision"]
    writer_vault = RealVault(real_path)
    writer_vault.unlock(PASSPHRASE)
    started = Event()
    original = real_api.verify_account_event_digests
    pending = []

    def write_event():
        started.set()
        with writer_vault.session() as db:
            append_event(db, account, str(uuid4()), EventInput(
                kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("25"),
                occurred_at=datetime.now(timezone.utc),
            ))

    with ThreadPoolExecutor(max_workers=1) as pool:
        def verified_then_competing_write(db, account_id):
            count = original(db, account_id)
            if not pending:
                pending.append(pool.submit(write_event))
                assert started.wait(timeout=2)
                time.sleep(0.1)
                assert not pending[0].done()
            return count

        monkeypatch.setattr(real_api, "verify_account_event_digests", verified_then_competing_write)
        before = client.get(risk_path, headers=headers)
        assert before.status_code == 200, before.text
        pending[0].result(timeout=5)
        after = client.get(risk_path, headers=headers)
    writer_vault.lock()
    assert after.status_code == 200
    assert before.json()["book_revision"] == initial_revision
    assert after.json()["book_revision"] != before.json()["book_revision"]
    assert before.json()["trade_count"] == after.json()["trade_count"] == 0
