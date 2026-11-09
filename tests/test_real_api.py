from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import RealAccess, create_real_router
from app.live_execution.grvt_testnet_recovery import (
    GrvtTestnetRecoveryItem, GrvtTestnetRecoveryReport,
)
from app.live_execution.grvt_testnet_evidence import GrvtTestnetEvidenceError
from app.live_execution.grvt_testnet_journal import (
    GrvtTestnetJournalError, GrvtTestnetRecord,
)
from app.live_execution.grvt_testnet_preflight import GrvtTestnetPreflightError
from app.portfolio.vault import RealVault


def test_dashboard_reads_do_not_extend_real_vault_idle_deadline(tmp_path, monkeypatch):
    touched = []
    original_touch = RealAccess._touch

    def count_touch(self):
        touched.append(True)
        original_touch(self)

    monkeypatch.setattr(RealAccess, "_touch", count_touch)
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    with TestClient(app) as client:
        token = client.post("/real/vault/create", json={"passphrase": "a robust local password"}).json()["token"]
        headers = {"X-Orbis-Vault-Token": token, "X-Orbis-Passive": "1"}
        touched.clear()
        assert client.get("/real/accounts", headers={"X-Orbis-Passive": "1"}).status_code == 401
        assert client.get("/real/accounts", headers=headers).status_code == 200
        assert touched == []
        assert client.get("/real/accounts", headers={"X-Orbis-Vault-Token": token}).status_code == 200
        assert len(touched) == 1
        created = client.post("/real/accounts", headers=headers, json={"name": "Principal", "base_currency": "EUR"})
        assert created.status_code == 200
        assert len(touched) == 2  # La cabecera pasiva no desactiva el toque en escrituras.
        combined = client.get(f"/real/accounts/{created.json()['id']}/snapshot?include_events=true", headers=headers)
        assert combined.status_code == 200 and combined.json()["events"] == []
        assert len(touched) == 2


