"""Finanzas conjuntas OCO: SQLCipher/firma reales, venue HTTP sintético."""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient, LOOKUP_URL, ORDER_URL
from app.live_execution.grvt_testnet_evidence import list_testnet_audits
from tests.test_grvt_testnet_journal import SUB, CID, NOW_NS, order
from tests.test_grvt_testnet_oco import sender_case, STAMP


def finance_case(tmp_path, *, direction="LONG", size="0.01", funding="0.02"):
    case = sender_case(tmp_path,direction=direction)
    case.send()
    case.clock[0] = STAMP+2_000_000_000
    terminal_ns = STAMP+1_000_000_000
    case.settings["states"] = {0:("CANCELLED","0"),1:("FILLED" if size == "0.01" else "CANCELLED",size)}
    parent = list_testnet_audits(case.vault,SUB,CID)[-1]
    plan = case.orders[1]
    cid = plan["metadata"]["client_order_id"]
    price = "53000" if direction == "LONG" else "47000"
    pnl = (Decimal(price)-50000)*Decimal(size)*(1 if direction == "LONG" else -1)
    rows = []
    for fill in parent["fills"]["rows"]:
        rows.append({**fill,"event_time":str(fill["event_time_ns"]),"sub_account_id":SUB,
            "order_id":parent["order"]["order_id"],"client_order_id":CID,
            "instrument":"BTC_USDT_Perp","is_buyer":direction == "LONG","is_liquidation":False})
    if Decimal(size):
        rows.append({"trade_id":"fill-oco-1","event_time":str(terminal_ns),"sub_account_id":SUB,
            "order_id":"0xoco1","client_order_id":cid,"instrument":"BTC_USDT_Perp",
            "is_buyer":direction == "SHORT","is_liquidation":False,"is_taker":True,
            "size":size,"price":price,"fee":"0.05","fee_currency":"USDT",
            "realized_pnl":str(pnl),"builder_fee":"0"})
    remaining = (Decimal("0.01")-Decimal(size))*(1 if direction == "LONG" else -1)
    settings = SimpleNamespace(fills=rows,funding=([{"tx_id":"fill-oco-1","event_time":str(terminal_ns),
        "sub_account_id":SUB,"instrument":"BTC_USDT_Perp","currency":"USDT","amount":funding}]
        if Decimal(funding) else []),balance=str(Decimal(parent["account"]["settle_balance"])+pnl
        -(Decimal("0.05") if Decimal(size) else 0)-Decimal(funding)),position=str(remaining),requests=[])

    def post(url,body,headers):
        if getattr(settings,'ticking_http',False): case.clock[0] += 100_000_000
        settings.requests.append((url,deepcopy(body)))
        if url == ORDER_URL:
            settings.requests.append(('created_ioc',deepcopy(body['order'])))
            return httpx.Response(200,json={'result':{**body['order'],'order_id':'0xafteroco'}})
        if url == LOOKUP_URL:
            if body["client_order_id"] == CID:
                candidate = order()
                candidate["legs"][0]["is_buying_asset"] = direction == "LONG"
                candidate.update(order_id="0x123abc",state={"status":"FILLED","traded_size":["0.01"],
                    "update_time":str(NOW_NS+1000)})
                candidate["metadata"]["create_time"] = str(NOW_NS+1)
                if getattr(settings,'decimal_format',False):
                    candidate['legs'][0]['size'] = '0.010000000'
                    candidate['state']['traded_size'] = ['0.010000000']
                return httpx.Response(200,json={"result":candidate})
            response = case.http_post(url,body,headers)
            data = response.json(); data["result"]["state"]["update_time"] = str(terminal_ns)
            if getattr(settings,'decimal_format',False):
                data['result']['state']['traded_size'] = [format(Decimal(value),'.9f') for value in data['result']['state']['traded_size']]
            return httpx.Response(200,json=data)
        if url.endswith("/fill_history"):
            values = deepcopy(settings.fills)
            if getattr(settings,'decimal_format',False):
                for value in values:
                    for key in ('size','price','fee','realized_pnl','builder_fee'): value[key] = format(Decimal(value[key]),'.9f')
            return httpx.Response(200,json={"result":values,"next":""})
        if url.endswith("/funding_payment_history"):
            values = deepcopy(settings.funding)
            if getattr(settings,'decimal_format',False):
                for value in values: value['amount'] = format(Decimal(value['amount']),'.9f')
            return httpx.Response(200,json={"result":values,"next":""})
        if any(url.endswith('/'+name) for name in ('transfer_history','withdrawal_history','deposit_history','interest_payment_history')):
            name = url.rsplit('/',1)[-1]
            return httpx.Response(200,json={"result":getattr(settings,'histories',{}).get(name,[]),"next":""})
        if url.endswith('/open_orders'):
            return httpx.Response(200,json={"result":[]})
        if url.endswith('/account_summary'):
            case.clock[0] += getattr(settings,'account_delay_ns',0)
            account = {"sub_account_id":SUB,"event_time":str(case.clock[0]),
                "settle_currency":"USDT","margin_type":"SIMPLE_CROSS_MARGIN","sub_account_mode":"SINGLE_ASSET_MODE",
                "total_equity":settings.balance,"unrealized_pnl":"0",
                "initial_margin":"1" if Decimal(settings.position) else "0",
                "maintenance_margin":"0.5" if Decimal(settings.position) else "0",
                "positions":[{"sub_account_id":SUB,"instrument":"BTC_USDT_Perp","size":settings.position}] if Decimal(settings.position) else [],
                "spot_balances":[{"currency":"USDT","balance":settings.balance}]}
            account.update(getattr(settings,'account_overrides',{}))
            return httpx.Response(200,json={"result":account})
        return case.http_post(url,body,headers)
    case.finance = settings
    case.reader = GrvtTestnetOrderClient('fake-testnet-key',http_post=post,clock_ns=lambda:case.clock[0])
    return case


