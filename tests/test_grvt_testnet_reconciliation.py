"""Conciliación exacta, recibo inmutable y continuidad; nunca asientos REAL."""

from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from uuid import uuid4

from app.portfolio.vault import SCHEMA_VERSION
import pytest
from sqlalchemy import select

from app.live_execution.grvt_testnet_journal import (
    GrvtTestnetJournalError, prepare_testnet_order, claim_testnet_post,
    record_testnet_observation, require_new_testnet_submission, stop_testnet_subaccount)
from app.live_execution.grvt_testnet_reconciliation import (
    GrvtTestnetReconciliationError, reconcile_testnet_order, require_reconciled_continuity)
from app.live_execution.grvt_testnet_recovery import read_testnet_recovery
from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskState
from app.portfolio.models import GrvtTestnetAuditEvidence,GrvtTestnetOrderIntent,GrvtTestnetReconciliation
from tests.test_grvt_testnet_journal import (
    NOW,NOW_NS,CID,SUB,vault_at,order,limits,archive_preflight,observation)


def fixture(tmp_path, *, side="BUY", status="FILLED", traded="0.01", fee="0.25", reducing=False):
    vault = vault_at(tmp_path / "real.db")
    candidate = order()
    candidate["legs"][0]["is_buying_asset"] = side == "BUY"
    candidate["reduce_only"] = reducing
    pre_size = Decimal("-0.01") if side == "BUY" else Decimal("0.01")
    risk_state = (GrvtTestnetRiskState(SUB,"USDT",NOW_NS,NOW_NS,"SIMPLE_CROSS_MARGIN","SINGLE_ASSET_MODE",
        Decimal("10000"),Decimal("10000"),Decimal("1"),Decimal("0.5"),Decimal(0),(("BTC_USDT_Perp",pre_size),),0)
        if reducing else None)
    prepare_testnet_order(vault,candidate,limits(),now=NOW,now_ns=NOW_NS)
    archive_preflight(vault,candidate=candidate,risk_state=risk_state)
    claim_testnet_post(vault,candidate,limits(),now=NOW,now_ns=NOW_NS)
    record_testnet_observation(vault,observation(status=status,side=side,traded=traded,
        update_ns=NOW_NS+1000),now=NOW)
    with vault.session() as db:
        from app.portfolio.models import GrvtTestnetPreflightEvidence
        pre = db.get(GrvtTestnetPreflightEvidence,CID)
        pre_digest = pre.payload_digest
    balance = format(Decimal("10000")-Decimal(fee),"f")
    position = format((pre_size if reducing else Decimal(0))+Decimal(traded)*(1 if side == "BUY" else -1),"f")
    payload = {"schema":"orbis.grvt.testnet.audit.v1",
        "order":{"client_order_id":CID,"sub_account_id":SUB,"order_id":"0x123abc",
                 "instrument":"BTC_USDT_Perp","side":side,"size":"0.01","status":status,
                 "update_time_ns":str(NOW_NS+1000),"traded_size":traded},
        "fills":{"count":1,"rows":[{"trade_id":"fill-1","event_time_ns":NOW_NS+1000,
                    "size":traded,"price":"50000","fee":fee,"fee_currency":"USDT",
                    "realized_pnl":"0","builder_fee":"0","is_taker":True}],
                 "history_start_ns":NOW_NS,"external_rows":[],"external_fill_count":0,
                 "external_activity_detected":False,"sizes_match":True,"pages_drained":1,
                 "gross_notional":format(Decimal(traded)*50000,"f"),"realized_pnl":"0",
                 "builder_fee":"0","fees_by_currency":[["USDT",fee]]},
        "account":{"event_time_ns":NOW_NS,"settle_currency":"USDT","settle_balance":balance,
                   "total_equity":balance,"initial_margin":"1" if Decimal(position) else "0",
                   "maintenance_margin":"0.5" if Decimal(position) else "0",
                   "positions":[["BTC_USDT_Perp",position]] if Decimal(position) else [],"open_order_count":0},
        "account_scope":{"margin_type":"SIMPLE_CROSS_MARGIN","sub_account_mode":"SINGLE_ASSET_MODE",
                         "unrealized_pnl":"0","other_balances":[]},
        "comparison":{"available":True,"preflight_digest":pre_digest,"position_delta_matches":True}}
    for section,count in (("funding","payment_count"),("transfers","transfer_count"),
                          ("withdrawals","withdrawal_count"),("deposits","deposit_count"),("interest","payment_count")):
        payload[section] = {"rows":[],count:0,"history_start_ns":NOW_NS,"observed_until_ns":NOW_NS,
                            "pages_drained":1,"unrelated_row_count":0}

    def audit(seconds, change=None):
        data = json.loads(json.dumps(payload))
        data["account"]["event_time_ns"] = NOW_NS + seconds * 1_000_000_000
        for section in ("funding","transfers","withdrawals","deposits","interest"):
            data[section]["observed_until_ns"] = NOW_NS + seconds * 1_000_000_000
        if change: change(data)
        encoded = json.dumps(data,sort_keys=True,separators=(",",":"))
        with vault.session() as db:
            db.add(GrvtTestnetAuditEvidence(id=str(uuid4()),client_order_id=CID,sub_account_id=SUB,
                observed_at=NOW+timedelta(seconds=seconds),payload_json=encoded,
                payload_digest=hashlib.sha256(encoded.encode("ascii")).hexdigest()))
            db.commit()
    return vault,audit


