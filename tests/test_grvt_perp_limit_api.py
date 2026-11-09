"""Actual private HTTP activation, background LIMIT execution and cold readback."""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.perp_dataset import TradeBar
from app.live_execution import grvt_perp_limit_execution as execution
from app.live_execution import grvt_perp_limit_supervisor as supervision
from app.live_execution.grvt_mainnet_journal import require_new_mainnet_bracket
from app.live_execution.grvt_mainnet_preflight import canonical
from app.live_execution.grvt_mainnet_transport import BULK_URL
from app.live_execution.grvt_perp_limit_plan import PerpLimitEntryPlan
from app.live_execution.grvt_perp_limit_preflight import collect_limit_preflight
from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from app.live_execution.grvt_perp_signals import replay_signal_records
from app.portfolio import grvt_perp_limit_api as limit_api
from tests.perp_observed_fixture import bar
from tests.perp_observed_source_fixture import OwnedPublicAdapter
from tests.test_grvt_mainnet_api import ROOT, connect, terms as market_terms
from tests.test_grvt_mainnet_bot_service import wait_for
from tests.test_grvt_perp_limit_entry import CID, NS, fill
from tests.test_grvt_perp_limit_execution import SUB, VENUE_ORDER_ID, enable_order_history, setup
from tests.test_grvt_perp_limit_exit import FIRST_EXIT, SECOND_EXIT, controlled_exit, position
from tests.test_grvt_perp_limit_supervisor import prove_position
from tests.test_grvt_testnet_bracket_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY, SIGNER


BASE = ROOT+'/bots/perp/limit'


def partial_exit_fixture(case,session):
    original = session['client_factory']
    posts = []
    expected = ('0.125','0.105','0.175','0.2')
    prices = ('103','103','106','96')

    def factory():
        client = original()
        original_post = client._post

        def post(url,body,headers):
            if url != BULK_URL or not body['orders'] or not body['orders'][0]['reduce_only']:
                return original_post(url,body,headers)
            index = len(posts)
            wire, = body['orders']
            assert wire['legs'][0]['size'] == expected[index]
            cid = wire['metadata']['client_order_id']
            posts.append(wire)
            at = str(case.clock[0])
            actual = Decimal('0.02' if index==0 else expected[index])
            order_id = '0x981'+str(index)
            raw = {**deepcopy(wire),'order_id':order_id,'state':{'status':'CANCELLED' if index==0 else 'FILLED',
                'book_size':['0'],'traded_size':[canonical(actual)],'update_time':at,
                'reject_reason':'IOC_CANCEL' if index==0 else 'UNSPECIFIED'}}
            raw['metadata']['create_time'] = at
            case.settings['orders'][cid] = raw
            pnl = actual*(Decimal(prices[index])-Decimal('99.9'))
            case.settings['histories']['fill_history'].append(fill(trade_id=f'http-partial-{index}',order_id=order_id,
                client_order_id=cid,event_time=at,is_buyer=False,is_taker=True,
                size=canonical(actual),price=prices[index],fee='0',realized_pnl=canonical(pnl)))
            left = Decimal(case.settings['positions'][0]['size'])-actual
            cash = canonical(Decimal(case.settings['account_balance'])+pnl)
            case.settings.update(account_balance=cash,account_equity=cash,
                                 positions=[position(canonical(left))] if left else [])
            if index==0:
                raise httpx.ReadTimeout('Owned partial IOC acknowledgement lost')
            return httpx.Response(200,json={'result':{'orders':[raw],'cancel_acks':[]}},
                                  request=httpx.Request('POST',url))
        client._post = post
        return client

    return {**session,'client_factory':factory},posts


