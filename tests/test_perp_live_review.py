"""Full original replay, independent LIVE settings and revocation during HTTP."""
from uuid import uuid4

import pytest

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.perp_observed_archive import ObservedArchive
from app.derivatives.perp_observed_wire import CloseEvent, observation_input
from app.integrations.grvt_position_config import configuration_response
from app.lab.access import lab_access
from app.lab.store import content_hash, encoded
from app.research.perp_live_review import review_perp_live, PerpLiveReview
from tests.perp_observed_archive_fixture import owned_archive, PAPER_KEY, NS
from tests.perp_observed_fixture import batch, bar


def uid():return str(uuid4())


def reviewed_case(tmp_path,monkeypatch,*,complete=True):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    id=archive.create(origin,seed,request_id=uid())['id']
    if complete:
        initial=seed.start().state
        for second in range(10,61,10):
            event=observation_input(batch(initial,second,bars=(bar(initial.started_at_ns//NS),) if second==60 else ()))
            clock['now']=int(event.received_at_ns);archive.append(id,event,request_id=uid())
        clock['now']+=NS
        archive.append(id,CloseEvent(schema_name='orbis.paper.perp.observed.input.v1',kind='CLOSE_REQUEST',
            received_at_ns=str(clock['now'])),request_id=uid())
        state=archive.current_state(id)
        event=observation_input(batch(state,70,bid='103',ask='104'))
        clock['now']=int(event.received_at_ns);archive.append(id,event,request_id=uid())
    class Client:
        calls=0
        hook=None
        row={'instrument':'BTC_USDT_Perp','margin_type':seed.profile.margin_mode,'leverage':seed.profile.leverage,
             'min_leverage':'1','max_leverage':'50'}
        def position_configuration(self,sub,instrument):
            self.calls+=1
            if self.hook:self.hook(self.calls)
            return configuration_response({'results':[self.row]},sub,instrument,
                requested_at_ns=clock['now'],received_at_ns=clock['now'])
    return lab,paper,archive,id,clock,Client()


def review(case,**kwargs):
    lab,paper,archive,id,clock,client=case
    return review_perp_live(lab,archive,id,'123',client,clock_ns=lambda:clock['now'],**kwargs)


def test_complete_observed_source_and_current_account_configuration_match_without_authority(tmp_path,monkeypatch):
    case=reviewed_case(tmp_path,monkeypatch)
    lab,paper,archive,id,clock,client=case
    before=archive.read(id)
    result=review(case)
    assert result.blockers==['PERP_EXECUTOR_NOT_IMPLEMENTED']
    assert result.paper.closed_trades==1 and result.paper.observed_bars==1
    assert result.paper.head_hash==before['session']['head_hash']
    assert result.paper.stored_status=='STOPPED' and client.calls==2
    assert result.execution_authority=='NONE' and result.execution_supported is False
    assert result.sizing=='REQUIRES_SEPARATE_REAL_BUDGET'
    assert result.research.profile.payload()==before['origin']['reference']['profile']
    assert archive.read(id)==before
    # No PAPER capital/account/checkpoint, cookie or signer is copied into REAL.
    raw=result.model_dump(mode='json',by_alias=True)
    assert 'cash' not in str(raw) and 'account_id' not in raw and 'account' not in raw['paper']
    assert PerpLiveReview.model_validate(raw)==result
    raw['configurations'][1]['margin_type']='ISOLATED' if client.row['margin_type']=='CROSS' else 'CROSS'
    raw['sha256']=content_hash({key:value for key,value in raw.items() if key!='sha256'})
    with pytest.raises(ValueError):PerpLiveReview.model_validate(raw)


def test_research_replays_cold_without_reattaching_and_returns_missing_paper_requirements(tmp_path,monkeypatch):
    case=list(reviewed_case(tmp_path,monkeypatch,complete=False))
    case[2]=ObservedArchive(case[1],clock_ns=lambda:case[4]['now'])
    result=review(case)
    assert result.paper.sequence==0 and result.paper.closed_trades==0
    assert result.blockers==['NO_OBSERVED_CLOSED_TRADE','NO_OBSERVED_CANDLE','PERP_EXECUTOR_NOT_IMPLEMENTED']
    assert not case[2].active_ids()


def test_account_mode_leverage_and_limits_mismatch_are_explicit(tmp_path,monkeypatch):
    case=reviewed_case(tmp_path,monkeypatch)
    client=case[-1];client.row={**client.row,'margin_type':'ISOLATED' if client.row['margin_type']=='CROSS' else 'CROSS',
        'leverage':'1','max_leverage':'1'}
    result=review(case)
    assert {'MARGIN_MODE_MISMATCH','LEVERAGE_MISMATCH','LEVERAGE_OUTSIDE_ACCOUNT_RANGE'}<=set(result.blockers)


def test_gap_and_estimated_closure_cannot_supply_observed_proof(tmp_path,monkeypatch):
    from tests.perp_gap_close_fixture import owned_gap
    paper,archive,close_store,id,clock,adapter=owned_gap(tmp_path,monkeypatch)
    # owned_gap uses the same separately encrypted local LAB source.
    from app.lab.store import LabStore
    lab=LabStore(tmp_path/'lab.db')
    state=archive.verified_inactive(id)[1].state
    class Client:
        def position_configuration(self,sub,instrument):
            return configuration_response({'results':[{'instrument':instrument,'margin_type':state.profile.margin_mode,
                'leverage':state.profile.leverage,'min_leverage':'1','max_leverage':'50'}]},sub,instrument,
                requested_at_ns=clock['now'],received_at_ns=clock['now'])
    close_store.close(id,request_id=uid(),expected_head=archive.read(id)['session']['head_hash'])
    result=review_perp_live(lab,archive,id,'123',Client(),clock_ns=lambda:clock['now'])
    assert 'PAPER_GAP' in result.blockers and 'ESTIMATED_CLOSURE_EXCLUDED' in result.blockers
    assert result.paper.closed_trades==0  # Estimated trade never enters this count.


@pytest.mark.parametrize('change',['paper_lock','paper_reauth','lab_lock','lab_changed','source_changed','permission','changed_config'])
def test_revoked_or_changed_source_during_private_http_never_returns_a_review(tmp_path,monkeypatch,change):
    case=reviewed_case(tmp_path,monkeypatch)
    lab,paper,archive,id,clock,client=case;allowed=[True]
    def hook(count):
        if count!=1:return
        if change=='paper_lock':paper_perp_access.lock(paper.path)
        elif change=='paper_reauth':paper_perp_access.unlock(paper.path,PAPER_KEY)
        elif change=='lab_lock':lab_access.lock(lab.path)
        elif change=='lab_changed':
            with lab.connect() as db:db.execute("UPDATE strategy_versions SET definition_json='{}'")
        elif change=='source_changed':
            with paper.connect() as db:db.execute("UPDATE paper_observed_sessions SET runtime_id=? WHERE id=?",(uid(),id))
        elif change=='permission':allowed[0]=False
        elif change=='changed_config':client.row={**client.row,'leverage':'1'}
    # For changing-config, change only the second read, after returning the first.
    client.hook=hook if change!='changed_config' else lambda count:hook(1) if count==2 else None
    def checked():
        if not allowed[0]:raise ValueError('revoked')
    with pytest.raises(ValueError):review(case,require_permission=checked)


def test_append_during_network_keeps_pinned_prefix_but_requires_fresh_review(tmp_path,monkeypatch):
    case=reviewed_case(tmp_path,monkeypatch,complete=False)
    lab,paper,archive,id,clock,client=case
    before=archive.read(id)['session']
    def hook(count):
        if count==1:
            clock['now']+=NS
            event=observation_input(batch(archive.current_state(id),1))
            archive.append(id,event,request_id=uid())
    client.hook=hook
    result=review(case)
    assert result.paper.sequence==before['sequence'] and result.paper.head_hash==before['head_hash']
    assert result.paper.source_advanced and 'SOURCE_ADVANCED' in result.blockers


def test_rehashed_financial_checkpoint_corruption_is_replayed_before_private_http(tmp_path,monkeypatch):
    from app.derivatives.perp_observed_archive_format import unpack,pack,event_hash
    case=reviewed_case(tmp_path,monkeypatch,complete=False)
    lab,paper,archive,id,clock,client=case
    with paper.connect() as db:
        row=db.execute('SELECT * FROM paper_observed_events WHERE session_id=?',(id,)).fetchone()
        source=unpack(row['input_blob']);evidence=unpack(row['evidence_blob']);evidence['checkpoint']['account']['cash']='999999'
        fake=event_hash(id,0,None,source,evidence)
        db.execute('UPDATE paper_observed_events SET evidence_blob=?,hash=? WHERE session_id=?',(pack(evidence),fake,id))
        db.execute('UPDATE paper_observed_sessions SET checkpoint_json=?,head_hash=? WHERE id=?',(encoded(evidence['checkpoint']),fake,id))
    with pytest.raises(ValueError):review(case)
    assert client.calls==0


def test_self_consistent_forged_seed_cannot_replace_the_original_lab_price_prefix(tmp_path,monkeypatch):
    """An internally valid PAPER stream still must match the actual LAB bars."""
    import json
    from app.derivatives.perp_observed_archive_format import pack,event_hash,start_source
    from app.derivatives.perp_observed_wire import ObservedSeed,transition_evidence
    case=reviewed_case(tmp_path,monkeypatch,complete=False)
    lab,paper,archive,id,clock,client=case
    with paper.connect() as db:
        header=db.execute('SELECT * FROM paper_observed_sessions WHERE id=?',(id,)).fetchone()
        raw=json.loads(header['seed_json']);origin=json.loads(header['origin_json'])
        raw['history'][0].update(o='200',h='201',l='199',c='200')
        seed=ObservedSeed.model_validate(raw)
        origin['trade_prefix_hash']=content_hash([bar.model_dump() for bar in seed.history[:origin['trade_prefix_count']]])
        seed_hash=content_hash(seed.payload());origin_hash=content_hash(origin)
        source=start_source(seed_hash,origin_hash)
        _,evidence=transition_evidence(None,seed.start(),None)
        digest=event_hash(id,0,None,source,evidence)
        db.execute('UPDATE paper_observed_sessions SET seed_json=?,seed_hash=?,origin_json=?,origin_hash=?,request_hash=?,head_hash=?,checkpoint_json=? WHERE id=?',
            (encoded(seed.payload()),seed_hash,encoded(origin),origin_hash,origin_hash,digest,encoded(evidence['checkpoint']),id))
        db.execute('UPDATE paper_observed_events SET input_hash=?,hash=?,input_blob=?,evidence_blob=? WHERE session_id=?',
            (content_hash(source),digest,pack(source),pack(evidence),id))
        db.execute('UPDATE paper_observed_candles SET candle_json=? WHERE session_id=? AND candle_t=?',
            (encoded(seed.history[0].model_dump()),id,seed.history[0].t))
    assert archive.read(id)['session']['verification']=='OBSERVED_REPLAY_VERIFIED'
    with pytest.raises(ValueError,match='original LAB'):review(case)
    assert client.calls==0
