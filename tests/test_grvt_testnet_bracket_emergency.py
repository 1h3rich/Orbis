"""Salida de emergencia con diario/SQLCipher reales y sólo HTTP ficticio."""

from copy import deepcopy
from datetime import datetime,timezone,timedelta
from decimal import Decimal

import pytest

from app.live_execution.grvt_testnet_bracket_finance import audit_bracket,reconcile_bracket
from app.live_execution.grvt_testnet_bracket_journal import read_bracket
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient,ORDER_URL,LOOKUP_URL,FILL_HISTORY_URL,FUNDING_HISTORY_URL
from app.live_execution.grvt_testnet_quote import TESTNET_TICKER_URL
from app.live_execution.grvt_testnet_signer import GrvtTestnetLimitDraft,sign_testnet_limit
from app.portfolio.vault import RealVault
from tests.test_grvt_testnet_bracket_finance import finance_case
from tests.test_grvt_testnet_bracket_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER


def emergency_case(tmp_path,direction='LONG'):
    case=finance_case(tmp_path,direction,exit_size='0.2')
    parent=case.origin['client_order_id']
    for index in range(2):
        case.clock[0]+=index*6_000_000_000
        audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    case.source_receipt=reconcile_bracket(case.vault,parent,now_ns=case.clock[0])
    case.clock[0]+=1_000_000_000
    case.second=RealVault(tmp_path/'real.db'); case.second.unlock(PASSWORD)
    case.controls={'sent':[],'lost':False,'orders':{},'fill_size':None}
    original=case.reader._post

    def post(url,body,headers):
        if url==ORDER_URL:
            candidate=deepcopy(body['order']); cid=candidate['metadata']['client_order_id']
            # Otra conexión ve la concesión ya confirmada antes del POST.
            attempts=read_bracket(case.second,parent)['emergency_attempts']
            assert any(row['client_order_id']==cid and row['status']=='POST_CLAIMED' for row in attempts)
            case.controls['sent'].append(candidate)
            filled=case.controls['fill_size'] or candidate['legs'][0]['size']
            stamp=case.clock[0]
            case.controls['orders'][cid]={**candidate,'order_id':'0xemergency'+str(len(case.controls['sent'])),
                'metadata':{**candidate['metadata'],'create_time':str(stamp)},
                'state':{'status':'FILLED' if filled==candidate['legs'][0]['size'] else 'CANCELLED',
                    'traded_size':[filled],'update_time':str(stamp)}}
            case.settings['position']=str(Decimal(case.settings['position'])+
                Decimal(filled)*(1 if candidate['legs'][0]['is_buying_asset'] else -1))
            case.settings['balance']=str(Decimal(case.settings['balance'])-Decimal('0.01'))
            case.settings['fills'].append({'sub_account_id':case.cap.sub_account_id,
                'order_id':case.controls['orders'][cid]['order_id'],'client_order_id':cid,
                'trade_id':'emergency-fill-'+str(len(case.controls['sent'])),'event_time':str(stamp),
                'instrument':case.cap.instrument,'is_buyer':candidate['legs'][0]['is_buying_asset'],
                'is_taker':True,'is_liquidation':False,'size':filled,'price':'100',
                'fee':'0.01','fee_currency':'USDT','realized_pnl':'0','builder_fee':'0'})
            if case.controls['lost']:
                import httpx
                raise httpx.ReadTimeout('synthetic lost emergency reply')
            return case.venue.response(url,{'result':{**candidate,'order_id':case.controls['orders'][cid]['order_id']}})
        if url==LOOKUP_URL and body['client_order_id'] in case.controls['orders']:
            return case.venue.response(url,{'result':deepcopy(case.controls['orders'][body['client_order_id']])})
        response=original(url,body,headers)
        if url in (FILL_HISTORY_URL,FUNDING_HISTORY_URL):
            data=response.json(); start=int(body['start_time']); end=int(body.get('end_time',case.clock[0]+2_000_000_000))
            data['result']=[row for row in data['result'] if start<int(row['event_time'])<=end]
            return case.venue.response(url,data)
        return response

    original_public=case.public
    def public(url,body):
        response=original_public(url,body)
        if url==TESTNET_TICKER_URL:
            data=response.json(); data['result']['event_time']=str(case.clock[0])
            return case.venue.response(url,data)
        return response
    case.reader=GrvtTestnetOrderClient('fake-testnet-key-'+case.cap.sub_account_id,
        http_post=post,clock_ns=lambda:case.clock[0])
    case.public=public
    return case


