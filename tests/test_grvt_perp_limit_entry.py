"""Owned signed GTT contracts, independent cash and partial-fill arithmetic."""
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from eth_account import Account

from app.derivatives.perp_observed_wire import observation_input
from app.lab.access import lab_access
from app.lab.store import content_hash
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError, _message
from app.live_execution.grvt_order_codec import order_signable
from app.live_execution.grvt_perp_limit_origin import verified_limit_paper_origin, verified_partial_paper_origin
from app.live_execution.grvt_perp_limit_plan import PerpLimitPlanInput, build_limit_plan, limit_plan_from_signal, unsigned_limit_entry
from app.live_execution.grvt_perp_limit_preflight import collect_limit_preflight, verify_limit_preflight
from app.live_execution.grvt_perp_limit_receipt import (
    PerpLimitContract, PerpLimitReceiptInput, collect_limit_observations, reconcile_limit_entry,
)
from app.live_execution.grvt_perp_plan import PerpMarketInput
from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from app.live_execution.grvt_perp_signals import PerpLimitSignalSeed, PerpPartialSignalSeed, record_signal_input
from app.research.perp_reference import PerpLimitResearchReference
from tests.test_grvt_perp_plan import NOW, plan_source
from tests.test_perp_limit_simulation import limit_profile
from tests.grvt_perp_mainnet_fixture import perp_case
from tests.perp_observed_archive_fixture import LAB_KEY, owned_archive
from tests.perp_observed_fixture import bar, batch
from tests.perp_observed_source_fixture import OwnedPublicAdapter


NS = 10**9
CID = str(2**63 + 57)


def source(side='LONG', *, origin=None, definition=None):
    raw = plan_source(side).payload()
    strategy = raw['terms']['definition'] if definition is None else definition
    at = NOW//NS
    start = at-61*3600
    end = at-2*3600
    profile = limit_profile(strategy, offset='0.02')
    if origin is None:
        ref = {
            'schema':'orbis.research.perp.reference.v2', 'family':'PERPETUAL',
            'artifact_id':'11111111-1111-4111-8111-111111111111',
            'dataset_id':'22222222-2222-4222-8222-222222222222',
            'profile_id':'33333333-3333-4333-8333-333333333333',
            'artifact_hash':'a'*64, 'dataset_hash':'b'*64,
            'created_at':'2026-11-07T00:00:00+00:00',
            'profile':profile.payload(), 'profile_hash':content_hash(profile.payload()),
            'symbol':'BTC/USDT:PERP', 'timeframe':'1h', 'dataset_from_t':start,
            'dataset_to_t':end, 'method':'CHRONOLOGICAL_HOLDOUT', 'validation_engine':'perp-validation-2.0',
            'simulation_engine':'perp-ohlc-2.0', 'mode':'EXPERIMENTAL_CURRENT_RULES',
            'windows':[{'from_t':end-29*3600,'to_t':end,'bars':30,'result_hash':'c'*64}],
            'sample_bars':30, 'warnings':[], 'execution_authority':'NONE',
        }
        origin = PerpLimitResearchReference.model_validate({**ref,'sha256':content_hash(ref)})
    step = 60 if origin.timeframe == '1m' else 3600
    raw.update(
        terms={'schema':'orbis.grvt.perp.limit.entry.terms.v1',
               'research':origin.model_dump(mode='json',by_alias=True),'definition':strategy,
               'budget':raw['terms']['budget'],'max_pending_seconds':30,
               'paper':{'session_id':'44444444-4444-4444-8444-444444444444','sequence':12,
                        'head_hash':'e'*64,'seed_hash':'f'*64,'origin_hash':'a'*64,'stored_status':'STOPPED',
                        'started_at_ns':str(NOW-7200*NS),'last_received_at_ns':str(NOW-60*NS),
                        'observed_bars':2,'closed_trades':1,'has_estimated_closure':False,'source_advanced':False}},
        signal_candle={'t':at-step,'o':'99','h':'102','l':'98','c':'100','v':'10'},
        signal_known_at_ns=str(NOW), signal_decision_hash='d'*64, entry_deadline_ns=str(NOW+60*NS),
    )
    return PerpLimitPlanInput.model_validate(raw)


def signed_contract(side='LONG', *, chosen=None):
    plan = build_limit_plan(chosen or source(side))
    wire = unsigned_limit_entry(plan, client_order_id=CID, nonce=17, issued_at_ns=NOW)
    signer = Account.from_key(bytes.fromhex('1f'*32))
    message = _message(wire, plan.source.instrument.instrument())
    signature = signer.sign_message(order_signable(message,chain_id=325))
    wire['signature'].update(signer=signer.address,r='0x'+signature.r.to_bytes(32,'big').hex(),
                             s='0x'+signature.s.to_bytes(32,'big').hex(),v=signature.v)
    return PerpLimitContract(schema_name='orbis.grvt.perp.limit.entry.contract.v1',plan=plan,
        client_order_id=CID,nonce=17,issued_at_ns=str(NOW),expected_signer=signer.address,wire=wire)


