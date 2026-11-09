"""Archivo GRVT cifrado: integridad, monotonicidad y separación contable."""

from copy import deepcopy

import pytest

from app.portfolio.domain import PortfolioError
from app.portfolio.grvt_evidence import list_evidence, read_evidence, save_evidence
from app.portfolio.models import GrvtOrderEvidence
from app.portfolio.vault import RealVault


def snapshot():
    return {"source": "GRVT_MAINNET_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
            "observed_at": "2026-10-15T12:00:00+00:00", "account_ref": "a" * 64,
            "history_pages_drained": True, "fill_sizes_match_order_state": True,
            "complete_for_execution": False,
            "order": {"sub_account_id": "123", "order_id": "0x123",
                      "client_order_id": "9223372036854775808", "status": "FILLED",
                      "reduce_only": False, "create_time_ns": "1790000000000000000",
                      "update_time_ns": "1790000000000000002",
                      "legs": [{"instrument": "BTC_USDT_Perp", "side": "BUY", "size": "0.3",
                                "traded_size": "0.3", "limit_price": "65000"}]},
            "fills": [{"trade_id": "trade-1", "event_time_ns": "1790000000000000001",
                       "instrument": "BTC_USDT_Perp", "side": "BUY", "role": "TAKER",
                       "size": "0.3", "price": "65000", "fee": "0.12",
                       "builder_fee": "0", "fee_currency": "USDT", "realized_pnl": "1.5"}]}


def test_grvt_evidence_is_encrypted_idempotent_and_not_contable(tmp_path):
    path = tmp_path / "real.db"
    vault = RealVault(path)
    vault.unlock("long test passphrase", create=True)
    evidence = snapshot()
    with vault.session() as db:
        first = save_evidence(db, evidence)
        second = save_evidence(db, evidence)
        assert first["archived"] is True and second == {"id": first["id"], "archived": False,
                                                        "fill_count": 1}
        assert len(list_evidence(db)) == 1
        detail = read_evidence(db, first["id"])
        assert detail["fills"][0]["fee"] == "0.12"
        assert detail["complete_for_execution"] is False
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
    backup_path = vault.backup(tmp_path / "backup.db")
    vault.lock()
    raw = path.read_bytes()
    assert b"trade-1" not in raw and b"BTC_USDT_Perp" not in raw
    vault.unlock("long test passphrase")
    with vault.session() as db:
        assert read_evidence(db, first["id"])["order"]["status"] == "FILLED"
    vault.lock()
    backup = RealVault(backup_path)
    backup.unlock("long test passphrase")
    with backup.session() as db:
        assert read_evidence(db, first["id"])["fills"][0]["trade_id"] == "trade-1"
    backup.lock()


def test_grvt_evidence_rejects_raw_signer_mismatch_and_tamper(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock("long test passphrase", create=True)
    evidence = snapshot()
    with vault.session() as db:
        bad = deepcopy(evidence)
        bad["fills"][0]["signer"] = "0x" + "1" * 40
        with pytest.raises(PortfolioError, match="fill is invalid"):
            save_evidence(db, bad)
        bad = deepcopy(evidence)
        bad["fill_sizes_match_order_state"] = False
        with pytest.raises(PortfolioError, match="size comparison"):
            save_evidence(db, bad)
        bad = deepcopy(evidence)
        bad["order"]["update_time_ns"] = "1800000000000000000"
        with pytest.raises(PortfolioError, match="identity"):
            save_evidence(db, bad)
        identifier = save_evidence(db, evidence)["id"]
        row = db.get(GrvtOrderEvidence, identifier)
        row.payload_json = row.payload_json.replace("65000", "65001")
        db.commit()
        with pytest.raises(PortfolioError, match="integrity"):
            read_evidence(db, identifier)
    vault.lock()


def test_grvt_evidence_rejects_regression_and_dropped_fill(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock("long test passphrase", create=True)
    original = snapshot()
    with vault.session() as db:
        save_evidence(db, original)
        older = deepcopy(original)
        older["observed_at"] = "2026-10-15T11:59:00+00:00"
        older["order"]["update_time_ns"] = "1790000000000000001"
        with pytest.raises(PortfolioError, match="regressed"):
            save_evidence(db, older)
        omitted = deepcopy(original)
        omitted["observed_at"] = "2026-10-15T12:01:00+00:00"
        omitted["fills"] = []
        omitted["fill_sizes_match_order_state"] = False
        with pytest.raises(PortfolioError, match="omitted"):
            save_evidence(db, omitted)
        changed = deepcopy(original)
        changed["observed_at"] = "2026-10-15T12:02:00+00:00"
        changed["order"]["legs"][0]["traded_size"] = "0.2"
        changed["fill_sizes_match_order_state"] = False
        with pytest.raises(PortfolioError, match="verified size match"):
            save_evidence(db, changed)
        assert len(list_evidence(db)) == 1
    vault.lock()
