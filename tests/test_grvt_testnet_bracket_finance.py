"""Conciliación del grupo completo: persistencia/firma reales, venue sintético."""

from copy import deepcopy
from decimal import Decimal,localcontext
from types import SimpleNamespace
import time

from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient,FILL_HISTORY_URL,FUNDING_HISTORY_URL,ORDER_URL
from tests.test_grvt_testnet_bracket_submit import live_group,PASSWORD


def finance_case(tmp_path,direction='LONG',*,exit_size='0.5',funding='0'):
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path,direction)
    submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
        session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    stamp=time.time_ns(); clock=[stamp+1_000_000_000]
    parent,stop,target=[recorded['venue_state'][order['metadata']['client_order_id']] for order in orders]
    stop['state'].update(status='CANCELLED',update_time=str(stamp))
    target['state'].update(status='FILLED' if exit_size=='0.5' else 'CANCELLED',traded_size=[exit_size],update_time=str(stamp))
    size=Decimal(exit_size); price=Decimal(target['legs'][0]['limit_price']); sign=1 if direction=='LONG' else -1
    with localcontext() as ctx:
        ctx.prec=100
        pnl=(price-100)*size*sign; position=(Decimal('0.5')-size)*sign
        balance=Decimal('10000')+pnl-Decimal('0.01')*(1+bool(size))-Decimal(funding)
    def fill(order,index,amount,at,realized):
        return {'sub_account_id':cap.sub_account_id,'order_id':order['order_id'],
            'client_order_id':order['metadata']['client_order_id'],'trade_id':'bracket-fill-'+str(index),
            'event_time':str(at),'instrument':cap.instrument,'is_buyer':order['legs'][0]['is_buying_asset'],
            'is_taker':True,'is_liquidation':False,'size':amount,'price':order['legs'][0]['limit_price'],
            'fee':'0.01','fee_currency':'USDT','realized_pnl':str(realized),'builder_fee':'0'}
    fills=[fill(parent,0,'0.5',int(parent['state']['update_time']),Decimal(0))]
    if size: fills.append(fill(target,2,exit_size,stamp,pnl))
    payments=[] if funding=='0' else [{'sub_account_id':cap.sub_account_id,'tx_id':'bracket-fill-2',
        'event_time':str(stamp),'instrument':cap.instrument,'currency':'USDT','amount':funding}]
    settings={'balance':str(balance),'position':str(position),'fills':fills,'funding':payments,
        'venue_state':recorded['venue_state'],'created_ioc':[]}
    original=client._post
    def post(url,body,headers):
        if url==ORDER_URL:
            settings['created_ioc'].append(deepcopy(body['order']))
            return venue.response(url,{'result':{**body['order'],'order_id':'0xafterbracket'}})
        if url.endswith('/account_summary'):
            pos=Decimal(settings['position'])
            return venue.response(url,{'result':{'sub_account_id':cap.sub_account_id,'event_time':str(clock[0]),
                'settle_currency':'USDT','margin_type':'SIMPLE_CROSS_MARGIN','sub_account_mode':'SINGLE_ASSET_MODE',
                'total_equity':settings['balance'],'initial_margin':'1' if pos else '0',
                'maintenance_margin':'0.5' if pos else '0','unrealized_pnl':'0',
                'positions':[{'sub_account_id':cap.sub_account_id,'instrument':cap.instrument,'size':str(pos)}] if pos else [],
                'spot_balances':[{'currency':'USDT','balance':settings['balance']}]}})
        if url==FILL_HISTORY_URL: return venue.response(url,{'result':deepcopy(settings['fills']),'next':''})
        if url==FUNDING_HISTORY_URL: return venue.response(url,{'result':deepcopy(settings['funding']),'next':''})
        if url.endswith(('/transfer_history','/withdrawal_history','/deposit_history','/interest_payment_history')):
            return venue.response(url,{'result':[],'next':''})
        return original(url,body,headers)
    reader=GrvtTestnetOrderClient('fake-testnet-key-'+cap.sub_account_id,http_post=post,clock_ns=lambda:clock[0])
    return SimpleNamespace(venue=venue,origin=origin,vault=vault,orders=orders,cap=cap,instrument=instrument,
        reader=reader,clock=clock,settings=settings,pnl=pnl,balance=balance,position=position,public=public)


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_joint_audit_explains_all_three_orders_and_exact_net_balance(tmp_path,direction):
    from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,read_bracket_audits
    case=finance_case(tmp_path,direction)
    audited=audit_bracket(case.vault,case.reader,case.origin['client_order_id'],clock_ns=lambda:case.clock[0])
    assert Decimal(audited['result']['settle_balance'])==case.balance
    assert audited['result']['positions']==[]
    assert Decimal(audited['result']['fees_usdt'])==Decimal('0.02')
    assert Decimal(audited['result']['net_realized_pnl_usdt'])==case.pnl-Decimal('0.02')
    assert len(audited['legs'])==3 and audited['real_book_events_created'] is False
    assert read_bracket_audits(case.vault,case.origin['client_order_id'])==[audited]
    case.vault.backup(tmp_path/'audited-group.db'); case.vault.lock()