def read(contract, *, status, filled, update, received, reason='UNSPECIFIED'):
    remaining = Decimal(contract.plan.result['size']) - Decimal(filled) if status == 'OPEN' else Decimal(0)
    return {'received_at_ns':str(NOW+received*NS),'order':{
        **deepcopy(contract.wire),'order_id':'0x555',
        'state':{'status':status,'reject_reason':reason,'update_time':str(NOW+update*NS),
                 'traded_size':[filled],'book_size':[str(remaining)]},
    }}


def fill(side='LONG', **changes):
    row = {'sub_account_id':'123456','trade_id':'trade-1','order_id':'0x555','client_order_id':CID,
        'event_time':str(NOW+4*NS),'is_buyer':side=='LONG','is_taker':False,'size':'0.300',
        'price':'97.99' if side=='LONG' else '102.01','fee':'0','fee_currency':'USDT',
        'realized_pnl':'0','builder_fee':'0','is_liquidation':False,'instrument':'BTC_USDT_Perp'}
    return {**row, **changes}


def evidence(contract, *, side='LONG'):
    return {'schema':'orbis.grvt.perp.limit.entry.observations.v1',
        'first':read(contract,status='OPEN',filled='0.300',update=5,received=6),
        'last':read(contract,status='CANCELLED',filled='0.300',update=11,received=12,reason='CLIENT_CANCEL'),
        'fill_cutoff_ns':str(NOW+10*NS),'fill_history_drained':True,'fills':[fill(side)]}


@pytest.mark.parametrize(('side','bound','actual'),[('LONG','98','29.397'),('SHORT','102','30.603')])
def test_cancelled_limit_keeps_exact_confirmed_partial_exposure(side,bound,actual):
    contract = signed_contract(side)
    plan = contract.plan
    assert Decimal(plan.result['limit_price']) == Decimal(bound)
    assert Decimal(plan.result['size']) == Decimal('1.249')
    assert plan.result['cash_required_estimate'] == '24.980000000000000002'
    assert contract.wire['time_in_force'] == 'GOOD_TILL_TIME'
    assert contract.wire['signature']['expiration'] == str(NOW+30*NS)
    result = reconcile_limit_entry(contract, PerpLimitReceiptInput.model_validate(evidence(contract,side=side)))
    assert result['phase'] == 'ENTRY_TERMINAL_WITH_FILLS' and result['terminal']
    assert result['filled_size'] == '0.3' and result['entry_notional'] == actual
    assert result['execution_authority'] == 'NONE' and not result['policy_breaches']


def test_timeout_requires_terminal_proof_even_when_grvt_reports_no_fills():
    contract = signed_contract()
    raw = {'schema':'orbis.grvt.perp.limit.entry.observations.v1',
           'first':read(contract,status='OPEN',filled='0',update=2,received=33),
           'last':read(contract,status='OPEN',filled='0',update=2,received=35),
           'fill_cutoff_ns':str(NOW+34*NS),'fill_history_drained':True,'fills':[]}
    check = lambda: reconcile_limit_entry(contract, PerpLimitReceiptInput.model_validate(raw))
    assert check()['phase'] == 'AWAIT_TERMINAL_PROOF' and not check()['terminal']
    raw['last'] = read(contract,status='CANCELLED',filled='0',update=34,received=35,reason='EXPIRED')
    assert check()['phase'] == 'ENTRY_TERMINAL_EMPTY' and check()['terminal']


def test_better_sell_price_can_exceed_usdt_cap_and_remains_recorded():
    chosen = source('SHORT').payload()
    chosen['terms']['budget'].update(entry_notional_limit='100', exit_notional_limit='110')
    contract = signed_contract(chosen=PerpLimitPlanInput.model_validate(chosen))
    raw = evidence(contract,side='SHORT')
    raw['fills'] = [fill('SHORT',price='400',fee='0.12')]
    result = reconcile_limit_entry(contract, PerpLimitReceiptInput.model_validate(raw))
    assert result['entry_notional'] == '120' and result['filled_size'] == '0.3'
    assert result['policy_breaches'] == ['ENTRY_NOTIONAL_EXCEEDED','EXIT_NOTIONAL_EXCEEDED_AT_ENTRY','FEE_CEILING_EXCEEDED']