@contextmanager
def limit_fixture(tmp_path,monkeypatch,*,hold_first_wait=False,no_signal=False,hold_pending=False,
                  on_wait=None, recover_from_history=False, partial=False, optimized=False):
    import app.portfolio.api as module

    case,calls,session,send = setup(tmp_path,monkeypatch,lose_submit=True,lose_cancel=True,partial=partial,optimized=optimized)
    case.vault.lock()
    trial = send['origin']
    lab_path,paper_path = trial.origin.lab.path,trial.archive.paper.path
    session,posts = partial_exit_fixture(case,session) if partial else controlled_exit(case,session,'LONG')
    generated = [str(int(CID)+100+i) for i in range(50)] if partial else [FIRST_EXIT,SECOND_EXIT]
    if recover_from_history:
        enable_order_history(case,CID,*generated)
    feed = replay_signal_records(send['seed'],send['records']).state
    history = [TradeBar.model_validate(candle.json()) for candle in feed.history]
    adapter = OwnedPublicAdapter(SimpleNamespace(history=history,funding=[],quote=send['seed'].quote),
                                 {'now':case.clock[0]})
    next_t = history[-1].t+60
    adapter.rows[next_t] = bar(next_t,'SHORT' if no_signal else 'LONG').json()
    # Use a direct synthetic ticker only at the public GRVT boundary.
    ticker = adapter.get_ticker

    def tick(symbol):
        adapter.clock['now'] = case.clock[0]
        return ticker(symbol)

    adapter.get_ticker = tick
    adapter.quote_override.update(bid='99.99',ask='100.01')
    original_public = case.public

    def public(url,body):
        response = original_public(url,body)
        if not partial or not url.endswith('/ticker'):
            return response
        value = response.json()
        value['result'].update(best_bid_price=adapter.quote_override['bid'],best_ask_price=adapter.quote_override['ask'])
        return httpx.Response(200,json=value,request=httpx.Request('POST',url))
    at_wait,release,at_pending = Event(),Event(),Event()
    position_proved = False
    read_failures = []

    def private_hook(url,body):
        if url.endswith('/order') and body['client_order_id'] == CID and not read_failures:
            read_failures.append(CID)
            raise httpx.ReadTimeout('Owned initial LIMIT lookup delayed')

    case.settings['private_hook'] = private_hook

    def waiter(stop,seconds):
        nonlocal position_proved
        if on_wait is not None:
            on_wait(stop)
        if hold_first_wait and not at_wait.is_set():
            at_wait.set()
            while not release.wait(.01):
                if stop.is_set():
                    return True
        order = case.settings['orders'].get(CID)
        if hold_pending and order is not None and not at_pending.is_set():
            at_pending.set()
            while not stop.wait(.01):
                pass
            return True
        case.clock[0] += int(seconds*NS)
        if order is not None and order['state']['status'] == 'OPEN' and not case.settings['histories']:
            wire_size = Decimal(order['legs'][0]['size'])
            order['state'].update(traded_size=['0.3'],book_size=[str(wire_size-Decimal('0.3'))],
                                  update_time=str(case.clock[0]))
            case.settings['histories']['fill_history'] = [
                fill(size='0.3',price='99.9',order_id=VENUE_ORDER_ID,event_time=str(case.clock[0]))]
        elif order is not None and order['state']['status'] == 'CANCELLED' and not position_proved:
            prove_position(case)
            position_proved = True
            if not partial:
                adapter.quote_override.update(bid='49',ask='49.01',mark_price='49')
        if partial and position_proved:
            price = '103' if len(posts)<2 else '106' if len(posts)==2 else '96'
            adapter.quote_override.update(bid=price,ask=canonical(Decimal(price)+Decimal('.01')),mark_price=price)
        return stop.is_set()

    monkeypatch.setattr(limit_api,'secrets',SimpleNamespace(randbits=lambda _:int(CID)-2**63))
    ids = iter(int(cid)-2**63 for cid in generated)
    monkeypatch.setattr(supervision,'secrets',SimpleNamespace(randbits=lambda _:next(ids)))
    access = module.RealAccess(case.vault.path,lab_path=lab_path)
    monkeypatch.setattr(module,'RealAccess',lambda *args,**kwargs:access)
    app = FastAPI()
    app.include_router(module.create_real_router(case.vault.path,lab_path=lab_path,paper_perp_path=paper_path,
        grvt_mainnet_factory=lambda _:session['client_factory'](),grvt_mainnet_public_post=public,
        grvt_mainnet_clock_ns=session['clock_ns'],grvt_mainnet_waiter=waiter))
    bots = access._grvt_mainnet_bots
    bots.perp_source_factory = lambda permission:GrvtPerpSignalSource(
        adapter=adapter,clock_ns=session['clock_ns'],require_permission=permission)
    case.vault = access.vault
    try:
        yield SimpleNamespace(app=app,access=access,case=case,calls=calls,posts=posts,send=send,
                              read_failures=read_failures,bots=bots,session_id=trial.paper.session_id,
                              at_wait=at_wait,at_pending=at_pending,partial=partial)
    finally:
        access.lock()
        release.set()
        wait_for(lambda:not bots.active_sub(SUB),seconds=12)


