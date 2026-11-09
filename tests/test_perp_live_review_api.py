"""REAL review is local, authenticated, read-only and revocable across HTTP."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.derivatives.paper_access import paper_perp_access
from app.lab.access import lab_access
from tests.test_perp_live_review import reviewed_case
from tests.perp_observed_archive_fixture import LAB_KEY, PAPER_KEY


def test_real_review_api_replays_originals_without_grant_or_account_mutation(tmp_path,monkeypatch):
    import app.portfolio.api as api
    case=reviewed_case(tmp_path,monkeypatch)
    lab,paper,archive,id,clock,grvt=case
    grvt.snapshot=lambda: {'source':'GRVT_MAINNET_PRIVATE_READONLY','trading_accounts':[{'sub_account_id':'123'}]}
    access=api.RealAccess(tmp_path/'real.db',lab_path=lab.path,grvt_factory=lambda key:grvt)
    monkeypatch.setattr(api,'RealAccess',lambda *args,**kwargs:access)
    monkeypatch.setattr(api.time,'time_ns',lambda:clock['now'])
    app=FastAPI();app.include_router(api.create_real_router(tmp_path/'real.db',lab_path=lab.path,paper_perp_path=paper.path))
    client=TestClient(app);url='/real/exchanges/grvt/perp-review';body={'session_id':id,'sub_account_id':'123'}
    assert client.post(url,json=body).status_code==401
    token=client.post('/real/vault/create',json={'passphrase':'owned REAL review test password'}).json()['token']
    headers={'X-Orbis-Vault-Token':token}
    assert client.post(url,json=body,headers=headers).status_code==409  # No private connection.
    assert client.post('/real/exchanges/grvt/connect',json={'api_key':'fixture-view-only-key'},headers=headers).status_code==200
    assert client.post(url,json=body,headers={**headers,'Origin':'https://foreign.test'}).status_code==403
    for bad in ({**body,'private_key':'forbidden'}, {**body,'sub_account_id':'00123'}, {**body,'session_id':'x'}):
        assert client.post(url,json=bad,headers=headers).status_code==422
    before=client.get('/real/accounts',headers=headers).json()
    observed=archive.read(id)
    response=client.post(url,json=body,headers=headers)
    assert response.status_code==200,response.text
    result=response.json()
    assert response.headers['cache-control']=='no-store'
    assert result['blockers']==['PERP_EXECUTOR_NOT_IMPLEMENTED']
    assert result['execution_authority']=='NONE' and result['execution_supported'] is False
    assert client.get('/real/accounts',headers=headers).json()==before
    assert archive.read(id)==observed
    for path,key,unlock in ((paper.path,PAPER_KEY,paper_perp_access),(lab.path,LAB_KEY,lab_access)):
        unlock.lock(path)
        count=grvt.calls
        assert client.post(url,json=body,headers=headers).status_code==423
        assert grvt.calls==count
        unlock.unlock(path,key)
    # The connection can be removed promptly while private HTTP is in flight;
    # its result cannot be published under the former connection identity.
    grvt.hook=lambda count:access.disconnect_grvt(token)
    assert client.post(url,json=body,headers=headers).status_code==409
    assert client.post('/real/vault/lock',headers=headers).status_code==200
    assert client.post(url,json=body,headers=headers).status_code==401
