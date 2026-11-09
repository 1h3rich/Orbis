"""Owned public I/O, encrypted LAB origin and no PAPER account construction."""
import json

import pytest

from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from app.live_execution.grvt_perp_signals import PerpSignalSeed, advance_signals, replay_signals, signal_checkpoint
from tests.perp_observed_archive_fixture import NS, owned_archive
from tests.perp_observed_source_fixture import OwnedPublicAdapter


def source_fixture(tmp_path, monkeypatch, **kwargs):
    lab, paper, archive, origin, seed, clock = owned_archive(tmp_path, monkeypatch)
    adapter = OwnedPublicAdapter(seed, clock)
    source = GrvtPerpSignalSource(adapter=adapter, clock_ns=lambda:clock['now'], **kwargs)
    return source, adapter, origin, seed, clock, archive


def test_preparation_replays_current_lab_prefix_without_fictitious_funds_or_funding(tmp_path, monkeypatch):
    source, adapter, origin, old, clock, archive = source_fixture(tmp_path, monkeypatch)
    seed = source.prepare(origin)
    state = seed.start()
    assert [b.model_dump() for b in seed.history[:len(json.loads(origin.trade_prefix_json))]]==json.loads(origin.trade_prefix_json)
    assert seed.origin_reference_hash==origin.reference().sha256
    assert state.evaluated_bars==0 and len(state.history)==len(old.history)
    assert seed.execution_authority=='NONE'
    assert set(adapter.calls)=={'candles','quote'}
    assert archive.page()['total']==0


def test_public_collection_and_cold_replay_match_without_polling_paper(tmp_path, monkeypatch):
    source, adapter, origin, old, clock, archive = source_fixture(tmp_path, monkeypatch)
    seed = source.prepare(origin)
    state, events, decisions = seed.start(), [], []
    adapter.calls.clear()
    for seconds in range(10, 121, 10):
        clock['now'] = state.started_at_ns+seconds*NS
        if seconds%60==0:
            stamp=state.started_at_ns//NS+seconds-60
            adapter.rows[stamp]={**old.history[-1].model_dump(),'t':stamp}
        event=source.poll(state)
        result=advance_signals(state,event)
        state=result.state
        events.append(event)
        decisions.extend(json.loads(value) for value in result.decisions_json)
    assert state.evaluated_bars==2 and state.status=='RUNNING'
    assert adapter.calls.count('candles')==2 and set(adapter.calls)=={'candles','quote'}
    assert signal_checkpoint(replay_signals(seed,events).state)==signal_checkpoint(state)
    assert len(decisions)==2 and archive.page()['total']==0


@pytest.mark.parametrize('change', ['missing','duplicate','changed_overlap','open_candle'])
def test_provider_history_defects_fail_before_publishing_a_signal(tmp_path,monkeypatch,change):
    source,adapter,origin,old,clock,archive=source_fixture(tmp_path,monkeypatch)
    state=source.prepare(origin).start()
    stamp=state.started_at_ns//NS
    clock['now']=state.started_at_ns+60*NS
    adapter.rows[stamp]={**old.history[-1].model_dump(),'t':stamp}
    original=adapter.get_historical_candles
    def altered(*args,**kwargs):
        rows=original(*args,**kwargs)
        if change=='missing':return rows[:-1]
        if change=='duplicate':return rows+[rows[-1]]
        if change=='open_candle':return rows+[{**rows[-1],'t':stamp+60}]
        rows[0]['v']='999'
        return rows
    monkeypatch.setattr(adapter,'get_historical_candles',altered)
    with pytest.raises(ValueError,match='Velas públicas|histórico cerrado'):source.poll(state)
    assert state.evaluated_bars==0


