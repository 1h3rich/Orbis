"""Instantáneas cifradas MEXC: privacidad, reproducción y migración segura."""

from app.portfolio.vault import SCHEMA_VERSION
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from app.portfolio.domain import PortfolioError
from app.live_execution.mexc_uid_guard import claim_mexc_uid_in_transaction
from app.portfolio.mexc_transfers import (
    compare_transfer_snapshots, list_transfer_snapshots, read_transfer_snapshot,
    save_transfer_snapshot,
)
from app.portfolio.vault import RealVault, VaultError


PASSPHRASE = "only temporary test passphrase"
REF = "a" * 64


def snapshot():
    return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
            "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT", "start_ms": 1_800_000_000_000,
            "end_ms": 1_800_086_400_000, "observed_at": "2026-10-13T12:00:00+00:00",
            "deposits": [{"direction": "deposit", "asset": "BTC", "network": "BTC",
                          "amount": "0.123456789012345678", "fee": "0", "status": 5,
                          "time_ms": 1_800_000_001_000}],
            "withdrawals": [{"direction": "withdrawal", "asset": "USDT", "network": "ERC20",
                             "amount": "100.01", "fee": "1.25", "status": 7,
                             "time_ms": 1_800_000_002_000}]}


def test_transfer_snapshots_persist_without_address_or_book_event(tmp_path):
    path = tmp_path / "vault.db"
    vault = RealVault(path)
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        first = save_transfer_snapshot(db, REF, snapshot())
        second = save_transfer_snapshot(db, REF, snapshot())
        assert first["id"] != second["id"]  # fotografías, no eventos únicos
        assert len(list_transfer_snapshots(db)) == 2
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
    vault.lock()
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert b"0.123456789012345678" not in path.read_bytes()
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        saved = read_transfer_snapshot(db, first["id"])
        assert saved["deposits"] == snapshot()["deposits"]
        assert saved["withdrawals"] == snapshot()["withdrawals"]
    vault.lock()


def test_verified_view_uid_is_pinned_without_storing_uid_or_secret(tmp_path):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        unverified = save_transfer_snapshot(db, REF, snapshot())
        assert not unverified["identity_verified"]
        assert not read_transfer_snapshot(db, unverified["id"])["identity_verified"]
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        claim_mexc_uid_in_transaction(db, REF, "123456789")
        db.commit()
        with pytest.raises(PortfolioError, match="differs"):
            save_transfer_snapshot(db, REF, snapshot(), view_uid="987654321")
        verified = save_transfer_snapshot(db, REF, snapshot(), view_uid="123456789")
        assert verified["identity_verified"]
        assert read_transfer_snapshot(db, verified["id"])["identity_verified"]
        assert {row["identity_verified"] for row in list_transfer_snapshots(db)} == {False, True}
        raw = db.connection().exec_driver_sql(
            "SELECT uid_digest FROM mexc_transfer_identity_evidence WHERE snapshot_id=?",
            (verified["id"],),
        ).scalar_one()
        assert len(raw) == 64 and raw != "123456789"
    vault.lock()
    assert b"123456789" not in (tmp_path / "vault.db").read_bytes()


def test_withdrawal_route_is_archived_and_old_snapshot_remains_readable(tmp_path):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        current = snapshot()
        current["withdrawals"][0]["transfer_type"] = 1
        current_id = save_transfer_snapshot(db, REF, current)["id"]
        old_id = save_transfer_snapshot(db, REF, snapshot())["id"]
        assert read_transfer_snapshot(db, current_id)["withdrawals"][0]["transfer_type"] == 1
        assert "transfer_type" not in read_transfer_snapshot(db, old_id)["withdrawals"][0]
        bad = snapshot()
        bad["withdrawals"][0]["transfer_type"] = "0"
        with pytest.raises(PortfolioError, match="route"):
            save_transfer_snapshot(db, REF, bad)
    vault.lock()