def test_joint_audit_explains_one_oco_exit_without_treating_parent_or_sibling_as_external(tmp_path):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco, read_oco_audits
    case = finance_case(tmp_path)
    result = audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    assert result["result"] == {"settle_balance":"10029.68","positions":[],"filled_size":"0.01",
        "fees_usdt":"0.05","funding_paid_usdt":"0.02","reported_realized_pnl_usdt":"30",
        "net_realized_pnl_usdt":"29.93"}
    assert result["causal_proof"] is False and result["real_book_events_created"] is False
    assert read_oco_audits(case.vault,case.group_id) == [result]
    case.vault.backup(tmp_path/'joint-audit.db')


def test_later_snapshot_cannot_rewrite_a_fill_that_was_already_observed(tmp_path):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco, read_oco_audits
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    case = finance_case(tmp_path)
    first = audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    case.clock[0] += 6_000_000_000
    case.finance.fills[-1].update(price='54000',realized_pnl='40')
    case.finance.balance = str(Decimal(case.finance.balance)+10)
    with pytest.raises(GrvtTestnetOcoError):
        audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    assert read_oco_audits(case.vault,case.group_id) == [first]


def test_two_independent_audits_produce_an_immutable_joint_receipt_after_restart(tmp_path):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco, reconcile_oco, read_oco_reconciliation
    from tests.test_grvt_testnet_journal import vault_at
    case = finance_case(tmp_path)
    first = audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    case.clock[0] += 6_000_000_000
    second = audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    receipt = reconcile_oco(case.vault,case.group_id,now_ns=case.clock[0]+1)
    assert receipt['result'] == second['result'] == first['result']
    assert [row[0] for row in receipt['audits']] == [first['id'],second['id']]
    assert receipt['scope'] == 'ISOLATED_TESTNET_OCO_BALANCE_POSITION_MATCH'
    assert receipt['causal_proof'] is False and receipt['real_book_events_created'] is False
    case.vault.lock()
    reopened = vault_at(tmp_path/'real.db')
    assert read_oco_reconciliation(reopened,case.group_id) == receipt
    assert reconcile_oco(reopened,case.group_id,now_ns=case.clock[0]+86_400_000_000_000) == receipt
    reopened.backup(tmp_path/'reconciled-oco.db')


