"""Vínculos explícitos de traslados, sólo con bóvedas y datos ficticios."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.portfolio.vault import SCHEMA_VERSION
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from app.live_execution.mexc_uid_guard import claim_mexc_uid_in_transaction
from app.portfolio.domain import EventInput, PortfolioError
from app.portfolio.mexc_transfer_matches import (
    confirm_mexc_transfer_match, verify_mexc_transfer_match,
)
from app.portfolio.mexc_transfer_audit import audit_mexc_transfer_archive
from app.portfolio.mexc_transfers import (
    find_mexc_deposit_by_evm_tx_hash, find_mexc_withdrawal_by_evm_tx_hash,
    save_transfer_snapshot,
)
from app.portfolio.mexc_transfer_comparison import compare_mexc_transfer_snapshot
from app.portfolio.models import (
    MexcTransferIdentityEvidence, MexcTransferSnapshot, RealMexcAccountLink,
    RealMexcTransferMatch,
)
from app.portfolio.repository import (
    append_event, create_account, transfer_cash_between_accounts,
)
from app.portfolio.vault import RealVault, VaultError


WHEN = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
ACCOUNT_REF = "a" * 64
PASSWORD = "isolated transfer match password"
TX_HASH = "0x" + "ab" * 32
MEXC_DESTINATION = "0x" + "33" * 20
MEXC_TOKEN_CONTRACT = "0x" + "44" * 20


def snapshot(*, status=5, amount="25", fee="0", observed=WHEN,
             source_id="private-deposit-identifier"):
    return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
            "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
            "start_ms": int((WHEN - timedelta(days=1)).timestamp() * 1000),
            "end_ms": int(WHEN.timestamp() * 1000),
            "observed_at": observed.isoformat(),
            "deposits": [{"direction": "deposit", "asset": "USDT", "network": "ERC20",
                          "amount": amount, "fee": fee, "status": status,
                          "time_ms": int((WHEN - timedelta(minutes=5)).timestamp() * 1000),
                          "source_identity": source_id}],
            "withdrawals": []}


def withdrawal_snapshot(*, status=7, amount="25", fee="0.5", observed=WHEN,
                        chain_hash=TX_HASH, destination=MEXC_DESTINATION,
                        source_id="withdrawal-private-id"):
    data = snapshot(observed=observed)
    data["deposits"] = []
    data["withdrawals"] = [{"direction": "withdrawal", "asset": "USDT", "network": "ERC20",
                            "amount": amount, "fee": fee, "status": status,
                            "time_ms": int((WHEN - timedelta(minutes=5)).timestamp() * 1000),
                            "transfer_type": 0, "source_identity": source_id,
                            "chain_identity": chain_hash, "destination_identity": destination}]
    return data


def setup(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    with vault.session() as db:
        wallet = create_account(db, "Wallet", "USDT").id
        mexc = create_account(db, "MEXC", "USDT").id
        append_event(db, wallet, str(uuid4()), EventInput(
            "CONTRIBUTION", "USDT", WHEN - timedelta(days=2), amount=Decimal("100")))
        transfer = transfer_cash_between_accounts(
            db, wallet, mexc, str(uuid4()), currency="USDT",
            amount=Decimal("25"), occurred_at=WHEN - timedelta(minutes=5),
        )
        db.add(RealMexcAccountLink(account_ref=ACCOUNT_REF, account_id=mexc,
                                   created_at=WHEN))
        db.commit()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        claim_mexc_uid_in_transaction(db, ACCOUNT_REF, "123456789", now=WHEN)
        db.commit()
        snapshot_id = save_transfer_snapshot(
            db, ACCOUNT_REF, snapshot(), view_uid="123456789",
        )["id"]
        assert db.query(RealMexcTransferMatch).count() == 0
        from app.portfolio.mexc_transfers import read_transfer_snapshot
        source_ref = read_transfer_snapshot(db, snapshot_id)["deposits"][0]["source_ref"]
    return vault, wallet, mexc, transfer["target_event_id"], snapshot_id, source_ref


def confirm(db, mexc, event_id, snapshot_id, source_ref):
    return confirm_mexc_transfer_match(
        db, mexc, snapshot_id, "deposit", source_ref, event_id,
        confirmation="VINCULAR TRASLADO", now=WHEN + timedelta(minutes=1),
    )


def test_evm_tx_hash_finds_only_verified_mexc_deposit_without_persisting_hash(tmp_path):
    vault, wallet, mexc, _event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        before = db.connection().exec_driver_sql("SELECT count(*) FROM mexc_transfer_snapshots").scalar_one()
        assert find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH)["status"] == "NOT_FOUND_IN_VERIFIED_ARCHIVE"
        save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            status=4, source_id=TX_HASH, observed=WHEN + timedelta(minutes=1)),
            view_uid="123456789")
        pending = find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH.upper().replace("0X", "0x"))
        assert pending["status"] == "NOT_FINAL_MEXC_DEPOSIT"
        assert pending["verified_snapshots"] == 2
        save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            source_id=TX_HASH, observed=WHEN + timedelta(minutes=2)),
            view_uid="123456789")
        final = find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH)
        assert final["status"] == "FINAL_MEXC_DEPOSIT"
        assert final["amount"] == "25" and final["asset"] == "USDT"
        assert final["observations"] == 2
        assert final["wallet_ownership_verified"] is False
        assert final["chain_receipt_verified"] is False
        assert final["real_book_link_checked"] is True
        assert final["book_link_status"] == "NO_EXPLICIT_LINK"
        assert final["ready_for_execution"] is False
        assert TX_HASH not in str(final)
        assert db.connection().exec_driver_sql("SELECT count(*) FROM mexc_transfer_snapshots").scalar_one() == before + 2
        with pytest.raises(PortfolioError, match="única"):
            find_mexc_deposit_by_evm_tx_hash(db, wallet, TX_HASH)
        with pytest.raises(PortfolioError, match="inválido"):
            find_mexc_deposit_by_evm_tx_hash(db, mexc, "not-a-tx")
        save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            source_id=TX_HASH, amount="26", observed=WHEN + timedelta(minutes=3)),
            view_uid="123456789")
        assert find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH)["status"] == "CONTRADICTORY_MEXC_DEPOSIT"
    vault.lock()
    assert TX_HASH.encode("ascii") not in (tmp_path / "real.db").read_bytes()


def test_evm_destination_is_hmac_only_and_compared_without_leaking_address(tmp_path):
    vault, _wallet, mexc, _event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        saved = save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            source_id=TX_HASH, observed=WHEN + timedelta(minutes=1)) | {
                "deposits": [{**snapshot(source_id=TX_HASH)["deposits"][0],
                              "destination_identity": MEXC_DESTINATION}]
            }, view_uid="123456789")
        from app.portfolio.mexc_transfers import read_transfer_snapshot
        archived = read_transfer_snapshot(db, saved["id"])
        assert MEXC_DESTINATION not in str(archived)
        assert MEXC_DESTINATION not in db.get(MexcTransferSnapshot, saved["id"]).payload_json
        result = find_mexc_deposit_by_evm_tx_hash(
            db, mexc, TX_HASH, ["0x" + "44" * 20, MEXC_DESTINATION.upper().replace("0X", "0x")])
        assert result["status"] == "FINAL_MEXC_DEPOSIT"
        assert result["destination_evidence_available"] is True
        assert result["matched_destination_indices"] == [1]
        assert MEXC_DESTINATION not in str(result)
        with pytest.raises(PortfolioError, match="Destinos EVM inválidos"):
            find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH, ["not-an-address"])
    vault.lock()
    assert MEXC_DESTINATION.encode("ascii") not in (tmp_path / "real.db").read_bytes()


def test_evm_withdrawal_lookup_is_private_and_does_not_assume_fee_semantics(tmp_path):
    vault, _wallet, mexc, _event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        assert find_mexc_withdrawal_by_evm_tx_hash(db, mexc, TX_HASH)["status"] == "NOT_FOUND_IN_VERIFIED_ARCHIVE"
        saved = save_transfer_snapshot(db, ACCOUNT_REF, withdrawal_snapshot(),
                                       view_uid="123456789")["id"]
        from app.portfolio.mexc_transfers import read_transfer_snapshot
        archived = read_transfer_snapshot(db, saved)
        assert TX_HASH not in str(archived) and MEXC_DESTINATION not in str(archived)
        found = find_mexc_withdrawal_by_evm_tx_hash(db, mexc, TX_HASH, MEXC_DESTINATION)
        assert found["status"] == "FINAL_MEXC_WITHDRAWAL"
        assert found["recipient_matches_archive"] is True
        assert found["amount"] == "25" and found["fee"] == "0.5"
        assert found["amount_semantics"] == "MEXC_GROSS_NET_UNVERIFIED"
        assert found["chain_receipt_verified"] is False
        assert found["real_book_link_checked"] is False
        assert found["ready_for_execution"] is False
        assert TX_HASH not in str(found) and MEXC_DESTINATION not in str(found)
        mismatch = find_mexc_withdrawal_by_evm_tx_hash(db, mexc, TX_HASH, "0x" + "55" * 20)
        assert mismatch["recipient_matches_archive"] is False
        with pytest.raises(PortfolioError, match="Destino EVM inválido"):
            find_mexc_withdrawal_by_evm_tx_hash(db, mexc, TX_HASH, "not-an-address")
        save_transfer_snapshot(db, ACCOUNT_REF, withdrawal_snapshot(
            amount="26", observed=WHEN + timedelta(minutes=1)), view_uid="123456789")
        assert find_mexc_withdrawal_by_evm_tx_hash(db, mexc, TX_HASH)["status"] == "CONTRADICTORY_MEXC_WITHDRAWAL"
    vault.lock()
    raw = (tmp_path / "real.db").read_bytes()
    assert TX_HASH.encode("ascii") not in raw and MEXC_DESTINATION.encode("ascii") not in raw


def test_withdrawal_contract_metadata_is_optional_and_conflicts_block_match(tmp_path):
    vault, _wallet, mexc, _event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        first = withdrawal_snapshot(observed=WHEN + timedelta(minutes=1))
        first["withdrawals"][0]["token_contract"] = MEXC_TOKEN_CONTRACT
        save_transfer_snapshot(db, ACCOUNT_REF, first, view_uid="123456789")
        found = find_mexc_withdrawal_by_evm_tx_hash(db, mexc, TX_HASH)
        assert found["status"] == "FINAL_MEXC_WITHDRAWAL"
        assert found["token_contract"] == MEXC_TOKEN_CONTRACT
        second = withdrawal_snapshot(observed=WHEN + timedelta(minutes=2))
        second["withdrawals"][0]["token_contract"] = "0x" + "55" * 20
        save_transfer_snapshot(db, ACCOUNT_REF, second, view_uid="123456789")
        assert find_mexc_withdrawal_by_evm_tx_hash(
            db, mexc, TX_HASH)["status"] == "CONTRADICTORY_MEXC_WITHDRAWAL"
        assert audit_mexc_transfer_archive(db, mexc)["counts"]["conflicted_references"] >= 1
    vault.lock()


def test_evm_withdrawal_lookup_rejects_nonexternal_and_nonfinal(tmp_path):
    vault, _wallet, mexc, _event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        pending = withdrawal_snapshot(status=6, observed=WHEN + timedelta(minutes=1))
        save_transfer_snapshot(db, ACCOUNT_REF, pending, view_uid="123456789")
        result = find_mexc_withdrawal_by_evm_tx_hash(db, mexc, TX_HASH)
        assert result["status"] == "NOT_FINAL_MEXC_WITHDRAWAL"
        internal = withdrawal_snapshot(status=7, observed=WHEN + timedelta(minutes=2),
                                       source_id="internal-withdrawal")
        internal["withdrawals"][0]["transfer_type"] = 1
        internal["withdrawals"][0]["chain_identity"] = "0x" + "cd" * 32
        save_transfer_snapshot(db, ACCOUNT_REF, internal, view_uid="123456789")
        assert find_mexc_withdrawal_by_evm_tx_hash(
            db, mexc, "0x" + "cd" * 32)["status"] == "NON_EXTERNAL_MEXC_WITHDRAWAL"
    vault.lock()


def test_evm_withdrawal_lookup_rejects_reassigned_or_shared_chain_hash(tmp_path):
    vault, _wallet, mexc, _event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        save_transfer_snapshot(db, ACCOUNT_REF, withdrawal_snapshot(),
                               view_uid="123456789")
        save_transfer_snapshot(db, ACCOUNT_REF, withdrawal_snapshot(
            observed=WHEN + timedelta(minutes=1), chain_hash="0x" + "cd" * 32),
            view_uid="123456789")
        assert find_mexc_withdrawal_by_evm_tx_hash(
            db, mexc, TX_HASH)["status"] == "CONTRADICTORY_MEXC_WITHDRAWAL"
        shared = withdrawal_snapshot(observed=WHEN + timedelta(minutes=2),
                                     source_id="another-withdrawal")
        save_transfer_snapshot(db, ACCOUNT_REF, shared, view_uid="123456789")
        assert find_mexc_withdrawal_by_evm_tx_hash(
            db, mexc, TX_HASH)["status"] == "CONTRADICTORY_MEXC_WITHDRAWAL"
        assert audit_mexc_transfer_archive(db, mexc)["counts"]["conflicted_references"] >= 2
    vault.lock()


def test_evm_withdrawal_lookup_api_requires_vault_and_never_echoes_secrets(tmp_path):
    vault, _wallet, mexc, _event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        save_transfer_snapshot(db, ACCOUNT_REF, withdrawal_snapshot(),
                               view_uid="123456789")
    vault.lock()
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db"))
    client = TestClient(app)
    path = f"/real/accounts/{mexc}/mexc-withdrawal-tx-lookup"
    assert client.post(path, json={"tx_hash": TX_HASH}).status_code == 401
    token = client.post("/real/vault/unlock", json={"passphrase": PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    response = client.post(path, json={"tx_hash": TX_HASH,
                                       "recipient": MEXC_DESTINATION}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "FINAL_MEXC_WITHDRAWAL"
    assert response.json()["recipient_matches_archive"] is True
    assert response.headers["cache-control"] == "no-store"
    assert TX_HASH not in response.text and MEXC_DESTINATION not in response.text
    for body in ({"tx_hash": TX_HASH + "0"},
                 {"tx_hash": TX_HASH, "recipient": MEXC_DESTINATION + "0"},
                 {"tx_hash": {"value": TX_HASH}},
                 [{"tx_hash": TX_HASH}]):
        rejected = client.post(path, json=body, headers=headers)
        assert rejected.status_code == 422
        assert TX_HASH not in rejected.text and MEXC_DESTINATION not in rejected.text


def test_conflicting_known_mexc_destinations_block_manual_link(tmp_path):
    vault, _wallet, mexc, event_id, snapshot_id, source_ref = setup(tmp_path)
    with vault.session() as db:
        for minute, destination in ((1, MEXC_DESTINATION), (2, "0x" + "44" * 20)):
            observed = snapshot(source_id="private-deposit-identifier",
                                observed=WHEN + timedelta(minutes=minute))
            observed["deposits"][0]["destination_identity"] = destination
            save_transfer_snapshot(db, ACCOUNT_REF, observed, view_uid="123456789")
        audit = audit_mexc_transfer_archive(db, mexc)
        assert audit["counts"]["conflicted_references"] == 1
        with pytest.raises(PortfolioError, match="destination changed"):
            confirm(db, mexc, event_id, snapshot_id, source_ref)
        assert db.query(RealMexcTransferMatch).count() == 0


def test_conflicting_published_token_contracts_are_not_reported_as_verified(tmp_path):
    vault, _wallet, mexc, event_id, snapshot_id, source_ref = setup(tmp_path)
    with vault.session() as db:
        first = snapshot(source_id=TX_HASH, observed=WHEN + timedelta(minutes=1))
        first["deposits"][0]["token_contract"] = MEXC_TOKEN_CONTRACT
        save_transfer_snapshot(db, ACCOUNT_REF, first, view_uid="123456789")
        found = find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH)
        assert found["status"] == "FINAL_MEXC_DEPOSIT"
        assert found["token_contract"] == MEXC_TOKEN_CONTRACT
        second = snapshot(source_id=TX_HASH, observed=WHEN + timedelta(minutes=2))
        second["deposits"][0]["token_contract"] = "0x" + "55" * 20
        save_transfer_snapshot(db, ACCOUNT_REF, second, view_uid="123456789")
        assert find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH)["status"] == "CONTRADICTORY_MEXC_DEPOSIT"
        assert audit_mexc_transfer_archive(db, mexc)["counts"]["conflicted_references"] >= 1
        for minute, contract in ((3, MEXC_TOKEN_CONTRACT), (4, "0x" + "55" * 20)):
            historical = snapshot(source_id="private-deposit-identifier",
                                  observed=WHEN + timedelta(minutes=minute))
            historical["deposits"][0]["token_contract"] = contract
            save_transfer_snapshot(db, ACCOUNT_REF, historical, view_uid="123456789")
        with pytest.raises(PortfolioError, match="token contract changed"):
            confirm(db, mexc, event_id, snapshot_id, source_ref)
    vault.lock()


def test_evm_tx_hash_lookup_api_is_authenticated_and_does_not_echo_hash(tmp_path):
    vault, _wallet, mexc, _event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        deposit = snapshot(source_id=TX_HASH)
        deposit["deposits"][0]["destination_identity"] = MEXC_DESTINATION
        save_transfer_snapshot(db, ACCOUNT_REF, deposit,
                               view_uid="123456789")
    vault.lock()
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db"))
    client = TestClient(app)
    path = f"/real/accounts/{mexc}/mexc-deposit-tx-lookup"
    assert client.post(path, json={"tx_hash": TX_HASH}).status_code == 401
    token = client.post("/real/vault/unlock", json={"passphrase": PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    response = client.post(path, json={"tx_hash": TX_HASH,
                                       "destinations": [MEXC_DESTINATION]}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "FINAL_MEXC_DEPOSIT"
    assert response.json()["matched_destination_indices"] == [0]
    assert TX_HASH not in response.text and MEXC_DESTINATION not in response.text
    assert response.headers["cache-control"] == "no-store"
    invalid = client.post(path, json={"tx_hash": TX_HASH + "0"}, headers=headers)
    assert invalid.status_code == 422
    assert TX_HASH not in invalid.text
    malformed_hash = client.post(path, json={"tx_hash": {"hash": TX_HASH}}, headers=headers)
    assert malformed_hash.status_code == 422
    assert TX_HASH not in malformed_hash.text
    malformed_body = client.post(path, content='{"tx_hash":"' + TX_HASH + '",',
                                 headers={**headers, "Content-Type": "application/json"})
    assert malformed_body.status_code == 422
    assert TX_HASH not in malformed_body.text
    wrong_shape = client.post(path, json=[{"tx_hash": TX_HASH}], headers=headers)
    assert wrong_shape.status_code == 422
    assert TX_HASH not in wrong_shape.text
    invalid_destination = client.post(path, json={"tx_hash": TX_HASH,
                                                   "destinations": [MEXC_DESTINATION + "x"]}, headers=headers)
    assert invalid_destination.status_code == 422
    assert MEXC_DESTINATION not in invalid_destination.text
    malformed_destination = client.post(path, json={"tx_hash": TX_HASH,
                                                     "destinations": [{"address": MEXC_DESTINATION}]},
                                        headers=headers)
    assert malformed_destination.status_code == 422
    assert MEXC_DESTINATION not in malformed_destination.text


def test_archive_audit_deduplicates_verified_windows_without_enabling_orders(tmp_path):
    vault, _wallet, mexc, event_id, snapshot_id, source_ref = setup(tmp_path)
    with vault.session() as db:
        before = db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one()
        first = audit_mexc_transfer_archive(db, mexc)
        assert first["conclusion"] == "ARCHIVE_AUDIT_NOT_ACCOUNT_RECONCILIATION"
        assert first["ready_for_execution"] is False
        assert first["counts"]["final_distinct_references"] == 1
        assert first["counts"]["unmatched_final_references"] == 1
        assert first["counts"]["unlinked_local_events"] == 1
        assert first["counts"]["unlinked_local_fully_covered"] == 0
        assert first["counts"]["unlinked_local_partial_or_outside"] == 1
        assert first["unmatched_references"] == [{"direction": "deposit", "source_ref": source_ref}]
        assert len(first["coverage_windows"]) == 1
        with pytest.raises(PortfolioError, match="exactly one linked"):
            audit_mexc_transfer_archive(db, _wallet)
        confirm(db, mexc, event_id, snapshot_id, source_ref)
        save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            observed=WHEN + timedelta(minutes=1)), view_uid="123456789")
        old = save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            source_id="unverified"))
        after = audit_mexc_transfer_archive(db, mexc)
        assert after["snapshots"] == {"total": 3, "verified": 2, "unverified": 1}
        assert after["counts"]["final_distinct_references"] == 1
        assert after["counts"]["confirmed_matches"] == 1
        assert after["counts"]["unmatched_final_references"] == 0
        assert after["counts"]["unlinked_local_events"] == 0
        assert after["ready_for_execution"] is False
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == before
        assert old["identity_verified"] is False
    vault.lock()


def test_archive_audit_separates_complete_and_missing_local_coverage(tmp_path):
    vault, wallet, mexc, _event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        transfer_cash_between_accounts(
            db, wallet, mexc, str(uuid4()), currency="USDT", amount=Decimal("10"),
            occurred_at=WHEN - timedelta(days=2) + timedelta(hours=1),
        )
        partial = audit_mexc_transfer_archive(db, mexc)
        assert partial["counts"]["unlinked_local_events"] == 2
        assert partial["counts"]["unlinked_local_fully_covered"] == 0
        broad = snapshot(observed=WHEN + timedelta(minutes=1))
        broad["start_ms"] = int((WHEN - timedelta(days=2)).timestamp() * 1000)
        broad["end_ms"] = int((WHEN + timedelta(days=1)).timestamp() * 1000)
        save_transfer_snapshot(db, ACCOUNT_REF, broad, view_uid="123456789")
        covered = audit_mexc_transfer_archive(db, mexc)
        assert covered["counts"]["unlinked_local_events"] == 2
        assert covered["counts"]["unlinked_local_fully_covered"] == 1
        assert covered["counts"]["unlinked_local_partial_or_outside"] == 1
        assert covered["counts"]["final_distinct_references"] == 1
        assert covered["ready_for_execution"] is False
    vault.lock()


def test_archive_audit_flags_conflicting_reference_without_match(tmp_path):
    vault, _wallet, mexc, _event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            amount="26", observed=WHEN + timedelta(minutes=2)), view_uid="123456789")
        report = audit_mexc_transfer_archive(db, mexc)
        assert report["counts"]["final_distinct_references"] == 1
        assert report["counts"]["conflicted_references"] == 1
        assert report["counts"]["unmatched_final_references"] == 0
        assert report["ready_for_execution"] is False
    vault.lock()


def test_archive_audit_flags_finality_regression_but_allows_pending_to_final(tmp_path):
    vault, _wallet, mexc, _event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            status=4, observed=WHEN - timedelta(minutes=1)), view_uid="123456789")
        initial = audit_mexc_transfer_archive(db, mexc)
        assert initial["counts"]["conflicted_references"] == 0
        assert initial["counts"]["unmatched_final_references"] == 1
        save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            status=4, observed=WHEN + timedelta(minutes=1)), view_uid="123456789")
        regressed = audit_mexc_transfer_archive(db, mexc)
        assert regressed["counts"]["final_distinct_references"] == 1
        assert regressed["counts"]["conflicted_references"] == 1
        assert regressed["counts"]["unmatched_final_references"] == 0
        assert regressed["ready_for_execution"] is False
    vault.lock()


def test_exact_paired_match_is_durable_private_and_idempotent(tmp_path):
    vault, wallet, mexc, event_id, snapshot_id, source_ref = setup(tmp_path)
    with vault.session() as db:
        with pytest.raises(PortfolioError, match="explicitly"):
            confirm_mexc_transfer_match(db, mexc, snapshot_id, "deposit", source_ref,
                                        event_id, confirmation="SI")
        first = confirm(db, mexc, event_id, snapshot_id, source_ref)
        assert first["status"] == "LINKED"
        assert first["conclusion"] == "EXPLICIT_MATCH_NOT_ACCOUNT_RECONCILIATION"
        assert confirm(db, mexc, event_id, snapshot_id, source_ref)["status"] == "ALREADY_LINKED"
        row = db.get(RealMexcTransferMatch, first["id"])
        verify_mexc_transfer_match(db, row)
        assert db.query(RealMexcTransferMatch).count() == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 3
        assert "private-deposit-identifier" not in str(row.__dict__)
        with pytest.raises(PortfolioError, match="not linked"):
            confirm(db, wallet, event_id, snapshot_id, source_ref)
    vault.lock()
    assert b"private-deposit-identifier" not in (tmp_path / "real.db").read_bytes()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        verify_mexc_transfer_match(db, db.get(RealMexcTransferMatch, first["id"]))
    vault.lock()


def test_evm_tx_lookup_checks_explicit_real_link_without_claiming_reconciliation(tmp_path):
    vault, _wallet, mexc, event_id, snapshot_id, source_ref = setup(tmp_path)
    with vault.session() as db:
        deposit = snapshot(source_id=TX_HASH, observed=WHEN + timedelta(minutes=1))
        deposit["deposits"][0]["destination_identity"] = MEXC_DESTINATION
        tx_snapshot_id = save_transfer_snapshot(db, ACCOUNT_REF, deposit, view_uid="123456789")["id"]
        from app.portfolio.mexc_transfers import read_transfer_snapshot
        tx_source_ref = read_transfer_snapshot(db, tx_snapshot_id)["deposits"][0]["source_ref"]
        before = find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH)
        assert before["book_link_status"] == "NO_EXPLICIT_LINK"
        linked = confirm(db, mexc, event_id, tx_snapshot_id, tx_source_ref)
        after = find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH)
        assert after["book_link_status"] == "VERIFIED_EXPLICIT_LINK"
        assert after["real_book_link_checked"] is True
        assert after["ready_for_execution"] is False
        assert after["chain_receipt_verified"] is False
        assert after["wallet_ownership_verified"] is False
        assert TX_HASH not in str(after) and event_id not in str(after)
        db.get(RealMexcTransferMatch, linked["id"]).event_digest = "0" * 64
        db.commit()
        assert find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH)["book_link_status"] == "INCONSISTENT_LINK"
    vault.lock()


def test_later_mexc_conflict_downgrades_existing_book_link(tmp_path):
    vault, _wallet, mexc, event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        first = save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            source_id=TX_HASH, observed=WHEN + timedelta(minutes=1)), view_uid="123456789")["id"]
        from app.portfolio.mexc_transfers import read_transfer_snapshot
        source_ref = read_transfer_snapshot(db, first)["deposits"][0]["source_ref"]
        confirm(db, mexc, event_id, first, source_ref)
        save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            source_id=TX_HASH, amount="26", observed=WHEN + timedelta(minutes=2)),
            view_uid="123456789")
        result = find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH)
        assert result["status"] == "CONTRADICTORY_MEXC_DEPOSIT"
        assert result["book_link_status"] == "INCONSISTENT_LINK"
        assert result["ready_for_execution"] is False
    vault.lock()


def test_book_lookup_does_not_promote_legacy_link_without_view_identity(tmp_path):
    vault, _wallet, mexc, event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        archived = save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            source_id=TX_HASH, observed=WHEN + timedelta(minutes=1)),
            view_uid="123456789")["id"]
        from app.portfolio.mexc_transfers import read_transfer_snapshot
        source_ref = read_transfer_snapshot(db, archived)["deposits"][0]["source_ref"]
        confirm(db, mexc, event_id, archived, source_ref)
        db.delete(db.get(MexcTransferIdentityEvidence, archived))
        db.commit()
        result = find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH)
        assert result["status"] == "NOT_FOUND_IN_VERIFIED_ARCHIVE"
        assert result["book_link_status"] == "NO_MATCHING_DEPOSIT"
        # Una observación nueva verificada del mismo txId no convierte el
        # vínculo anterior sin UID en correspondencia verificada.
        save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            source_id=TX_HASH, observed=WHEN + timedelta(minutes=2)),
            view_uid="123456789")
        result = find_mexc_deposit_by_evm_tx_hash(db, mexc, TX_HASH)
        assert result["status"] == "FINAL_MEXC_DEPOSIT"
        assert result["book_link_status"] == "LEGACY_LINK_UNVERIFIED_IDENTITY"
        assert result["ready_for_execution"] is False
    vault.lock()


def test_rejects_ambiguous_changed_or_nonfinal_source(tmp_path):
    vault, _wallet, mexc, event_id, snapshot_id, source_ref = setup(tmp_path)
    with vault.session() as db:
        pending = save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            status=4, observed=WHEN + timedelta(minutes=1)), view_uid="123456789")["id"]
        with pytest.raises(PortfolioError, match="finality"):
            confirm(db, mexc, event_id, snapshot_id, source_ref)
        with pytest.raises(PortfolioError, match="not final"):
            confirm(db, mexc, event_id, pending, source_ref)
        changed = save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            amount="26", observed=WHEN + timedelta(minutes=2)), view_uid="123456789")["id"]
        with pytest.raises(PortfolioError, match="changed"):
            confirm(db, mexc, event_id, changed, source_ref)
        assert db.query(RealMexcTransferMatch).count() == 0
    vault.lock()


def test_rejects_fee_unpaired_event_and_double_booking(tmp_path):
    vault, wallet, mexc, event_id, snapshot_id, source_ref = setup(tmp_path)
    with vault.session() as db:
        paid = save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            fee="1", source_id="fee-source"), view_uid="123456789")["id"]
        from app.portfolio.mexc_transfers import read_transfer_snapshot
        fee_ref = read_transfer_snapshot(db, paid)["deposits"][0]["source_ref"]
        with pytest.raises(PortfolioError, match="fee"):
            confirm(db, mexc, event_id, paid, fee_ref)
        with pytest.raises(PortfolioError, match="not linked"):
            confirm(db, wallet, event_id, snapshot_id, source_ref)
        confirm(db, mexc, event_id, snapshot_id, source_ref)
        duplicate = save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            source_id="different-deposit"), view_uid="123456789")["id"]
        other_ref = read_transfer_snapshot(db, duplicate)["deposits"][0]["source_ref"]
        with pytest.raises(PortfolioError, match="already linked"):
            confirm(db, mexc, event_id, duplicate, other_ref)
    vault.lock()


def test_legacy_snapshot_without_uid_evidence_cannot_gain_new_match(tmp_path):
    vault, _wallet, mexc, event_id, _snapshot_id, _source_ref = setup(tmp_path)
    with vault.session() as db:
        old = save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            source_id="old-unverified-reference"))["id"]
        from app.portfolio.mexc_transfers import read_transfer_snapshot
        archived = read_transfer_snapshot(db, old)
        assert not archived["identity_verified"]
        with pytest.raises(PortfolioError, match="verified View identity"):
            confirm(db, mexc, event_id, old, archived["deposits"][0]["source_ref"])
        assert db.query(RealMexcTransferMatch).count() == 0
    vault.lock()


def test_v22_migrates_with_backup_and_match_integrity_is_checked(tmp_path):
    vault, _wallet, mexc, event_id, snapshot_id, source_ref = setup(tmp_path)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_mexc_transfer_matches")
        db.connection().exec_driver_sql("PRAGMA user_version = 22")
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    assert vault.last_migration_backup is not None
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        linked = confirm(db, mexc, event_id, snapshot_id, source_ref)
        db.get(RealMexcTransferMatch, linked["id"]).source_digest = "0" * 64
        db.commit()
    with pytest.raises(VaultError, match="transfer matches"):
        vault.backup(tmp_path / "corrupt-match-backup.db")
    vault.lock()


def test_later_provider_conflict_blocks_comparison_but_not_encrypted_backup(tmp_path):
    vault, _wallet, mexc, event_id, snapshot_id, source_ref = setup(tmp_path)
    with vault.session() as db:
        linked = confirm(db, mexc, event_id, snapshot_id, source_ref)
        save_transfer_snapshot(db, ACCOUNT_REF, snapshot(
            amount="26", observed=WHEN + timedelta(minutes=2)), view_uid="123456789")
        with pytest.raises(PortfolioError, match="changed"):
            compare_mexc_transfer_snapshot(db, mexc, snapshot_id)
        assert db.get(RealMexcTransferMatch, linked["id"]) is not None
    backup = vault.backup(tmp_path / "recoverable.db")
    assert backup.is_file()
    vault.lock()


def test_v23_match_survives_v24_migration_as_legacy_unverified(tmp_path):
    vault, _wallet, mexc, event_id, snapshot_id, source_ref = setup(tmp_path)
    with vault.session() as db:
        matched = confirm(db, mexc, event_id, snapshot_id, source_ref)
        db.connection().exec_driver_sql("DROP TABLE mexc_transfer_identity_evidence")
        db.connection().exec_driver_sql("PRAGMA user_version = 23")
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    assert vault.last_migration_backup is not None
    with vault.session() as db:
        from app.portfolio.mexc_transfers import read_transfer_snapshot
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert not read_transfer_snapshot(db, snapshot_id)["identity_verified"]
        verify_mexc_transfer_match(db, db.get(RealMexcTransferMatch, matched["id"]))
        assert compare_mexc_transfer_snapshot(db, mexc, snapshot_id)["rows"][0]["confirmed_match_id"] == matched["id"]
    vault.lock()


def test_local_api_confirms_exact_match_and_comparison_recovers_it(tmp_path):
    vault, _wallet, mexc, event_id, snapshot_id, source_ref = setup(tmp_path)
    vault.lock()
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db"))
    client = TestClient(app)
    endpoint = f"/real/accounts/{mexc}/mexc-transfer-matches"
    audit_endpoint = f"/real/accounts/{mexc}/mexc-transfer-audit"
    body = {"snapshot_id": snapshot_id, "event_id": event_id,
            "direction": "deposit", "source_ref": source_ref,
            "confirmation": "VINCULAR TRASLADO"}
    assert client.post(endpoint, json=body).status_code == 401
    assert client.get(audit_endpoint).status_code == 401
    token = client.post("/real/vault/unlock", json={"passphrase": PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post(endpoint, json=body | {"confirmation": "SI"},
                       headers=headers).status_code == 409
    saved = client.post(endpoint, json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.headers["cache-control"] == "no-store"
    assert saved.json()["status"] == "LINKED"
    assert "private-deposit-identifier" not in saved.text
    assert client.post(endpoint, json=body, headers=headers).json()["status"] == "ALREADY_LINKED"
    comparison = client.get(
        f"/real/accounts/{mexc}/mexc-transfer-candidates/{snapshot_id}",
        headers=headers,
    )
    assert comparison.status_code == 200, comparison.text
    assert comparison.json()["rows"][0]["confirmed_match_id"] == saved.json()["id"]
    assert comparison.json()["observed_rows"][0]["confirmed_match_id"] == saved.json()["id"]
    assert comparison.json()["conclusion"] == "CANDIDATES_ONLY_NOT_RECONCILED"
    audit = client.get(audit_endpoint, headers=headers)
    assert audit.status_code == 200, audit.text
    assert audit.headers["cache-control"] == "no-store"
    assert audit.json()["counts"]["confirmed_matches"] == 1
    assert audit.json()["ready_for_execution"] is False
    client.post("/real/vault/lock", headers=headers)
    assert client.post(endpoint, json=body, headers=headers).status_code == 401
    assert client.get(audit_endpoint, headers=headers).status_code == 401
