"""Archivo cifrado de observaciones MEXC; nunca altera el libro REAL."""

from copy import deepcopy

from app.portfolio.vault import SCHEMA_VERSION
import pytest
from sqlalchemy import event

from app.portfolio.domain import PortfolioError
from app.portfolio.mexc_observations import (
    archived_window_coverage, list_observations, read_observations, save_observation,
)
from app.portfolio.vault import RealVault


PASSPHRASE = "mexc observation test passphrase"
REF = "a" * 64
OTHER_REF = "b" * 64


def snapshot() -> dict:
    return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
            "coverage": "RECENT_MONTH_SINGLE_PAGE_INCOMPLETE", "symbol": "BTCUSDT",
            "limit": 100, "observed_at": "2026-10-13T09:00:00+00:00", "trades": [
                {"id": "fill-1", "order_id": "order-1", "side": "BUY",
                 "time_ms": 1_760_000_000_000, "price": "65000.10000000",
                 "quantity": "0.000000000000000123", "quote_quantity": "0.0000000079959123",
                 "commission": "0.00000001", "commission_asset": "BNB"}]}


def test_observations_are_exact_encrypted_idempotent_and_persistent(tmp_path):
    path = tmp_path / "real.db"
    vault = RealVault(path)
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        assert save_observation(db, REF, snapshot())["new_fills"] == 1
        repeat = snapshot()
        repeat["trades"][0]["price"] = "65000.1"
        assert save_observation(db, REF, repeat)["new_fills"] == 0
        assert save_observation(db, OTHER_REF, snapshot())["new_fills"] == 1
        assert len(list_observations(db)) == 2
        saved = read_observations(db, REF, "BTCUSDT")
        assert saved["total"] == 1 and saved["truncated"] is False
        assert saved["trades"][0]["price"] == "65000.10000000"
        assert saved["trades"][0]["quantity"] == "0.000000000000000123"
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
    vault.lock()
    raw = path.read_bytes()
    assert raw[:16] != b"SQLite format 3\x00"
    assert b"fill-1" not in raw and b"65000" not in raw
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert read_observations(db, REF, "BTCUSDT")["total"] == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM mexc_observation_batches").scalar_one() == 3
    vault.lock()


