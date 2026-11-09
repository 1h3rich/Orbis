"""Historial privado entre recibos, sin credenciales ni red reales."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import json

import httpx
import pytest

from app.live_execution.grvt_testnet_continuity import check_testnet_history_continuity
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderAck
from app.live_execution.grvt_testnet_preflight import read_testnet_preflight
from app.live_execution.grvt_testnet_preflight import GrvtTestnetPreflightError
from app.live_execution.grvt_testnet_reconciliation import reconcile_testnet_order
from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskError
from app.live_execution.grvt_testnet_submit import submit_testnet_once
from app.live_execution.grvt_testnet_recovery import read_testnet_recovery
from app.portfolio.models import GrvtTestnetPreflightEvidence
from app.portfolio.vault import VaultError
from tests.test_grvt_testnet_reconciliation import fixture, with_funding
from tests.test_grvt_testnet_journal import NOW, NOW_NS, SUB, CID, observation, order, limits, vault_at
from tests.test_grvt_testnet_fills import fill
from tests.test_grvt_testnet_funding import payment
from tests.test_grvt_testnet_transfers import transfer
from tests.test_grvt_testnet_withdrawals import row as withdrawal
from tests.test_grvt_testnet_deposits import row as deposit
from tests.test_grvt_testnet_interest import row as interest


class HistoryVenue:
    """Frontera SDK falsa; el almacén y la conciliación sí son los reales."""

    def __init__(self):
        self.rows = {}
        self.requests = []
        self.submissions = []
        self.account_event_ns = NOW_NS+10_000_000_000
        self.balance = "9999.75"
        self.position = "0.01"
        self.upnl_reads = []

    def lookup_order(self, sub, client_id):
        assert sub == SUB
        return replace(observation(update_ns=NOW_NS+1000), create_time_ns=str(NOW_NS+1))

    def read_fill_history(self, sub, start, cursor, *, end_time_ns=None):
        self.requests.append(("fills", start, end_time_ns, cursor))
        return {"result":[{**fill(trade_id="fill-1"),"event_time":str(NOW_NS+1000)}],"next":""}

    def page(self, name, sub, start, end, cursor):
        assert sub == SUB
        self.requests.append((name,start,end,cursor))
        return {"result":self.rows.get(name,[]),"next":""}

    def read_funding_history(self,*args): return self.page("funding",*args)
    def read_transfer_history(self,*args): return self.page("transfers",*args)
    def read_withdrawal_history(self,*args): return self.page("withdrawals",*args)
    def read_deposit_history(self,*args): return self.page("deposits",*args)
    def read_interest_history(self,*args): return self.page("interest",*args)
    def matches_main_account_id(self, candidate): return candidate == "0x"+"a"*40

    def read_testnet(self, name, sub):
        assert sub == SUB
        if name == "open_orders": return {"result":[]}
        assert name == "account_summary"
        upnl = self.upnl_reads.pop(0) if self.upnl_reads else "0"
        return {"result":{"sub_account_id":SUB,"event_time":str(self.account_event_ns),
            "settle_currency":"USDT","margin_type":"SIMPLE_CROSS_MARGIN","sub_account_mode":"SINGLE_ASSET_MODE",
            "total_equity":str(Decimal(self.balance)+Decimal(upnl)),"initial_margin":"1" if Decimal(self.position) else "0",
            "maintenance_margin":"0.5" if Decimal(self.position) else "0","unrealized_pnl":upnl,
            "positions":[{"sub_account_id":SUB,"instrument":"BTC_USDT_Perp","size":self.position}] if Decimal(self.position) else [],
            "spot_balances":[{"currency":"USDT","balance":self.balance}]}}

    def submit_signed_limit(self,candidate,policy):
        cid = candidate["metadata"]["client_order_id"]
        self.submissions.append(cid)
        return GrvtTestnetOrderAck(SUB,cid,"0xnext",True)

    @staticmethod
    def public_rules(url,body):
        return httpx.Response(200,json={"result":{"instrument":"BTC_USDT_Perp","max_position_size":"10",
            "risk_brackets":[{"tier":1,"notional_floor":"0","notional_cap":None,"initial_margin_rate":"0.02",
                "maintenance_margin_rate":"0.01","max_leverage":50,"cumulative_maintenance_amount":"0"}]}})


def next_order():
    candidate = order()
    candidate["metadata"]["client_order_id"] = str(int(CID)+1)
    candidate["reduce_only"] = True
    candidate["legs"][0]["is_buying_asset"] = False
    return candidate


def ready(tmp_path):
    vault,audit = fixture(tmp_path)
    audit(2); audit(8)
    reconcile_testnet_order(vault,SUB,observation().client_order_id,now=NOW+timedelta(seconds=9))
    return vault,HistoryVenue()


@pytest.mark.parametrize("section",["funding","transfers","withdrawals","deposits","interest"])
def test_movements_after_receipt_block_even_with_unchanged_account(tmp_path,section):
    vault,venue = ready(tmp_path)
    event = NOW_NS+9_500_000_000
    rows = {
        "funding":[payment(tx_id="paid",event_time=event,amount="1"),
                   payment(tx_id="received",event_time=event,amount="-1")],
        "transfers":[transfer(tx_id="in",event_time=event),
                     transfer(tx_id="out",event_time=event,from_sub=SUB,to_sub="0")],
        "withdrawals":[{**withdrawal("withdrawal-1",owner=SUB),"event_time":str(event)}],
        "deposits":[{**deposit("0x12345678"),"initiated_time":str(event)}],
        "interest":[interest(sub=SUB,event=event,charge=event-1)],
    }
    venue.rows[section] = rows[section]
    with pytest.raises(GrvtTestnetRiskError,match="Historial"):
        check_testnet_history_continuity(vault,venue,SUB,now_ns=NOW_NS+10_000_000_000)


def test_next_preflight_preserves_history_proof_across_vault_reopen(tmp_path):
    vault,venue = ready(tmp_path)
    candidate = next_order()
    submit_testnet_once(vault,venue,candidate,limits(),
        clock_ns=lambda:NOW_NS+10_000_000_000,public_post=venue.public_rules)
    cid = candidate["metadata"]["client_order_id"]
    proof = read_testnet_preflight(vault,SUB,cid)["history_continuity"]
    assert proof["previous_client_order_id"] == CID
    assert proof["history_start_ns"] == NOW_NS
    assert proof["observed_until_ns"] == NOW_NS+10_000_000_000
    assert set(proof["empty_histories"]) == {"funding","transfers","withdrawals","deposits","interest"}
    assert {name for name,_,_,_ in venue.requests} == {"fills",*proof["empty_histories"]}
    assert all(start == str(NOW_NS-1) and end == str(proof["observed_until_ns"])
               for _,start,end,_ in venue.requests)
    assert venue.submissions == [cid]
    vault.lock()
    vault = vault_at(tmp_path / "real.db")
    assert read_testnet_preflight(vault,SUB,cid)["history_continuity"] == proof
    assert read_testnet_recovery(vault,SUB).items[-1].history_continuity == proof


@pytest.mark.parametrize("fault",["receipt","start","until","fills","pages","movement","missing","observed","account_time"])
def test_rehashed_history_proof_is_rejected_on_read(tmp_path,fault):
    vault,venue = ready(tmp_path)
    candidate = next_order()
    submit_testnet_once(vault,venue,candidate,limits(),
        clock_ns=lambda:NOW_NS+10_000_000_000,public_post=venue.public_rules)
    cid = candidate["metadata"]["client_order_id"]
    with vault.session() as db:
        row = db.get(GrvtTestnetPreflightEvidence,cid)
        data = json.loads(row.payload_json)
        proof = data["history_continuity"]
        if fault == "receipt": proof["reconciliation_digest"] = "0"*64
        if fault == "start": proof["history_start_ns"] += 1
        if fault == "until": proof["observed_until_ns"] += 1
        if fault == "fills": proof["fills"]["rows"][0]["price"] = "1"
        if fault == "pages": proof["fills"]["pages_drained"] = True
        if fault == "movement": proof["empty_histories"]["funding"]["rows"] = [{"amount":"0"}]
        if fault == "missing": data["history_continuity"] = None
        if fault == "observed": del data["observed_at_ns"]
        if fault == "account_time": del data["account_event_time_ns"]
        row.payload_json = json.dumps(data,sort_keys=True,separators=(",",":"))
        row.payload_digest = hashlib.sha256(row.payload_json.encode("ascii")).hexdigest()
        db.commit()
    with pytest.raises(GrvtTestnetPreflightError):
        read_testnet_preflight(vault,SUB,cid)


def test_backup_checks_history_semantics_and_preserves_previous_good_copy(tmp_path):
    vault,venue = ready(tmp_path)
    candidate = next_order()
    submit_testnet_once(vault,venue,candidate,limits(),
        clock_ns=lambda:NOW_NS+10_000_000_000,public_post=venue.public_rules)
    directory = tmp_path / "backups"; directory.mkdir()
    saved = vault.backup(directory / "good.db")
    assert vault.verify_managed_backup(saved)["verified"] is True
    with vault.session() as db:
        row = db.get(GrvtTestnetPreflightEvidence,candidate["metadata"]["client_order_id"])
        data = json.loads(row.payload_json)
        data["history_continuity"]["fills"]["rows"][0]["fee"] = "0"
        row.payload_json = json.dumps(data,sort_keys=True,separators=(",",":"))
        row.payload_digest = hashlib.sha256(row.payload_json.encode("ascii")).hexdigest()
        db.commit()
    with pytest.raises(VaultError): vault.backup(directory / "bad.db")
    assert vault.verify_managed_backup(saved)["verified"] is True


def test_account_before_history_cutoff_does_not_consume_new_signal(tmp_path):
    vault,venue = ready(tmp_path)
    venue.account_event_ns -= 1
    with pytest.raises((GrvtTestnetRiskError,GrvtTestnetPreflightError)):
        submit_testnet_once(vault,venue,next_order(),limits(),
            clock_ns=lambda:NOW_NS+10_000_000_000,public_post=venue.public_rules)
    assert venue.submissions == []
    assert [item.client_order_id for item in read_testnet_recovery(vault,SUB).items] == [CID]


@pytest.mark.parametrize("fault",[None,"changed","missing","downgraded"])
def test_funded_parent_history_is_recovered_and_cannot_be_rewritten(tmp_path,fault):
    vault,audit = fixture(tmp_path,reducing=True)
    audit(2,with_funding); audit(8,with_funding)
    reconcile_testnet_order(vault,SUB,CID,now=NOW+timedelta(seconds=9))
    venue = HistoryVenue(); venue.balance = "9999.50"; venue.position = "0"
    venue.rows["funding"] = [{"sub_account_id":SUB,"tx_id":"fill-1","event_time":str(NOW_NS+1000),
        "instrument":"BTC_USDT_Perp","currency":"USDT","amount":"0.25"}]
    candidate = order(); cid = str(int(CID)+1); candidate["metadata"]["client_order_id"] = cid
    submit_testnet_once(vault,venue,candidate,limits(),
        clock_ns=lambda:NOW_NS+10_000_000_000,public_post=venue.public_rules)
    proof = read_testnet_preflight(vault,SUB,cid)["history_continuity"]
    assert proof["schema"] == "orbis.grvt.testnet.history.v2"
    assert proof["funding"]["rows"][0]["amount_paid"] == "0.25"
    if fault is None:
        directory = tmp_path / "backups"; directory.mkdir()
        saved = vault.backup(directory / "funding-history.db")
        assert vault.verify_managed_backup(saved)["verified"] is True
        vault.lock(); vault = vault_at(tmp_path / "real.db")
        assert read_testnet_preflight(vault,SUB,cid)["history_continuity"] == proof
        assert venue.balance == "9999.50"  # El historial no vuelve a descontar el coste.
        return
    with vault.session() as db:
        row = db.get(GrvtTestnetPreflightEvidence,cid); data = json.loads(row.payload_json)
        stored = data["history_continuity"]
        if fault == "changed": stored["funding"]["rows"][0]["amount_paid"] = "0"
        if fault == "missing": stored["funding"]["rows"] = []
        if fault == "downgraded":
            stored["schema"] = "orbis.grvt.testnet.history.v1"
            del stored["funding"]; stored["empty_histories"]["funding"] = {"rows":[],"pages_drained":1}
        row.payload_json = json.dumps(data,sort_keys=True,separators=(",",":"))
        row.payload_digest = hashlib.sha256(row.payload_json.encode("ascii")).hexdigest(); db.commit()
    with pytest.raises(GrvtTestnetPreflightError): read_testnet_preflight(vault,SUB,cid)


def test_reducing_position_survives_market_pnl_change_between_reads(tmp_path):
    vault,venue = ready(tmp_path)
    venue.upnl_reads = ["0.1","0.2"]
    posted = submit_testnet_once(vault,venue,next_order(),limits(),
        clock_ns=lambda:NOW_NS+10_000_000_000,public_post=venue.public_rules)
    assert posted.status == "ACKNOWLEDGED"
    assert venue.submissions == [str(int(CID)+1)]


@pytest.mark.parametrize("changed",["cash","position"])
def test_market_pnl_change_does_not_hide_cash_or_exposure_change(tmp_path,changed):
    vault,_ = ready(tmp_path)
    class ChangingVenue(HistoryVenue):
        read_count = 0
        def read_testnet(self,name,sub):
            if name == "account_summary":
                self.read_count += 1
                if self.read_count == 2:
                    if changed == "cash": self.balance = "9999.76"
                    else: self.position = "0.02"
            return super().read_testnet(name,sub)
    venue = ChangingVenue(); venue.upnl_reads = ["0.1","0.2"]
    with pytest.raises(GrvtTestnetRiskError):
        submit_testnet_once(vault,venue,next_order(),limits(),
            clock_ns=lambda:NOW_NS+10_000_000_000,public_post=venue.public_rules)
    assert venue.submissions == []
    assert [item.client_order_id for item in read_testnet_recovery(vault,SUB).items] == [CID]
