"""Recuperación MAINNET de contratos propios, sin reemplazar transporte o bóveda."""

import pytest
from decimal import Decimal
from dataclasses import replace
from tests.test_grvt_mainnet_submit import live_case,arguments,PASSWORD
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError


def orphan_case(directory):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    case=live_case(directory);case.parent=str(2**63+2400)
    submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=case.parent,**arguments(case))
    case.clock[0]+=1_000_000_000
    case.settings['orders'][case.parent]['state'].update(status='CANCELLED',traded_size=['0'],update_time=str(case.clock[0]))
    return case


def test_a_lost_cancel_reply_is_recovered_by_reading_the_three_ids_without_repeating_the_post(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery,cancel_mainnet_bracket_once,read_mainnet_recovery
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    case=orphan_case(tmp_path);case.settings['cancel_lost']=True
    request=begin_mainnet_recovery(case.vault,case.parent,ttl_seconds=120,max_attempts=3,
        confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=case.clock[0])
    assert request['authorization'] is False
    result=cancel_mainnet_bracket_once(case.vault,case.parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert result['cancel_claimed'] is True and result['cancel_acknowledged'] is False
    case.vault.backup(tmp_path/'mainnet-lost-cancel.db');case.vault.lock();case.vault.unlock(PASSWORD)
    result=cancel_mainnet_bracket_once(case.vault,case.parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert result['native_orders_terminal'] is True
    assert len(case.settings['cancel_posts'])==1 and len(case.sent)==1
    assert case.settings['cancel_posts'][0]['client_order_i_ds']==[case.parent]
    for _ in range(2):
        audit_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        case.clock[0]+=6_000_000_000
    receipt=reconcile_mainnet_bracket(case.vault,case.parent,now_ns=case.clock[0])
    assert receipt['result']['entry_filled_size']=='0' and receipt['result']['net_realized_pnl_usdt']=='0'
    assert read_mainnet_recovery(case.vault,case.parent)['authorization'] is False
    case.vault.backup(tmp_path/'mainnet-orphan-retired.db');case.vault.lock()


def partial_case(directory,direction='LONG'):
    from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
    case=live_case(directory);case.parent=str(2**63+2500)
    if direction=='SHORT':case.draft=replace(case.draft,direction='SHORT',stop_loss=Decimal('105'),take_profit=Decimal('90'))
    submit_mainnet_bracket_once(case.vault,case.draft,parent_client_order_id=case.parent,**arguments(case))
    start=case.clock[0];case.clock[0]+=1_000_000_000
    target=case.settings['orders'][str(int(case.parent)^2)];stop=case.settings['orders'][str(int(case.parent)^1)]
    target['state'].update(status='CANCELLED',traded_size=['0.2'],update_time=str(case.clock[0]))
    stop['state'].update(status='CANCELLED',update_time=str(case.clock[0]))
    def fill(order,trade,stamp,size,price,fee,pnl):
        return {'sub_account_id':'123456','instrument':'BTC_USDT_Perp','trade_id':trade,
            'order_id':order['order_id'],'client_order_id':order['metadata']['client_order_id'],
            'event_time':str(stamp),'is_buyer':order['legs'][0]['is_buying_asset'],'is_taker':True,
            'size':size,'price':price,'fee':fee,'fee_currency':'USDT','realized_pnl':pnl,
            'builder_fee':'0','is_liquidation':False}
    case.settings['histories']['fill_history']=[fill(target,'partial-native-exit',case.clock[0],'0.2','110' if direction=='LONG' else '90','0.01','2'),
        fill(case.settings['orders'][case.parent],'partial-native-entry',start,'0.5','100','0.02','0')]
    case.settings.update(account_balance='10001.97',account_equity='10001.97',initial_margin='1',maintenance_margin='0.5',
        positions=[{'sub_account_id':'123456','instrument':'BTC_USDT_Perp','size':'0.3' if direction=='LONG' else '-0.3'}])
    return case


def test_a_partial_native_exit_is_audited_as_reduction_authority_without_being_called_flat(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery,audit_recovery_position
    from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket
    case=partial_case(tmp_path)
    begin_mainnet_recovery(case.vault,case.parent,ttl_seconds=120,max_attempts=3,
        confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=case.clock[0])
    result=audit_recovery_position(case.vault,case.parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert result['details']['result']['remaining_size']=='0.3'
    assert result['details']['result']['positions']==[['BTC_USDT_Perp','0.3']]
    assert result['details']['result']['financially_reconciled'] is False
    assert result['details']['result']['computed_realized_pnl_usdt'] is None
    assert read_mainnet_bracket(case.vault,case.parent)['financially_reconciled'] is False
    assert len(case.sent)==1 and case.settings['cancel_posts']==[]
    case.vault.backup(tmp_path/'mainnet-partial-reduction-only.db');case.vault.lock()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_two_stable_snapshots_authorize_only_the_remaining_reduce_only_ioc(tmp_path,direction):
    from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery,audit_recovery_position
    from app.live_execution.grvt_mainnet_reduction import submit_recovery_reduction_once,observe_recovery_reductions
    from tests.test_grvt_testnet_signer import KEY,SIGNER
    case=partial_case(tmp_path,direction)
    case.settings['format_echo']=True
    begin_mainnet_recovery(case.vault,case.parent,ttl_seconds=120,max_attempts=3,
        confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=case.clock[0])
    audit_recovery_position(case.vault,case.parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    case.clock[0]+=6_000_000_000
    audit_recovery_position(case.vault,case.parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    sent=submit_recovery_reduction_once(case.vault,case.parent,client_factory=case.factory,public_post=case.public,
        private_key=KEY,expected_signer=SIGNER,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert len(case.settings['reduction_posts'])==1 and len(case.sent)==1
    wire=case.settings['reduction_posts'][0]['orders'][0]
    assert wire['time_in_force']=='IMMEDIATE_OR_CANCEL' and wire['reduce_only'] is True
    assert wire['signature']['chain_id']=='325' and wire['legs'][0]['size']=='0.3'
    assert wire['legs'][0]['is_buying_asset'] is (direction=='SHORT')
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from app.live_execution.grvt_order_codec import ORDER_WITHOUT_BUILDER_TYPES
    signature=wire['signature'];leg=wire['legs'][0]
    message={'subAccountID':123456,'isMarket':False,'timeInForce':3,'postOnly':False,'reduceOnly':True,
        'legs':[{'assetID':123456789,'contractSize':300,'limitPrice':int(Decimal(leg['limit_price'])*10**9),
            'isBuyingContract':direction=='SHORT'}],'nonce':signature['nonce'],'expiration':int(signature['expiration'])}
    assert Account.recover_message(encode_typed_data({'name':'GRVT Exchange','version':'0','chainId':325},
        ORDER_WITHOUT_BUILDER_TYPES,message),vrs=(signature['v'],int(signature['r'],16),int(signature['s'],16)))==SIGNER
    assert sent['reductions'][0]['state'] is None
    observe_recovery_reductions(case.vault,case.parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    snapshot=audit_recovery_position(case.vault,case.parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert snapshot['details']['result']['remaining_size']=='0'
    assert snapshot['details']['result']['computed_realized_pnl_usdt']=='2'
    assert snapshot['details']['result']['settle_balance']=='10001.96'
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    for _ in range(2):
        audit_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        case.clock[0]+=6_000_000_000
    receipt=reconcile_mainnet_bracket(case.vault,case.parent,now_ns=case.clock[0])
    assert receipt['schema']=='orbis.grvt.mainnet.bracket.reconciliation.v2'
    assert receipt['result']['net_realized_pnl_usdt']=='1.96' and receipt['result']['positions']==[]
    case.vault.backup(tmp_path/'mainnet-reduced-residual.db');case.vault.lock()


def test_a_partially_filled_ioc_needs_two_new_stable_snapshots_before_closing_the_new_residue(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery,audit_recovery_position
    from app.live_execution.grvt_mainnet_reduction import submit_recovery_reduction_once
    from tests.test_grvt_testnet_signer import KEY,SIGNER
    case=partial_case(tmp_path);case.settings['reduction_fill_size']='0.1'
    begin_mainnet_recovery(case.vault,case.parent,ttl_seconds=120,max_attempts=3,
        confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=case.clock[0])
    def audit():return audit_recovery_position(case.vault,case.parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    def send():return submit_recovery_reduction_once(case.vault,case.parent,client_factory=case.factory,public_post=case.public,
        private_key=KEY,expected_signer=SIGNER,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    audit();case.clock[0]+=6_000_000_000;audit();send()
    with pytest.raises(GrvtMainnetError):send()
    result=audit();assert result['details']['result']['remaining_size']=='0.2'
    case.clock[0]+=6_000_000_000;audit()
    case.settings['reduction_fill_size']=None
    send()
    assert len(case.settings['reduction_posts'])==2
    assert case.settings['reduction_posts'][1]['orders'][0]['legs'][0]['size']=='0.2'
    result=audit();assert result['details']['result']['remaining_size']=='0'
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    for _ in range(2):
        audit_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0]);case.clock[0]+=6_000_000_000
    receipt=reconcile_mainnet_bracket(case.vault,case.parent,now_ns=case.clock[0])
    assert receipt['result']['net_realized_pnl_usdt']=='1.95'
    case.vault.backup(tmp_path/'mainnet-two-reductions.db');case.vault.lock()


def prepared_case(directory,*,attempts=3,direction='LONG'):
    from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery,audit_recovery_position
    case=partial_case(directory,direction)
    begin_mainnet_recovery(case.vault,case.parent,ttl_seconds=120,max_attempts=attempts,
        confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=case.clock[0])
    for _ in range(2):
        audit_recovery_position(case.vault,case.parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        case.clock[0]+=6_000_000_000
    # El último snapshot tiene una ventana de cinco segundos.
    case.clock[0]-=6_000_000_000
    return case


def send_case(case,**changes):
    from app.live_execution.grvt_mainnet_reduction import submit_recovery_reduction_once
    from tests.test_grvt_testnet_signer import KEY,SIGNER
    kwargs=dict(client_factory=case.factory,public_post=case.public,private_key=KEY,expected_signer=SIGNER,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0]);kwargs.update(changes)
    return submit_recovery_reduction_once(case.vault,case.parent,**kwargs)


def test_lost_ioc_reply_survives_restart_without_retransmission(tmp_path):
    from app.live_execution.grvt_mainnet_reduction import observe_recovery_reductions
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    case=prepared_case(tmp_path);case.settings['reduction_lost']=True
    result=send_case(case)
    assert result['reductions'][0]['acknowledgement'] is None and result['reductions'][0]['state'] is None
    case.vault.backup(tmp_path/'mainnet-lost-ioc.db');case.vault.lock();case.vault.unlock(PASSWORD)
    with pytest.raises(GrvtMainnetError):send_case(case)
    observe_recovery_reductions(case.vault,case.parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    for _ in range(2):
        audit_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0]);case.clock[0]+=6_000_000_000
    receipt=reconcile_mainnet_bracket(case.vault,case.parent,now_ns=case.clock[0])
    assert receipt['result']['net_realized_pnl_usdt']=='1.96' and len(case.settings['reduction_posts'])==1
    case.vault.backup(tmp_path/'mainnet-lost-ioc-reconciled.db');case.vault.lock()


@pytest.mark.parametrize('fault',['old_snapshot','deadline','wrong_signer','spread','instrument','quote','residual_mismatch'])
def test_bad_reduction_evidence_does_not_transmit_an_ioc(tmp_path,fault):
    case=prepared_case(tmp_path);changes={}
    if fault=='old_snapshot':case.clock[0]+=6_000_000_000
    if fault=='deadline':case.clock[0]+=121_000_000_000
    if fault=='wrong_signer':changes['expected_signer']='0x'+'a'*40
    if fault=='spread':case.settings['ask']='105'
    if fault=='instrument':case.settings['instrument_hash']='987654321'
    if fault=='quote':
        def clock_jump(url,body):
            if url.endswith('/ticker'):case.clock[0]+=6_000_000_000
        case.settings['public_hook']=clock_jump
    if fault=='residual_mismatch':
        from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
        case.settings['orders'][str(int(case.parent)^2)]['state']['traded_size']=['0.3']
        observe_mainnet_bracket(case.vault,case.parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    with pytest.raises(GrvtMainnetError):send_case(case,**changes)
    assert not case.settings['reduction_posts']
    case.vault.backup(tmp_path/'mainnet-reduction-rejected.db');case.vault.lock()


def test_attempt_limit_does_not_authorize_a_second_reduction(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import audit_recovery_position
    case=prepared_case(tmp_path,attempts=1);case.settings['reduction_fill_size']='0.1';send_case(case)
    for _ in range(2):
        audit_recovery_position(case.vault,case.parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0]);case.clock[0]+=6_000_000_000
    case.clock[0]-=6_000_000_000
    with pytest.raises(GrvtMainnetError):send_case(case)
    assert len(case.settings['reduction_posts'])==1
    case.vault.backup(tmp_path/'mainnet-reduction-limit.db');case.vault.lock()


def test_a_cancel_ack_without_terminal_orders_cannot_authorize_reductions(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery,cancel_mainnet_bracket_once,audit_recovery_position
    case=orphan_case(tmp_path);case.settings['cancel_ignored']=True
    begin_mainnet_recovery(case.vault,case.parent,ttl_seconds=120,max_attempts=3,
        confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=case.clock[0])
    for _ in range(2):
        result=cancel_mainnet_bracket_once(case.vault,case.parent,client_factory=case.factory,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert result['cancel_acknowledged'] is True and result['native_orders_terminal'] is False
    with pytest.raises(GrvtMainnetError):audit_recovery_position(case.vault,case.parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert len(case.settings['cancel_posts'])==1 and not case.settings['reduction_posts']
    case.vault.backup(tmp_path/'mainnet-cancel-ack-not-terminal.db');case.vault.lock()


def test_revocation_after_durable_ioc_claim_does_not_send_and_does_not_reuse_the_attempt(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
    case=prepared_case(tmp_path)
    def permission():
        # Sólo el commit visible desde otra conexión simula el cambio de RAM;
        # no se inspecciona el estado intermedio de la transacción emisora.
        from app.portfolio.vault import RealVault
        probe=RealVault(case.vault.path);probe.unlock(PASSWORD)
        try:reserved=bool(read_mainnet_recovery(probe,case.parent)['reductions'])
        finally:probe.lock()
        if reserved:
            raise GrvtMainnetError('Synthetic fresh RAM permission revoked')
    result=send_case(case,require_permission=permission)
    assert len(result['reductions'])==1 and not case.settings['reduction_posts']
    with pytest.raises(GrvtMainnetError):send_case(case)
    assert not case.settings['reduction_posts']
    case.vault.backup(tmp_path/'mainnet-revoked-ioc-claim.db');case.vault.lock()


def test_old_recovery_permission_cannot_be_extended_by_repeating_begin(tmp_path):
    from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery,read_mainnet_recovery
    case=prepared_case(tmp_path);before=read_mainnet_recovery(case.vault,case.parent)
    case.clock[0]+=121_000_000_000
    with pytest.raises(GrvtMainnetError):begin_mainnet_recovery(case.vault,case.parent,ttl_seconds=300,max_attempts=3,
        confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=case.clock[0])
    assert read_mainnet_recovery(case.vault,case.parent)==before
    case.vault.lock()


def test_backup_rejects_a_truncated_recovery_claim_event(tmp_path):
    from app.portfolio.models import GrvtMainnetRecoveryEvent
    from app.portfolio.vault import VaultError
    case=prepared_case(tmp_path);send_case(case)
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
    view=read_mainnet_recovery(case.vault,case.parent)
    event=next(value for value in view['events'] if value['type']=='REDUCTION_CLAIM')
    with case.vault.session() as db:
        db.delete(db.get(GrvtMainnetRecoveryEvent,(case.parent,event['sequence'])));db.commit()
    with pytest.raises(VaultError):case.vault.backup(tmp_path/'mainnet-truncated-recovery.db')
    case.vault.lock()


@pytest.mark.parametrize('ack,expected',[(True,True),('true',True),(False,False),('false',False)])
def test_official_cancel_ack_representations_are_facts_without_proving_terminal_orders(tmp_path,ack,expected):
    from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery,cancel_mainnet_bracket_once
    case=orphan_case(tmp_path);case.settings['cancel_ack']=ack;case.settings['cancel_ignored']=True
    begin_mainnet_recovery(case.vault,case.parent,ttl_seconds=120,max_attempts=3,
        confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=case.clock[0])
    result=cancel_mainnet_bracket_once(case.vault,case.parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert result['cancel_acknowledged'] is expected and result['native_orders_terminal'] is False
    assert len(case.settings['cancel_posts'])==1 and not case.settings['reduction_posts']
    case.vault.backup(tmp_path/'mainnet-cancel-ack-formats.db');case.vault.lock()