@contextmanager
def api_case(tmp_path,monkeypatch,**options):
    with limit_fixture(tmp_path,monkeypatch,**options) as fixture:
        with TestClient(fixture.app,base_url='http://127.0.0.1') as api:
            token = api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
            fixture.api,fixture.headers = api,{'X-Orbis-Vault-Token':token}
            yield fixture


def review(owned):
    response = connect(owned.api,owned.headers)
    assert response.status_code == 200,response.text
    revision = response.json()['permission_revision']
    body = {'sub_account_id':SUB,'permission_revision':revision,'session_id':owned.session_id,
            'collateral_limit':'1000','entry_cash_limit':'25','entry_notional_limit':'1000',
            'exit_notional_limit':'2000','fee_rate_ceiling':'0','dedicated_account_confirmed':True,
            'wait_seconds':80}
    if owned.partial:
        body.update(max_reductions=6,reserved_close_attempts=2)
    preview = owned.api.post(BASE+'/preview',headers=owned.headers,json=body)
    assert preview.status_code == 200,preview.text
    assert preview.headers['cache-control'] == 'no-store'
    return {**preview.json(),'permission_revision':revision,'confirmation':
        'ACTIVAR_BOT_LIMIT_PARCIALES_GRVT_MAINNET' if owned.partial else 'ACTIVAR_BOT_LIMIT_GRVT_MAINNET'}


def attempts(owned,headers=None):
    response = owned.api.get(BASE+'/attempts',headers=owned.headers if headers is None else headers,
                             params={'sub_account_id':SUB})
    assert response.status_code == 200,response.text
    return response.json()



def test_optimized_limit_review_pins_exact_lab_and_paper_without_starting(tmp_path, monkeypatch):
    with api_case(tmp_path, monkeypatch, optimized=True) as owned:
        body = review(owned)
        value = body['review']
        lab = value['lab_origin']
        assert lab == owned.send['origin'].origin.review_lab_origin()
        assert lab['version'] == 2
        assert lab['version_id'] == value['terms']['research']['profile']['strategy_version_id']
        assert lab['optimization']['version_id'] == lab['version_id']
        assert value['terms']['paper']['session_id'] == owned.session_id
        assert value['terms']['paper']['closed_trades'] == 1
        assert not value['authorization'] and not owned.calls
        forged = deepcopy(body)
        forged['review']['lab_origin']['optimization']['reserve_run_id'] = '00000000-0000-4000-8000-000000000001'
        assert owned.api.post(BASE+'/start', headers=owned.headers, json=forged).status_code == 409
        assert not owned.calls and not owned.bots.active_sub(SUB)