def test_next_ioc_uses_the_oco_balance_and_history_instead_of_the_old_open_position(tmp_path):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco, reconcile_oco
    from app.live_execution.grvt_testnet_continuity import check_testnet_history_continuity
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission
    from app.live_execution.grvt_testnet_reconciliation import require_reconciled_continuity
    from app.live_execution.grvt_testnet_submit import submit_testnet_once
    from app.live_execution.grvt_testnet_preflight import read_testnet_preflight
    from tests.test_grvt_testnet_journal import limits
    case = finance_case(tmp_path)
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    case.clock[0] += 6_000_000_000
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    receipt = reconcile_oco(case.vault,case.group_id,now_ns=case.clock[0])
    case.clock[0] += 1_000_000_000
    candidate = order(); cid = str(int(CID)+100)
    candidate['metadata']['client_order_id'] = cid
    require_new_testnet_submission(case.vault,SUB,cid)
    with case.vault.session() as db:
        require_reconciled_continuity(db,SUB,{'settle_currency':'USDT','positions':[],
            'settle_balance':receipt['result']['settle_balance']},now_ns=case.clock[0])
    proof = check_testnet_history_continuity(case.vault,case.reader,SUB,now_ns=case.clock[0])
    assert proof['schema'] == 'orbis.grvt.testnet.history.v3'
    assert proof['previous_group_id'] == case.group_id and len(proof['fills']['rows']) == 2
    result = submit_testnet_once(case.vault,case.reader,candidate,limits(),clock_ns=lambda:case.clock[0],public_post=case.public)
    assert result.client_order_id == cid
    assert read_testnet_preflight(case.vault,SUB,cid)['history_continuity']['previous_group_id'] == case.group_id
    assert len([entry for entry in case.finance.requests if entry[0] == 'created_ioc']) == 1
    case.vault.backup(tmp_path/'new-ioc-after-oco.db')


@pytest.mark.parametrize('fault',['boolean','cutoff'])
def test_rehashed_audit_semantics_are_verified_not_just_the_hash(tmp_path,fault):
    import hashlib,json
    from app.live_execution.grvt_testnet_oco_finance import audit_oco,read_oco_audits
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    from app.portfolio.models import GrvtTestnetOcoAudit
    case = finance_case(tmp_path)
    result = audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    with case.vault.session() as db:
        row = db.get(GrvtTestnetOcoAudit,result['id'])
        payload = json.loads(row.payload_json)
        if fault == 'boolean': payload['legs'][0]['fills']['external_rows'][0]['is_buyer'] = 1
        if fault == 'cutoff':
            for leg in payload['legs']: leg['funding']['observed_until_ns'] -= 1
        row.payload_json = json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True)
        row.payload_digest = hashlib.sha256(row.payload_json.encode('ascii')).hexdigest(); db.commit()
    with pytest.raises(GrvtTestnetOcoError): read_oco_audits(case.vault,case.group_id)


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('size,funding',[('0.01','0.02'),('0.01','0'),('0.01','-0.01'),
    ('0.004','0.02'),('0.004','0'),('0.004','-0.01'),('0','0')])
def test_joint_receipt_preserves_partial_position_and_exact_costs(tmp_path,direction,size,funding):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco,reconcile_oco
    from app.live_execution.grvt_testnet_oco import read_oco
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission
    from app.live_execution.grvt_testnet_reconciliation import require_reconciled_continuity
    case = finance_case(tmp_path,direction=direction,size=size,funding=funding)
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    case.clock[0] += 6_000_000_000
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    receipt = reconcile_oco(case.vault,case.group_id,now_ns=case.clock[0])
    result = receipt['result']
    assert Decimal(result['filled_size']) == Decimal(size)
    assert Decimal(result['funding_paid_usdt']) == Decimal(funding)
    assert Decimal(result['fees_usdt']) == (Decimal('0.05') if Decimal(size) else 0)
    remaining = (Decimal('0.01')-Decimal(size))*(1 if direction == 'LONG' else -1)
    assert result['positions'] == ([['BTC_USDT_Perp',format(remaining,'f')]] if remaining else [])
    assert Decimal(result['settle_balance']) == Decimal(case.finance.balance)
    assert read_oco(case.vault,case.group_id)['reconciliation'] == receipt
    require_new_testnet_submission(case.vault,SUB,str(int(CID)+100))
    with case.vault.session() as db:
        require_reconciled_continuity(db,SUB,{'settle_currency':'USDT','positions':result['positions'],
            'settle_balance':result['settle_balance']},now_ns=case.clock[0])


