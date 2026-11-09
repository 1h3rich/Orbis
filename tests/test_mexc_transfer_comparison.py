"""Las coincidencias MEXC son pistas, nunca conciliación ni importación."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from app.portfolio.domain import EventInput
from app.portfolio.mexc_transfer_comparison import compare_mexc_transfer_snapshot
from app.portfolio.mexc_transfers import save_transfer_snapshot
from app.portfolio.models import RealMexcAccountLink
from app.portfolio.repository import (append_event, create_account,
                                      transfer_asset_between_accounts,
                                      transfer_cash_between_accounts)
from app.portfolio.vault import RealVault


WHEN = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
REF = "a" * 64


def observation(*, amounts=("25",), asset="USDT"):
    return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
            "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
            "start_ms": int((WHEN - timedelta(days=1)).timestamp() * 1000),
            "end_ms": int((WHEN + timedelta(days=1)).timestamp() * 1000),
            "observed_at": (WHEN + timedelta(days=1)).isoformat(),
            "deposits": [{"direction": "deposit", "asset": asset, "network": "ERC20",
                          "amount": amount, "fee": "0", "status": 5,
                          "time_ms": int((WHEN + timedelta(minutes=5)).timestamp() * 1000)}
                         for amount in amounts],
            "withdrawals": []}


@pytest.mark.parametrize(("published_amount", "hypothesis", "sent", "fee"), [
    ("1", "AMOUNT_IS_GROSS", "1", "0.1"),
    ("0.9", "AMOUNT_IS_NET", "1", "0.1"),
    ("0.1234567890123456789012345678901234567889", "AMOUNT_IS_NET",
     "0.1234567890123456789012345678901234567890",
     "0.0000000000000000000000000000000000000001"),
])
def test_withdrawal_fee_candidates_are_hypotheses_not_linkable(
        tmp_path, published_amount, hypothesis, sent, fee):
    vault = RealVault(tmp_path / "fee-vault.db")
    vault.unlock("withdrawal fee comparison passphrase", create=True)
    with vault.session() as db:
        mexc = create_account(db, "MEXC", "EUR").id
        wallet = create_account(db, "Wallet", "EUR").id
        append_event(db, mexc, str(uuid4()), EventInput(
            "BUY", "EUR", WHEN - timedelta(hours=1), asset="BTC",
            quantity=Decimal("2"), unit_price=Decimal("100")))
        transfer_asset_between_accounts(
            db, mexc, wallet, str(uuid4()), asset="BTC", quote_currency="EUR",
            quantity=Decimal(sent), fee=Decimal(fee), occurred_at=WHEN)
        observed = observation(asset="BTC")
        observed["deposits"] = []
        observed["withdrawals"] = [{"direction": "withdrawal", "asset": "BTC",
            "network": "BTC", "amount": published_amount, "fee": fee, "status": 7,
            "transfer_type": 0, "time_ms": int((WHEN + timedelta(minutes=5)).timestamp() * 1000)}]
        snapshot_id = save_transfer_snapshot(db, REF, observed)["id"]
        compared = compare_mexc_transfer_snapshot(db, mexc, snapshot_id)
        row = compared["rows"][0]
        assert row["result"] == "FEE_HYPOTHESIS_ONLY"
        assert row["confirmed_match_id"] is None
        assert row["candidates"][0]["fee_hypothesis"] == hypothesis
        assert Decimal(row["candidates"][0]["fee"]) == Decimal(fee)
        assert compared["observed_rows"][0]["result"] == "FEE_HYPOTHESIS_ONLY"
        observed["withdrawals"][0]["fee"] = "0"
        no_fee_id = save_transfer_snapshot(db, REF, observed)["id"]
        assert compare_mexc_transfer_snapshot(db, mexc, no_fee_id)["rows"][0]["result"] == "NO_EXACT_CANDIDATE"
    vault.lock()


def test_comparison_is_read_only_and_ambiguity_is_explicit(tmp_path):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock("comparison test passphrase", create=True)
    with vault.session() as db:
        wallet = create_account(db, "Wallet").id
        mexc = create_account(db, "MEXC").id
        append_event(db, wallet, str(uuid4()), EventInput(
            "CONTRIBUTION", "USDT", WHEN - timedelta(hours=1), amount=Decimal("100")))
        transfer_cash_between_accounts(db, wallet, mexc, str(uuid4()),
                                       currency="USDT", amount=Decimal("25"), occurred_at=WHEN)
        snapshot_id = save_transfer_snapshot(db, REF, observation())["id"]
        before = db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one()
        result = compare_mexc_transfer_snapshot(db, mexc, snapshot_id)
        assert result["conclusion"] == "CANDIDATES_ONLY_NOT_RECONCILED"
        assert result["account_reference"] == "UNLINKED_ACCOUNT_ASSUMED"
        assert result["rows"][0]["result"] == "POSSIBLE_MATCH"
        assert result["rows"][0]["candidates"][0]["status_code"] == 5
        assert result["observed_rows"][0]["result"] == "POSSIBLE_MATCH"
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == before
        pending = observation()
        pending["deposits"][0]["status"] = 4
        pending_id = save_transfer_snapshot(db, REF, pending)["id"]
        pending_result = compare_mexc_transfer_snapshot(db, mexc, pending_id)
        assert pending_result["rows"][0]["result"] == "NON_FINAL_CANDIDATE_ONLY"
        assert pending_result["rows"][0]["candidates"] == []
        assert pending_result["observed_rows"][0]["result"] == "NOT_FINAL_NO_COMPARISON"
        rejected = observation()
        rejected["deposits"][0]["status"] = 7
        rejected_id = save_transfer_snapshot(db, REF, rejected)["id"]
        assert compare_mexc_transfer_snapshot(db, mexc, rejected_id)["rows"][0]["result"] == "NON_FINAL_CANDIDATE_ONLY"
        mixed = observation(amounts=("25", "25"))
        mixed["deposits"][1]["status"] = 4
        mixed_id = save_transfer_snapshot(db, REF, mixed)["id"]
        mixed_result = compare_mexc_transfer_snapshot(db, mexc, mixed_id)
        assert mixed_result["rows"][0]["result"] == "POSSIBLE_MATCH"
        assert [row["result"] for row in mixed_result["observed_rows"]] == [
            "POSSIBLE_MATCH", "NOT_FINAL_NO_COMPARISON"]
        ambiguous_id = save_transfer_snapshot(db, REF, observation(amounts=("25", "25")))["id"]
        ambiguous = compare_mexc_transfer_snapshot(db, mexc, ambiguous_id)
        assert ambiguous["rows"][0]["result"] == "AMBIGUOUS"
        assert {row["result"] for row in ambiguous["observed_rows"]} == {"AMBIGUOUS"}
        wrong_id = save_transfer_snapshot(db, REF, observation(asset="BTC"))["id"]
        wrong = compare_mexc_transfer_snapshot(db, mexc, wrong_id)
        assert wrong["rows"][0]["result"] == "NO_EXACT_CANDIDATE"
        assert wrong["observed_rows"][0]["result"] == "NO_PAIRED_LOCAL_CANDIDATE"
        edge = observation(asset="BTC")
        edge["start_ms"] = int((WHEN - timedelta(minutes=1)).timestamp() * 1000)
        edge_id = save_transfer_snapshot(db, REF, edge)["id"]
        assert compare_mexc_transfer_snapshot(db, mexc, edge_id)["rows"][0]["result"] == "PARTIAL_WINDOW_NO_CANDIDATE"
        boundary = observation(amounts=("25.0",))
        boundary["deposits"][0]["time_ms"] = int((WHEN + timedelta(days=1)).timestamp() * 1000)
        boundary_id = save_transfer_snapshot(db, REF, boundary)["id"]
        assert compare_mexc_transfer_snapshot(db, mexc, boundary_id)["rows"][0]["result"] == "POSSIBLE_MATCH"
        assert compare_mexc_transfer_snapshot(db, wallet, snapshot_id)["rows"][0]["direction"] == "withdrawal"
        withdrawal = observation()
        withdrawal["deposits"] = []
        withdrawal["withdrawals"] = [{**observation()["deposits"][0], "direction": "withdrawal",
                                     "status": 3, "transfer_type": 0}]
        withdrawal_id = save_transfer_snapshot(db, REF, withdrawal)["id"]
        assert compare_mexc_transfer_snapshot(db, wallet, withdrawal_id)["rows"][0]["result"] == "NON_FINAL_CANDIDATE_ONLY"
        withdrawal["withdrawals"][0]["status"] = 7
        success_id = save_transfer_snapshot(db, REF, withdrawal)["id"]
        assert compare_mexc_transfer_snapshot(db, wallet, success_id)["rows"][0]["result"] == "POSSIBLE_MATCH"
        internal = observation()
        internal["deposits"] = []
        internal["withdrawals"] = [{**withdrawal["withdrawals"][0], "transfer_type": 1}]
        internal_id = save_transfer_snapshot(db, REF, internal)["id"]
        internal_result = compare_mexc_transfer_snapshot(db, wallet, internal_id)
        assert internal_result["rows"][0]["result"] == "NON_EXTERNAL_WITHDRAWAL_ONLY"
        assert internal_result["rows"][0]["candidates"] == []
        assert internal_result["observed_rows"][0]["result"] == "INTERNAL_MEXC_NOT_WALLET"
        legacy = observation()
        legacy["deposits"] = []
        legacy["withdrawals"] = [{key: value for key, value in withdrawal["withdrawals"][0].items()
                                  if key != "transfer_type"}]
        legacy_id = save_transfer_snapshot(db, REF, legacy)["id"]
        legacy_result = compare_mexc_transfer_snapshot(db, wallet, legacy_id)
        assert legacy_result["rows"][0]["result"] == "NON_EXTERNAL_WITHDRAWAL_ONLY"
        assert legacy_result["observed_rows"][0]["result"] == "ROUTE_UNKNOWN_NO_COMPARISON"
        transfer_cash_between_accounts(db, wallet, mexc, str(uuid4()),
                                       currency="USDT", amount=Decimal("25"),
                                       occurred_at=WHEN + timedelta(minutes=1))
        assert {row["result"] for row in compare_mexc_transfer_snapshot(db, mexc, snapshot_id)["rows"]} == {"AMBIGUOUS"}
        assert compare_mexc_transfer_snapshot(db, mexc, snapshot_id)["observed_rows"][0]["result"] == "AMBIGUOUS"
        db.add(RealMexcAccountLink(account_ref=REF, account_id=mexc, created_at=WHEN))
        db.commit()
        assert compare_mexc_transfer_snapshot(db, mexc, snapshot_id)["account_reference"] == "LOCAL_LINK_MATCH_NOT_OWNERSHIP_PROOF"
        assert compare_mexc_transfer_snapshot(db, wallet, snapshot_id)["conclusion"] == "ACCOUNT_REFERENCE_CONFLICT_NO_COMPARISON"
        assert compare_mexc_transfer_snapshot(db, wallet, snapshot_id)["observed_rows"] == []
        other_id = save_transfer_snapshot(db, "b" * 64, observation())["id"]
        assert compare_mexc_transfer_snapshot(db, mexc, other_id)["conclusion"] == "ACCOUNT_REFERENCE_CONFLICT_NO_COMPARISON"
    vault.lock()


def test_comparison_api_requires_unlocked_vault_and_does_not_create_events(tmp_path):
    app = FastAPI()
    path = tmp_path / "vault.db"
    app.include_router(create_real_router(path))
    client = TestClient(app)
    passphrase = "long comparison passphrase"
    token = client.post("/real/vault/create", json={"passphrase": passphrase}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", json={"name": "MEXC"}, headers=headers).json()
    wallet = client.post("/real/accounts", json={"name": "Wallet"}, headers=headers).json()
    # Sin instantánea guardada, no se inventa un resultado satisfactorio.
    path = f"/real/accounts/{account['id']}/mexc-transfer-candidates/{uuid4()}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers).status_code == 422
    observer = RealVault(tmp_path / "vault.db")
    observer.unlock(passphrase)
    with observer.session() as db:
        snapshot_id = save_transfer_snapshot(db, REF, observation())["id"]
        append_event(db, wallet["id"], str(uuid4()), EventInput(
            "CONTRIBUTION", "USDT", WHEN - timedelta(hours=1), amount=Decimal("100")))
        transfer_cash_between_accounts(db, wallet["id"], account["id"], str(uuid4()),
                                       currency="USDT", amount=Decimal("25"), occurred_at=WHEN)
    observer.lock()
    url = f"/real/accounts/{account['id']}/mexc-transfer-candidates/{snapshot_id}"
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["rows"][0]["result"] == "POSSIBLE_MATCH"
    assert response.json()["observed_rows"][0]["result"] == "POSSIBLE_MATCH"
    assert response.json()["conclusion"] == "CANDIDATES_ONLY_NOT_RECONCILED"