def test_boundary_crossed_by_http_recollects_and_warms_all_past_bars(tmp_path,monkeypatch):
    source,adapter,origin,old,clock,archive=source_fixture(tmp_path,monkeypatch)
    stamp=clock['now']//NS
    adapter.rows[stamp]={**old.history[-1].model_dump(),'t':stamp}
    original=adapter.get_ticker
    def crossing(symbol):
        if adapter.calls.count('quote')==0:clock['now']+=60*NS
        return original(symbol)
    monkeypatch.setattr(adapter,'get_ticker',crossing)
    seed=source.prepare(origin)
    assert seed.history[-1].t==stamp and adapter.calls.count('quote')==2
    assert seed.start().evaluated_bars==0


@pytest.mark.parametrize('point', ['after_candles','after_quote'])
def test_revocation_during_public_http_prevents_publication_or_further_io(tmp_path,monkeypatch,point):
    permitted={'yes':True}
    def permission():
        if not permitted['yes']:raise RuntimeError('Owned revoked permission')
    source,adapter,origin,old,clock,archive=source_fixture(tmp_path,monkeypatch,require_permission=permission)
    method='get_historical_candles' if point=='after_candles' else 'get_ticker'
    original=getattr(adapter,method)
    def revoked(*args,**kwargs):
        result=original(*args,**kwargs)
        permitted['yes']=False
        return result
    monkeypatch.setattr(adapter,method,revoked)
    with pytest.raises(RuntimeError,match='revoked'):source.prepare(origin)
    assert adapter.calls[-1]==('candles' if point=='after_candles' else 'quote')
    assert archive.page()['total']==0


def test_lab_lock_or_changed_original_during_http_invalidates_preparation(tmp_path,monkeypatch):
    from app.lab.access import lab_access
    source,adapter,origin,old,clock,archive=source_fixture(tmp_path,monkeypatch)
    original=adapter.get_ticker
    def locked(symbol):
        result=original(symbol)
        lab_access.lock(origin.lab.path)
        return result
    monkeypatch.setattr(adapter,'get_ticker',locked)
    with pytest.raises(ValueError):source.prepare(origin)


def test_regressed_clock_future_venue_tick_and_expired_preparation_are_rejected(tmp_path,monkeypatch):
    source,adapter,origin,old,clock,archive=source_fixture(tmp_path,monkeypatch)
    source._checked()
    clock['now']-=1
    with pytest.raises(ValueError,match='Reloj'):source.prepare(origin)
    clock['now']+=1
    adapter.quote_override={'event_time_ns':clock['now']+NS}
    with pytest.raises(ValueError,match='futura'):source.prepare(origin)
    adapter.quote_override.clear()
    original=adapter.get_historical_candles
    def slow(*args,**kwargs):
        result=original(*args,**kwargs)
        clock['now']+=121*NS
        return result
    monkeypatch.setattr(adapter,'get_historical_candles',slow)
    with pytest.raises(ValueError):source.prepare(origin)


def test_production_grvt_codec_and_transport_only_use_public_candles_and_quotes(tmp_path,monkeypatch):
    source,owned,origin,old,clock,archive=source_fixture(tmp_path,monkeypatch)
    requests=[]
    monkeypatch.setattr('app.marketdata.adapters.grvt.time.time_ns',lambda:clock['now'])
    def post(url,body):
        assert url.startswith('https://market-data.grvt.io/full/v1/')
        endpoint=url.rsplit('/',1)[-1]
        requests.append(endpoint)
        assert body['instrument']==old.quote.instrument
        if endpoint=='ticker':
            quote=owned.get_ticker('BTC/USDT:PERP')
            return {'result':{'instrument':old.quote.instrument,'event_time':str(quote['event_time_ns']),
                'last_price':'100','mark_price':quote['mark_price'],
                'best_bid_price':quote['bid'],'best_ask_price':quote['ask']}}
        assert endpoint=='kline' and body['type']=='TRADE'
        return {'result':[{'instrument':old.quote.instrument,'open_time':str(stamp*NS),
            'close_time':str((stamp+60)*NS),'open':row['o'],'high':row['h'],
            'low':row['l'],'close':row['c'],'volume_b':row['v']}
            for stamp,row in sorted(owned.rows.items())
            if int(body['start_time'])<=stamp*NS<=int(body['end_time'])]}
    actual=GrvtPerpSignalSource(public_post=post,clock_ns=lambda:clock['now'])
    seed=actual.prepare(origin)
    state=seed.start()
    for seconds in range(10,61,10):
        clock['now']=state.started_at_ns+seconds*NS
        if seconds==60:
            stamp=state.started_at_ns//NS
            owned.rows[stamp]={**old.history[-1].model_dump(),'t':stamp}
        state=advance_signals(state,actual.poll(state)).state
    assert state.evaluated_bars==1
    assert set(requests)=={'kline','ticker'} and not archive.page()['items']


