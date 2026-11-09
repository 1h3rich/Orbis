"""Owned HTTP activation over the real reviewed source and RAM coordinator."""
from contextlib import contextmanager
from dataclasses import asdict

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.grvt_perp_coordinator_fixture import OwnedPerpCoordinator
from tests.test_grvt_mainnet_api import connect,ROOT
from tests.test_grvt_mainnet_submit import PASSWORD


@contextmanager
def perp_api_case(directory,monkeypatch,*,direction='LONG',mode='ISOLATED',optimized=False):
    import app.portfolio.api as module
    owned=OwnedPerpCoordinator(directory,monkeypatch,direction=direction,mode=mode,optimized=optimized)
    path=owned.case.vault.path;owned.case.vault.lock()
    access=module.RealAccess(path,lab_path=owned.owned.lab.path)
    monkeypatch.setattr(module,'RealAccess',lambda *args,**kwargs:access)
    app=FastAPI();app.include_router(module.create_real_router(path,lab_path=owned.owned.lab.path,
        paper_perp_path=owned.owned.paper.path,grvt_mainnet_factory=owned.factory,
        grvt_mainnet_public_post=owned.case.public,grvt_mainnet_clock_ns=lambda:owned.case.clock[0],
        grvt_mainnet_waiter=owned.waiter))
    owned.service=access._grvt_mainnet_bots
    owned.service.perp_source_factory=owned.source;owned.service.perp_worker_waiter=owned.worker_waiter
    with TestClient(app,base_url='http://127.0.0.1') as api:
        response=api.post('/real/vault/unlock',json={'passphrase':PASSWORD})
        assert response.status_code==200,response.text
        headers={'X-Orbis-Vault-Token':response.json()['token']}
        owned.case.vault=access.vault
        try:yield owned,access,api,headers
        finally:
            owned.close()
            access.lock()


def preview_body(owned,revision):
    budget=owned.owned.terms.budget.payload()
    body={key:budget[key] for key in ('sub_account_id','collateral_limit','entry_cash_limit','entry_notional_limit',
        'exit_notional_limit','fee_rate_ceiling','exit_seconds','max_spread_bps','max_slippage_bps')}
    policy=asdict(owned.owned.policy)
    body.update({key:format(policy[key],'f') if key.endswith('_cap') else policy[key] for key in (
        'total_entry_cap','loss_cap','max_entries','wait_seconds','recovery_seconds','max_attempts')})
    return {**body,'session_id':owned.owned.origin.source().session_id,'permission_revision':revision,
        'dedicated_account_confirmed':True,'keep_operating':True}


def reviewed(owned,api,headers):
    connected=connect(api,headers)
    assert connected.status_code==200,connected.text
    revision=connected.json()['permission_revision']
    response=api.post(ROOT+'/bots/perp/preview',headers=headers,json=preview_body(owned,revision))
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    return {**response.json(),'permission_revision':revision,'confirmation':'ACTIVAR_BOT_PERPETUO_GRVT_MAINNET'}