def test_two_stable_audits_produce_one_immutable_receipt_without_real_book_events(tmp_path):
    from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,reconcile_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.portfolio.vault import RealVault
    case=finance_case(tmp_path,funding='0.02')
    parent=case.origin['client_order_id']
    audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    with pytest.raises(GrvtTestnetBracketError): reconcile_bracket(case.vault,parent,now_ns=case.clock[0])
    case.clock[0]+=6_000_000_000
    audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    receipt=reconcile_bracket(case.vault,parent,now_ns=case.clock[0])
    assert Decimal(receipt['result']['settle_balance'])==case.balance
    assert receipt['result']['funding_paid_usdt']=='0.02'
    assert receipt['real_book_events_created'] is False and receipt['mainnet_order_sent'] is False
    assert reconcile_bracket(case.vault,parent,now_ns=case.clock[0]+100_000_000_000)==receipt
    case.vault.lock(); reopened=RealVault(tmp_path/'real.db'); reopened.unlock(PASSWORD)
    assert read_bracket(reopened,parent)['reconciliation']==receipt
    reopened.backup(tmp_path/'reconciled.db'); reopened.lock()


def test_reconciled_group_is_an_anchor_for_the_next_operation_not_a_history_bypass(tmp_path):
    from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,reconcile_bracket
    from app.live_execution.grvt_testnet_continuity import check_testnet_history_continuity
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission
    from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskError
    case=finance_case(tmp_path,funding='-0.03'); parent=case.origin['client_order_id']
    for index in range(2):
        case.clock[0]+=index*6_000_000_000
        audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    reconcile_bracket(case.vault,parent,now_ns=case.clock[0])
    require_new_testnet_submission(case.vault,case.cap.sub_account_id,str(2**63+500))
    proof=check_testnet_history_continuity(case.vault,case.reader,case.cap.sub_account_id,now_ns=case.clock[0])
    assert proof['schema']=='orbis.grvt.testnet.history.v4'
    assert proof['previous_bracket_parent_id']==parent and len(proof['fills']['rows'])==2
    assert proof['funding']['rows'][0]['amount_paid']=='-0.03'
    case.settings['fills'].append({**case.settings['fills'][0],'trade_id':'unexplained',
        'order_id':'0xexternal','client_order_id':str(2**63+600),'size':'0.001','fee':'0','realized_pnl':'0'})
    with pytest.raises(GrvtTestnetRiskError):
        check_testnet_history_continuity(case.vault,case.reader,case.cap.sub_account_id,now_ns=case.clock[0])
    case.vault.lock()