def test_partial_limit_api_requires_its_own_confirmation_and_resumes_the_fixed_policy(tmp_path,monkeypatch):
    with api_case(tmp_path,monkeypatch,partial=True,hold_pending=True,recover_from_history=True) as owned:
        api,headers = owned.api,owned.headers
        source, = api.get(BASE+'/sources',headers=headers).json()['items']
        assert source['session_id']==owned.session_id and source['partial_targets']==2
        exact = api.get(BASE+'/sources',params={'session_id':owned.session_id,'offset':25},headers=headers)
        assert exact.headers['Cache-Control']=='no-store'
        assert exact.json()=={'items':[source],'has_more':False,'execution_authority':'NONE'}
        assert api.get(BASE.removesuffix('/limit')+'/sources',params={'session_id':owned.session_id},headers=headers).json()['items']==[]
        assert api.get(BASE+'/sources',params={'session_id':'invalido'},headers=headers).status_code==422
        body = review(owned)
        assert body['review']['schema'].endswith('.v2')
        assert body['review']['terms']['max_attempts']==6 and body['review']['terms']['reserved_close_attempts']==2
        wrong = {**body,'confirmation':'ACTIVAR_BOT_LIMIT_GRVT_MAINNET'}
        assert api.post(BASE+'/start',headers=headers,json=wrong).status_code==409 and not owned.calls
        assert api.post(BASE+'/start',headers=headers,json=body).status_code==202
        assert owned.at_pending.wait(25) and len(owned.calls)==1
        assert api.post('/real/vault/lock',headers=headers).status_code==200
        wait_for(lambda:not owned.bots.active_sub(SUB),seconds=12)
        token = api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        headers = {'X-Orbis-Vault-Token':token}
        item, = attempts(owned,headers)['items']
        assert item['partial_limits']=={'max_attempts':6,'reserved_close_attempts':2}
        revision = connect(api,headers).json()['permission_revision']
        request = {'sub_account_id':SUB,'client_order_id':CID,'permission_revision':revision,
                   'authorization_seconds':180,'max_reductions':6}
        assert api.post(BASE+'/resume/preview',headers=headers,json={**request,'max_reductions':7}).status_code==409
        preview = api.post(BASE+'/resume/preview',headers=headers,json=request)
        assert preview.status_code==200,preview.text
        assert preview.json()['review']['schema'].endswith('.v2')
        approved = {**preview.json(),'permission_revision':revision,'confirmation':'RETOMAR_BOT_LIMIT_PARCIALES_GRVT_MAINNET'}
        assert api.post(BASE+'/resume/start',headers=headers,json={**approved,
            'confirmation':'RETOMAR_BOT_LIMIT_GRVT_MAINNET'}).status_code==409
        assert api.post(BASE+'/resume/start',headers=headers,json=approved).status_code==202
        wait_for(lambda:not owned.bots.active_sub(SUB),seconds=75)
        item, = attempts(owned,headers)['items']
        assert item['status']=='COMPLETE' and item['financial']['net_change_usdt']=='0.67'
        graph = api.get(BASE+f'/attempts/{CID}/executions-page',headers=headers).json()
        assert [fill['target_index'] for fill in graph['executions'] if fill['role']=='REDUCTION']==[1,1,2,None]
        assert graph['financially_reconciled'] and len(owned.calls)==2 and len(owned.posts)==4
        download={'sub_account_id':SUB,'revision':graph['revision'],'confirmation':'EXPORTAR_JSONL_GRVT_REAL'}
        export=api.post(BASE+f'/attempts/{CID}/research-export',headers=headers,json=download)
        assert export.status_code==200,export.text
        assert export.headers['cache-control']=='no-store'
        records=[json.loads(line) for line in export.json()['jsonl'].splitlines()]
        assert len(records)==6 and CID not in export.json()['jsonl']
        assert [row['fill']['target_index'] for row in records if row['fill']['role']=='REDUCTION']==[1,1,2,None]
        assert all(row['provenance']['paper_session_id']==owned.session_id and row['entry_kind']=='LIMIT' for row in records)
        assert all(row['provenance']['reserve']['artifact_id']==body['review']['terms']['research']['artifact_id']
                   for row in records)


def test_limit_api_future_signal_retries_reads_and_finishes_in_background(tmp_path,monkeypatch):
    with api_case(tmp_path,monkeypatch) as owned:
        api,headers = owned.api,owned.headers
        assert api.get(BASE+'/sources').status_code == 401
        assert api.post(BASE+'/start',json={}).status_code == 401
        sources = api.get(BASE+'/sources',headers=headers).json()['items']
        assert sources[0]['session_id'] == owned.session_id
        approved = review(owned)
        assert approved['review']['client_order_id'] == CID and not owned.calls
        changed = deepcopy(approved)
        changed['review']['max_reductions'] = 4
        assert api.post(BASE+'/start',headers=headers,json=changed).status_code == 409
        assert api.post(BASE+'/start',headers={**headers,'Origin':'https://foreign.invalid'},json=approved).status_code == 403
        response = api.post(BASE+'/start',headers=headers,json=approved)
        assert response.status_code == 202,response.text
        assert response.json()['client_order_id'] == CID
        assert api.post(BASE+'/start',headers=headers,json=approved).status_code == 409
        done = wait_for(lambda:(item if not (item:=owned.bots.limit_status(SUB))['active'] else None),seconds=75)
        assert done['status'] == 'COMPLETE',done
        assert len(owned.calls) == len(owned.posts) == 2 and owned.read_failures == [CID]
        result = attempts(owned)
        item, = result['items']
        assert item['financial']['net_change_usdt'] == '0.025'
        assert item['entry']['filled_size'] == '0.5' and item['decision']['reason'] == 'STOP_OR_TRAILING'
        api.post('/real/vault/lock',headers=headers)
        token = api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        reopened = attempts(owned,{'X-Orbis-Vault-Token':token})['items'][0]
        assert reopened == item and not reopened['authorization']
        serialized = json.dumps(result)+response.text
        for secret in (KEY.hex(),SIGNER,'synthetic-mainnet-api-key','synthetic-mainnet-session'):
            assert secret not in serialized