def test_changed_fill_aborts_whole_page_without_overwriting_original(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        save_observation(db, REF, snapshot())
        changed = deepcopy(snapshot())
        new_row = deepcopy(changed["trades"][0])
        new_row["id"] = "fill-2"
        changed["trades"] = [new_row, changed["trades"][0]]
        changed["trades"][1]["quantity"] = "0.2"
        with pytest.raises(PortfolioError, match="changed"):
            save_observation(db, REF, changed)
        assert read_observations(db, REF, "BTCUSDT")["total"] == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM mexc_observation_batches").scalar_one() == 1
    vault.lock()


def test_archived_fill_tampering_blocks_read_and_duplicate_rearchive(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        save_observation(db, REF, snapshot())
        db.connection().exec_driver_sql(
            "UPDATE mexc_observed_fills SET quantity='0.5' WHERE trade_id='fill-1'")
        db.commit()
        db.expire_all()
        with pytest.raises(PortfolioError, match="integrity failed"):
            read_observations(db, REF, "BTCUSDT")
        with pytest.raises(PortfolioError, match="integrity failed"):
            save_observation(db, REF, snapshot())
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM mexc_observation_batches").scalar_one() == 1
    vault.lock()


def test_window_coverage_refuses_corrupted_fill_inside_reported_range(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSPHRASE, create=True)
    item = snapshot()
    item.update({"coverage": "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT", "limit": 1000,
                 "start_ms": 1_759_000_000_000, "end_ms": 1_761_000_000_000,
                 "pages": 1})
    with vault.session() as db:
        save_observation(db, REF, item)
        report = archived_window_coverage(db, REF, "BTCUSDT", item["start_ms"], item["end_ms"])
        assert report["fully_covered"] is True and report["verified_fill_count"] == 1
        db.connection().exec_driver_sql(
            "UPDATE mexc_observed_fills SET quantity='9' WHERE trade_id='fill-1'")
        db.commit()
        db.expire_all()
        with pytest.raises(PortfolioError, match="integrity failed"):
            archived_window_coverage(db, REF, "BTCUSDT", item["start_ms"], item["end_ms"])
    vault.lock()


def test_encrypted_backup_and_restore_include_observations(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        save_observation(db, REF, snapshot())
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup = vault.backup(backup_dir / "real-observations.db")
    assert b"fill-1" not in backup.read_bytes()
    changed = deepcopy(snapshot())
    changed["trades"][0]["id"] = "fill-2"
    with vault.session() as db:
        save_observation(db, REF, changed)
        assert read_observations(db, REF, "BTCUSDT")["total"] == 2
    recovery = vault.restore_backup(backup)
    assert recovery.is_file() and not vault.unlocked
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        saved = read_observations(db, REF, "BTCUSDT")
        assert saved["total"] == 1 and saved["trades"][0]["id"] == "fill-1"
    vault.lock()
    previous = RealVault(recovery)
    previous.unlock(PASSPHRASE)
    with previous.session() as db:
        assert read_observations(db, REF, "BTCUSDT")["total"] == 2
    previous.lock()


def test_v2_observations_migrate_on_copy_without_losing_fills(tmp_path):
    path = tmp_path / "real.db"
    vault = RealVault(path)
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        save_observation(db, REF, snapshot())
        connection = db.connection()
        connection.exec_driver_sql("""CREATE TABLE mexc_observation_batches_v2 AS
            SELECT id,account_ref,symbol,observed_at,requested_limit,returned_count,
                   oldest_time_ms,newest_time_ms FROM mexc_observation_batches""")
        connection.exec_driver_sql("DROP TABLE mexc_observation_batches")
        connection.exec_driver_sql("ALTER TABLE mexc_observation_batches_v2 RENAME TO mexc_observation_batches")
        connection.exec_driver_sql("DROP TABLE mexc_transfer_snapshots")
        connection.exec_driver_sql("PRAGMA user_version = 2")
        db.commit()
    vault.lock()

    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert read_observations(db, REF, "BTCUSDT")["total"] == 1
        assert save_observation(db, REF, snapshot())["new_fills"] == 0
    vault.lock()
    backups = list((tmp_path / "backups").glob("pre-migrate-v2-*.db"))
    assert len(backups) == 1 and backups[0].read_bytes()[:16] != b"SQLite format 3\x00"
    assert RealVault._observation_counts(backups[0], PASSPHRASE) == (1, 1, 0, 0, 0, 0, 0)


def test_window_observation_preserves_requested_coverage_without_ledger_effect(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSPHRASE, create=True)
    item = snapshot()
    item.update({"coverage": "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT", "limit": 1000,
                 "start_ms": 1_759_000_000_000, "end_ms": 1_761_000_000_000, "pages": 3})
    with vault.session() as db:
        result = save_observation(db, REF, item)
        assert result["new_fills"] == 1 and result["requests"] == 3
        batch = db.connection().exec_driver_sql("""SELECT requested_start_ms,requested_end_ms,
            request_count,coverage FROM mexc_observation_batches""").one()
        assert tuple(batch) == (item["start_ms"], item["end_ms"], 3, item["coverage"])
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
        item["trades"][0]["time_ms"] = item["start_ms"] - 1
        with pytest.raises(PortfolioError):
            save_observation(db, REF, item)
    vault.lock()


def test_archived_window_coverage_merges_only_complete_windows_for_exact_pair(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        def archive(first, last, *, account_ref=REF, symbol="BTCUSDT"):
            item = snapshot()
            item.update({"symbol": symbol, "trades": [], "limit": 1000,
                         "coverage": "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT",
                         "start_ms": first, "end_ms": last, "pages": 1})
            save_observation(db, account_ref, item)

        # Una página incompleta, incluso vacía, no acredita el tramo que rodea la consulta.
        page = snapshot()
        page["trades"] = []
        save_observation(db, REF, page)
        archive(100, 199)
        archive(200, 250)
        archive(240, 299)
        archive(350, 400)
        archive(300, 349, account_ref=OTHER_REF)
        archive(300, 349, symbol="ETHUSDT")
        btc_index = next(item for item in list_observations(db)
                         if item["account_ref"] == REF and item["symbol"] == "BTCUSDT")
        assert (btc_index["complete_window_start_ms"], btc_index["complete_window_end_ms"]) == (100, 400)
        report = archived_window_coverage(db, REF, "BTCUSDT", 150, 380)
        assert report["complete_window_count"] == 4
        assert report["gaps"] == [{"start_ms": 300, "end_ms": 349}]
        assert report["fully_covered"] is False
        assert report["reconciled"] is False and report["can_submit_real_order"] is False
        assert archived_window_coverage(db, REF, "BTCUSDT", 100, 299)["fully_covered"] is True
        assert archived_window_coverage(db, REF, "BTCUSDT", 50, 99)["gaps"] == [
            {"start_ms": 50, "end_ms": 99}]
        assert archived_window_coverage(db, REF, "SOLUSDT", 100, 299)["complete_window_count"] == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
    vault.lock()


def test_observation_index_uses_fixed_aggregate_queries(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        for account_ref, symbol in ((REF, "BTCUSDT"), (OTHER_REF, "ETHUSDT")):
            for _ in range(12):
                item = snapshot()
                item["symbol"] = symbol
                item["trades"] = []
                save_observation(db, account_ref, item)
        statements = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(vault._engine, "before_cursor_execute", capture)
        try:
            index = list_observations(db)
        finally:
            event.remove(vault._engine, "before_cursor_execute", capture)
        assert len(statements) == 3
        assert {(row["account_ref"], row["symbol"], row["batch_count"], row["fill_count"])
                for row in index} == {(REF, "BTCUSDT", 12, 0), (OTHER_REF, "ETHUSDT", 12, 0)}
        assert all(row["complete_window_start_ms"] is None for row in index)
    vault.lock()


@pytest.mark.parametrize("start,end", [(0, 2), (2, 1), (True, 2), (1, 8_640_000_000_000_001)])
def test_archived_window_coverage_rejects_invalid_range(tmp_path, start, end):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        with pytest.raises(PortfolioError, match="coverage request"):
            archived_window_coverage(db, REF, "BTCUSDT", start, end)
    vault.lock()


@pytest.mark.parametrize("change", [
    ("source", "UNKNOWN"), ("coverage", "COMPLETE"), ("limit", True),
    ("observed_at", "2026-10-13"),
])
def test_invalid_observation_is_not_saved(tmp_path, change):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSPHRASE, create=True)
    item = snapshot()
    item[change[0]] = change[1]
    with vault.session() as db:
        with pytest.raises(PortfolioError):
            save_observation(db, REF, item)
        assert list_observations(db) == []
    vault.lock()


def test_extreme_decimal_exponent_is_rejected_before_formatting(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSPHRASE, create=True)
    item = snapshot()
    item["trades"][0]["quantity"] = "1e-100000"
    with vault.session() as db:
        with pytest.raises(PortfolioError):
            save_observation(db, REF, item)
        assert list_observations(db) == []
    vault.lock()
