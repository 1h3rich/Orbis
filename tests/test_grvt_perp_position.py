"""Explicit per-position metadata and actual dedicated-account equity checks."""
from copy import deepcopy

import pytest

from app.integrations.grvt_position_config import configuration_response
from app.live_execution.grvt_perp_position import (
    PerpPositionReviewInput,evaluate_position_review,position_account,
)
from tests.test_grvt_perp_plan import NOW,plan_source


NS=10**9
REF='a'*64


def position_input(direction='LONG',mode='ISOLATED'):
    source=plan_source(direction,mode)
    row={'instrument':source.terms.instrument,'margin_type':mode,'leverage':'5','min_leverage':'1','max_leverage':'50'}
    def config(requested,received):
        return configuration_response({'results':[row]},'123456',source.terms.instrument,
            requested_at_ns=NOW+requested,received_at_ns=NOW+received).payload()
    account={'sub_account_id':'123456','event_time':str(NOW-700_000_000),'settle_currency':'USDT',
        'margin_type':'SIMPLE_CROSS_MARGIN','sub_account_mode':'SINGLE_ASSET_MODE','total_equity':'100',
        'spot_balances':[{'currency':'USDT','balance':'100','unused_field':'discard'}],
        'positions':[{'sub_account_id':'123456','instrument':source.terms.instrument,'size':'1' if direction=='LONG' else '-1',
            'event_time':str(NOW-100*NS),'margin_type':mode,'leverage':'5',
            'isolated_balance':'20' if mode=='ISOLATED' else None,
            'isolated_im':'20' if mode=='ISOLATED' else None,
            'isolated_mm':'1' if mode=='ISOLATED' else None}]}
    payload={'schema':'orbis.grvt.perp.position.input.v1','account_ref':REF,
        'configurations':[config(-NS,-900_000_000),config(-600_000_000,-500_000_000)],
        'account':position_account(account,requested_at_ns=NOW-800_000_000,received_at_ns=NOW-700_000_000).payload(),
        'checked_at_ns':str(NOW)}
    return source.terms,payload


def review(terms,payload,**changes):
    args={'expected_account_ref':REF,'entry_size':'1','last_fill_ns':NOW-100*NS,
          'baseline_equity':'100','settled_net':'0','loss_cap':'10','now_ns':NOW}
    args.update(changes)
    return evaluate_position_review(PerpPositionReviewInput.model_validate(payload),terms,**args)


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_matching_explicit_position_and_config_are_hold_evidence_without_authority(direction,mode):
    terms,payload=position_input(direction,mode)
    result=review(terms,payload)
    assert result['action']=='HOLD' and not result['reasons'] and not result['halt_new_entries']
    assert result['position_size']==('1' if direction=='LONG' else '-1')
    assert result['execution_authority']=='NONE' and result['cycle_equity_change']=='0'
    # A position unchanged for 100s is not fabricated as a fresh position event.
    assert payload['account']['positions'][0]['event_time_ns']==str(NOW-100*NS)


@pytest.mark.parametrize('field,value,reason',[
    ('margin_type',None,'POSITION_MARGIN_MODE_MISMATCH'),
    ('margin_type','CROSS','POSITION_MARGIN_MODE_MISMATCH'),
    ('leverage',None,'POSITION_LEVERAGE_MISMATCH'),
    ('leverage','4','POSITION_LEVERAGE_MISMATCH'),
    ('isolated_balance',None,'ISOLATED_MARGIN_UNVERIFIED'),
    ('isolated_im',None,'ISOLATED_MARGIN_UNVERIFIED'),
    ('isolated_mm',None,'ISOLATED_MARGIN_UNVERIFIED'),
    ('isolated_balance','-1','ISOLATED_MARGIN_INVALID'),
    ('event_time_ns',None,'POSITION_TIME_UNVERIFIED')])
def test_aggregate_account_mode_never_fills_missing_or_different_position_fields(field,value,reason):
    terms,payload=position_input()
    payload['account']['positions'][0][field]=value
    result=review(terms,payload)
    assert result['action']=='CLOSE' and result['halt_new_entries'] and reason in result['reasons']