def test_later_observation_does_not_rewrite_the_already_issued_receipt(tmp_path):
    from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,reconcile_bracket
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    case=finance_case(tmp_path,exit_size='0.2'); parent=case.origin['client_order_id']
    for index in range(2):
        case.clock[0]+=index*6_000_000_000
        audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    receipt=reconcile_bracket(case.vault,parent,now_ns=case.clock[0])
    case.clock[0]+=1_000_000_000
    # Una publicación tardía del venue no altera el recibo histórico.
    target=case.orders[2]['metadata']['client_order_id']
    case.settings['venue_state'][target]['state'].update(traded_size=['0.3'],update_time=str(case.clock[0]))
    observed=observe_bracket_orders(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    assert observed['residual_size_from_orders']=='0.2'
    assert read_bracket(case.vault,parent)['reconciliation']==receipt
    from app.live_execution.grvt_testnet_continuity import check_testnet_history_continuity
    from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskError
    with pytest.raises(GrvtTestnetRiskError):
        check_testnet_history_continuity(case.vault,case.reader,case.cap.sub_account_id,now_ns=case.clock[0])
    case.vault.backup(tmp_path/'historical-receipt.db'); case.vault.lock()


def test_next_signed_ioc_uses_the_bracket_receipt_balance_and_full_history(tmp_path):
    from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,reconcile_bracket
    from app.live_execution.grvt_testnet_signer import GrvtTestnetLimitDraft,sign_testnet_limit
    from app.live_execution.grvt_testnet_submit import submit_testnet_once
    from app.live_execution.grvt_testnet_preflight import read_testnet_preflight
    from app.portfolio.vault import RealVault
    from tests.test_grvt_testnet_signer import KEY,SIGNER
    case=finance_case(tmp_path,funding='0.02'); parent=case.origin['client_order_id']
    for index in range(2):
        case.clock[0]+=index*6_000_000_000
        audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    reconcile_bracket(case.vault,parent,now_ns=case.clock[0])
    case.clock[0]+=1_000_000_000; cid=str(2**63+501)
    draft=GrvtTestnetLimitDraft(case.cap.sub_account_id,case.cap.instrument,'BUY',Decimal('0.1'),
        Decimal('100'),False,'0x'+'0'*40,cid,500,case.clock[0]+60_000_000_000)
    candidate=sign_testnet_limit(draft,case.cap,case.instrument,dedicated_private_key=KEY,
        expected_signer=SIGNER,now_ns=case.clock[0])
    result=submit_testnet_once(case.vault,case.reader,candidate,case.cap,
        public_post=case.public,clock_ns=lambda:case.clock[0])
    assert result.client_order_id==cid and result.status=='ACKNOWLEDGED'
    assert len(case.settings['created_ioc'])==1
    proof=read_testnet_preflight(case.vault,case.cap.sub_account_id,cid)
    assert proof['history_continuity']['previous_bracket_parent_id']==parent
    assert Decimal(proof['state']['settle_balance'])==case.balance
    case.vault.lock(); reopened=RealVault(tmp_path/'real.db'); reopened.unlock(PASSWORD)
    assert read_testnet_preflight(reopened,case.cap.sub_account_id,cid)==proof
    reopened.backup(tmp_path/'next-ioc.db'); reopened.lock()


@pytest.mark.parametrize('fault',['unknown_fill','missing_fill','duplicate_fill','price_outside_limit',
    'foreign_fee','unlinked_funding','wrong_balance','wrong_position'])
def test_unexplained_activity_or_amounts_never_become_a_financial_audit(tmp_path,fault):
    from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,read_bracket_audits
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    case=finance_case(tmp_path); parent=case.origin['client_order_id']
    if fault=='unknown_fill':
        case.settings['fills'].append({**case.settings['fills'][0],'trade_id':'foreign-fill',
            'order_id':'0xexternal','client_order_id':str(2**63+600),'size':'0.001','fee':'0','realized_pnl':'0'})
    elif fault=='missing_fill': case.settings['fills'].pop()
    elif fault=='duplicate_fill': case.settings['fills'].append(deepcopy(case.settings['fills'][0]))
    elif fault=='price_outside_limit': case.settings['fills'][0]['price']='100.01'
    elif fault=='foreign_fee': case.settings['fills'][0]['fee_currency']='BTC'
    elif fault=='unlinked_funding':
        case.settings['funding']=[{'sub_account_id':case.cap.sub_account_id,'tx_id':'unknown-payment',
            'event_time':str(case.clock[0]),'instrument':case.cap.instrument,'currency':'USDT','amount':'0.01'}]
        case.settings['balance']=str(case.balance-Decimal('0.01'))
    elif fault=='wrong_balance': case.settings['balance']=str(case.balance+Decimal('0.01'))
    elif fault=='wrong_position': case.settings['position']='0.001'
    with pytest.raises(GrvtTestnetBracketError):
        audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    assert read_bracket_audits(case.vault,parent)==[]
    assert case.settings['created_ioc']==[]
    case.vault.lock()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_reconciliation_retains_a_partial_position_and_never_clears_stop(tmp_path,direction):
    from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,reconcile_bracket
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission,GrvtTestnetJournalError
    case=finance_case(tmp_path,direction,exit_size='0.2'); parent=case.origin['client_order_id']
    for index in range(2):
        case.clock[0]+=index*6_000_000_000
        audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    receipt=reconcile_bracket(case.vault,parent,now_ns=case.clock[0])
    assert receipt['result']['positions']==[[case.cap.instrument,'0.3' if direction=='LONG' else '-0.3']]
    assert receipt['result']['exit_filled_size']=='0.2'
    with pytest.raises(GrvtTestnetJournalError,match='parada|Parada|detenida'):
        require_new_testnet_submission(case.vault,case.cap.sub_account_id,str(2**63+502))
    case.vault.backup(tmp_path/'partial-position.db'); case.vault.lock()


@pytest.mark.parametrize('separation,age',[(4_999_999_999,0),(6_000_000_000,30_000_000_001)])
def test_receipt_requires_independent_recent_observations(tmp_path,separation,age):
    from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,reconcile_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    case=finance_case(tmp_path); parent=case.origin['client_order_id']
    audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    case.clock[0]+=separation
    audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    with pytest.raises(GrvtTestnetBracketError,match='separación'):
        reconcile_bracket(case.vault,parent,now_ns=case.clock[0]+age)
    assert read_bracket(case.vault,parent)['reconciliation'] is None
    case.vault.lock()


def test_new_linked_funding_requires_another_stable_audit_before_receipt(tmp_path):
    from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,reconcile_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    case=finance_case(tmp_path); parent=case.origin['client_order_id']
    audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    case.clock[0]+=6_000_000_000
    case.settings['funding']=[{'sub_account_id':case.cap.sub_account_id,'tx_id':'bracket-fill-2',
        'event_time':case.settings['fills'][1]['event_time'],'instrument':case.cap.instrument,
        'currency':'USDT','amount':'0.03'}]
    case.settings['balance']=str(case.balance-Decimal('0.03'))
    audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    with pytest.raises(GrvtTestnetBracketError,match='inestables'):
        reconcile_bracket(case.vault,parent,now_ns=case.clock[0])
    case.clock[0]+=6_000_000_000
    audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    receipt=reconcile_bracket(case.vault,parent,now_ns=case.clock[0])
    assert receipt['result']['funding_paid_usdt']=='0.03'
    assert Decimal(receipt['result']['settle_balance'])==case.balance-Decimal('0.03')
    case.vault.backup(tmp_path/'late-linked-funding.db'); case.vault.lock()


@pytest.mark.parametrize('fault',['truncated_chain','rehashed_boolean','rehashed_cutoff','rehashed_receipt'])
def test_reads_and_encrypted_backups_reject_corrupted_financial_evidence(tmp_path,fault):
    import hashlib,json
    from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,reconcile_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.portfolio.models import GrvtTestnetBracketAudit,GrvtTestnetBracketAuditHead,GrvtTestnetBracketReconciliation
    from app.portfolio.vault import VaultError
    case=finance_case(tmp_path); parent=case.origin['client_order_id']
    for index in range(2):
        case.clock[0]+=index*6_000_000_000
        latest=audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    reconcile_bracket(case.vault,parent,now_ns=case.clock[0])
    with case.vault.session() as db:
        row=db.get(GrvtTestnetBracketAudit,latest['id'])
        if fault=='truncated_chain': db.delete(row)
        else:
            if fault=='rehashed_receipt': row=db.get(GrvtTestnetBracketReconciliation,parent)
            payload=json.loads(row.payload_json)
            if fault=='rehashed_boolean': payload['legs'][0]['fills']['external_rows'][0]['is_buyer']=1
            elif fault=='rehashed_cutoff': payload['legs'][0]['funding']['observed_until_ns']-=1
            elif fault=='rehashed_receipt': payload['result']['settle_balance']='20000'
            row.payload_json=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True)
            row.payload_digest=hashlib.sha256(row.payload_json.encode('ascii')).hexdigest()
            if fault!='rehashed_receipt': db.get(GrvtTestnetBracketAuditHead,parent).digest=row.payload_digest
        db.commit()
    with pytest.raises(GrvtTestnetBracketError): read_bracket(case.vault,parent)
    with pytest.raises(VaultError,match='linked entry evidence'): case.vault.backup(tmp_path/'invalid-evidence.db')
    case.vault.lock()


