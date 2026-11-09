"""Model-specific preflight has no legacy half-notional rule or authority."""
from decimal import Decimal as D

import pytest

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_preflight import verify_mainnet_preflight
from app.live_execution.grvt_perp_plan import PerpEntryPlan,plan_draft
from app.live_execution.grvt_perp_preflight import verify_perp_preflight
from tests.grvt_perp_mainnet_fixture import perp_case,preflight


@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_private_config_and_real_cash_produce_fresh_reproducible_protected_entry(tmp_path,mode,direction):
    case=perp_case(tmp_path,mode=mode,direction=direction)
    proof=preflight(case);plan=PerpEntryPlan.model_validate(proof['plan']);draft=plan_draft(plan)
    assert len(case.config_calls)==2 and not case.sent
    assert D(proof['states'][-1]['total_equity'])==100
    assert draft.size*draft.entry_price>100  # Leveraged by profile, not old half-notional scope.
    assert plan.result['account_equity']=='100' and plan.source.terms.definition['initial_capital']=='1000'
    assert proof['configurations'][-1]['margin_type']==mode
    verify_perp_preflight(proof,draft,now_ns=case.clock[0])
    with pytest.raises(GrvtMainnetError):verify_perp_preflight(proof,draft,now_ns=case.clock[0]+5_000_000_001)
    assert 'synthetic-mainnet-api-key' not in str(proof)
    case.vault.lock()


@pytest.mark.parametrize('fault',['config_mode','config_leverage','balance','rules','instrument','liquidity','mark','old_account','old_quote'])
def test_changed_or_unsupported_account_and_market_are_rejected(tmp_path,fault):
    case=perp_case(tmp_path)
    if fault in ('config_mode','config_leverage'):
        case.config_hook=lambda count:case.config.update(**({'margin_type':'CROSS'} if fault=='config_mode' else {'leverage':'10'})) if count==2 else None
    elif fault=='balance':
        case.config_hook=lambda count:case.settings.update(account_balance='90',account_equity='90') if count==2 else None
    elif fault in ('rules','instrument'):
        original=case.public;counts=[0]
        def post(url,body):
            reply=original(url,body)
            if url.endswith('/'+('margin_rules' if fault=='rules' else 'instrument')):
                counts[0]+=1
                if counts[0]==2:
                    import httpx
                    raw=reply.json()
                    if fault=='rules':raw['risk_brackets'][0]['initial_margin_rate']='0.03'
                    else:raw['result']['instrument_hash']='123456788'
                    return httpx.Response(200,request=httpx.Request('POST',url),json=raw)
            return reply
        case.public=post
    elif fault=='mark':case.mark='NaN'
    elif fault=='liquidity':
        original=case.public
        def post(url,body):
            reply=original(url,body)
            if url.endswith('/ticker'):
                import httpx
                raw=reply.json();raw['result']['best_ask_size']='0.001'
                return httpx.Response(200,request=httpx.Request('POST',url),json=raw)
            return reply
        case.public=post
    elif fault=='old_account':case.clock[0]+=6_000_000_000;case.settings['account_time']=case.clock[0]-6_000_000_000
    elif fault=='old_quote':
        original=case.public
        def post(url,body):
            reply=original(url,body)
            if url.endswith('/ticker'):
                import httpx
                raw=reply.json();raw['result']['event_time']=str(case.clock[0]-6_000_000_000)
                return httpx.Response(200,request=httpx.Request('POST',url),json=raw)
            return reply
        case.public=post
    with pytest.raises(GrvtMainnetError):preflight(case)
    assert not case.sent
    case.vault.lock()


def test_rehashing_a_plan_cannot_change_bound_budget_or_account_inputs(tmp_path):
    from app.live_execution.grvt_perp_plan import PerpPlanInput,build_perp_plan
    case=perp_case(tmp_path);proof=preflight(case)
    original=PerpEntryPlan.model_validate(proof['plan']);source=original.source.payload()
    source['account_equity']='200'
    forged=build_perp_plan(PerpPlanInput.model_validate(source))
    proof['plan']=forged.payload()
    with pytest.raises(GrvtMainnetError):verify_perp_preflight(proof,plan_draft(forged),now_ns=case.clock[0])
    case.vault.lock()


def test_revocation_after_config_read_stops_before_the_next_private_request(tmp_path):
    case=perp_case(tmp_path);allowed=[True]
    case.config_hook=lambda count:allowed.__setitem__(0,False)
    def checked():
        if not allowed[0]:raise ValueError('revoked')
        return case.clock[0]
    with pytest.raises(GrvtMainnetError):preflight(case,checked=checked)
    assert len(case.config_calls)==1
    assert not any(url.endswith('/account_summary') for url in case.calls)
    assert not case.sent
    case.vault.lock()
