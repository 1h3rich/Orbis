"""Entrada real firmada y bóveda SQLCipher; sólo HTTP privado es controlado."""

from decimal import Decimal
from copy import deepcopy
from dataclasses import replace

import pytest

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from tests.test_grvt_mainnet_submit import live_case,arguments,PASSWORD


def closed_case(directory,direction='LONG'):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    case=live_case(directory); parent=str(2**63+1200)
    if direction=='SHORT':case.draft=replace(case.draft,direction='SHORT',stop_loss=Decimal('105'),take_profit=Decimal('90'))
    submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=parent,**arguments(case))
    entry=case.settings['orders'][parent]; start=case.clock[0]
    case.clock[0]+=1_000_000_000; finish=case.clock[0]
    stop=case.settings['orders'][str(int(parent)^1)]; target=case.settings['orders'][str(int(parent)^2)]
    stop['state'].update(status='CANCELLED',update_time=str(finish))
    target['state'].update(status='FILLED',traded_size=['0.5'],update_time=str(finish))
    def fill(order,trade,stamp,price,fee,pnl):
        return {'sub_account_id':'123456','instrument':'BTC_USDT_Perp','trade_id':trade,
            'order_id':order['order_id'],'client_order_id':order['metadata']['client_order_id'],
            'event_time':str(stamp),'is_buyer':order['legs'][0]['is_buying_asset'],'is_taker':True,
            'size':'0.5','price':price,'fee':fee,'fee_currency':'USDT','realized_pnl':pnl,
            'builder_fee':'0','is_liquidation':False}
    case.settings['histories']['fill_history']=[fill(target,'native-exit',finish,'110' if direction=='LONG' else '90','0.03','5'),
        fill(entry,'native-entry',start,'100','0.02','0')]
    case.settings['account_balance']=case.settings['account_equity']='10004.95'
    case.parent=parent
    return case


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_native_mainnet_exit_reconciles_from_two_independent_audits_and_survives_backup(tmp_path,direction):
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=closed_case(tmp_path,direction)
    def audit():
        return audit_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    first=audit()
    assert first['network']=='MAINNET' and first['result']['net_realized_pnl_usdt']=='4.95'
    with pytest.raises(GrvtMainnetError): reconcile_mainnet_bracket(case.vault,case.parent,now_ns=case.clock[0])
    case.clock[0]+=6_000_000_000; second=audit()
    assert second['result']==first['result']
    receipt=reconcile_mainnet_bracket(case.vault,case.parent,now_ns=case.clock[0])
    assert receipt['result']['computed_realized_pnl_usdt']=='5'
    assert receipt['result']['fees_usdt']=='0.05' and receipt['result']['positions']==[]
    saved=read_mainnet_bracket(case.vault,case.parent)
    assert saved['financially_reconciled'] is True and saved['position_verified'] is True
    assert saved['authorization'] is False and saved['reconciliation']==receipt
    assert len(case.sent)==1
    case.vault.backup(tmp_path/'mainnet-native-reconciled.db'); case.vault.lock(); case.vault.unlock(PASSWORD)
    assert read_mainnet_bracket(case.vault,case.parent)==saved
    for secret in ('synthetic-mainnet-api-key','synthetic-mainnet-session'):
        assert secret not in str(saved)
    case.vault.lock()


def reconcile_case(case):
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    for _ in range(2):
        audit_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        case.clock[0]+=6_000_000_000
    return reconcile_mainnet_bracket(case.vault,case.parent,now_ns=case.clock[0])


def test_only_a_financial_receipt_releases_the_slot_for_an_explicit_new_mainnet_entry(tmp_path):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=closed_case(tmp_path); next_parent=str(int(case.parent)+8)
    with pytest.raises(GrvtMainnetError):
        submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=next_parent,**arguments(case))
    receipt=reconcile_case(case)
    before=read_mainnet_bracket(case.vault,case.parent)
    assert before['reconciliation']==receipt and before['authorization'] is False
    result=submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=next_parent,**arguments(case))
    assert result['status']=='ACKNOWLEDGED' and result['parent_client_order_id']==next_parent
    assert len(case.sent)==2
    assert read_mainnet_bracket(case.vault,case.parent)==before
    case.vault.backup(tmp_path/'two-mainnet-groups.db');case.vault.lock();case.vault.unlock(PASSWORD)
    assert read_mainnet_bracket(case.vault,case.parent)==before
    case.vault.lock()