@pytest.mark.parametrize("side,status,traded,fee", [("BUY","FILLED","0.01","0.25"),
    ("SELL","FILLED","0.01","-0.01"),("BUY","CANCELLED","0.004000000000000001","0.1")])
def test_reconciles_terminal_fills_replay_and_next_order(tmp_path,side,status,traded,fee):
    vault,audit = fixture(tmp_path,side=side,status=status,traded=traded,fee=fee)
    audit(2); audit(8)
    proof = reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=9))
    assert proof["result"]["settle_balance"] == format(Decimal("10000")-Decimal(fee),"f")
    assert proof["result"]["filled_size"] == traded and proof["causal_proof"] is False
    assert not proof["real_book_events_created"]
    require_new_testnet_submission(vault,SUB,str(int(CID)+1))
    assert read_testnet_recovery(vault,SUB).items[0].next_check == "ISOLATED_TESTNET_RECONCILED"
    assert reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(days=1)) == proof
    with vault.session() as db:
        assert len(db.scalars(select(GrvtTestnetReconciliation)).all()) == 1
        require_reconciled_continuity(db,SUB,{"settle_currency":"USDT","positions":proof["result"]["positions"],"settle_balance":proof["result"]["settle_balance"]})
        with pytest.raises(GrvtTestnetReconciliationError):
            require_reconciled_continuity(db,SUB,{"settle_currency":"USDT","positions":[],"settle_balance":"10000"})
    vault.lock()
    vault = vault_at(tmp_path / "real.db")
    assert read_testnet_recovery(vault,SUB).items[0].reconciliation == proof


@pytest.mark.parametrize("fault", ["one","too_soon","old","balance","position","external","funding", "coverage",
    "builder","foreign_fee","scope","collateral","equity","pnl_on_open","changed_fill","cancel","limit"])
def test_discrepancy_preserves_pending_without_receipt(tmp_path,fault):
    vault,audit = fixture(tmp_path)
    audit(2)
    def change(data):
        if fault == "balance": data["account"]["settle_balance"] = "9999"
        if fault == "position": data["account"]["positions"][0][1] = "0.02"
        if fault == "external": data["fills"]["external_rows"] = ["external"]
        if fault == "funding": data["funding"]["rows"] = ["payment"]
        if fault == "coverage": data["funding"]["observed_until_ns"] -= 10_000_000_000
        if fault == "builder": data["fills"]["builder_fee"] = "1"
        if fault == "foreign_fee": data["fills"]["fees_by_currency"] = [["BTC","0.25"]]
        if fault == "scope": del data["account_scope"]
        if fault == "collateral": data["account_scope"]["other_balances"] = [["ETH","1"]]
        if fault == "equity": data["account"]["total_equity"] = "10001"
        if fault == "pnl_on_open": data["fills"]["rows"][0]["realized_pnl"] = "1"
        if fault == "changed_fill": data["fills"]["rows"][0]["size"] = "0.02"
        if fault == "limit": data["fills"]["rows"][0]["price"] = "50001"
    if fault != "one": audit(3 if fault == "too_soon" else 8,change)
    if fault == "cancel":
        from app.portfolio.models import GrvtTestnetCancelAttempt
        with vault.session() as db:
            db.add(GrvtTestnetCancelAttempt(client_order_id=CID,sub_account_id=SUB,status="CLAIMED",claimed_at=NOW,updated_at=NOW)); db.commit()
    with pytest.raises(GrvtTestnetReconciliationError):
        reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=50 if fault == "old" else 9))
    with vault.session() as db:
        assert db.get(GrvtTestnetOrderIntent,CID).status == "OBSERVED"
        assert db.get(GrvtTestnetReconciliation,CID) is None
    with pytest.raises(GrvtTestnetJournalError): require_new_testnet_submission(vault,SUB,str(int(CID)+1))