def test_v39_migration_preserves_claim_and_orders_before_enabling_joint_finance(tmp_path):
    from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
    from app.live_execution.grvt_testnet_bracket_journal import read_bracket
    from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,reconcile_bracket
    from app.portfolio.vault import RealVault
    case=finance_case(tmp_path); parent=case.origin['client_order_id']
    observe_bracket_orders(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    before=read_bracket(case.vault,parent)
    with case.vault.session() as db:
        for table in ('grvt_testnet_bracket_reconciliations','grvt_testnet_bracket_audit_heads','grvt_testnet_bracket_audits'):
            db.connection().exec_driver_sql('DROP TABLE '+table)
        db.connection().exec_driver_sql('PRAGMA user_version = 39'); db.commit()
    case.vault.lock(); migrated=RealVault(tmp_path/'real.db'); migrated.unlock(PASSWORD)
    assert read_bracket(migrated,parent)==before
    preimage=migrated.last_migration_backup
    assert preimage.is_file() and preimage.read_bytes()[:16]!=b'SQLite format 3\x00'
    with migrated.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
    legacy=RealVault(preimage); legacy.unlock(PASSWORD,allow_legacy_for_verification=True)
    assert read_bracket(legacy,parent)==before; legacy.lock()
    for index in range(2):
        case.clock[0]+=index*6_000_000_000
        audit_bracket(migrated,case.reader,parent,clock_ns=lambda:case.clock[0])
    assert reconcile_bracket(migrated,parent,now_ns=case.clock[0])['result']['positions']==[]
    migrated.backup(tmp_path/'migrated-finance.db'); migrated.lock()