@pytest.mark.parametrize('payment,net,balance',[('0.25','4.76','10004.76'),('-0.25','5.26','10005.26')])
def test_rebates_and_linked_funding_use_their_real_sign_without_rounding(tmp_path,payment,net,balance):
    case=closed_case(tmp_path)
    fills=case.settings['histories']['fill_history']
    fills[0]['fee']='0.01'; fills[1]['fee']='-0.02'
    case.settings['histories']['funding_payment_history']=[{'sub_account_id':'123456','tx_id':'native-exit',
        'event_time':str(case.clock[0]),'instrument':'BTC_USDT_Perp','currency':'USDT','amount':payment}]
    case.settings['account_balance']=case.settings['account_equity']=balance
    receipt=reconcile_case(case)
    assert receipt['result']['net_realized_pnl_usdt']==net
    assert receipt['result']['fees_usdt']=='-0.01'
    assert receipt['result']['funding_paid_usdt']==payment
    assert all(body['sub_account_id']=='123456' and body['limit']==500 for _,body in case.settings['history_requests'])
    case.vault.backup(tmp_path/'mainnet-rebate-funding.db');case.vault.lock()


@pytest.mark.parametrize('fault',['pnl_and_balance','missing_fill','duplicate_fill','external_fill','external_transfer',
    'unlinked_funding','foreign_fee','residual','incomplete_page','repeated_cursor'])
def test_ambiguous_or_unexplained_finances_cannot_release_a_mainnet_entry(tmp_path,fault):
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    case=closed_case(tmp_path);fills=case.settings['histories']['fill_history']
    if fault=='pnl_and_balance':
        fills[0]['realized_pnl']='6';case.settings['account_balance']=case.settings['account_equity']='10005.95'
    if fault=='missing_fill':fills.pop()
    if fault=='duplicate_fill':fills.append(deepcopy(fills[0]))
    if fault=='external_fill':fills[0]['client_order_id']='1234'
    if fault=='external_transfer':case.settings['histories']['transfer_history']=[{'destination':'0x'+'a'*40,'amount':'5'}]
    if fault=='unlinked_funding':case.settings['histories']['funding_payment_history']=[{'sub_account_id':'123456',
        'tx_id':'other-trade','event_time':str(case.clock[0]),'instrument':'BTC_USDT_Perp','currency':'USDT','amount':'0'}]
    if fault=='foreign_fee':fills[0]['fee_currency']='USDC'
    if fault=='residual':
        case.settings['orders'][str(int(case.parent)^2)]['state'].update(status='CANCELLED',traded_size=['0.2'])
    if fault=='incomplete_page':case.settings['history_pages']['fill_history']={'':{'result':fills}}
    if fault=='repeated_cursor':case.settings['history_pages']['fill_history']={
        '':{'result':[],'next':'same'},'same':{'result':[],'next':'same'}}
    with pytest.raises(GrvtMainnetError):audit_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    saved=read_mainnet_bracket(case.vault,case.parent)
    assert saved['audits']==[] and saved['financially_reconciled'] is False
    assert '0x'+'a'*40 not in str(saved)
    with pytest.raises(GrvtMainnetError):reconcile_mainnet_bracket(case.vault,case.parent,now_ns=case.clock[0])
    calls=len(case.calls)
    with pytest.raises(GrvtMainnetError):submit_mainnet_bracket_once(case.vault,case.draft,
        parent_client_order_id=str(int(case.parent)+8),**arguments(case))
    assert len(case.calls)==calls and len(case.sent)==1
    case.vault.backup(tmp_path/'pending-mainnet-review.db');case.vault.lock()


def test_a_manual_mainnet_stop_is_not_cleared_by_financial_reconciliation(tmp_path):
    from app.live_execution.grvt_mainnet_journal import stop_mainnet_subaccount
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    case=closed_case(tmp_path);stop_mainnet_subaccount(case.vault,'123456')
    receipt=reconcile_case(case)
    assert receipt['authorization'] is False
    calls=len(case.calls)
    with pytest.raises(GrvtMainnetError):submit_mainnet_bracket_once(case.vault,case.draft,
        parent_client_order_id=str(int(case.parent)+8),**arguments(case))
    assert len(case.calls)==calls and len(case.sent)==1
    case.vault.backup(tmp_path/'mainnet-manual-stop.db');case.vault.lock()


def test_backup_and_next_entry_reject_a_financial_result_even_with_recomputed_hashes(tmp_path):
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket
    from app.live_execution.grvt_mainnet_journal import encode
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.portfolio.models import GrvtMainnetAudit,GrvtMainnetAuditHead
    from app.portfolio.vault import VaultError
    import json
    case=closed_case(tmp_path)
    audit_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    with case.vault.session() as db:
        record=db.get(GrvtMainnetAudit,(case.parent,1));value=json.loads(record.payload_json)
        value['result']['net_realized_pnl_usdt']='999'
        record.payload_json,record.payload_digest=encode(value)
        db.get(GrvtMainnetAuditHead,case.parent).digest=record.payload_digest;db.commit()
    with pytest.raises(VaultError):case.vault.backup(tmp_path/'invalid-financial-proof.db')
    calls=len(case.calls)
    with pytest.raises(GrvtMainnetError):submit_mainnet_bracket_once(case.vault,case.draft,
        parent_client_order_id=str(int(case.parent)+8),**arguments(case))
    assert len(case.calls)==calls
    case.vault.lock()