def test_receipt_tampering_blocks_next_submission_even_rehashed(tmp_path):
    vault,audit = fixture(tmp_path); audit(2); audit(8)
    reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=9))
    with vault.session() as db:
        receipt = db.get(GrvtTestnetReconciliation,CID)
        data = json.loads(receipt.payload_json); data["result"]["settle_balance"] = "999999"
        receipt.payload_json = json.dumps(data,sort_keys=True,separators=(",",":"))
        receipt.payload_digest = hashlib.sha256(receipt.payload_json.encode("ascii")).hexdigest(); db.commit()
    with pytest.raises(GrvtTestnetJournalError): require_new_testnet_submission(vault,SUB,str(int(CID)+1))


def test_reconciliation_does_not_remove_stop(tmp_path):
    vault,audit = fixture(tmp_path); audit(2); audit(8)
    stop_testnet_subaccount(vault,SUB,now=NOW+timedelta(seconds=9))
    reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=9))
    assert read_testnet_recovery(vault,SUB).stopped
    with pytest.raises(GrvtTestnetJournalError,match="detenida"):
        require_new_testnet_submission(vault,SUB,str(int(CID)+1))


def test_concurrent_reconciliation_records_one_receipt(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    vault,audit = fixture(tmp_path); audit(2); audit(8)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _:reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=9)),range(4)))
    assert all(result == results[0] for result in results)
    with vault.session() as db:
        assert len(db.scalars(select(GrvtTestnetReconciliation)).all()) == 1


def test_v29_migration_preserves_legacy_evidence_without_certifying_it(tmp_path):
    from app.portfolio.models import GrvtTestnetPreflightEvidence
    vault,audit = fixture(tmp_path); audit(2); audit(8)
    with vault.session() as db:
        pre = db.get(GrvtTestnetPreflightEvidence,CID)
        data = json.loads(pre.payload_json)
        del data["state"]["margin_type"]; del data["state"]["sub_account_mode"]
        pre.payload_json = json.dumps(data,sort_keys=True,separators=(",",":"))
        pre.payload_digest = hashlib.sha256(pre.payload_json.encode("ascii")).hexdigest()
        for row in db.scalars(select(GrvtTestnetAuditEvidence)).all():
            data = json.loads(row.payload_json); del data["account_scope"]
            data["comparison"]["preflight_digest"] = pre.payload_digest
            row.payload_json = json.dumps(data,sort_keys=True,separators=(",",":"))
            row.payload_digest = hashlib.sha256(row.payload_json.encode("ascii")).hexdigest()
        db.commit()
        db.connection().exec_driver_sql("DROP TABLE grvt_testnet_reconciliations")
        db.connection().exec_driver_sql("PRAGMA user_version=29"); db.commit()
    vault.lock()
    vault = vault_at(tmp_path / "real.db")
    assert vault.last_migration_backup is not None and vault.last_migration_backup.exists()
    assert vault.last_migration_backup.read_bytes()[:16] != b"SQLite format 3\x00"
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert len(db.scalars(select(GrvtTestnetAuditEvidence)).all()) == 2
    report = read_testnet_recovery(vault,SUB)
    assert report.items[0].journal_status == "OBSERVED" and not report.items[0].can_reconcile
    with pytest.raises(GrvtTestnetReconciliationError):
        reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=9))


def test_receipt_survives_verified_encrypted_backup_and_restore(tmp_path):
    from app.portfolio.repository import create_account
    from app.portfolio.models import RealAccount
    vault,audit = fixture(tmp_path); audit(2); audit(8)
    proof = reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=9))
    directory = tmp_path / "backups"; directory.mkdir()
    saved = vault.backup(directory / "reconciled.db")
    assert vault.verify_managed_backup(saved)["verified"] is True
    with vault.session() as db: create_account(db,"Cuenta posterior")
    previous = vault.restore_backup(saved)
    assert previous.exists() and not vault.unlocked
    vault.unlock("long test passphrase")
    assert read_testnet_recovery(vault,SUB).items[0].reconciliation == proof
    with vault.session() as db: assert db.scalars(select(RealAccount)).all() == []


def with_funding(data, amount="0.25"):
    data["funding"].update({"payment_count":1,"paid_by_currency":[["USDT",amount]],
        "rows":[{"tx_id":"fill-1","event_time_ns":NOW_NS+1000,"instrument":"BTC_USDT_Perp",
                 "currency":"USDT","amount_paid":amount}]})
    balance = format(Decimal("9999.75")-Decimal(amount),"f")
    data["account"]["settle_balance"] = balance
    data["account"]["total_equity"] = balance


@pytest.mark.parametrize("side,paid,traded",[("BUY","0.25","0.01"),("SELL","-0.10","0.01"),
    ("BUY","0.250000000000000001","0.004"),("SELL","0","0.01")])
