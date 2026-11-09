"""OCO nativo, diario SQLCipher y replays; nunca credenciales del usuario."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
import pytest
import httpx
from types import SimpleNamespace
from copy import deepcopy
import json
import hashlib
from app.live_execution.grvt_testnet_order import (
    AUTH_URL, ORDER_URL, LOOKUP_URL, GrvtTestnetOrderAck, GrvtTestnetOrderClient, GrvtTestnetOrderError,
)

from app.live_execution.grvt_testnet_protection import GrvtTestnetTrigger, sign_testnet_protection
from app.live_execution.grvt_testnet_reconciliation import reconcile_testnet_order
from app.live_execution.grvt_testnet_oco import oco_client_ids, prepare_oco, read_oco
from tests.test_grvt_testnet_protection import draft, KEY, SIGNER, INSTRUMENT
from tests.test_grvt_testnet_reconciliation import fixture
from tests.test_grvt_testnet_journal import NOW, NOW_NS, SUB, CID, limits, vault_at
from app.live_execution.grvt_testnet_journal import GrvtTestnetJournalError, require_new_testnet_submission


STAMP = NOW_NS + 10_000_000_000


def orders_for(group_id, side="SELL"):
    ids = oco_client_ids(group_id)
    orders = []
    levels = (("STOP_LOSS", "49000", "48877.5"), ("TAKE_PROFIT", "53000", "52867.5")) if side == "SELL" else (
        ("STOP_LOSS", "51000", "51127.5"), ("TAKE_PROFIT", "47000", "47117.5"))
    for i, (kind, price, limit) in enumerate(levels):
        candidate = replace(draft(), client_order_id=ids[i], nonce=42+i,
            side=side,limit_price=Decimal(limit), expiration_ns=STAMP+3_600_000_000_000)
        orders.append(sign_testnet_protection(candidate, limits(), INSTRUMENT,
            GrvtTestnetTrigger(kind, Decimal(price)), dedicated_private_key=KEY,
            expected_signer=SIGNER, now_ns=STAMP))
    return orders


def ready(tmp_path, side="BUY"):
    vault, audit = fixture(tmp_path,side=side)
    audit(2); audit(8)
    reconcile_testnet_order(vault, SUB, CID, now=NOW+timedelta(seconds=9))
    return vault


def test_prepared_oco_survives_unlock_and_keeps_only_terms_not_full_signatures(tmp_path):
    vault = ready(tmp_path)
    group_id = str(uuid4())
    orders = orders_for(group_id)
    result = prepare_oco(vault, group_id, CID, orders, limits(), INSTRUMENT, now_ns=STAMP)
    assert result["status"] == "PREPARED" and result["parent_client_order_id"] == CID
    assert result["size"] == "0.01" and result["side"] == "SELL"
    assert [leg["role"] for leg in result["legs"]] == ["STOP_LOSS", "TAKE_PROFIT"]
    assert result["can_submit"] is False and result["protection_guaranteed"] is False
    assert all(order["signature"]["r"] not in str(result) for order in orders)
    assert SIGNER not in str(result) and KEY.hex() not in str(result)
    vault.lock()
    reopened = vault_at(tmp_path / "real.db")
    assert read_oco(reopened, group_id) == result
    assert prepare_oco(reopened, group_id, CID, orders, limits(), INSTRUMENT, now_ns=STAMP) == result


def test_pending_protection_blocks_old_ioc_path_even_before_login(tmp_path):
    vault = ready(tmp_path)
    group_id = str(uuid4())
    prepare_oco(vault, group_id, CID, orders_for(group_id), limits(), INSTRUMENT, now_ns=STAMP)
    with pytest.raises(GrvtTestnetJournalError, match="OCO"):
        require_new_testnet_submission(vault, SUB, str(int(CID)+1))


def test_claim_is_durable_and_cannot_be_reclaimed_or_abandoned_after_restart(tmp_path):
    from app.live_execution.grvt_testnet_oco import claim_oco, abandon_oco, GrvtTestnetOcoError
    vault = ready(tmp_path)
    group_id = str(uuid4())
    orders = orders_for(group_id)
    prepare_oco(vault, group_id, CID, orders, limits(), INSTRUMENT, now_ns=STAMP)
    result = claim_oco(vault, group_id, orders, limits(), now_ns=STAMP+1000)
    assert result["status"] == "POST_CLAIMED" and result["can_submit"] is False
    vault.lock()
    reopened = vault_at(tmp_path / "real.db")
    assert read_oco(reopened, group_id)["status"] == "POST_CLAIMED"
    with pytest.raises(GrvtTestnetOcoError):
        claim_oco(reopened, group_id, orders, limits(), now_ns=STAMP+2000)
    with pytest.raises(GrvtTestnetOcoError):
        abandon_oco(reopened, group_id, now_ns=STAMP+2000)


def test_abandoned_group_releases_scope_but_never_reuses_its_child_ids(tmp_path):
    from app.live_execution.grvt_testnet_oco import abandon_oco
    vault = ready(tmp_path)
    group_id = str(uuid4())
    orders = orders_for(group_id)
    prepare_oco(vault, group_id, CID, orders, limits(), INSTRUMENT, now_ns=STAMP)
    assert abandon_oco(vault, group_id, now_ns=STAMP+1000)["status"] == "ABANDONED"
    require_new_testnet_submission(vault, SUB, str(int(CID)+1))
    for child_id in oco_client_ids(group_id):
        with pytest.raises(GrvtTestnetJournalError, match="OCO"):
            require_new_testnet_submission(vault, SUB, child_id)


def test_two_protections_use_one_native_bulk_post_and_one_shared_transmission():
    from app.live_execution.grvt_testnet_order import BULK_ORDERS_URL
    group_id = str(uuid4())
    orders = orders_for(group_id)
    calls = []
    def post(url, body, headers):
        calls.append((url,body))
        if url == AUTH_URL:
            return httpx.Response(200,json={"status":"success","sub_account_id":SUB},
                headers={"X-Grvt-Account-Id":"0x"+"a"*40,"Set-Cookie":"gravity=fake-testnet-cookie; Path=/"},
                request=httpx.Request("POST",url))
        assert url == BULK_ORDERS_URL == "https://trades.testnet.grvt.io/full/v2/bulk_orders"
        assert body == {"sub_account_id":SUB,"orders":orders,"order_i_ds":[],"client_order_i_ds":[]}
        return httpx.Response(200,json={"result":{"orders":[
            {**order,"order_id":"0xoco"+str(i)} for i,order in enumerate(orders)],"cancel_acks":[]}})
    client = GrvtTestnetOrderClient("fake-testnet-key",http_post=post,clock_ns=lambda:STAMP)
    acks = client.submit_signed_oco(orders,limits())
    assert tuple(ack.client_order_id for ack in acks) == oco_client_ids(group_id)
    assert all(ack.accepted_by_testnet and ack.execution_unverified for ack in acks)
    assert [url for url,_ in calls] == [AUTH_URL,BULK_ORDERS_URL]
    with pytest.raises(GrvtTestnetOrderError,match="consumida"):
        client.submit_signed_oco(orders,limits())
    assert ORDER_URL not in [url for url,_ in calls]


def test_joint_ack_survives_restart_without_becoming_execution_or_new_permission(tmp_path):
    from app.live_execution.grvt_testnet_oco import claim_oco, record_oco_ack, GrvtTestnetOcoError
    vault = ready(tmp_path)
    group_id = str(uuid4())
    orders = orders_for(group_id)
    prepare_oco(vault, group_id, CID, orders, limits(), INSTRUMENT, now_ns=STAMP)
    claim_oco(vault, group_id, orders, limits(), now_ns=STAMP+1000)
    acks = tuple(GrvtTestnetOrderAck(SUB,cid,"0xoco"+str(i),True)
                 for i,cid in enumerate(oco_client_ids(group_id)))
    result = record_oco_ack(vault,group_id,acks,now_ns=STAMP+2000)
    assert result["status"] == "ACKNOWLEDGED" and result["can_submit"] is False
    assert [row["order_id"] for row in result["acknowledged_orders"]] == ["0xoco0","0xoco1"]
    vault.lock()
    reopened = vault_at(tmp_path / "real.db")
    assert read_oco(reopened,group_id) == result
    assert record_oco_ack(reopened,group_id,acks,now_ns=STAMP+3000) == result
    with pytest.raises(GrvtTestnetJournalError,match="OCO"):
        require_new_testnet_submission(reopened,SUB,str(int(CID)+1))
    with pytest.raises(GrvtTestnetOcoError):
        record_oco_ack(reopened,group_id,(acks[0],replace(acks[1],order_id="0xdifferent")),now_ns=STAMP+3000)


@pytest.mark.parametrize("advancing", [False, True])
@pytest.mark.parametrize("position_format", ["0.01", "0.0100"])
@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_native_sender_reads_real_risk_and_history_then_commits_claim_before_bulk(tmp_path,advancing,position_format,direction):
    case = sender_case(tmp_path,advancing=advancing,position_format=position_format,direction=direction)
    result = case.send()
    assert result["status"] == "ACKNOWLEDGED" and result["preflight_available"] is True
    assert case.calls == [AUTH_URL,case.bulk_url]
    assert len(case.venue.requests) == 6
    assert read_oco(case.second,case.group_id) == result


def sender_case(tmp_path, *, advancing=False, position_format="0.01",direction="LONG"):
    from app.live_execution.grvt_testnet_oco_submit import submit_oco_once
    from tests.test_grvt_testnet_history_continuity import HistoryVenue
    from app.live_execution.grvt_testnet_order import BULK_ORDERS_URL
    from app.live_execution.grvt_testnet_signer import INSTRUMENT_URL
    from app.live_execution.grvt_testnet_quote import TESTNET_TICKER_URL
    vault = ready(tmp_path,side="BUY" if direction == "LONG" else "SELL")
    group_id = str(uuid4())
    orders = orders_for(group_id,side="SELL" if direction == "LONG" else "BUY")
    prepare_oco(vault,group_id,CID,orders,limits(),INSTRUMENT,now_ns=STAMP)
    second = vault_at(tmp_path / "real.db")
    clock = [STAMP]
    calls = []
    settings = {}
    def post(url,body,headers):
        calls.append(url)
        if url == AUTH_URL:
            return httpx.Response(200,json={"status":"success","sub_account_id":SUB},
                headers={"X-Grvt-Account-Id":"0x"+"a"*40,"Set-Cookie":"gravity=fake-testnet-cookie; Path=/"},
                request=httpx.Request("POST",url))
        if url == LOOKUP_URL:
            order = next(order for order in settings["accepted_orders"]
                         if order["metadata"]["client_order_id"] == body["client_order_id"])
            row = deepcopy(order)
            i = settings["accepted_orders"].index(order)
            row["order_id"] = "0xoco"+str(i)
            row["metadata"]["create_time"] = str(settings["accepted_at_ns"])
            state = settings.get("states",{}).get(i,("OPEN","0"))
            row["state"] = {"status":state[0],"traded_size":[state[1]],"update_time":str(clock[0])}
            if settings.get("normalised_values"): normalise_venue_values(row)
            if settings.get("bad_trigger"): row["metadata"]["trigger"]["tpsl"]["trigger_price"] = "49000.1"
            return httpx.Response(200,json={"result":row})
        assert url == BULK_ORDERS_URL
        # Otra conexión observa el claim ya confirmado antes de abrir la red.
        assert read_oco(second,group_id)["status"] == "POST_CLAIMED"
        settings["accepted_orders"] = deepcopy(body["orders"])
        settings["accepted_at_ns"] = clock[0]
        if settings.get("lost_reply"):
            raise httpx.ReadTimeout("untrusted secret-like external error")
        rows = [{**order,"order_id":"0xoco"+str(i)} for i,order in enumerate(body["orders"])]
        if settings.get("normalised_values"):
            rows = deepcopy(rows)
            for row in rows: normalise_venue_values(row)
        if settings.get("partial_ack"): rows = rows[:1]
        clock[0] += settings.get("reply_delay_ns",0)
        return httpx.Response(200,json={"result":{"orders":rows,"cancel_acks":[]}})
    writer = GrvtTestnetOrderClient("fake-testnet-key",http_post=post,clock_ns=lambda:clock[0])
    class Venue(HistoryVenue):
        def submit_signed_oco(self,orders,policy): return writer.submit_signed_oco(orders,policy)
        def lookup_order(self,*args):
            return replace(super().lookup_order(*args),side="BUY" if direction == "LONG" else "SELL")
        def read_fill_history(self,*args,**kwargs):
            data = super().read_fill_history(*args,**kwargs)
            for row in data["result"]: row["is_buyer"] = direction == "LONG"
            return data
        def read_testnet(self,name,sub):
            self.account_event_ns = clock[0]
            result = super().read_testnet(name,sub)
            if advancing and name == "account_summary": clock[0] += 100_000_000
            return result
    def public(url,body):
        if url == INSTRUMENT_URL:
            return httpx.Response(200,json={"result":{"instrument":"BTC_USDT_Perp","kind":"PERPETUAL",
                "venues":["ORDERBOOK"],"instrument_hash":"123456789","base_decimals":3,
                "tick_size":"0.1","min_size":"0.001","min_notional":"10"}})
        if url == TESTNET_TICKER_URL:
            return httpx.Response(200,json={"result":{"instrument":"BTC_USDT_Perp","event_time":str(clock[0]),
                "best_bid_price":"49999","best_ask_price":"50001","best_bid_size":"10","best_ask_size":"10",
                "last_price":"50000"}})
        return HistoryVenue.public_rules(url,body)
    venue = Venue()
    venue.position = position_format if direction == "LONG" else "-"+position_format
    case = SimpleNamespace(vault=vault,second=second,group_id=group_id,orders=orders,venue=venue,
        calls=calls,clock=clock,public=public,bulk_url=BULK_ORDERS_URL,settings=settings,writer=writer,
        http_post=post)
    case.send = lambda:submit_oco_once(vault,venue,group_id,orders,limits(),clock_ns=lambda:clock[0],public_post=case.public)
    return case


def normalise_venue_values(row):
    for leg in row["legs"]:
        for field in ("size","limit_price"): leg[field] = format(Decimal(leg[field]),".9f")
    tpsl = row["metadata"]["trigger"]["tpsl"]
    tpsl["trigger_price"] = format(Decimal(tpsl["trigger_price"]),".9f")
    row["signature"]["signer"] = row["signature"]["signer"].lower()


@pytest.mark.parametrize("fault", ["balance","size","flat","trigger_met","missing_last","spread","stale_ticker","instrument_changed","funding","stop","expired"])
def test_invalid_market_account_or_stop_never_consumes_claim_or_posts_oco(tmp_path,fault):
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskError
    from app.live_execution.grvt_testnet_quote import GrvtTestnetQuoteError
    from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
    from app.live_execution.grvt_testnet_quote import TESTNET_TICKER_URL
    from app.live_execution.grvt_testnet_signer import INSTRUMENT_URL
    case = sender_case(tmp_path)
    if fault == "balance": case.venue.balance = "9998"
    if fault == "size": case.venue.position = "0.02"
    if fault == "flat": case.venue.position = "0"
    if fault == "expired": case.clock[0] += 3_601_000_000_000
    if fault == "stop": stop_testnet_subaccount(case.vault,SUB,now=NOW+timedelta(seconds=10))
    if fault == "funding":
        from tests.test_grvt_testnet_funding import payment
        case.venue.rows["funding"] = [payment(tx_id="external-payment",event_time=STAMP,amount="1")]
    original = case.public
    def public(url,body):
        response = original(url,body)
        data = response.json()
        if url == TESTNET_TICKER_URL:
            if fault == "trigger_met": data["result"]["last_price"] = "49000"
            if fault == "missing_last": data["result"].pop("last_price")
            if fault == "spread": data["result"].update(best_bid_price="49000",best_ask_price="51000")
            if fault == "stale_ticker": data["result"]["event_time"] = str(STAMP-6_000_000_000)
        if url == INSTRUMENT_URL and fault == "instrument_changed": data["result"]["tick_size"] = "0.2"
        return httpx.Response(200,json=data)
    case.public = public
    with pytest.raises((GrvtTestnetOcoError,GrvtTestnetRiskError,GrvtTestnetQuoteError)):
        case.send()
    assert read_oco(case.vault,case.group_id)["status"] == "PREPARED"
    assert case.bulk_url not in case.calls


@pytest.mark.parametrize("fault", ["lost_reply","partial_ack"])
def test_uncertain_bulk_remains_claimed_after_restart_and_cannot_be_resent(tmp_path,fault):
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    from app.live_execution.grvt_testnet_oco_submit import submit_oco_once
    case = sender_case(tmp_path)
    case.settings[fault] = True
    with pytest.raises(GrvtTestnetOrderError,match="desconocido") as raised:
        case.send()
    assert "secret-like" not in str(raised.value)
    assert len(case.settings["accepted_orders"]) == 2
    assert case.calls.count(case.bulk_url) == 1
    case.vault.lock(); case.second.lock()
    reopened = vault_at(tmp_path / "real.db")
    assert read_oco(reopened,case.group_id)["status"] == "POST_CLAIMED"
    with pytest.raises(GrvtTestnetOcoError,match="consumido"):
        submit_oco_once(reopened,case.venue,case.group_id,case.orders,limits(),
            clock_ns=lambda:STAMP,public_post=case.public)
    assert case.calls.count(case.bulk_url) == 1
    with pytest.raises(GrvtTestnetJournalError,match="OCO"):
        require_new_testnet_submission(reopened,SUB,str(int(CID)+1))


def test_moving_unrealized_pnl_does_not_invalidate_an_otherwise_exact_reduction(tmp_path):
    case = sender_case(tmp_path,advancing=True)
    case.venue.upnl_reads = ["10.25","-10"]
    assert case.send()["status"] == "ACKNOWLEDGED"


def test_v33_migration_preserves_reconciled_entry_before_adding_oco_diary(tmp_path):
    vault = ready(tmp_path)
    with vault.session() as db:
        for table in ("grvt_testnet_oco_reconciliations","grvt_testnet_oco_audits","grvt_testnet_oco_cancels","grvt_testnet_oco_observations","grvt_testnet_oco_claims","grvt_testnet_oco_legs","grvt_testnet_oco_groups"):
            db.connection().exec_driver_sql("DROP TABLE "+table)
        db.connection().exec_driver_sql("PRAGMA user_version=33"); db.commit()
    vault.lock(); vault = vault_at(tmp_path / "real.db")
    assert vault.last_migration_backup and vault.last_migration_backup.exists()
    from app.live_execution.grvt_testnet_recovery import read_testnet_recovery
    assert read_testnet_recovery(vault,SUB).items[0].journal_status == "RECONCILED"
    group_id = str(uuid4())
    prepare_oco(vault,group_id,CID,orders_for(group_id),limits(),INSTRUMENT,now_ns=STAMP)
    vault.backup(tmp_path / "valid-current.db")
    vault.lock()


@pytest.mark.parametrize("field", ["size","side","parent_receipt_digest","role","nonce","trigger","limit","mapping","claim_status"])
def test_tampered_terms_fail_reads_and_backup_even_if_outer_hash_is_recomputed(tmp_path,field):
    from app.portfolio.models import GrvtTestnetOcoGroup, GrvtTestnetOcoLeg
    from app.portfolio.vault import VaultError
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError, claim_oco
    vault = ready(tmp_path)
    group_id = str(uuid4()); orders = orders_for(group_id)
    prepare_oco(vault,group_id,CID,orders,limits(),INSTRUMENT,now_ns=STAMP)
    if field == "claim_status": claim_oco(vault,group_id,orders,limits(),now_ns=STAMP+1000)
    with vault.session() as db:
        row = db.get(GrvtTestnetOcoGroup,group_id); p = json.loads(row.payload_json)
        if field == "size": p["size"] = "0.02"
        if field == "side": p["side"] = "BUY"
        if field == "parent_receipt_digest": p[field] = "f"*64
        if field == "role": p["legs"][0]["role"] = "TAKE_PROFIT"
        if field == "nonce": p["legs"][1]["nonce"] = p["legs"][0]["nonce"]
        if field == "trigger": p["legs"][0]["trigger_price"] = "60000"
        if field == "limit": p["legs"][0]["limit_price"] = "48000"
        if field == "mapping": db.get(GrvtTestnetOcoLeg,p["legs"][0]["client_order_id"]).role = "INVALID"
        if field == "claim_status": row.status = "ABANDONED"
        row.payload_json = json.dumps(p,sort_keys=True,separators=(",",":"),ensure_ascii=True)
        row.payload_digest = hashlib.sha256(row.payload_json.encode("ascii")).hexdigest()
        db.commit()
    with pytest.raises(GrvtTestnetOcoError): read_oco(vault,group_id)
    with pytest.raises(VaultError,match="OCO"): vault.backup(tmp_path / "invalid-v34.db")


def test_two_connections_can_claim_native_group_only_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.live_execution.grvt_testnet_oco import claim_oco, GrvtTestnetOcoError
    vault = ready(tmp_path)
    group_id = str(uuid4()); orders = orders_for(group_id)
    prepare_oco(vault,group_id,CID,orders,limits(),INSTRUMENT,now_ns=STAMP)
    second = vault_at(tmp_path / "real.db")
    def claim(store):
        try: return claim_oco(store,group_id,orders,limits(),now_ns=STAMP+1000)["status"]
        except GrvtTestnetOcoError: return "BLOCKED"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim,(vault,second)))
    assert sorted(results) == ["BLOCKED","POST_CLAIMED"]
    vault.lock(); second.lock()


@pytest.mark.parametrize("lost_reply", [False, True])
def test_recovery_queries_both_native_contracts_without_saved_signatures_or_resending(tmp_path,lost_reply):
    from app.live_execution.grvt_testnet_oco_observation import observe_oco_orders
    case = sender_case(tmp_path)
    case.settings["lost_reply"] = lost_reply
    if lost_reply:
        with pytest.raises(GrvtTestnetOrderError): case.send()
    else: case.send()
    case.vault.lock(); reopened = vault_at(tmp_path / "real.db")
    case.clock[0] += 1_000_000_000
    observed = observe_oco_orders(reopened,case.writer,case.group_id,clock_ns=lambda:case.clock[0])
    assert [row["status"] for row in observed["orders"]] == ["OPEN","OPEN"]
    assert observed["scope"] == "ORDER_STATE_ONLY" and observed["can_submit"] is False
    assert all(order["signature"]["r"] not in str(observed) for order in case.orders)
    assert read_oco(reopened,case.group_id)["last_observation"] == observed
    assert case.calls.count(case.bulk_url) == 1 and case.calls.count(LOOKUP_URL) == 2
    reopened.backup(tmp_path / "observed-oco.db")


def test_native_ack_and_recovery_accept_same_exact_values_with_provider_decimal_formatting(tmp_path):
    from app.live_execution.grvt_testnet_oco_observation import observe_oco_orders
    case = sender_case(tmp_path)
    case.settings["normalised_values"] = True
    assert case.send()["status"] == "ACKNOWLEDGED"
    observed = observe_oco_orders(case.vault,case.writer,case.group_id,clock_ns=lambda:case.clock[0])
    assert [row["status"] for row in observed["orders"]] == ["OPEN","OPEN"]
    assert case.calls.count(case.bulk_url) == 1


def test_ack_received_after_expiry_is_not_discarded_and_clock_never_infers_cancel(tmp_path):
    from app.live_execution.grvt_testnet_oco_observation import observe_oco_orders
    case = sender_case(tmp_path)
    case.settings["reply_delay_ns"] = 3_601_000_000_000
    assert case.send()["status"] == "ACKNOWLEDGED"
    observed = observe_oco_orders(case.vault,case.writer,case.group_id,clock_ns=lambda:case.clock[0])
    assert [row["status"] for row in observed["orders"]] == ["OPEN","OPEN"]
    assert observed["can_submit"] is False


def test_observation_chain_records_terminal_pair_without_releasing_account(tmp_path):
    from app.live_execution.grvt_testnet_oco_observation import observe_oco_orders
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    case = sender_case(tmp_path); case.send()
    first = observe_oco_orders(case.vault,case.writer,case.group_id,clock_ns=lambda:case.clock[0])
    case.settings["states"] = {0:("CANCELLED","0"),1:("FILLED","0.01")}
    case.clock[0] += 1_000_000_000
    terminal = observe_oco_orders(case.vault,case.writer,case.group_id,clock_ns=lambda:case.clock[0])
    assert terminal["sequence"] == 2 and terminal["previous_digest"]
    assert [row["status"] for row in terminal["orders"]] == ["CANCELLED","FILLED"]
    with pytest.raises(GrvtTestnetJournalError,match="OCO"):
        require_new_testnet_submission(case.vault,SUB,str(int(CID)+1))
    case.settings["bad_trigger"] = True
    with pytest.raises(GrvtTestnetOcoError):
        observe_oco_orders(case.vault,case.writer,case.group_id,clock_ns=lambda:case.clock[0])
    assert read_oco(case.vault,case.group_id)["last_observation"] == terminal
    assert first["scope"] == terminal["scope"] == "ORDER_STATE_ONLY"
    case.vault.backup(tmp_path / "terminal-observed.db")


@pytest.mark.parametrize("fault", ["overfill","backward_sequence","broken_link","wrong_contract","false_permission"])
def test_observation_corruption_is_rejected_by_reads_and_backup_after_rehash(tmp_path,fault):
    from app.live_execution.grvt_testnet_oco_observation import observe_oco_orders
    from app.portfolio.models import GrvtTestnetOcoObservation
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    from app.portfolio.vault import VaultError
    case = sender_case(tmp_path); case.send()
    observe_oco_orders(case.vault,case.writer,case.group_id,clock_ns=lambda:case.clock[0])
    with case.vault.session() as db:
        row = db.query(GrvtTestnetOcoObservation).one()
        p = json.loads(row.payload_json)
        if fault == "overfill": p["orders"][0]["traded_size"] = "0.02"
        if fault == "backward_sequence": p["sequence"] = 0
        if fault == "broken_link": p["previous_digest"] = "f"*64
        if fault == "wrong_contract": p["orders"][0]["wire_digest"] = "f"*64
        if fault == "false_permission": p["can_submit"] = True
        row.payload_json = json.dumps(p,sort_keys=True,separators=(",",":"),ensure_ascii=True)
        row.payload_digest = hashlib.sha256(row.payload_json.encode("ascii")).hexdigest(); db.commit()
    with pytest.raises(GrvtTestnetOcoError): read_oco(case.vault,case.group_id)
    with pytest.raises(VaultError,match="OCO"): case.vault.backup(tmp_path / "bad-observation.db")


def test_stop_between_claim_and_send_blocks_bulk_without_erasing_marker(tmp_path):
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    case = sender_case(tmp_path)
    with case.vault.session() as db:
        stamp = (NOW+timedelta(seconds=10)).isoformat()
        db.connection().exec_driver_sql("CREATE TRIGGER stop_oco_before_network AFTER INSERT ON grvt_testnet_oco_claims "
            "BEGIN INSERT OR REPLACE INTO grvt_testnet_stops(sub_account_id,stopped,updated_at) VALUES ('"+SUB+"',1,'"+stamp+"'); END")
        db.commit()
    with pytest.raises(GrvtTestnetOcoError): case.send()
    assert case.bulk_url not in case.calls
    assert read_oco(case.vault,case.group_id)["status"] == "POST_CLAIMED"


def test_stop_keeps_existing_exchange_protections_and_observation_available(tmp_path):
    from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
    from app.live_execution.grvt_testnet_oco_observation import observe_oco_orders
    case = sender_case(tmp_path); case.send()
    stop_testnet_subaccount(case.vault,SUB,now=NOW+timedelta(seconds=10))
    observed = observe_oco_orders(case.vault,case.writer,case.group_id,clock_ns=lambda:case.clock[0])
    assert [row["status"] for row in observed["orders"]] == ["OPEN","OPEN"]
    assert not any(url.endswith("cancel_order") for url in case.calls)


@pytest.mark.parametrize("group_id", [None,"not-a-uuid","0"*36,123])
def test_invalid_group_identity_is_rejected_consistently(group_id):
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    with pytest.raises(GrvtTestnetOcoError): oco_client_ids(group_id)


def test_order_state_regression_never_overwrites_verified_history(tmp_path):
    from app.live_execution.grvt_testnet_oco_observation import observe_oco_orders
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    case = sender_case(tmp_path); case.send()
    case.settings["states"] = {0:("CANCELLED","0"),1:("FILLED","0.01")}
    final = observe_oco_orders(case.vault,case.writer,case.group_id,clock_ns=lambda:case.clock[0])
    case.settings["states"] = {0:("OPEN","0"),1:("OPEN","0")}
    case.clock[0] += 1_000_000_000
    with pytest.raises(GrvtTestnetOcoError):
        observe_oco_orders(case.vault,case.writer,case.group_id,clock_ns=lambda:case.clock[0])
    assert read_oco(case.vault,case.group_id)["last_observation"] == final