def test_real_read_port_drains_history_and_keeps_late_fill_evidence(tmp_path):
    case = perp_case(tmp_path)
    contract = signed_contract()
    case.clock[0] = NOW+36*NS
    case.settings['orders'][CID] = read(contract,status='CANCELLED',filled='0.300',update=34,received=36,
                                         reason='EXPIRED')['order']
    case.settings['history_pages']['fill_history'] = {
        '':{'result':[],'next':'second'},
        'second':{'result':[fill(event_time=str(NOW+31*NS))],'next':''},
    }
    client = case.factory()
    salt = 'a'*64
    options = {'identity_salt':salt,'account_ref':client.identity_ref(salt),
               'clock_ns':lambda:case.clock[0],'require_permission':lambda:None}
    proof = collect_limit_observations(client, contract, **options)
    result = reconcile_limit_entry(contract, proof)
    assert result['filled_size'] == '0.3' and result['phase'] == 'ENTRY_TERMINAL_WITH_FILLS'
    assert result['policy_breaches'] == ['FILL_AT_OR_AFTER_EXPIRY']
    pages = case.settings['history_requests']
    assert [body['cursor'] for _,body in pages] == ['', 'second']
    assert all(body['start_time'] == str(NOW-1) and body['end_time'] == str(NOW+36*NS) for _,body in pages)
    assert not case.sent and not case.settings['cancel_posts'] and not case.settings['reduction_posts']
    with pytest.raises(ValueError, match='Identidad'):
        collect_limit_observations(client, contract, **{**options,'account_ref':'0'*64})
    case.settings['history_pages']['fill_history']['second']['next'] = 'second'
    with pytest.raises(ValueError, match='Paginación'):
        collect_limit_observations(client, contract, **options)


@pytest.mark.parametrize('change', ['PRICE','SIZE','DUPLICATE','CUTOFF','REPLACED','REGRESSION','PAGE','TRIGGER'])
def test_incomplete_or_incompatible_proof_is_refused(change):
    contract = signed_contract()
    raw = evidence(contract)
    if change == 'PRICE':
        raw['fills'][0]['price'] = '98.01'
    elif change == 'SIZE':
        raw['fills'][0]['size'] = '0.2'
    elif change == 'DUPLICATE':
        raw['fills'].append(raw['fills'][0])
    elif change == 'CUTOFF':
        raw['fills'][0]['event_time'] = str(NOW+11*NS)
    elif change == 'REPLACED':
        raw['last']['order']['order_id'] = '0x556'
    elif change == 'REGRESSION':
        raw['last']['order']['state']['update_time'] = str(NOW+4*NS)
    elif change == 'TRIGGER':
        raw['last']['order']['metadata']['trigger'] = {'trigger_type':'STOP_LOSS'}
    else:
        raw['fill_history_drained'] = False
    with pytest.raises(ValueError):
        reconcile_limit_entry(contract, PerpLimitReceiptInput.model_validate(raw))


def test_tamper_and_expiry_cannot_be_covered_by_a_recomputed_digest():
    chosen = source()
    plan = build_limit_plan(chosen)
    with pytest.raises(ValueError,match='Vigencia'):
        unsigned_limit_entry(plan,client_order_id=CID,nonce=17,issued_at_ns=NOW+6*NS)
    contract = signed_contract()
    altered = contract.payload()
    altered['wire']['signature']['expiration'] = str(NOW+31*NS)
    with pytest.raises(ValueError,match='firmado'):
        PerpLimitContract.model_validate(altered)
    altered = contract.payload()
    altered['plan']['result']['limit_price'] = '110'
    payload = {key:value for key,value in altered['plan'].items() if key!='sha256'}
    altered['plan']['sha256'] = content_hash(payload)
    with pytest.raises(ValueError,match='reproducible'):
        PerpLimitContract.model_validate(altered)