def test_owned_reduction_funding_is_exact_replayable_and_survives_backup(tmp_path,side,paid,traded):
    vault,audit = fixture(tmp_path,side=side,reducing=True,traded=traded,
                          status="FILLED" if traded == "0.01" else "CANCELLED")
    audit(2,lambda data:with_funding(data,paid)); audit(8,lambda data:with_funding(data,paid))
    stopped = side == "BUY" and paid == "0.25"
    if stopped: stop_testnet_subaccount(vault,SUB,now=NOW+timedelta(seconds=9))
    proof = reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=9))
    assert proof["schema"] == "orbis.grvt.testnet.reconciliation.v2"
    assert proof["result"]["funding_paid_usdt"] == paid
    assert Decimal(proof["result"]["net_realized_pnl_usdt"]) == -Decimal("0.25")-Decimal(paid)
    assert proof["result"]["settle_balance"] == format(Decimal("9999.75")-Decimal(paid),"f")
    assert not proof["causal_proof"] and not proof["real_book_events_created"]
    assert reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(days=1)) == proof
    directory = tmp_path / "backups"; directory.mkdir()
    saved = vault.backup(directory / "funding.db")
    assert vault.verify_managed_backup(saved)["verified"] is True
    vault.lock(); vault = vault_at(tmp_path / "real.db")
    assert read_testnet_recovery(vault,SUB).items[0].reconciliation == proof
    if stopped:
        assert read_testnet_recovery(vault,SUB).stopped
        with pytest.raises(GrvtTestnetJournalError,match="detenida"):
            require_new_testnet_submission(vault,SUB,str(int(CID)+1))


@pytest.mark.parametrize("fault",["foreign_tx","foreign_instrument","foreign_currency","before_fill",
    "after_account","duplicate","changed","disappeared","balance","equity","open_position","net_zero_external"])
def test_unattributed_funding_does_not_release_pending_order(tmp_path,fault):
    vault,audit = fixture(tmp_path,reducing=fault != "open_position")
    def modify(data):
        with_funding(data)
        row = data["funding"]["rows"][0]
        if fault == "foreign_tx": row["tx_id"] = "external"
        if fault == "foreign_instrument": row["instrument"] = "ETH_USDT_Perp"
        if fault == "foreign_currency": row["currency"] = "USDC"
        if fault == "before_fill": row["event_time_ns"] -= 1
        if fault == "after_account": row["event_time_ns"] = NOW_NS+9_000_000_000
        if fault == "duplicate": data["funding"]["rows"].append(dict(row)); data["funding"]["payment_count"] = 2
        if fault == "balance": data["account"]["settle_balance"] = "9999.75"
        if fault == "equity": data["account"]["total_equity"] = "9999.75"
        if fault == "net_zero_external":
            data["funding"]["rows"].append({**row,"tx_id":"external","amount_paid":"-0.25"})
            data["funding"]["payment_count"] = 2
            data["funding"]["paid_by_currency"] = [["USDT","0"]]
            data["account"]["settle_balance"] = data["account"]["total_equity"] = "9999.75"
    audit(2,modify)
    if fault == "changed": audit(8,lambda data:with_funding(data,"0.10"))
    elif fault == "disappeared": audit(8); audit(14)
    else: audit(8,modify)
    with pytest.raises(GrvtTestnetReconciliationError):
        reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=15 if fault == "disappeared" else 9))
    with pytest.raises(GrvtTestnetJournalError): require_new_testnet_submission(vault,SUB,str(int(CID)+1))


def test_recovery_explains_funding_without_claiming_real_execution(tmp_path):
    vault,audit = fixture(tmp_path,reducing=True)
    audit(2,with_funding); audit(8,with_funding)
    observed = read_testnet_recovery(vault,SUB).items[0]
    assert observed.funding_summary["paid_usdt"] == "0.25"
    assert observed.funding_summary["linked_to_own_fills"] is True
    assert not observed.can_reconcile  # El reloj sintético ya no es fresco.
    reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=9))
    report = read_testnet_recovery(vault,SUB)
    assert not report.can_submit
    assert report.items[0].funding_summary == observed.funding_summary
    assert "FUNDING_ACTIVITY" not in report.items[0].current_audit_findings


def test_zero_payment_linked_to_own_opening_is_preserved_not_mistaken_for_debt(tmp_path):
    vault,audit = fixture(tmp_path)
    audit(2,lambda data:with_funding(data,"0")); audit(8,lambda data:with_funding(data,"0"))
    proof = reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=9))
    assert proof["result"]["funding_paid_usdt"] == "0"
    assert proof["result"]["settle_balance"] == "9999.75"