def test_real_api_create_register_reopen_and_lock(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    status=client.get("/real/vault/status")
    assert status.json() == {"exists": False, "unlocked": False,
        "capabilities":{"dex_receipt_order_proof":True,"dex_gas_order_proof":True,"economic_review":True,"economic_review_gas":True,"economic_review_order":True,"economic_review_wallets":True,"external_recovery":True}}
    assert status.headers['cache-control']=='no-store'
    assert client.get("/real/accounts").status_code == 401
    token = client.post("/real/vault/create", json={"passphrase": "a robust local password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    intents = client.get("/real/execution/mexc/intents", headers=headers)
    assert intents.status_code == 200 and intents.json() == []
    assert intents.headers["cache-control"] == "no-store"
    close_url = f"/real/execution/mexc/intents/{uuid4()}/close-reservation"
    observe_url = f"/real/execution/mexc/intents/{uuid4()}/observe-stopped"
    assert client.post(close_url, json={"confirmation": "CERRAR"}).status_code == 401
    assert client.post(observe_url).status_code == 401
    assert client.post(observe_url, headers=headers).status_code == 409
    assert client.post(close_url, json={"confirmation": "NO"}, headers=headers).status_code == 422
    assert client.post(close_url, json={"confirmation": "CERRAR"}, headers=headers).status_code == 409
    account = client.post("/real/accounts", json={"name": "Principal", "base_currency": "EUR"}, headers=headers).json()
    assert account["name"] == "Principal"
    payload = {
        "request_id": str(uuid4()), "kind": "BUY", "asset": "BTC", "quote_currency": "EUR",
        "quantity": "0.123456789012345678", "unit_price": "100.01", "fee": "0.01",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    url = f"/real/accounts/{account['id']}/events"
    contribution = {
        "request_id": str(uuid4()), "kind": "CONTRIBUTION", "quote_currency": "EUR",
        "amount": "1000", "occurred_at": payload["occurred_at"],
    }
    assert client.post(url, json=contribution, headers=headers).status_code == 200
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert client.post(url, json=payload, headers=headers).json()["id"] == response.json()["id"]
    assert client.post(url, json={**payload, "quantity": "0.2"}, headers=headers).status_code == 409
    assert client.get(f"/real/accounts/{account['id']}/snapshot", headers=headers).json()["positions"][0]["quantity"] == "0.123456789012345678"
    monkeypatch.setattr(
        "app.portfolio.api.get_configured_price_with_meta",
        lambda *args, **kwargs: {"price": "120", "source": "test-public", "as_of": datetime.now(timezone.utc).isoformat(), "stale": False},
    )
    valued = client.get(f"/real/accounts/{account['id']}/valuation", headers=headers)
    assert valued.status_code == 200, valued.text
    assert valued.json()["currencies"][0]["complete"] is True
    assert valued.json()["positions"][0]["price_source"] == "test-public"
    assert valued.json()["positions"][0]["unrealized_pnl"] is not None
    row = valued.json()["currencies"][0]
    assert Decimal(row["total_pnl"]) == Decimal(row["realized_pnl"]) + Decimal(row["unrealized_pnl"])
    assert valued.headers["Cache-Control"] == "no-store"
    assert Decimal(row["contributions"]) == Decimal("1000") and row["withdrawals"] == "0"
    assert sum(part["weight_bps"] for part in row["allocation"]) == 10000
    assert sum(Decimal(part["value"]) for part in row["allocation"]) == Decimal(row["equity"])
    monkeypatch.setattr(
        "app.portfolio.api.get_configured_price_with_meta",
        lambda *args, **kwargs: {"price": "120", "as_of": datetime.now(timezone.utc).isoformat(), "stale": False},
    )
    unproven = client.get(f"/real/accounts/{account['id']}/valuation", headers=headers)
    assert unproven.status_code == 200
    assert unproven.json()["currencies"][0]["complete"] is False
    assert unproven.json()["currencies"][0]["equity"] is None
    for observed_at in (datetime.now(timezone.utc) - timedelta(minutes=6),
                        datetime.now(timezone.utc) + timedelta(minutes=2)):
        monkeypatch.setattr(
            "app.portfolio.api.get_configured_price_with_meta",
            lambda *args, **kwargs: {"price": "120", "source": "test-public",
                                     "as_of": observed_at.isoformat(), "stale": False},
        )
        out_of_window = client.get(f"/real/accounts/{account['id']}/valuation", headers=headers)
        assert out_of_window.status_code == 200
        assert out_of_window.json()["currencies"][0]["complete"] is False
        assert out_of_window.json()["currencies"][0]["equity"] is None
    backup = client.post("/real/vault/backup", headers=headers)
    assert backup.status_code == 200, backup.text
    assert backup.json()["encrypted"] is True
    backup_name = backup.json()["path"].rsplit("/", 1)[-1]
    assert any(item["name"] == backup_name for item in client.get("/real/vault/backups", headers=headers).json())
    verify_url = f"/real/vault/backups/{backup_name}/verify"
    assert client.get(verify_url).status_code == 401
    checked = client.get(verify_url, headers=headers)
    assert checked.status_code == 200, checked.text
    assert checked.headers["cache-control"] == "no-store"
    assert (checked.json()["accounts"], checked.json()["events"]) == (1, 2)
    assert client.get("/real/vault/backups/bad-name/verify", headers=headers).status_code == 422
    (tmp_path / "backups" / "real-damaged.db").write_bytes(b"not encrypted")
    assert client.get("/real/vault/backups/real-damaged.db/verify", headers=headers).status_code == 409
    assert client.post("/real/vault/lock", headers=headers).json() == {"locked": True}
    assert client.get("/real/accounts", headers=headers).status_code == 401
    token2 = client.post("/real/vault/unlock", json={"passphrase": "a robust local password"}).json()["token"]
    assert token2 != token
    assert len(client.get(url, headers={"X-Orbis-Vault-Token": token2}).json()) == 2
    headers2 = {"X-Orbis-Vault-Token": token2}
    assert client.post(url, json={
        "request_id": str(uuid4()), "kind": "WITHDRAWAL", "quote_currency": "EUR",
        "amount": "10", "occurred_at": payload["occurred_at"],
    }, headers=headers2).status_code == 200
    assert client.post("/real/vault/restore", json={"name": "../outside.db", "confirmation": "RESTAURAR"}, headers=headers2).status_code == 422
    assert client.post("/real/vault/restore", json={"name": backup_name, "confirmation": "NO"}, headers=headers2).status_code == 422
    restored = client.post("/real/vault/restore", json={"name": backup_name, "confirmation": "RESTAURAR"}, headers=headers2)
    assert restored.status_code == 200, restored.text
    assert restored.json()["locked"] is True
    assert restored.json()["previous_vault_backup"].startswith("pre-restore-")
    assert client.get(url, headers=headers2).status_code == 401
    token3 = client.post("/real/vault/unlock", json={"passphrase": "a robust local password"}).json()["token"]
    assert len(client.get(url, headers={"X-Orbis-Vault-Token": token3}).json()) == 2


def test_real_api_rejects_float_and_cross_origin(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    assert client.post("/real/vault/create", json={"passphrase": "long secret password"}, headers={"Origin": "https://evil.test"}).status_code == 403
    token = client.post("/real/vault/create", json={"passphrase": "long secret password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.get("/real/execution/mexc/intents").status_code == 401
    assert client.get("/real/execution/mexc/intents", headers={**headers,
                      "Origin": "https://evil.test"}).status_code == 403
    account = client.post("/real/accounts", json={"name": "Principal"}, headers=headers).json()
    response = client.post(
        f"/real/accounts/{account['id']}/events",
        json={"request_id": str(uuid4()), "kind": "BUY", "quote_currency": "EUR", "asset": "BTC",
              "quantity": 0.1, "unit_price": "100", "occurred_at": datetime.now(timezone.utc).isoformat()},
        headers=headers,
    )
    assert response.status_code == 422
    assert client.get("/real/accounts", headers={**headers, "Origin": "https://evil.test"}).status_code == 403


def test_realized_result_of_closed_position_remains_in_snapshot_and_valuation(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "isolated realized pnl password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", json={"name": "Principal"}, headers=headers).json()
    url = f"/real/accounts/{account['id']}"
    now = datetime.now(timezone.utc) - timedelta(minutes=3)
    movements = (
        {"kind": "CONTRIBUTION", "amount": "1000"},
        {"kind": "BUY", "asset": "BTC", "quantity": "1", "unit_price": "100"},
        {"kind": "SELL", "asset": "BTC", "quantity": "1", "unit_price": "120"},
    )
    for index, movement in enumerate(movements):
        response = client.post(url + "/events", headers=headers, json={
            "request_id": str(uuid4()), "quote_currency": "EUR",
            "occurred_at": (now + timedelta(minutes=index)).isoformat(), **movement,
        })
        assert response.status_code == 200, response.text
    snapshot = client.get(url + "/snapshot", headers=headers)
    assert snapshot.status_code == 200
    assert snapshot.json()["realized_pnl"] == {"EUR": "20"}
    assert snapshot.json()["positions"][0]["quantity"] == "0"
    archived = client.get(url + "/events", headers=headers).json()
    assert [event["kind"] for event in archived] == ["CONTRIBUTION", "BUY", "SELL"]
    from app.portfolio import repository
    original_list = repository.list_events
    reads = []

    def read_once(db, account_id):
        reads.append(account_id)
        return original_list(db, account_id)

    with monkeypatch.context() as local:
        local.setattr(repository, "list_events", read_once)
        combined = client.get(url + "/snapshot?include_events=true", headers=headers)
    assert combined.status_code == 200 and combined.headers["cache-control"] == "no-store"
    assert combined.json() == {**snapshot.json(), "events": archived}
    assert reads == [account["id"]]
    assert client.get(url + "/snapshot?include_events=true").status_code == 401
    assert client.get(url + "/snapshot?include_events=true", headers={**headers, "Origin": "https://outside.invalid"}).status_code == 403
    valuation = client.get(url + "/valuation", headers=headers)
    assert valuation.status_code == 200
    assert valuation.json()["positions"] == []
    assert valuation.json()["currencies"][0]["equity"] == "1020"
    assert valuation.json()["currencies"][0]["realized_pnl"] == "20"
    assert valuation.json()["currencies"][0]["unrealized_pnl"] == "0"
    assert valuation.json()["currencies"][0]["total_pnl"] == "20"
    assert client.get(url + "/snapshot").status_code == 401


def test_real_api_paired_transfer_is_local_atomic_and_persistent(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "long secret password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    wallet = client.post("/real/accounts", json={"name": "Wallet", "base_currency": "EUR"}, headers=headers).json()
    mexc = client.post("/real/accounts", json={"name": "MEXC", "base_currency": "EUR"}, headers=headers).json()
    when = datetime.now(timezone.utc).isoformat()
    assert client.post(f"/real/accounts/{wallet['id']}/events", json={
        "request_id": str(uuid4()), "kind": "BUY", "quote_currency": "EUR", "asset": "BTC",
        "quantity": "2", "unit_price": "100", "occurred_at": when,
    }, headers=headers).status_code == 200
    path = f"/real/accounts/{wallet['id']}/transfers"
    body = {"request_id": str(uuid4()), "target_account_id": mexc["id"], "asset": "BTC",
            "quote_currency": "EUR", "quantity": "0.5", "fee": "0.01", "occurred_at": when,
            "note": "Traslado ya realizado fuera de Orbis"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path + "/preview", json={**body, "quantity": "1e100000"},
                       headers=headers).status_code == 422
    assert client.post(path, json=body, headers={**headers,
                       "Origin": "https://evil.test"}).status_code == 403
    preview = client.post(path + "/preview", json=body, headers=headers)
    assert preview.status_code == 200 and preview.json()["status"] == "PREVIEW"
    assert Decimal(preview.json()["cost_basis"]) == Decimal("50")
    assert preview.json()["received_quantity"] == "0.49"
    assert client.get(f"/real/accounts/{mexc['id']}/events", headers=headers).json() == []
    assert client.post(path, json={**body, "expected_cost_basis": "49"},
                       headers=headers).status_code == 409
    body["expected_cost_basis"] = preview.json()["cost_basis"]
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["execution"] == "MANUAL_RECORD_ONLY_NO_WALLET_OR_EXCHANGE_TRANSFER"
    assert Decimal(response.json()["cost_basis"]) == Decimal("50")
    assert response.json()["received_quantity"] == "0.49"
    assert client.post(path, json=body, headers=headers).json()["status"] == "ALREADY_RECORDED"
    assert client.post(path, json={**body, "quantity": "1"}, headers=headers).status_code == 409
    assert client.get(f"/real/accounts/{wallet['id']}/snapshot", headers=headers).json()["positions"][0]["quantity"] == "1.5"
    assert client.get(f"/real/accounts/{mexc['id']}/snapshot", headers=headers).json()["positions"][0]["quantity"] == "0.49"
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    new_token = client.post("/real/vault/unlock", json={"passphrase": "long secret password"}).json()["token"]
    assert client.get(f"/real/accounts/{mexc['id']}/events", headers={"X-Orbis-Vault-Token": new_token}).json()[0]["kind"] == "TRANSFER_IN"


def test_real_api_cash_transfer_preview_and_commit(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "long secret password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    wallet = client.post("/real/accounts", json={"name": "Wallet"}, headers=headers).json()
    mexc = client.post("/real/accounts", json={"name": "MEXC"}, headers=headers).json()
    when = datetime.now(timezone.utc).isoformat()
    assert client.post(f"/real/accounts/{wallet['id']}/events", json={
        "request_id": str(uuid4()), "kind": "CONTRIBUTION", "quote_currency": "USDT",
        "amount": "200", "occurred_at": when,
    }, headers=headers).status_code == 200
    path = f"/real/accounts/{wallet['id']}/cash-transfers"
    body = {"request_id": str(uuid4()), "target_account_id": mexc["id"],
            "currency": "USDT", "amount": "75", "occurred_at": when}
    assert client.post(path + "/preview", json=body).status_code == 401
    assert client.post(path + "/preview", json={**body, "amount": "1e100000"}, headers=headers).status_code == 422
    preview = client.post(path + "/preview", json=body, headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["available_at_time"] == "200"
    assert client.get(f"/real/accounts/{mexc['id']}/events", headers=headers).json() == []
    assert client.post(path, json={**body, "expected_source_balance": "199"}, headers=headers).status_code == 409
    recorded = client.post(path, json={**body, "expected_source_balance": "200"}, headers=headers)
    assert recorded.status_code == 200, recorded.text
    assert recorded.headers["cache-control"] == "no-store"
    assert recorded.json()["execution"] == "MANUAL_RECORD_ONLY_NO_WALLET_OR_EXCHANGE_TRANSFER"
    assert client.post(path, json={**body, "expected_source_balance": "200"}, headers=headers).json()["status"] == "ALREADY_RECORDED"
    assert client.get(f"/real/accounts/{wallet['id']}/snapshot", headers=headers).json()["cash"]["USDT"] == "125"
    target = client.get(f"/real/accounts/{mexc['id']}/snapshot", headers=headers).json()
    assert target["cash"]["USDT"] == "75" and target["contributions"] == {}
    assert client.get(f"/real/accounts/{mexc['id']}/events", headers=headers).json()[0]["kind"] == "CASH_TRANSFER_IN"


def test_grvt_testnet_recovery_api_is_local_locked_and_read_only(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    url = "/real/exchanges/grvt/testnet/recovery?sub_account_id=123456"
    assert client.get(url).status_code == 401
    assert client.get("/real/exchanges/grvt/testnet/subaccounts").status_code == 401
    token = client.post("/real/vault/create", json={
        "passphrase": "long secret password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.get(url, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
    assert client.get("/real/exchanges/grvt/testnet/recovery?sub_account_id=bad",
                      headers=headers).status_code == 422
    result = client.get(url, headers=headers)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json() == {"sub_account_id": "123456", "stopped": False,
                             "can_submit": False, "items": []}
    known = client.get("/real/exchanges/grvt/testnet/subaccounts", headers=headers)
    assert known.status_code == 200 and known.headers["cache-control"] == "no-store"
    assert known.json() == {"network": "TESTNET", "can_submit": False,
                            "sub_account_ids": []}
    monkeypatch.setattr("app.portfolio.api.read_testnet_recovery", lambda _vault, _sub:
        GrvtTestnetRecoveryReport("123456", True, (GrvtTestnetRecoveryItem(
            "9223372036854775809", "BTC_USDT_Perp", "BUY", Decimal("0.010000000000000001"),
            "OBSERVED", "CANCELLED", Decimal("0.004000000000000001"),
            "AUDITED", "CANCELLED", 1, True,
            ("EXTERNAL_FILLS", "POSITION_DELTA_MISMATCH"),
            "REVIEW_AUDIT_FINDINGS_NO_RELEASE",
            {"kind": "GRVT_AUTO_PAPER", "action": "OPEN", "version_id": "version-123"},
            {"source": "GRVT_TESTNET_PUBLIC_TICKER", "bid": "49999", "ask": "50000",
             "limit_price": "50000", "max_spread_bps": 100,
             "max_slippage_bps": 100},
            {"fill_count": 1, "filled_size": "0.004000000000000001",
             "weighted_average_price": "50000.000000000000000000",
             "fees_by_currency": [["USDT", "0.10"]],
             "causal_proof": False}),)))
    diagnostic = client.get(url, headers=headers).json()
    assert diagnostic["can_submit"] is False and diagnostic["stopped"] is True
    assert diagnostic["items"][0]["intended_size"] == "0.010000000000000001"
    assert diagnostic["items"][0]["external_traded_size"] == "0.004000000000000001"
    assert diagnostic["items"][0]["current_audit_findings"] == [
        "EXTERNAL_FILLS", "POSITION_DELTA_MISMATCH"]
    assert diagnostic["items"][0]["signal_source"]["version_id"] == "version-123"
    assert diagnostic["items"][0]["price_guard"]["bid"] == "49999"
    assert diagnostic["items"][0]["price_guard"]["max_slippage_bps"] == 100
    assert diagnostic["items"][0]["fill_summary"]["filled_size"] == "0.004000000000000001"
    assert diagnostic["items"][0]["fill_summary"]["causal_proof"] is False
    def corrupted(_vault, _sub):
        raise GrvtTestnetEvidenceError("Evidencia testnet alterada")
    monkeypatch.setattr("app.portfolio.api.read_testnet_recovery", corrupted)
    assert client.get(url, headers=headers).status_code == 409
    monkeypatch.setattr("app.portfolio.api.read_testnet_recovery", lambda _vault, _sub:
                        (_ for _ in ()).throw(GrvtTestnetPreflightError("Preflight alterado")))
    assert client.get(url, headers=headers).status_code == 409
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    assert client.get(url, headers=headers).status_code == 401


def test_grvt_testnet_rejected_close_is_local_confirmed_and_never_sends(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={
        "passphrase": "testnet archive password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    url = "/real/exchanges/grvt/testnet/rejected-zero-fill/close"
    body = {"sub_account_id": "123456", "client_order_id": "9223372036854775809",
            "confirmation": "CERRAR_RECHAZO_TESTNET"}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json={**body, "confirmation": "CERRAR"},
                       headers=headers).status_code == 422
    calls = []

    def reject(_vault, sub, client_id, *, now):
        calls.append((sub, client_id, now))
        raise GrvtTestnetJournalError("private diagnostic")

    monkeypatch.setattr("app.portfolio.api.close_rejected_testnet_zero_fill", reject)
    denied = client.post(url, json=body, headers=headers)
    assert denied.status_code == 409
    assert denied.json()["detail"] == "Rechazo GRVT testnet no conciliado"
    assert len(calls) == 1

    def close(_vault, sub, client_id, *, now):
        calls.append((sub, client_id, now))
        return GrvtTestnetRecord(client_id, "REJECTED_CLOSED", "0x123abc",
                                 "REJECTED", Decimal(0))

    monkeypatch.setattr("app.portfolio.api.close_rejected_testnet_zero_fill", close)
    result = client.post(url, json=body, headers=headers)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json() == {"network": "TESTNET", "client_order_id": body["client_order_id"],
                             "journal_status": "REJECTED_CLOSED", "can_submit": False,
                             "new_order_sent": False}
    assert len(calls) == 2 and all(row[:2] == ("123456", body["client_order_id"])
                                   for row in calls)
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    assert client.post(url, json=body, headers=headers).status_code == 401


def test_real_api_transfer_and_adjustment_round_trip(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "long secret password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", json={"name": "Principal"}, headers=headers).json()
    url = f"/real/accounts/{account['id']}/events"
    when = datetime.now(timezone.utc).isoformat()

    def submit(kind, **fields):
        return client.post(url, json={"request_id": str(uuid4()), "kind": kind, "asset": "BTC",
                                       "quote_currency": "EUR", "occurred_at": when, **fields}, headers=headers)

    assert submit("BUY", quantity="2", unit_price="100").status_code == 200
    transfer = submit("TRANSFER", quantity="1", note="Cartera propia")
    assert transfer.status_code == 200, transfer.text
    assert transfer.json()["note"] == "Cartera propia"
    assert submit("ADJUSTMENT_IN", quantity="1", unit_price="80", note="Inventario omitido").status_code == 200
    assert submit("ADJUSTMENT_OUT", quantity="0.5", note="Corrección").status_code == 200
    assert submit("ADJUSTMENT_OUT", quantity="1").status_code == 422
    assert submit("TRANSFER", quantity="1", note="0x" + "a" * 40).status_code == 422
    before = client.get(f"/real/accounts/{account['id']}/snapshot", headers=headers).json()
    assert before["positions"][0]["quantity"] == "2.5"
    assert before["positions"][0]["realized_pnl"] == "0"
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    token2 = client.post("/real/vault/unlock", json={"passphrase": "long secret password"}).json()["token"]
    headers2 = {"X-Orbis-Vault-Token": token2}
    assert len(client.get(url, headers=headers2).json()) == 4
    assert client.get(f"/real/accounts/{account['id']}/snapshot", headers=headers2).json() == before


def test_v1_unlock_reports_recoverable_migration_backup(tmp_path):
    path = tmp_path / "vault.db"
    vault = RealVault(path)
    vault.unlock("long secret password", create=True)
    assert vault._engine is not None
    with vault._engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE mexc_transfer_snapshots")
        connection.exec_driver_sql("DROP TABLE mexc_observation_batches")
        connection.exec_driver_sql("DROP TABLE mexc_observed_fills")
        connection.exec_driver_sql("PRAGMA user_version = 1")
    vault.lock()

    app = FastAPI()
    app.include_router(create_real_router(path))
    client = TestClient(app)
    response = client.post("/real/vault/unlock", json={"passphrase": "long secret password"})
    assert response.status_code == 200, response.text
    name = response.json()["migrated_backup"]
    assert name.startswith("pre-migrate-v1-") and name.endswith(".db")
    headers = {"X-Orbis-Vault-Token": response.json()["token"]}
    backups = client.get("/real/vault/backups", headers=headers).json()
    assert name in [item["name"] for item in backups]
    assert client.get("/real/exchanges/mexc/observations", headers=headers).json() == []
    restored = client.post("/real/vault/restore", headers=headers,
                           json={"name": name, "confirmation": "RESTAURAR"})
    assert restored.status_code == 200, restored.text
    assert restored.json()["locked"] is True
    reloaded = client.post("/real/vault/unlock", json={"passphrase": "long secret password"})
    assert reloaded.status_code == 200
    assert reloaded.json()["migrated_backup"].startswith("pre-migrate-v1-")


def test_recent_migration_backup_is_listed_and_restorable(tmp_path):
    path = tmp_path / "vault.db"
    app = FastAPI()
    app.include_router(create_real_router(path))
    client = TestClient(app)
    passphrase = "recent migration password"
    token = client.post("/real/vault/create", json={"passphrase": passphrase}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", json={"name": "Principal", "base_currency": "EUR"},
                          headers=headers).json()
    event = {"request_id": str(uuid4()), "kind": "CONTRIBUTION",
             "quote_currency": "EUR", "amount": "1000",
             "occurred_at": datetime.now(timezone.utc).isoformat()}
    assert client.post(f"/real/accounts/{account['id']}/events", json=event,
                       headers=headers).status_code == 200
    assert client.post("/real/vault/lock", headers=headers).status_code == 200

    vault = RealVault(path)
    vault.unlock(passphrase)
    assert vault._engine is not None
    with vault._engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE real_events DROP COLUMN fee_asset")
        connection.exec_driver_sql("PRAGMA user_version = 17")
    vault.lock()

    migrated = client.post("/real/vault/unlock", json={"passphrase": passphrase})
    assert migrated.status_code == 200, migrated.text
    name = migrated.json()["migrated_backup"]
    assert name.startswith("pre-migrate-v17-")
    headers = {"X-Orbis-Vault-Token": migrated.json()["token"]}
    backups = client.get("/real/vault/backups", headers=headers).json()
    assert name in [item["name"] for item in backups]
    assert client.post("/real/vault/restore", headers=headers,
                       json={"name": "pre-migrate-v0-invalid.db",
                             "confirmation": "RESTAURAR"}).status_code == 422
    restored = client.post("/real/vault/restore", headers=headers,
                           json={"name": name, "confirmation": "RESTAURAR"})
    assert restored.status_code == 200, restored.text
    assert restored.json()["locked"] is True
    reloaded = client.post("/real/vault/unlock", json={"passphrase": passphrase})
    assert reloaded.status_code == 200, reloaded.text
    headers = {"X-Orbis-Vault-Token": reloaded.json()["token"]}
    events = client.get(f"/real/accounts/{account['id']}/events", headers=headers).json()
    assert len(events) == 1 and events[0]["kind"] == "CONTRIBUTION"
    assert client.get(f"/real/accounts/{account['id']}/snapshot",
                      headers=headers).json()["cash"]["EUR"] == "1000"


@pytest.mark.parametrize("during_quote",["lock","contribution"])
def test_inflight_valuation_respects_lock_and_keeps_one_book_snapshot(tmp_path, monkeypatch, during_quote):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    entered, proceed = Event(), Event()

    def delayed_price(*args, **kwargs):
        entered.set()
        if not proceed.wait(timeout=15):
            raise RuntimeError("timed out waiting for fixture")
        return {"price":"150", "source":"owned", "as_of":datetime.now(timezone.utc).isoformat(), "stale":False}

    monkeypatch.setattr("app.portfolio.api.get_configured_price_with_meta", delayed_price)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        token = client.post("/real/vault/create", json={"passphrase":"owned delayed quote password"}).json()["token"]
        headers = {"X-Orbis-Vault-Token":token, "X-Orbis-Passive":"1"}
        account = client.post("/real/accounts", headers=headers,
                              json={"name":"Principal", "base_currency":"EUR"}).json()["id"]
        stamp = datetime.now(timezone.utc).isoformat()
        events = f"/real/accounts/{account}/events"
        contribution = {"request_id":str(uuid4()),"kind":"CONTRIBUTION","quote_currency":"EUR",
                        "amount":"1000","occurred_at":stamp}
        buy = {"request_id":str(uuid4()),"kind":"BUY","asset":"BTC","quantity":"1","unit_price":"100",
               "quote_currency":"EUR","occurred_at":stamp}
        assert client.post(events,headers=headers,json=contribution).status_code == 200
        assert client.post(events,headers=headers,json=buy).status_code == 200
        pending = pool.submit(client.get,f"/real/accounts/{account}/valuation",headers=headers)
        try:
            assert entered.wait(timeout=15)
            if during_quote == "lock":
                assert client.post("/real/vault/lock",headers=headers).status_code == 200
            else:
                extra = {**contribution,"request_id":str(uuid4()),"amount":"500",
                         "occurred_at":datetime.now(timezone.utc).isoformat()}
                assert client.post(events,headers=headers,json=extra).status_code == 200
        finally:
            proceed.set()
        response = pending.result(timeout=10)
        if during_quote == "lock":
            assert response.status_code == 401
            assert set(response.json()) == {"detail"}
        else:
            assert response.status_code == 200
            [value] = response.json()["currencies"]
            assert value["contributions"] == "1000" and value["equity"] == "1050"
            assert sum(Decimal(part["value"]) for part in value["allocation"]) == Decimal("1050")
            updated = client.get(f"/real/accounts/{account}/valuation",headers=headers).json()["currencies"][0]
            assert updated["contributions"] == "1500" and updated["equity"] == "1550"