def test_equivalent_decimal_formatting_does_not_break_joint_receipt_or_next_history(tmp_path):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco,reconcile_oco
    from app.live_execution.grvt_testnet_continuity import check_testnet_history_continuity
    case = finance_case(tmp_path)
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    case.clock[0] += 6_000_000_000; case.finance.decimal_format = True
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    receipt = reconcile_oco(case.vault,case.group_id,now_ns=case.clock[0])
    assert receipt['result']['net_realized_pnl_usdt'] == '29.93'
    case.clock[0] += 1_000_000_000; case.finance.decimal_format = False
    proof = check_testnet_history_continuity(case.vault,case.reader,SUB,now_ns=case.clock[0])
    assert proof['previous_group_id'] == case.group_id


@pytest.mark.parametrize('fault',['balance','position','extra_fill','missing_parent_fill','changed_parent_fill',
    'wrong_child_identity','fill_beyond_limit','wrong_fee_currency','builder_fee','unlinked_funding',
    'account_before_cutoff','slow_snapshot','foreign_collateral','wrong_margin_mode','incomplete_fill_total','active_leg'])
def test_discrepancy_never_saves_financial_proof_or_releases_the_group(tmp_path,fault):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco,read_oco_audits,read_oco_reconciliation
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission,GrvtTestnetJournalError
    case = finance_case(tmp_path)
    if fault == 'balance': case.finance.balance = str(Decimal(case.finance.balance)+1)
    if fault == 'position': case.finance.position = '0.001'
    if fault == 'extra_fill': case.finance.fills.append({**case.finance.fills[-1],
        'trade_id':'foreign-fill','order_id':'0xforeign','client_order_id':str(int(CID)+99)})
    if fault == 'missing_parent_fill': case.finance.fills.pop(0)
    if fault == 'changed_parent_fill': case.finance.fills[0]['price'] = '49000'
    if fault == 'wrong_child_identity': case.finance.fills[-1]['client_order_id'] = str(int(CID)+99)
    if fault == 'fill_beyond_limit': case.finance.fills[-1]['price'] = '50000'
    if fault == 'wrong_fee_currency': case.finance.fills[-1]['fee_currency'] = 'BNB'
    if fault == 'builder_fee': case.finance.fills[-1]['builder_fee'] = '1'
    if fault == 'unlinked_funding': case.finance.funding[0]['tx_id'] = 'foreign-funding'
    if fault == 'account_before_cutoff': case.finance.account_overrides = {'event_time':str(case.clock[0]-1)}
    if fault == 'slow_snapshot': case.finance.account_delay_ns = 6_000_000_000
    if fault == 'foreign_collateral': case.finance.account_overrides = {'spot_balances':[
        {'currency':'USDT','balance':case.finance.balance},{'currency':'ETH','balance':'1'}]}
    if fault == 'wrong_margin_mode': case.finance.account_overrides = {'margin_type':'PORTFOLIO_MARGIN'}
    if fault == 'incomplete_fill_total': case.finance.fills[-1]['size'] = '0.004'
    if fault == 'active_leg': case.settings['states'][1] = ('OPEN','0.01')
    with pytest.raises(GrvtTestnetOcoError):
        audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    assert read_oco_audits(case.vault,case.group_id) == []
    assert read_oco_reconciliation(case.vault,case.group_id) is None
    with pytest.raises(GrvtTestnetJournalError): require_new_testnet_submission(case.vault,SUB,str(int(CID)+100))
    assert not any(entry[0] == 'created_ioc' for entry in case.finance.requests)