@pytest.mark.parametrize('level',['config','position'])
def test_leverage_change_is_detected_without_automatically_changing_exchange_settings(level):
    terms,payload=position_input()
    if level=='config':
        payload['configurations'][1]['leverage']='4'
    else:payload['account']['positions'][0]['leverage']='4'
    result=review(terms,payload)
    assert result['action']=='CLOSE'
    if level=='config':assert 'CONFIGURATION_CHANGED_DURING_READ' in result['reasons']
    else:assert 'POSITION_LEVERAGE_MISMATCH' in result['reasons']


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('remaining',['0','0.5'])
def test_a_flat_or_smaller_position_between_reads_requests_order_recheck_before_halt(direction,remaining):
    terms,payload=position_input(direction)
    if remaining=='0':payload['account']['positions']=[]
    else:payload['account']['positions'][0]['size']=remaining if direction=='LONG' else '-'+remaining
    result=review(terms,payload)
    assert result['action']=='RECHECK' and not result['halt_new_entries']
    assert result['reasons']==['POSITION_SIZE_CHANGED']


@pytest.mark.parametrize('change', ['external','multiple','other_collateral','collateral_limit','loss'])
def test_foreign_activity_capital_and_equity_loss_are_separate_explicit_conditions(change):
    terms,payload=position_input()
    if change=='external':payload['account']['positions'][0]['instrument']='ETH_USDT_Perp'
    elif change=='multiple':payload['account']['positions'].append(deepcopy(payload['account']['positions'][0]))
    elif change=='other_collateral':payload['account']['balances'].append({'currency':'BTC','balance':'1'})
    elif change=='collateral_limit':payload['account']['total_equity']='1001'
    else:payload['account']['total_equity']='95'
    result=review(terms,payload,settled_net='-5')
    assert result['halt_new_entries']
    assert result['action']==('REVIEW' if change in ('external','multiple') else 'CLOSE')
    if change=='loss':
        assert result['reasons']==['LOSS_LIMIT'] and result['settled_plus_current_equity_change']=='-10'


@pytest.mark.parametrize('change',['wrong_identity','stale','future_account','regressed','wrong_subaccount'])
def test_identity_and_freshness_are_verified_before_using_private_values(change):
    terms,payload=position_input()
    if change=='wrong_identity':payload['account_ref']='b'*64
    elif change=='stale':payload['configurations'][0]['requested_at_ns']=str(NOW-6*NS)
    elif change=='future_account':payload['account']['event_time_ns']=str(NOW+3*NS)
    elif change=='regressed':payload['configurations'][1]['requested_at_ns']=str(NOW-2*NS)
    else:payload['account']['sub_account_id']='123'
    with pytest.raises(ValueError):review(terms,payload)


def test_balance_projection_discards_unused_provider_columns_but_requires_real_values():
    raw={'sub_account_id':'123456','event_time':str(NOW),'settle_currency':'USDT',
        'margin_type':'SIMPLE_CROSS_MARGIN','sub_account_mode':'SINGLE_ASSET_MODE','total_equity':'100',
        'spot_balances':[{'currency':'USDT','balance':'100','private_unused':'discard'}],'positions':[]}
    value=position_account(raw,requested_at_ns=NOW,received_at_ns=NOW)
    assert value.balances[0].payload()=={'currency':'USDT','balance':'100'}
    raw['spot_balances'][0].pop('balance')
    with pytest.raises(ValueError):position_account(raw,requested_at_ns=NOW,received_at_ns=NOW)


@pytest.mark.parametrize('fault',[None,'slow','revoked'])
def test_actual_client_collects_only_fresh_fixed_private_reads_and_checks_revocation(tmp_path,fault):
    from app.live_execution.grvt_perp_position import collect_position_review
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    from tests.grvt_perp_mainnet_fixture import perp_case
    case=perp_case(tmp_path)
    case.clock[0]=NOW
    terms,payload=position_input()
    position=payload['account']['positions'][0]
    case.settings['positions']=[{key:value for key,value in position.items() if key!='event_time_ns'}|
                                {'event_time':position['event_time_ns']}]
    allowed=[True]
    def config_hook(count):
        if fault=='slow':case.clock[0]+=3*NS
        if fault=='revoked':allowed[0]=False
    case.config_hook=config_hook
    def permission():
        if not allowed[0]:raise ValueError('Owned revocation')
    try:
        client=case.factory();salt='c'*64
        args={'identity_salt':salt,'expected_account_ref':client.identity_ref(salt),
              'require_permission':permission,'clock_ns':lambda:case.clock[0]}
        if fault is not None:
            with pytest.raises(GrvtMainnetError):collect_position_review(client,terms,**args)
            if fault=='revoked':assert not any(url.endswith('/account_summary') for url in case.calls)
        else:
            source=collect_position_review(client,terms,**args)
            result=review(terms,source.payload(),expected_account_ref=args['expected_account_ref'])
            assert result['action']=='HOLD'
        assert not case.sent
    finally:case.vault.lock()