def test_json_seed_is_evidence_and_never_accepted_as_original_lab_capability(tmp_path,monkeypatch):
    source,adapter,origin,old,clock,archive=source_fixture(tmp_path,monkeypatch)
    seed=source.prepare(origin)
    assert PerpSignalSeed.model_validate(seed.payload()).execution_authority=='NONE'
    calls=len(adapter.calls)
    with pytest.raises(ValueError,match='original LAB verificado'):source.prepare(seed.payload())
    assert len(adapter.calls)==calls


def test_public_worker_records_before_current_real_budget_entry_with_owned_venue_io(tmp_path,monkeypatch):
    """Module integration, not a bot grant or GUI activation acceptance test."""
    from threading import Event
    from app.live_execution.grvt_perp_signal_worker import PerpSignalWorker
    from app.live_execution.grvt_perp_plan import PerpEntryTerms
    from app.live_execution.grvt_perp_submit import submit_perp_entry_once
    from app.live_execution.grvt_perp_signals import replay_signal_records
    from tests.grvt_perp_mainnet_fixture import perp_case
    from tests.test_grvt_mainnet_submit import arguments
    source,adapter,origin,old,clock,archive=source_fixture(tmp_path,monkeypatch)
    seed=source.prepare(origin)
    records=[]
    ready=Event()
    def waiter(stop,seconds):
        if len(records)>=7:
            ready.set()
            stop.wait(10)
        else:
            clock['now']+=10*NS
            if clock['now']% (60*NS)==0:
                stamp=clock['now']//NS-60
                adapter.rows[stamp]={**old.history[-1].model_dump(),'t':stamp}
    worker=PerpSignalWorker(seed,source=source,append_record=records.append,
        require_permission=lambda:None,clock_ns=lambda:clock['now'],waiter=waiter)
    case=perp_case(tmp_path/'own-real')
    try:
        worker.start()
        assert ready.wait(5)
        candidate=worker.candidate()
        assert candidate is not None and len(records)==7
        assert replay_signal_records(seed,records).state.matched_entries==1
        raw=case.terms.payload()
        raw.update(profile=seed.profile.payload(),definition=seed.definition,timeframe=seed.timeframe)
        terms=PerpEntryTerms.model_validate(raw)
        case.clock[0]=clock['now']
        args=arguments(case)
        args['confirmation']='ENVIAR_ENTRADA_PERPETUA_GRVT_MAINNET'
        def own_test_permission():
            assert worker.candidate()==candidate
        args['require_permission']=own_test_permission
        saved=submit_perp_entry_once(case.vault,terms,parent_client_order_id=str(2**63+19000),**args)
        plan=saved['claim']['preflight']['plan']
        assert plan['source']['terms']['profile']==seed.profile.payload()
        assert plan['source']['terms']['definition']['initial_capital']=='1000'
        assert plan['result']['account_equity']=='100' and plan['result']['entry_cash_budget']=='25'
        assert len(case.sent)==1 and saved['status']=='ACKNOWLEDGED'
        assert saved['authorization'] is False
        # This fixture calls the protected-entry kernel with its own explicit
        # RAM permission. The pending production grant/coordinator is not
        # substituted with PAPER rows or this test callback.
        assert archive.page()['total']==0
    finally:
        worker.stop()
        assert worker.join(5)
        case.vault.lock()