@pytest.mark.parametrize('fault',['one','too_soon','stale'])
def test_receipt_requires_two_separated_recent_audits(tmp_path,fault):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco,reconcile_oco,read_oco_reconciliation
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    case = finance_case(tmp_path)
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    if fault != 'one':
        case.clock[0] += 1_000_000_000 if fault == 'too_soon' else 6_000_000_000
        audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    at = case.clock[0]+(31_000_000_000 if fault == 'stale' else 0)
    with pytest.raises(GrvtTestnetOcoError): reconcile_oco(case.vault,case.group_id,now_ns=at)
    assert read_oco_reconciliation(case.vault,case.group_id) is None


def settled_case(tmp_path):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco,reconcile_oco
    case = finance_case(tmp_path)
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    case.clock[0] += 6_000_000_000
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    case.receipt = reconcile_oco(case.vault,case.group_id,now_ns=case.clock[0])
    case.clock[0] += 1_000_000_000
    return case


@pytest.mark.parametrize('fault',['changed_fill','extra_fill','late_funding','missing_funding'])
def test_changed_history_after_oco_receipt_blocks_the_next_order_without_reposting(tmp_path,fault):
    from app.live_execution.grvt_testnet_submit import submit_testnet_once
    from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskError
    from tests.test_grvt_testnet_journal import limits
    case = settled_case(tmp_path)
    if fault == 'changed_fill': case.finance.fills[-1]['price'] = '54000'
    if fault == 'extra_fill': case.finance.fills.append({**case.finance.fills[-1],
        'trade_id':'foreign-fill','order_id':'0xforeign','client_order_id':str(int(CID)+99)})
    if fault == 'late_funding': case.finance.funding.append({**case.finance.funding[0],
        'event_time':str(case.clock[0]),'amount':'0.03'})
    if fault == 'missing_funding': case.finance.funding.clear()
    candidate = order(); candidate['metadata']['client_order_id'] = str(int(CID)+100)
    with pytest.raises(GrvtTestnetRiskError):
        submit_testnet_once(case.vault,case.reader,candidate,limits(),clock_ns=lambda:case.clock[0],public_post=case.public)
    assert not any(row[0] == 'created_ioc' for row in case.finance.requests)


@pytest.mark.parametrize('fault',['balance','audits','false_causal_proof','false_mainnet','time'])
def test_corrupt_joint_receipt_blocks_reads_backup_and_new_order_even_after_rehash(tmp_path,fault):
    import hashlib,json
    from app.live_execution.grvt_testnet_oco_finance import read_oco_reconciliation
    from app.live_execution.grvt_testnet_oco import read_oco,GrvtTestnetOcoError
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission,GrvtTestnetJournalError
    from app.portfolio.models import GrvtTestnetOcoReconciliation
    from app.portfolio.vault import VaultError
    case = settled_case(tmp_path)
    with case.vault.session() as db:
        row = db.get(GrvtTestnetOcoReconciliation,case.group_id)
        payload = json.loads(row.payload_json)
        if fault == 'balance': payload['result']['settle_balance'] = '999999'
        if fault == 'audits': payload['audits'].reverse()
        if fault == 'false_causal_proof': payload['causal_proof'] = True
        if fault == 'false_mainnet': payload['mainnet_order_sent'] = True
        if fault == 'time': row.reconciled_at_ns = str(int(row.reconciled_at_ns)-10_000_000_000)
        row.payload_json = json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True)
        row.payload_digest = hashlib.sha256(row.payload_json.encode('ascii')).hexdigest(); db.commit()
    with pytest.raises(GrvtTestnetOcoError): read_oco_reconciliation(case.vault,case.group_id)
    with pytest.raises(GrvtTestnetOcoError): read_oco(case.vault,case.group_id)
    with pytest.raises(GrvtTestnetJournalError): require_new_testnet_submission(case.vault,SUB,str(int(CID)+100))
    with pytest.raises(VaultError,match='GRVT'): case.vault.backup(tmp_path/'bad-joint-receipt.db')