def emergency_order(case,*,cid=None,size=None,side=None,reduce_only=True):
    held=Decimal(case.settings['position'])
    cid=cid or str(2**63+700)
    draft=GrvtTestnetLimitDraft(case.cap.sub_account_id,case.cap.instrument,
        side or ('SELL' if held>0 else 'BUY'),Decimal(size or str(abs(held))),Decimal('100'),
        reduce_only,'0x'+'0'*40,cid,int(cid)%2**32,case.clock[0]+10_000_000_000)
    return sign_testnet_limit(draft,case.cap,case.instrument,dedicated_private_key=KEY,
        expected_signer=SIGNER,now_ns=case.clock[0])


def audit_emergency(case,cid):
    from app.live_execution.grvt_testnet_submit import audit_testnet_intent
    from app.live_execution.grvt_testnet_reconciliation import reconcile_testnet_order
    for index in range(2):
        case.clock[0]+=(6 if index else 1)*1_000_000_000
        audit_testnet_intent(case.vault,case.reader,case.cap.sub_account_id,cid,clock_ns=lambda:case.clock[0])
    return reconcile_testnet_order(case.vault,case.cap.sub_account_id,cid,
        now=datetime.fromtimestamp(case.clock[0]//1_000_000_000,timezone.utc)+
            timedelta(microseconds=case.clock[0]%1_000_000_000//1000))


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_emergency_closes_only_the_reconciled_residual_and_preserves_stop_after_reopen(tmp_path,direction):
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission,GrvtTestnetJournalError
    case=emergency_case(tmp_path,direction); parent=case.origin['client_order_id']
    candidate=emergency_order(case); cid=candidate['metadata']['client_order_id']
    result=submit_bracket_emergency_once(case.vault,case.reader,parent,candidate,
        public_post=case.public,clock_ns=lambda:case.clock[0])
    assert result.client_order_id==cid and result.status=='ACKNOWLEDGED'
    receipt=audit_emergency(case,cid)
    assert receipt['result']['positions']==[]
    assert Decimal(receipt['result']['settle_balance'])==case.balance-Decimal('0.01')
    assert len(case.controls['sent'])==1 and case.controls['sent'][0]['reduce_only'] is True
    with pytest.raises(GrvtTestnetJournalError):
        require_new_testnet_submission(case.vault,case.cap.sub_account_id,str(2**63+701))
    case.second.lock(); case.vault.lock(); reopened=RealVault(tmp_path/'real.db'); reopened.unlock(PASSWORD)
    attempts=read_bracket(reopened,parent)['emergency_attempts']
    assert len(attempts)==1 and attempts[0]['status']=='RECONCILED'
    reopened.backup(tmp_path/'closed-emergency.db'); reopened.lock()


def test_removing_the_stop_exception_is_detected_from_the_archived_preflight(tmp_path):
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    from app.live_execution.grvt_testnet_preflight import read_testnet_preflight,GrvtTestnetPreflightError
    from app.portfolio.models import GrvtTestnetBracketEmergency
    from app.portfolio.vault import VaultError
    case=emergency_case(tmp_path); candidate=emergency_order(case); cid=candidate['metadata']['client_order_id']
    submit_bracket_emergency_once(case.vault,case.reader,case.origin['client_order_id'],candidate,
        public_post=case.public,clock_ns=lambda:case.clock[0])
    with case.vault.session() as db:
        db.delete(db.get(GrvtTestnetBracketEmergency,cid)); db.commit()
    with pytest.raises(GrvtTestnetPreflightError): read_testnet_preflight(case.vault,case.cap.sub_account_id,cid)
    with pytest.raises(VaultError): case.vault.backup(tmp_path/'missing-emergency-scope.db')
    case.second.lock(); case.vault.lock()


def test_lost_emergency_reply_is_recovered_after_restart_without_resubmission(tmp_path):
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_order import GrvtTestnetOrderError
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; candidate=emergency_order(case)
    case.controls['lost']=True
    with pytest.raises(GrvtTestnetOrderError):
        submit_bracket_emergency_once(case.vault,case.reader,parent,candidate,
            public_post=case.public,clock_ns=lambda:case.clock[0])
    assert read_bracket(case.vault,parent)['emergency_attempts'][0]['status']=='POST_CLAIMED'
    case.vault.lock(); case.vault=RealVault(tmp_path/'real.db'); case.vault.unlock(PASSWORD)
    case.reader=GrvtTestnetOrderClient('fake-testnet-key-'+case.cap.sub_account_id,
        http_post=case.reader._post,clock_ns=lambda:case.clock[0])
    with pytest.raises(GrvtTestnetBracketError,match='archivada'):
        submit_bracket_emergency_once(case.vault,case.reader,parent,candidate,
            public_post=case.public,clock_ns=lambda:case.clock[0])
    receipt=audit_emergency(case,candidate['metadata']['client_order_id'])
    assert receipt['result']['positions']==[] and len(case.controls['sent'])==1
    case.vault.backup(tmp_path/'recovered-emergency.db'); case.second.lock(); case.vault.lock()


def test_partial_emergency_requires_reconciliation_and_a_new_signed_id_for_the_remainder(tmp_path):
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; candidate=emergency_order(case)
    case.controls['fill_size']='0.1'
    submit_bracket_emergency_once(case.vault,case.reader,parent,candidate,
        public_post=case.public,clock_ns=lambda:case.clock[0])
    with pytest.raises(GrvtTestnetBracketError):
        submit_bracket_emergency_once(case.vault,case.reader,parent,emergency_order(case,cid=str(2**63+701)),
            public_post=case.public,clock_ns=lambda:case.clock[0])
    receipt=audit_emergency(case,candidate['metadata']['client_order_id'])
    assert Decimal(receipt['result']['positions'][0][1])==Decimal('0.2')
    case.clock[0]+=1_000_000_000; case.controls['fill_size']=None
    case.reader=GrvtTestnetOrderClient('fake-testnet-key-'+case.cap.sub_account_id,
        http_post=case.reader._post,clock_ns=lambda:case.clock[0])
    other=emergency_order(case,cid=str(2**63+701))
    submit_bracket_emergency_once(case.vault,case.reader,parent,other,
        public_post=case.public,clock_ns=lambda:case.clock[0])
    final=audit_emergency(case,other['metadata']['client_order_id'])
    assert final['result']['positions']==[] and len(case.controls['sent'])==2
    assert len(read_bracket(case.vault,parent)['emergency_attempts'])==2
    case.vault.backup(tmp_path/'two-scoped-emergencies.db'); case.second.lock(); case.vault.lock()


@pytest.mark.parametrize('fault',['opening','same_side','oversized','signature'])
def test_a_stop_exception_cannot_authorize_other_orders(tmp_path,fault):
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_signer import GrvtTestnetSignError
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']
    candidate=emergency_order(case,reduce_only=fault!='opening',
        side='BUY' if fault=='same_side' else None,size='0.4' if fault=='oversized' else None)
    if fault=='signature': candidate['signature']['r']='0x'+'1'*64
    with pytest.raises((GrvtTestnetBracketError,GrvtTestnetSignError)):
        submit_bracket_emergency_once(case.vault,case.reader,parent,candidate,
            public_post=case.public,clock_ns=lambda:case.clock[0])
    assert read_bracket(case.vault,parent)['emergency_attempts']==[]
    assert case.controls['sent']==[]
    case.second.lock(); case.vault.lock()


def test_rehashed_emergency_cannot_be_moved_to_another_subaccount(tmp_path):
    import hashlib,json
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.portfolio.models import GrvtTestnetBracketEmergency,GrvtTestnetOrderIntent,GrvtTestnetStop
    from app.portfolio.vault import VaultError
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; candidate=emergency_order(case)
    cid=candidate['metadata']['client_order_id']
    submit_bracket_emergency_once(case.vault,case.reader,parent,candidate,
        public_post=case.public,clock_ns=lambda:case.clock[0])
    with case.vault.session() as db:
        row=db.get(GrvtTestnetBracketEmergency,cid); p=json.loads(row.payload_json)
        p['sub_account_id']='998877'; db.get(GrvtTestnetOrderIntent,cid).sub_account_id=p['sub_account_id']
        db.add(GrvtTestnetStop(sub_account_id=p['sub_account_id'],stopped=True,
            updated_at=db.get(GrvtTestnetStop,case.cap.sub_account_id).updated_at))
        row.payload_json=json.dumps(p,sort_keys=True,separators=(',',':'),ensure_ascii=True)
        row.payload_digest=hashlib.sha256(row.payload_json.encode('ascii')).hexdigest(); db.commit()
    with pytest.raises(GrvtTestnetBracketError): read_bracket(case.vault,parent)
    with pytest.raises(VaultError): case.vault.backup(tmp_path/'wrong-emergency-subaccount.db')
    case.second.lock(); case.vault.lock()


def test_expiration_after_claim_keeps_the_id_consumed_without_sending(tmp_path):
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.live_execution.grvt_testnet_journal import GrvtTestnetJournalError
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; candidate=emergency_order(case)
    start=case.clock[0]
    def clock():
        if any(row['status']=='POST_CLAIMED' for row in read_bracket(case.vault,parent)['emergency_attempts']):
            case.clock[0]=start+6_000_000_000
        return case.clock[0]
    with pytest.raises(GrvtTestnetJournalError):
        submit_bracket_emergency_once(case.vault,case.reader,parent,candidate,public_post=case.public,clock_ns=clock)
    assert not case.controls['sent']
    assert read_bracket(case.vault,parent)['emergency_attempts'][0]['status']=='POST_CLAIMED'
    with pytest.raises(GrvtTestnetBracketError,match='archivada'):
        submit_bracket_emergency_once(case.vault,case.reader,parent,candidate,
            public_post=case.public,clock_ns=lambda:case.clock[0])
    case.vault.backup(tmp_path/'expired-claimed-emergency.db'); case.second.lock(); case.vault.lock()


def test_unexplained_balance_change_prevents_the_emergency_post(tmp_path):
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    from app.live_execution.grvt_testnet_preflight import GrvtTestnetPreflightError
    case=emergency_case(tmp_path); candidate=emergency_order(case)
    case.settings['balance']=str(case.balance+Decimal('1'))
    with pytest.raises(GrvtTestnetPreflightError):
        submit_bracket_emergency_once(case.vault,case.reader,case.origin['client_order_id'],candidate,
            public_post=case.public,clock_ns=lambda:case.clock[0])
    assert case.controls['sent']==[]
    case.second.lock(); case.vault.lock()


@pytest.mark.parametrize('role',[0,1,2])
def test_emergency_never_reuses_any_reserved_bracket_id(tmp_path,role):
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    from app.live_execution.grvt_testnet_journal import GrvtTestnetJournalError
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']
    candidate=emergency_order(case,cid=case.orders[role]['metadata']['client_order_id'])
    with pytest.raises(GrvtTestnetJournalError):
        submit_bracket_emergency_once(case.vault,case.reader,parent,candidate,
            public_post=case.public,clock_ns=lambda:case.clock[0])
    assert not case.controls['sent'] and not read_bracket(case.vault,parent)['emergency_attempts']
    case.second.lock(); case.vault.lock()


def test_v40_migration_preserves_the_original_financial_receipt_and_stop(tmp_path):
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']
    before=read_bracket(case.vault,parent); case.second.lock()
    with case.vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_bracket_emergencies')
        db.connection().exec_driver_sql('PRAGMA user_version = 40'); db.commit()
    case.vault.lock(); case.vault=RealVault(tmp_path/'real.db'); case.vault.unlock(PASSWORD)
    assert read_bracket(case.vault,parent)==before
    preimage=case.vault.last_migration_backup
    assert preimage.is_file() and preimage.read_bytes()[:16]!=b'SQLite format 3\x00'
    legacy=RealVault(preimage); legacy.unlock(PASSWORD,allow_legacy_for_verification=True)
    assert read_bracket(legacy,parent)==before; legacy.lock()
    case.second=RealVault(tmp_path/'real.db'); case.second.unlock(PASSWORD)
    candidate=emergency_order(case)
    submit_bracket_emergency_once(case.vault,case.reader,parent,candidate,
        public_post=case.public,clock_ns=lambda:case.clock[0])
    assert audit_emergency(case,candidate['metadata']['client_order_id'])['result']['positions']==[]
    case.vault.backup(tmp_path/'migrated-emergency.db'); case.second.lock(); case.vault.lock()


def test_a_flat_reconciled_bracket_cannot_authorize_an_emergency_order(tmp_path):
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    case=finance_case(tmp_path); parent=case.origin['client_order_id']
    for index in range(2):
        case.clock[0]+=index*6_000_000_000
        audit_bracket(case.vault,case.reader,parent,clock_ns=lambda:case.clock[0])
    reconcile_bracket(case.vault,parent,now_ns=case.clock[0])
    candidate=emergency_order(case,size='0.1',side='SELL')
    with pytest.raises(GrvtTestnetBracketError):
        submit_bracket_emergency_once(case.vault,case.reader,parent,candidate,
            public_post=case.public,clock_ns=lambda:case.clock[0])
    assert case.settings['created_ioc']==[]
    case.vault.lock()


def test_late_change_in_an_original_exit_blocks_a_subsequent_emergency(tmp_path):
    from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    case=emergency_case(tmp_path); parent=case.origin['client_order_id']; first=emergency_order(case)
    case.controls['fill_size']='0.1'
    submit_bracket_emergency_once(case.vault,case.reader,parent,first,
        public_post=case.public,clock_ns=lambda:case.clock[0])
    audit_emergency(case,first['metadata']['client_order_id'])
    case.clock[0]+=1_000_000_000; case.controls['fill_size']=None
    target=case.orders[2]['metadata']['client_order_id']
    case.settings['venue_state'][target]['state'].update(traded_size=['0.3'],update_time=str(case.clock[0]))
    case.reader=GrvtTestnetOrderClient('fake-testnet-key-'+case.cap.sub_account_id,
        http_post=case.reader._post,clock_ns=lambda:case.clock[0])
    with pytest.raises(GrvtTestnetBracketError):
        submit_bracket_emergency_once(case.vault,case.reader,parent,emergency_order(case,cid=str(2**63+701)),
            public_post=case.public,clock_ns=lambda:case.clock[0])
    assert len(case.controls['sent'])==1
    case.second.lock(); case.vault.lock()