@pytest.mark.parametrize('stop_path',['/real/vault/lock','/real/vault/protect-view',ROOT+'/stop'])
def test_limit_api_stops_waiting_job_without_any_order(tmp_path,monkeypatch,stop_path):
    with api_case(tmp_path,monkeypatch,hold_first_wait=True) as owned:
        body = review(owned)
        response = owned.api.post(BASE+'/start',headers=owned.headers,json=body)
        assert response.status_code == 202,response.text
        assert owned.at_wait.wait(10)
        data = {'sub_account_id':SUB,'permission_revision':body['permission_revision'],**market_terms()}
        assert owned.api.post(ROOT+'/preview',headers=owned.headers,json=data).status_code == 409
        payload = {'sub_account_id':SUB,'confirmation':'DETENER_GRVT_MAINNET'} if stop_path==ROOT+'/stop' else None
        result = owned.api.post(stop_path,headers=owned.headers,json=payload)
        assert result.status_code == 200,result.text
        wait_for(lambda:not owned.bots.active_sub(SUB),seconds=10)
        assert owned.bots.limit_status(SUB)['status'] == 'STOPPED'
        assert not owned.calls and not owned.posts


def test_limit_api_wait_expires_without_promoting_a_warmup_signal(tmp_path,monkeypatch):
    with api_case(tmp_path,monkeypatch,no_signal=True) as owned:
        body = review(owned)
        response = owned.api.post(BASE+'/start',headers=owned.headers,json=body)
        assert response.status_code == 202,response.text
        wait_for(lambda:not owned.bots.active_sub(SUB),seconds=25)
        state = attempts(owned)
        assert state['job']['status'] == 'NO_SIGNAL'
        assert state['items'] == [] and not owned.calls and not owned.posts


def test_limit_resume_after_lock_reuses_attempt_and_cannot_create_another_entry(tmp_path,monkeypatch):
    with api_case(tmp_path,monkeypatch,hold_pending=True) as owned:
        api,headers = owned.api,owned.headers
        first_review = review(owned)
        accepted = api.post(BASE+'/start',headers=headers,json=first_review)
        assert accepted.status_code == 202 and accepted.json()['review_id'] == first_review['review']['review_id']
        assert owned.at_pending.wait(25) and len(owned.calls) == 1 and not owned.posts
        assert api.post('/real/vault/lock',headers=headers).status_code == 200
        wait_for(lambda:not owned.bots.active_sub(SUB),seconds=10)
        token = api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        headers = {'X-Orbis-Vault-Token':token}
        history = attempts(owned,headers)
        item, = history['items']
        assert item['status'] == 'INTERRUPTED' and item['resumable']
        assert history['job']['review_id'] == first_review['review']['review_id']
        abandon = {'sub_account_id':SUB,'client_order_id':CID,'confirmation':'LIBERAR_RESERVA_LIMIT_SIN_ENVIO'}
        assert api.post(BASE+'/attempts/abandon',headers=headers,json=abandon).status_code == 409
        revision = connect(api,headers).json()['permission_revision']
        preview = api.post(BASE+'/resume/preview',headers=headers,json={'sub_account_id':SUB,
            'client_order_id':CID,'permission_revision':revision,'authorization_seconds':180,'max_reductions':3})
        assert preview.status_code == 200,preview.text
        body = {**preview.json(),'permission_revision':revision,'confirmation':'RETOMAR_BOT_LIMIT_GRVT_MAINNET'}
        assert body['review']['execution_scope'] == 'MANAGE_OWN_EXISTING_LIMIT_ONLY'
        for key, alteration in [('event_head','0'*64), ('review_id','z'*32)]:
            changed = deepcopy(body)
            changed['review'][key] = alteration
            assert api.post(BASE+'/resume/start',headers=headers,json=changed).status_code == 409
        assert len(owned.calls) == 1 and not owned.posts
        response = api.post(BASE+'/resume/start',headers=headers,json=body)
        assert response.status_code == 202,response.text
        review_id = body['review']['review_id']
        assert response.json()['client_order_id'] == CID and response.json()['review_id'] == review_id
        assert review_id != history['job']['review_id']
        assert api.post(BASE+'/resume/start',headers=headers,json=body).status_code == 409
        wait_for(lambda:not owned.bots.active_sub(SUB),seconds=50)
        resumed = attempts(owned,headers)
        assert resumed['job']['review_id'] == review_id
        result, = resumed['items']
        assert result['client_order_id'] == CID and result['status'] == 'COMPLETE'
        assert result['financial']['net_change_usdt'] == '0.025'
        assert len(owned.calls) == len(owned.posts) == 2
        graph = BASE+f'/attempts/{CID}/executions-page'
        previous_requests = len(owned.case.calls)
        assert api.get(graph).status_code == 401
        first = api.get(graph,headers=headers,params={'limit':2}).json()
        assert first['total'] == 4 and first['next_offset'] == 2 and first['timeframe'] == '1m'
        assert [fill['role'] for fill in first['executions']] == ['ENTRY','ENTRY']
        assert api.get(graph,headers=headers,params={'revision':'0'*64}).status_code == 409
        last = api.get(graph,headers=headers,params={'limit':2,'offset':2,'revision':first['revision']}).json()
        assert [fill['role'] for fill in last['executions']] == ['REDUCTION','REDUCTION']
        assert last['financially_reconciled'] and last['net_realized_pnl_usdt'] == '0.025'
        assert last['next_offset'] is None and len(owned.case.calls) == previous_requests