def completed_limit_trial(tmp_path, monkeypatch, *, direction='LONG', partial=False, optimized=False):
    targets = [{'target':'0.02','fraction':'0.25'},{'target':'0.05','fraction':'0.35'}] if partial else None
    lab, paper, archive, lab_origin, paper_seed, clock = owned_archive(
        tmp_path,monkeypatch,limited=True,direction=direction,partial_targets=targets,optimized=optimized,
        strategy_changes={'stop_loss':'0.03','take_profit':'0.08'} if partial else None)
    session = archive.create(lab_origin, paper_seed, request_id=str(uuid4()))['id']
    verify = verified_partial_paper_origin if partial else verified_limit_paper_origin
    with pytest.raises(ValueError, match='Completa'):
        verify(lab, archive, session)
    start = int(paper_seed.received_at_ns)
    first = bar(start//NS,direction)

    def advance(seconds, **values):
        current = archive.current_state(session)
        incoming = observation_input(batch(current, seconds, **values))
        clock['now'] = int(incoming.received_at_ns)
        return archive.append(session, incoming, request_id=str(uuid4()))

    for second in range(10, 61, 10):
        advance(second, bars=(first,) if second == 60 else ())
    entry_bid, entry_ask = ('98','99') if direction == 'LONG' else ('101','102')
    advance(70,bid=entry_bid,ask=entry_ask)
    if partial:
        first_exit = ('106','107') if direction == 'LONG' else ('93','94')
        advance(80,bid=first_exit[0],ask=first_exit[1])
    clock['now'] = start+(81 if partial else 71)*NS
    archive.control(session, 'CLOSE_REQUEST', request_id=str(uuid4()))
    close_bid, close_ask = ('103','104') if direction == 'LONG' else ('96','97')
    advance(90 if partial else 80,bid=close_bid,ask=close_ask)
    trial = verify(lab, archive, session)
    budget = plan_source().terms.budget
    terms = (trial.limit_terms(budget,max_pending_seconds=30,max_attempts=6,reserved_close_attempts=2)
             if partial else trial.terms(budget,max_pending_seconds=30))
    assert terms.paper.closed_trades == 1 and terms.paper.stored_status == 'STOPPED'
    assert terms.paper.head_hash == archive.read(session)['session']['head_hash']
    assert terms.budget.entry_cash_limit == '25' and terms.definition['initial_capital'] == '1000'

    adapter = OwnedPublicAdapter(paper_seed, clock)
    adapter.rows[first.t] = first.json()
    real_feed = GrvtPerpSignalSource(adapter=adapter, clock_ns=lambda:clock['now'])
    seed = real_feed.prepare_partial(trial,terms.exit_terms()) if partial else real_feed.prepare(trial.origin)
    assert isinstance(seed, PerpPartialSignalSeed if partial else PerpLimitSignalSeed)
    state = seed.start()
    assert state.matched_entries == 0
    signal_bar = bar(start//NS+60,direction)
    adapter.rows[signal_bar.t] = signal_bar.json()
    records = []
    for second in ((100,110,120) if partial else (90,100,110,120)):
        clock['now'] = start+second*NS
        transition, record = record_signal_input(state, real_feed.poll(state))
        records.append(record)
        state = transition.state
    assert records[-1]['decisions'][0]['entry_matched'] is True
    return lab, trial, terms, seed, records, clock, signal_bar


def test_completed_paper_origin_and_new_signal_feed_are_bound_to_limit_plan(tmp_path, monkeypatch):
    lab, trial, terms, seed, records, clock, signal_bar = completed_limit_trial(tmp_path, monkeypatch)
    raw = plan_source().payload()
    raw.pop('terms')
    now = str(clock['now'])
    raw.update(checked_at_ns=now, quote={**raw['quote'],'event_time_ns':now})
    for name in ('rules','specification'):
        raw[name]['fetched_at_ns'] = now
    market = PerpMarketInput.model_validate(raw)
    deadline = str(clock['now']+10*NS)
    plan = limit_plan_from_signal(terms, market, seed, records, entry_deadline_ns=deadline)
    assert plan.source.terms.paper == terms.paper
    assert plan.source.signal_candle.t == signal_bar.t
    assert plan.source.signal_decision_hash == records[-1]['decisions'][0]['sha256']
    assert plan.result['limit_price'] == '99.99' and plan.result['expires_at_ns'] == deadline
    case = perp_case(tmp_path)
    case.clock[0] = clock['now']
    def collect():
        return collect_limit_preflight(case.factory(), terms, seed, records, entry_deadline_ns=deadline,
                                       public_post=case.public, clock_ns=lambda:case.clock[0])
    proof = collect()
    verified = verify_limit_preflight(proof, terms, seed, records, entry_deadline_ns=deadline, now_ns=case.clock[0])
    assert verified.result == plan.result and len(case.config_calls) == 2 and not case.sent
    assert verified.source.account_equity == '100'
    changed_market = deepcopy(proof)
    changed_market['market']['states'][1]['open_order_count'] = 1
    with pytest.raises(ValueError):
        verify_limit_preflight(changed_market, terms, seed, records, entry_deadline_ns=deadline, now_ns=case.clock[0])
    case.config['leverage'] = '4'
    with pytest.raises(GrvtMainnetError, match='Preflight LIMIT'):
        collect()
    altered = deepcopy(records)
    altered[-1]['decisions'][0]['known_at_ns'] = str(clock['now']+1)
    altered[-1]['sha256'] = content_hash({key:value for key,value in altered[-1].items() if key!='sha256'})
    with pytest.raises(ValueError, match='replay causal'):
        limit_plan_from_signal(terms, market, seed, altered, entry_deadline_ns=deadline)
    lab_access.lock(lab.path)
    lab_access.unlock(lab.path, LAB_KEY)
    with pytest.raises(ValueError, match='sesión LAB cambió'):
        trial.terms(terms.budget, max_pending_seconds=30)
