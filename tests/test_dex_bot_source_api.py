"""Protected LAB/PAPER preparation is read-only and never activates signing."""
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.portfolio.api import create_real_router
from tests.dex_bot_source_fixture import SourceCase,PASSWORD


def test_preparation_api_requires_real_session_and_reproduces_native_source(tmp_path):
    case=SourceCase(tmp_path,research=True)
    calls=[]
    def forbidden_rpc():
        calls.append('rpc');raise AssertionError('Preparation must not call a wallet or RPC')
    app=FastAPI();app.include_router(create_real_router(tmp_path/'real.db',spot_paper=case.paper,dex_rpc_factory=forbidden_rpc))
    with TestClient(app) as client:
        token=client.post('/real/vault/create',json={'passphrase':PASSWORD}).json()['token']
        headers={'X-Orbis-Vault-Token':token}
        account=client.post('/real/accounts',headers=headers,json={'name':'Wallet','base_currency':'USDT'}).json()['id']
        base=f'/real/accounts/{account}/dex-bots'
        body={'source_kind':'STRATEGY','paper_session_id':case.session['id'],'research_validation_id':case.validation['id'],'entry_usdt':'12.000000000000000001',
              'budget_usdt':'60','exposure_usdt':'25','loss_usdt':'5','wbnb_quota':'1'}
        assert client.get(base+'/sources').status_code==401
        assert client.post(base+'/preview',json=body).status_code==401
        assert client.post(base+'/preview',headers={**headers,'Origin':'https://outside.invalid'},json=body).status_code==403
        candidates=client.get(base+'/sources',headers=headers)
        assert candidates.status_code==200,candidates.text
        assert candidates.headers['cache-control']=='no-store'
        assert candidates.json()[0]['ready'] and candidates.json()[0]['symbol']=='BNB/USDT'
        assert candidates.json()[0]['paper_ready'] and candidates.json()[0]['research_options'][0]['id']==case.validation['id']
        exact=base+'/sources/STRATEGY/'+case.session['id']
        assert client.get(exact).status_code==401
        found=client.get(exact,headers=headers)
        assert found.status_code==200 and found.headers['cache-control']=='no-store'
        assert found.json()==candidates.json()
        assert client.get(base+'/sources/STRATEGY/'+str(uuid4()),headers=headers).json()==[]
        assert client.get(base+'/sources/DCA/'+case.session['id'],headers=headers).status_code==409
        assert client.post(base+'/preview',headers=headers,json={k:v for k,v in body.items() if k!='research_validation_id'}).status_code==409
        response=client.post(base+'/preview',headers=headers,json=body)
        assert response.status_code==200,response.text
        assert response.json()['amounts']['entry_units']=='12000000000000000001'
        assert response.json()['source']['source_run_id']==case.run['id']
        assert response.json()['preparation_only'] and not response.json()['automatic_execution']
        assert response.headers['cache-control']=='no-store'
        assert client.post(base+'/preview',headers=headers,json={**body,'entry_usdt':1.2}).status_code==422
        assert client.post(base+'/preview',headers=headers,json={**body,'private_key':'not-accepted'}).status_code==422
        assert client.post(f'/real/accounts/{uuid4()}/dex-bots/preview',headers=headers,json=body).status_code==404
        assert not calls
        assert client.post('/real/vault/lock',headers=headers).status_code==200
        assert client.get(base+'/sources',headers=headers).status_code==401
        assert client.get(exact,headers=headers).status_code==401
    case.close()


def test_observed_paper_without_reserve_is_not_ready_for_a_new_dex_binding(tmp_path):
    case=SourceCase(tmp_path)
    try:
        from app.live_execution.dex_bot_source import DexBotSources
        row=DexBotSources(case.paper).candidates(now=case.now)[0]
        assert row['paper_ready'] and not row['ready'] and row['research_options']==[]
        assert 'fuera de muestra' in row['reason']
    finally:case.close()


def test_lock_remains_available_during_backtest_and_revokes_pending_preparation(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.live_execution.dex_bot_source import DexBotSources
    case=SourceCase(tmp_path,research=True);started=Event();release=Event()
    original=DexBotSources.preview
    def paused(self,**values):
        started.set();assert release.wait(10)
        return original(self,**values)
    monkeypatch.setattr(DexBotSources,'preview',paused)
    app=FastAPI();app.include_router(create_real_router(tmp_path/'real.db',spot_paper=case.paper))
    with TestClient(app) as client,ThreadPoolExecutor() as pool:
        headers={'X-Orbis-Vault-Token':client.post('/real/vault/create',json={'passphrase':PASSWORD}).json()['token']}
        account=client.post('/real/accounts',headers=headers,json={'name':'Wallet','base_currency':'USDT'}).json()['id']
        body={'source_kind':'STRATEGY','paper_session_id':case.session['id'],'research_validation_id':case.validation['id'],'entry_usdt':'12','budget_usdt':'60',
              'exposure_usdt':'25','loss_usdt':'5','wbnb_quota':'1'}
        waiting=pool.submit(client.post,f'/real/accounts/{account}/dex-bots/preview',headers=headers,json=body)
        try:
            assert started.wait(5)
            assert client.post('/real/vault/lock',headers=headers).status_code==200
        finally:release.set()
        assert waiting.result(timeout=10).status_code==401
    case.close()