def test_limit_api_abandons_only_a_local_reservation_and_keeps_history(tmp_path,monkeypatch):
    with api_case(tmp_path,monkeypatch) as owned:
        api,headers = owned.api,owned.headers
        send = owned.send
        terms = send['origin'].terms(send['budget'],max_pending_seconds=30)
        client,clock = send['client_factory'](),send['clock_ns']
        proof = collect_limit_preflight(client,terms,send['seed'],send['records'],entry_deadline_ns=send['entry_deadline_ns'],
                                       public_post=send['public_post'],clock_ns=clock)
        contract = execution._sign(PerpLimitEntryPlan.model_validate(proof['plan']),CID,key=KEY,expected_signer=SIGNER,at=clock())
        execution.claim_limit_attempt(owned.access.vault,contract,proof,send['seed'],send['records'],client=client,checked=clock)
        before, = attempts(owned)['items']
        assert before['abandonable'] and not before['resumable']
        previous_requests = len(owned.case.calls)
        action = BASE+'/attempts/abandon'
        body = {'sub_account_id':SUB,'client_order_id':CID,'confirmation':'LIBERAR_RESERVA_LIMIT_SIN_ENVIO'}
        assert api.post(action,headers=headers,json={**body,'sub_account_id':'123457'}).status_code == 409
        assert api.post(action,headers={**headers,'Origin':'https://foreign.invalid'},json=body).status_code == 403
        result = api.post(action,headers=headers,json=body)
        assert result.status_code == 200,result.text
        released = result.json()
        assert released['status'] == 'ABANDONED' and not released['reserved'] and not released['abandonable']
        assert api.post(action,headers=headers,json=body).json() == released
        assert not owned.calls and not owned.posts and len(owned.case.calls) == previous_requests
        api.post('/real/vault/lock',headers=headers)
        token = api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        assert attempts(owned,{'X-Orbis-Vault-Token':token})['items'] == [released]
        with owned.access.vault.session() as db:
            require_new_mainnet_bracket(db,SUB,str(int(CID)+50))


def test_failed_limit_worker_launch_does_not_publish_an_activation_receipt(tmp_path, monkeypatch):
    from app.live_execution import grvt_perp_limit_worker as worker

    class FailedThread:
        def __init__(self, **_options):
            pass

        def start(self):
            raise RuntimeError('Owned worker launch failure')

    with api_case(tmp_path,monkeypatch) as owned:
        body = review(owned)
        monkeypatch.setattr(worker,'Thread',FailedThread)
        response = owned.api.post(BASE+'/start',headers=owned.headers,json=body)
        assert response.status_code == 409
        history = attempts(owned)
        assert history['job'] is None and history['items'] == []
        assert not owned.bots.active_sub(SUB) and not owned.calls and not owned.posts
