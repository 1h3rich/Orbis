"""Actual sign/claim/POST/read/finance path with isolated synthetic venue I/O."""
from decimal import Decimal as D
import json

import pytest

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket,encode
from app.live_execution.grvt_perp_submit import submit_perp_entry_once
from tests.grvt_perp_mainnet_fixture import perp_case,cold_read
from tests.test_grvt_mainnet_submit import arguments,PASSWORD
from app.portfolio.vault import VaultError


def send(case,parent):
    args=arguments(case);args['confirmation']='ENVIAR_ENTRADA_PERPETUA_GRVT_MAINNET'
    return submit_perp_entry_once(case.vault,case.terms,parent_client_order_id=parent,**args)


@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('funding',['0','0.125','-0.125'])
def test_profile_entry_is_claimed_once_then_actual_native_fills_reconcile_and_reopen(tmp_path,mode,direction,funding):
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    case=perp_case(tmp_path,mode=mode,direction=direction);parent=str(2**63+18000)
    saved=send(case,parent)
    assert saved['claim']['schema']=='orbis.grvt.mainnet.bracket.claim.v2'
    assert saved['claim']['source']=='PERP_PROFILE_NATIVE_PROTECTED_ENTRY'
    assert saved['claim']['preflight']['plan']['source']['terms']['profile']['margin_mode']==mode
    assert saved['execution_unverified'] and not saved['position_verified']
    assert len(case.sent)==1 and len(case.sent[0]['orders'])==3
    requests=list(case.calls)
    with pytest.raises(GrvtMainnetError):send(case,parent)
    assert case.calls==requests and len(case.sent)==1
    start=case.clock[0];case.clock[0]+=1_000_000_000;finish=case.clock[0]
    entry=case.settings['orders'][parent];stop=case.settings['orders'][str(int(parent)^1)];target=case.settings['orders'][str(int(parent)^2)]
    quantity=entry['legs'][0]['size']
    stop['state'].update(status='CANCELLED',update_time=str(finish))
    target['state'].update(status='FILLED',traded_size=[quantity],update_time=str(finish))
    pnl=D(quantity)*10
    def fill(order,id,stamp,price,fee,realized):
        return {'sub_account_id':'123456','instrument':'BTC_USDT_Perp','trade_id':id,
            'order_id':order['order_id'],'client_order_id':order['metadata']['client_order_id'],
            'event_time':str(stamp),'is_buyer':order['legs'][0]['is_buying_asset'],'is_taker':True,
            'size':quantity,'price':price,'fee':fee,'fee_currency':'USDT','realized_pnl':realized,
            'builder_fee':'0','is_liquidation':False}
    case.settings['histories']['fill_history']=[fill(target,'profile-exit',finish,'110' if direction=='LONG' else '90','0.03',str(pnl)),
        fill(entry,'profile-entry',start,'100','0.02','0')]
    if D(funding):
        case.settings['histories']['funding_payment_history']=[{'sub_account_id':'123456','tx_id':'profile-exit',
            'event_time':str(finish),'instrument':'BTC_USDT_Perp','currency':'USDT','amount':funding}]
    net=pnl-D('.05')-D(funding);case.settings['account_balance']=case.settings['account_equity']=str(100+net)
    for _ in range(2):
        audit=audit_mainnet_bracket(case.vault,parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        assert D(audit['result']['net_realized_pnl_usdt'])==net
        case.clock[0]+=6_000_000_000
    receipt=reconcile_mainnet_bracket(case.vault,parent,now_ns=case.clock[0])
    assert D(receipt['result']['computed_realized_pnl_usdt'])==pnl
    assert D(receipt['result']['fees_usdt'])==D('.05')
    assert D(receipt['result']['funding_paid_usdt'])==D(funding)
    assert read_mainnet_bracket(case.vault,parent)['financially_reconciled']
    case.vault.backup(tmp_path/'profile-closed-backup.db');case.vault.lock()
    if mode=='CROSS' and direction=='LONG' and funding=='0':
        cold=cold_read(case.vault.path,parent,PASSWORD)
        assert cold['status']=='RECONCILED' and cold['reconciled'] and cold['authorization'] is False
        assert D(cold['net'])==net
    case.vault.unlock(PASSWORD)
    reopened=read_mainnet_bracket(case.vault,parent)
    assert reopened['reconciliation']==receipt and reopened['authorization'] is False
    from app.live_execution.grvt_mainnet_executions import executions_view
    before=list(case.calls);view=executions_view(reopened,timeframe='1h')
    assert view['environment']=='REAL' and view['evidence']=='OWN_VERIFIED_FILLS'
    assert {fill['role'] for fill in view['executions']}=={'ENTRY','TP'}
    assert next(fill['price'] for fill in view['executions'] if fill['role']=='ENTRY')=='100'
    assert case.calls==before  # Inspect actual fills, never the planned bound or a new quote.
    case.vault.lock()


def test_lost_profile_post_is_durable_and_never_retransmits_after_reopen(tmp_path):
    from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
    case=perp_case(tmp_path);case.settings['lose']=True;parent=str(2**63+18001)
    with pytest.raises(GrvtMainnetError):send(case,parent)
    assert len(case.sent)==1
    saved=read_mainnet_bracket(case.vault,parent)
    assert saved['status']=='POST_CLAIMED' and saved['acknowledgement'] is None
    case.vault.backup(tmp_path/'profile-lost-backup.db');case.vault.lock()
    cold=cold_read(case.vault.path,parent,PASSWORD)
    assert cold['status']=='POST_CLAIMED' and cold['authorization'] is False and not cold['reconciled']
    case.vault.unlock(PASSWORD)
    count=len(case.calls)
    with pytest.raises(GrvtMainnetError):send(case,parent)
    assert len(case.calls)==count and len(case.sent)==1
    observation=observe_mainnet_bracket(case.vault,parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert observation['state']['orders'][0]['status']=='FILLED'
    assert len(case.sent)==1
    case.vault.lock()


def test_stop_after_profile_claim_prevents_post_and_cannot_reuse_claim(tmp_path,monkeypatch):
    import app.live_execution.grvt_mainnet_submit as shared
    case=perp_case(tmp_path);parent=str(2**63+18002);allowed=[True]
    original=shared.claim_mainnet_bracket
    def claimed(*args,**kwargs):
        result=original(*args,**kwargs);allowed[0]=False;return result
    monkeypatch.setattr(shared,'claim_mainnet_bracket',claimed)
    args=arguments(case);args['confirmation']='ENVIAR_ENTRADA_PERPETUA_GRVT_MAINNET'
    def checked():
        if not allowed[0]:raise GrvtMainnetError('revoked')
    args['require_permission']=checked
    with pytest.raises(GrvtMainnetError):submit_perp_entry_once(case.vault,case.terms,parent_client_order_id=parent,**args)
    assert not case.sent and read_mainnet_bracket(case.vault,parent)['status']=='POST_CLAIMED'
    allowed[0]=True
    with pytest.raises(GrvtMainnetError):send(case,parent)
    assert not case.sent
    case.vault.lock()


def test_recomputed_digest_cannot_relabel_profile_preflight_as_legacy_authorization(tmp_path):
    from app.portfolio.models import GrvtMainnetBracket
    case=perp_case(tmp_path);parent=str(2**63+18003);send(case,parent)
    with case.vault.session() as db:
        row=db.get(GrvtMainnetBracket,parent);raw=json.loads(row.payload_json)
        raw.update(schema='orbis.grvt.mainnet.bracket.claim.v1',source='MANUAL_NATIVE_PROTECTED_ENTRY')
        row.payload_json,row.payload_digest=encode(raw);db.commit()
    with pytest.raises(GrvtMainnetError):read_mainnet_bracket(case.vault,parent)
    with pytest.raises((VaultError,GrvtMainnetError)):case.vault.backup(tmp_path/'corrupt-profile.db')
    case.vault.lock()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('expired',[False,True])
def test_profile_partial_native_exit_recovers_only_real_residual_and_reconciles(tmp_path,direction,expired):
    from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery,audit_recovery_position
    from app.live_execution.grvt_mainnet_reduction import submit_recovery_reduction_once,observe_recovery_reductions
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    from tests.test_grvt_testnet_signer import KEY,SIGNER
    case=perp_case(tmp_path,direction=direction);parent=str(2**63+18004);send(case,parent)
    start=case.clock[0];case.clock[0]+=1_000_000_000
    entry=case.settings['orders'][parent];stop=case.settings['orders'][str(int(parent)^1)];target=case.settings['orders'][str(int(parent)^2)]
    quantity=entry['legs'][0]['size'];residual=D(quantity)-D('.2')
    stop['state'].update(status='CANCELLED',update_time=str(case.clock[0]))
    target['state'].update(status='CANCELLED',traded_size=['0.2'],update_time=str(case.clock[0]))
    def fill(order,id,stamp,size,price,fee,pnl):
        return {'sub_account_id':'123456','instrument':'BTC_USDT_Perp','trade_id':id,
            'order_id':order['order_id'],'client_order_id':order['metadata']['client_order_id'],
            'event_time':str(stamp),'is_buyer':order['legs'][0]['is_buying_asset'],'is_taker':True,
            'size':size,'price':price,'fee':fee,'fee_currency':'USDT','realized_pnl':pnl,
            'builder_fee':'0','is_liquidation':False}
    case.settings['histories']['fill_history']=[fill(target,'profile-partial-exit',case.clock[0],'0.2',
        '110' if direction=='LONG' else '90','0.01','2'),fill(entry,'profile-partial-entry',start,quantity,'100','0.02','0')]
    case.settings.update(account_balance='101.97',account_equity='101.97',initial_margin='10',maintenance_margin='1',
        positions=[{'sub_account_id':'123456','instrument':'BTC_USDT_Perp','size':str(residual if direction=='LONG' else -residual)}])
    begin_mainnet_recovery(case.vault,parent,ttl_seconds=120,max_attempts=3,
        confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=case.clock[0])
    audit_recovery_position(case.vault,parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    case.clock[0]+=6_000_000_000
    audit_recovery_position(case.vault,parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    if expired:
        case.clock[0]+=6_000_000_000
        with pytest.raises(GrvtMainnetError):
            submit_recovery_reduction_once(case.vault,parent,client_factory=case.factory,public_post=case.public,
                private_key=KEY,expected_signer=SIGNER,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        assert not case.settings['reduction_posts']
        case.vault.lock()
        return
    submit_recovery_reduction_once(case.vault,parent,client_factory=case.factory,public_post=case.public,
        private_key=KEY,expected_signer=SIGNER,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    wire=case.settings['reduction_posts'][0]['orders'][0]
    assert D(wire['legs'][0]['size'])==residual and wire['reduce_only'] is True
    assert wire['legs'][0]['is_buying_asset'] is (direction=='SHORT')
    observe_recovery_reductions(case.vault,parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    for _ in range(2):
        audit_mainnet_bracket(case.vault,parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        case.clock[0]+=6_000_000_000
    receipt=reconcile_mainnet_bracket(case.vault,parent,now_ns=case.clock[0])
    assert D(receipt['result']['net_realized_pnl_usdt'])==D('1.96')
    assert len(case.sent)==1 and len(case.settings['reduction_posts'])==1
    case.vault.backup(tmp_path/'profile-residual-closed.db');case.vault.lock()


def test_old_confirmation_and_instrument_change_cannot_enter_the_profile_path(tmp_path):
    case=perp_case(tmp_path);parent=str(2**63+18005)
    with pytest.raises(GrvtMainnetError):submit_perp_entry_once(case.vault,case.terms,parent_client_order_id=parent,**arguments(case))
    assert not case.calls and not case.sent
    count=[0]
    def hook(url,body):
        if url.endswith('/instrument'):
            count[0]+=1
            if count[0]==3:case.settings['instrument_hash']='123456788'
    case.settings['public_hook']=hook
    with pytest.raises(GrvtMainnetError):send(case,parent)
    assert not case.sent
    with pytest.raises(GrvtMainnetError,match='no encontrado'):read_mainnet_bracket(case.vault,parent)
    case.vault.lock()