def test_late_financial_data_blocks_reconciliation_without_consuming_another_order(tmp_path):
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket
    case=closed_case(tmp_path)
    def delay(url,body):
        if url.endswith('/interest_payment_history'):case.clock[0]+=6_000_000_000
    case.settings['private_hook']=delay
    with pytest.raises(GrvtMainnetError):audit_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    assert read_mainnet_bracket(case.vault,case.parent)['audits']==[] and len(case.sent)==1
    case.vault.lock()


def test_ram_revocation_during_history_reads_prevents_a_financial_receipt(tmp_path):
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=closed_case(tmp_path);revoked=[False]
    def revoke(url,body):
        if url.endswith('/deposit_history'):revoked[0]=True
    case.settings['private_hook']=revoke
    def permission():
        if revoked[0]:raise GrvtMainnetError('Synthetic audit permission revoked')
    with pytest.raises(GrvtMainnetError):audit_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,
        require_permission=permission,clock_ns=lambda:case.clock[0])
    assert read_mainnet_bracket(case.vault,case.parent)['audits']==[] and len(case.sent)==1
    case.vault.lock()


@pytest.mark.parametrize('fault',['audit_tail','receipt','settlement_head'])
def test_financial_history_truncation_never_produces_a_valid_backup(tmp_path,fault):
    from app.portfolio.models import GrvtMainnetAudit,GrvtMainnetReconciliation,GrvtMainnetSettlementControl
    from app.portfolio.vault import VaultError
    case=closed_case(tmp_path);reconcile_case(case)
    with case.vault.session() as db:
        if fault=='audit_tail':db.delete(db.get(GrvtMainnetAudit,(case.parent,2)))
        if fault=='receipt':db.delete(db.get(GrvtMainnetReconciliation,case.parent))
        if fault=='settlement_head':db.delete(db.get(GrvtMainnetSettlementControl,'123456'))
        db.commit()
    with pytest.raises(VaultError):case.vault.backup(tmp_path/'truncated-financial-history.db')
    case.vault.lock()


def test_two_receipts_form_a_durable_subaccount_chain(tmp_path):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket,encode
    case=closed_case(tmp_path);first=reconcile_case(case)
    second_parent=str(int(case.parent)+8)
    submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=second_parent,**arguments(case))
    case.clock[0]+=1_000_000_000
    for index,cid in enumerate((second_parent,str(int(second_parent)^1),str(int(second_parent)^2))):
        raw=case.settings['orders'][cid]
        raw['state'].update(status='FILLED' if index in (0,2) else 'CANCELLED',
            traded_size=['0.5' if index in (0,2) else '0'],update_time=str(case.clock[0]))
    old_fills=case.settings['histories']['fill_history'];new_fills=[]
    for old,index in zip(old_fills,(2,0)):
        cid=(second_parent,str(int(second_parent)^1),str(int(second_parent)^2))[index]
        raw=case.settings['orders'][cid]
        new_fills.append({**old,'trade_id':old['trade_id']+'-next','order_id':raw['order_id'],
            'client_order_id':cid,'event_time':str(case.clock[0])})
    case.settings['histories']['fill_history']=new_fills
    case.settings['account_balance']=case.settings['account_equity']='10009.9';case.parent=second_parent
    second=reconcile_case(case)
    assert second['sub_sequence']==2 and second['previous_parent_client_order_id']==first['parent_client_order_id']
    assert second['previous_receipt_digest']==encode(first)[1]
    assert read_mainnet_bracket(case.vault,first['parent_client_order_id'])['reconciliation']==first
    case.vault.backup(tmp_path/'two-financial-receipts.db');case.vault.lock();case.vault.unlock(PASSWORD)
    assert read_mainnet_bracket(case.vault,second_parent)['reconciliation']==second
    case.vault.lock()


def test_a_price_outside_grvt_precision_is_rejected_even_when_pnl_and_balance_match(tmp_path):
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket
    case=closed_case(tmp_path)
    fill=case.settings['histories']['fill_history'][0]
    fill['price']='110.0000000001';fill['realized_pnl']='5.00000000005'
    case.settings['account_balance']=case.settings['account_equity']='10004.95000000005'
    with pytest.raises(GrvtMainnetError):audit_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    case.vault.lock()