def test_private_source_identity_becomes_vault_scoped_reference(tmp_path):
    private_id = "private-on-chain-transaction-id"
    data = snapshot()
    data["deposits"][0]["source_identity"] = private_id
    data["withdrawals"][0]["source_identity"] = "private-mexc-withdrawal-id"
    vault = RealVault(tmp_path / "first.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        first = save_transfer_snapshot(db, REF, data)["id"]
        second = save_transfer_snapshot(db, REF, data)["id"]
        one = read_transfer_snapshot(db, first)
        two = read_transfer_snapshot(db, second)
        deposit_ref = one["deposits"][0]["source_ref"]
        assert len(deposit_ref) == 64 and deposit_ref == two["deposits"][0]["source_ref"]
        assert one["withdrawals"][0]["source_ref"] != deposit_ref
        assert "source_identity" not in str(one)
        stored = db.connection().exec_driver_sql(
            "SELECT payload_json FROM mexc_transfer_snapshots WHERE id=?", (first,)).scalar_one()
        assert private_id not in stored and "private-mexc-withdrawal-id" not in stored
    vault.lock()
    assert private_id.encode() not in (tmp_path / "first.db").read_bytes()
    other = RealVault(tmp_path / "other.db")
    other.unlock(PASSPHRASE, create=True)
    with other.session() as db:
        third = save_transfer_snapshot(db, REF, data)["id"]
        assert read_transfer_snapshot(db, third)["deposits"][0]["source_ref"] != deposit_ref
    other.lock()


def test_snapshot_comparison_uses_only_overlapping_identified_rows(tmp_path):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock(PASSPHRASE, create=True)
    before = snapshot()
    before["deposits"][0]["source_identity"] = "deposit-1"
    before["withdrawals"][0]["source_identity"] = "withdrawal-1"
    before["deposits"].append({**before["deposits"][0], "source_identity": "disappears"})
    before["deposits"].append({**before["deposits"][0], "source_identity": None})
    after = snapshot()
    after["observed_at"] = "2026-10-13T12:01:00+00:00"
    after["deposits"][0].update(source_identity="deposit-1", status=4)
    after["withdrawals"][0]["source_identity"] = "withdrawal-1"
    after["deposits"].append({**after["deposits"][0], "source_identity": "appears"})
    with vault.session() as db:
        old_id = save_transfer_snapshot(db, REF, before)["id"]
        new_id = save_transfer_snapshot(db, REF, after)["id"]
        report = compare_transfer_snapshots(db, old_id, new_id)
        assert report["conclusion"] == "OBSERVATIONAL_NOT_RECONCILED"
        assert report["counts"] == {
            "unchanged": 1, "status_changed": 0, "finality_regression": 1,
            "fields_changed": 0, "not_returned": 1, "newly_returned": 1,
            "ambiguous_reference": 0}
        assert report["unlinked_before"] == 1 and report["unlinked_after"] == 0
        assert {row["outcome"] for row in report["changes"]} == {
            "finality_regression", "not_returned", "newly_returned"}
        assert all(len(row["source_ref"]) == 64 for row in report["changes"])
        assert not report["truncated"]
        with pytest.raises(PortfolioError, match="order"):
            compare_transfer_snapshots(db, new_id, old_id)
        other_ref_id = save_transfer_snapshot(db, "b" * 64, after)["id"]
        with pytest.raises(PortfolioError, match="different local connections"):
            compare_transfer_snapshots(db, old_id, other_ref_id)
        after["start_ms"] = before["end_ms"] + 1
        after["end_ms"] = after["start_ms"] + 1000
        after["deposits"] = []
        after["withdrawals"] = []
        no_overlap = save_transfer_snapshot(db, REF, after)["id"]
        with pytest.raises(PortfolioError, match="overlap"):
            compare_transfer_snapshots(db, old_id, no_overlap)
    vault.lock()


def test_snapshot_comparison_marks_reused_reference_ambiguous(tmp_path):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock(PASSPHRASE, create=True)
    before = snapshot()
    before["deposits"][0]["source_identity"] = "same-chain-tx"
    before["deposits"].append({**before["deposits"][0], "asset": "ETH"})
    after = snapshot()
    after["observed_at"] = "2026-10-13T12:02:00+00:00"
    after["deposits"][0]["source_identity"] = "same-chain-tx"
    with vault.session() as db:
        old_id = save_transfer_snapshot(db, REF, before)["id"]
        new_id = save_transfer_snapshot(db, REF, after)["id"]
        report = compare_transfer_snapshots(db, old_id, new_id)
        assert report["counts"]["ambiguous_reference"] == 1
        assert report["changes"][0]["before"] is None
        assert report["changes"][0]["before_count"] == 2
    vault.lock()


def test_snapshot_comparison_distinguishes_status_and_amount_changes(tmp_path):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock(PASSPHRASE, create=True)
    before = snapshot()
    before["deposits"][0].update(source_identity="deposit-2", status=4)
    before["withdrawals"][0]["source_identity"] = "withdrawal-2"
    after = snapshot()
    after["observed_at"] = "2026-10-13T12:04:00+00:00"
    after["deposits"][0]["source_identity"] = "deposit-2"
    after["withdrawals"][0].update(source_identity="withdrawal-2", amount="101.01")
    with vault.session() as db:
        old_id = save_transfer_snapshot(db, REF, before)["id"]
        new_id = save_transfer_snapshot(db, REF, after)["id"]
        report = compare_transfer_snapshots(db, old_id, new_id)
        assert report["counts"]["status_changed"] == 1
        assert report["counts"]["fields_changed"] == 1
        assert report["counts"]["finality_regression"] == 0
        assert {row["outcome"] for row in report["changes"]} == {
            "status_changed", "fields_changed"}
    vault.lock()


def test_snapshot_comparison_api_is_local_read_only(tmp_path):
    path = tmp_path / "vault.db"
    app = FastAPI()
    app.include_router(create_real_router(path))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": PASSPHRASE}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    observer = RealVault(path)
    observer.unlock(PASSPHRASE)
    before = snapshot()
    before["deposits"][0]["source_identity"] = "api-deposit"
    after = snapshot()
    after["observed_at"] = "2026-10-13T12:03:00+00:00"
    after["deposits"][0].update(source_identity="api-deposit", status=4)
    with observer.session() as db:
        old_id = save_transfer_snapshot(db, REF, before)["id"]
        new_id = save_transfer_snapshot(db, REF, after)["id"]
        event_count = db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one()
    observer.lock()
    url = f"/real/exchanges/mexc/transfers/archive/{old_id}/compare/{new_id}"
    assert client.get(url).status_code == 401
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["counts"]["finality_regression"] == 1
    assert "api-deposit" not in response.text
    assert client.get(url.replace(new_id, old_id), headers=headers).status_code == 422
    check = RealVault(path)
    check.unlock(PASSPHRASE)
    with check.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == event_count
    check.lock()


def test_long_range_snapshot_preserves_request_count_and_many_rows(tmp_path):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock(PASSPHRASE, create=True)
    data = snapshot()
    data["start_ms"] = data["end_ms"] - 30 * 86_400_000
    data["requests"] = 10
    data["deposits"] = [{**data["deposits"][0], "time_ms": data["start_ms"] + i}
                        for i in range(1001)]
    with vault.session() as db:
        identifier = save_transfer_snapshot(db, REF, data)["id"]
        restored = read_transfer_snapshot(db, identifier)
        assert restored["requests"] == 10
        assert len(restored["deposits"]) == 1001
    vault.lock()


@pytest.mark.parametrize("where,value", [
    ("snapshot", {"address": "secret-address"}),
    ("deposit", {"txId": "secret-hash"}),
    ("withdrawal", {"memo": "secret-memo"}),
    ("deposit", {"amount": "NaN"}),
    ("withdrawal", {"time_ms": 1}),
])
def test_transfer_archive_fails_closed_on_extra_or_invalid_fields(tmp_path, where, value):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock(PASSPHRASE, create=True)
    data = snapshot()
    target = data if where == "snapshot" else data["deposits" if where == "deposit" else "withdrawals"][0]
    target.update(value)
    with vault.session() as db:
        with pytest.raises(PortfolioError):
            save_transfer_snapshot(db, REF, data)
        assert db.connection().exec_driver_sql("SELECT count(*) FROM mexc_transfer_snapshots").scalar_one() == 0
    vault.lock()


def test_transfer_snapshot_detects_tampering(tmp_path):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        identifier = save_transfer_snapshot(db, REF, snapshot())["id"]
        db.connection().exec_driver_sql(
            "UPDATE mexc_transfer_snapshots SET payload_json='{}' WHERE id=?", (identifier,))
        db.commit()
        with pytest.raises(PortfolioError, match="integrity"):
            read_transfer_snapshot(db, identifier)
    vault.lock()


def test_transfer_snapshot_survives_encrypted_backup_restore(tmp_path):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        identifier = save_transfer_snapshot(db, REF, snapshot())["id"]
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup = vault.backup(backup_dir / "before.db")
    with vault.session() as db:
        save_transfer_snapshot(db, REF, snapshot())
        assert len(list_transfer_snapshots(db)) == 2
    recovery = vault.restore_backup(backup)
    assert recovery.is_file() and not vault.unlocked
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert len(list_transfer_snapshots(db)) == 1
        assert read_transfer_snapshot(db, identifier)["deposits"][0]["amount"] == "0.123456789012345678"
    vault.lock()


def test_hmac_destination_survives_encrypted_backup_restore_without_raw_address(tmp_path):
    destination = "0x" + "33" * 20
    vault = RealVault(tmp_path / "vault-destination.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        source = snapshot()
        source["deposits"][0].update({"asset": "USDT", "network": "ERC20",
                                      "destination_identity": destination})
        identifier = save_transfer_snapshot(db, REF, source)["id"]
        saved = read_transfer_snapshot(db, identifier)["deposits"][0]
        assert "destination_ref" in saved and destination not in str(saved)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup = vault.backup(backup_dir / "destination-backup.db")
    vault.restore_backup(backup)
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert read_transfer_snapshot(db, identifier)["deposits"][0]["destination_ref"] == saved["destination_ref"]
    vault.lock()
    assert destination.encode("ascii") not in (tmp_path / "vault-destination.db").read_bytes()


def test_destination_identity_rejected_outside_supported_evm_network(tmp_path):
    vault = RealVault(tmp_path / "vault-invalid-destination.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        source = snapshot()
        source["deposits"][0]["destination_identity"] = "0x" + "33" * 20
        with pytest.raises(PortfolioError, match="destination is invalid"):
            save_transfer_snapshot(db, REF, source)
        assert list_transfer_snapshots(db) == []
    vault.lock()


def test_token_contract_rejected_outside_supported_evm_network(tmp_path):
    vault = RealVault(tmp_path / "vault-invalid-token-contract.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        source = snapshot()
        source["deposits"][0]["token_contract"] = "0x" + "44" * 20
        with pytest.raises(PortfolioError, match="token contract is invalid"):
            save_transfer_snapshot(db, REF, source)
        assert list_transfer_snapshots(db) == []
    vault.lock()


def test_v3_to_v4_migration_preserves_cifrado_and_preimage(tmp_path):
    path = tmp_path / "vault.db"
    vault = RealVault(path)
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE mexc_transfer_snapshots")
        db.connection().exec_driver_sql("PRAGMA user_version = 3")
        db.commit()
    vault.lock()
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert save_transfer_snapshot(db, REF, snapshot())["deposit_count"] == 1
    vault.lock()
    backups = list((tmp_path / "backups").glob("pre-migrate-v3-*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes()[:16] != b"SQLite format 3\x00"
    assert RealVault._observation_counts(backups[0], PASSPHRASE) == (0, 0, 0, 0, 0, 0, 0)


def test_v3_migration_replace_failure_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "vault.db"
    vault = RealVault(path)
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE mexc_transfer_snapshots")
        db.connection().exec_driver_sql("PRAGMA user_version = 3")
        db.commit()
    vault.lock()

    def fail_replace(*_):
        raise OSError("test")

    monkeypatch.setattr("app.portfolio.vault.os.replace", fail_replace)
    with pytest.raises(VaultError, match="original file was preserved"):
        vault.unlock(PASSPHRASE)
    assert RealVault._verify_portfolio_file(path, PASSPHRASE, expected_version=3) == (0, 0)