def test_concurrent_joint_reconciliation_records_one_receipt(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.live_execution.grvt_testnet_oco_finance import audit_oco,reconcile_oco
    from tests.test_grvt_testnet_journal import vault_at
    case = finance_case(tmp_path)
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    case.clock[0] += 6_000_000_000
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    other = vault_at(tmp_path/'real.db')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda vault:reconcile_oco(vault,case.group_id,now_ns=case.clock[0]),(case.vault,other)))
    assert results[0] == results[1]
    other.backup(tmp_path/'concurrent-receipt.db')


def test_joint_reconciliation_never_removes_stop(tmp_path):
    from datetime import timedelta
    from app.live_execution.grvt_testnet_oco_finance import audit_oco,reconcile_oco
    from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount,require_new_testnet_submission,GrvtTestnetJournalError
    from tests.test_grvt_testnet_journal import NOW
    case = finance_case(tmp_path)
    stop_testnet_subaccount(case.vault,SUB,now=NOW+timedelta(seconds=12))
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    case.clock[0] += 6_000_000_000
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    reconcile_oco(case.vault,case.group_id,now_ns=case.clock[0])
    with pytest.raises(GrvtTestnetJournalError,match='detenida'):
        require_new_testnet_submission(case.vault,SUB,str(int(CID)+100))


def test_v35_migration_preserves_pending_group_and_adds_joint_finance_tables(tmp_path):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco
    from app.live_execution.grvt_testnet_oco import read_oco
    from tests.test_grvt_testnet_journal import vault_at
    case = finance_case(tmp_path)
    before = read_oco(case.vault,case.group_id)
    case.second.lock()
    with case.vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_oco_reconciliations')
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_oco_audits')
        db.connection().exec_driver_sql('PRAGMA user_version=35'); db.commit()
    case.vault.lock()
    reopened = vault_at(tmp_path/'real.db')
    assert reopened.last_migration_backup and reopened.last_migration_backup.exists()
    assert read_oco(reopened,case.group_id) == before
    audit_oco(reopened,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    reopened.backup(tmp_path/'migrated-finance.db')


def test_joint_snapshot_uses_realistic_advancing_request_clock_and_one_common_cutoff(tmp_path):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco,reconcile_oco
    case = finance_case(tmp_path)
    case.finance.ticking_http = True
    first = audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    assert first['observed_at_ns'] > first['cutoff_ns']
    assert first['legs'][0]['account']['event_time_ns'] >= first['cutoff_ns']
    for name in ('funding','transfers','withdrawals','deposits','interest'):
        assert all(leg[name]['observed_until_ns'] == first['cutoff_ns'] for leg in first['legs'])
    case.clock[0] += 6_000_000_000
    audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    assert reconcile_oco(case.vault,case.group_id,now_ns=case.clock[0])['result'] == first['result']


@pytest.mark.parametrize('name',['transfer_history','withdrawal_history','deposit_history','interest_payment_history'])
def test_other_well_formed_movements_are_not_silently_attributed_to_oco(tmp_path,name):
    from app.live_execution.grvt_testnet_oco_finance import audit_oco,read_oco_audits
    from app.live_execution.grvt_testnet_oco import GrvtTestnetOcoError
    from tests.test_grvt_testnet_transfers import transfer
    from tests.test_grvt_testnet_withdrawals import row as withdrawal
    from tests.test_grvt_testnet_deposits import row as deposit
    from tests.test_grvt_testnet_interest import row as interest
    case = finance_case(tmp_path)
    at = STAMP+1_000_000_000
    rows = {
        'transfer_history':[transfer(tx_id='in',event_time=at),transfer(tx_id='out',from_sub=SUB,to_sub='0',event_time=at)],
        'withdrawal_history':[{**withdrawal('withdraw',owner=SUB),'event_time':str(at)}],
        'deposit_history':[{**deposit('0x12345678'),'initiated_time':str(at)}],
        'interest_payment_history':[interest(amount='0',sub=SUB,event=at,charge=at)],
    }
    case.finance.histories = {name:rows[name]}
    with pytest.raises(GrvtTestnetOcoError) as error:
        audit_oco(case.vault,case.reader,case.group_id,clock_ns=lambda:case.clock[0])
    assert 'Sensitive' not in str(error.value) and 'FullPrivate' not in str(error.value)
    assert read_oco_audits(case.vault,case.group_id) == []
