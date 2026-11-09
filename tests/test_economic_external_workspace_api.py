"""The editor reads only owned, proved sources and exact original candidates."""
from app.portfolio import economic_external_workspace_api as workspace
from tests.test_economic_external_group_api import api_case,prove_all,rows,preview,confirmation
import pytest
from tests.test_wallet_swap import PASSWORD


def test_catalog_and_options_require_all_proofs_and_never_write_or_echo_wallets(tmp_path):
    with api_case(tmp_path)as c:
        base=f'/real/accounts/{c.account}/external-review';primary={'address':c.meta['owner']}
        body={**primary,'wallets':[{'context_account_id':c.peer,'address':c.meta['operator']}]};before=rows(c.vault)
        assert c.client.post(base+'/catalog',json=primary).status_code==401
        assert c.client.post(base+'/catalog',headers=c.headers,json=primary).status_code==409
        prove_all(c)
        one=c.client.post(base+'/catalog',headers=c.headers,json=primary);assert one.status_code==200,one.text
        assert len(one.json()['sources'])==1 and len(one.json()['wallets'])==2
        both=c.client.post(base+'/catalog',headers=c.headers,json=body);assert both.status_code==200,both.text
        assert len(both.json()['sources'])==2 and all(value['context_verified']for value in both.json()['sources'])
        assert both.json()['root_account_id']==c.account
        url=base+'/options/WALLET_SWAP/'+c.entries[0].current.source.id
        response=c.client.post(url,headers=c.headers,json=body);assert response.status_code==200,response.text
        data=response.json();assert data['observation_id']==c.entries[0].observation_id and len(data['bindings'])==2
        assert {item['id']for item in data['accounts']}=={c.account,c.peer}
        for text in(one.text,both.text,response.text):
            for value in(c.meta['owner'],c.meta['operator'],*(item.current.hash for item in c.entries)):
                assert value.lower()not in text.lower()
        assert rows(c.vault)==before and not c.reads
        extra=c.client.post(url,headers=c.headers,json={**body,'evidence':{}})
        assert extra.status_code==422


def test_workspace_refuses_wrong_owner_source_and_rechecks_detached_generation(tmp_path,monkeypatch):
    with api_case(tmp_path)as c:
        prove_all(c);base=f'/real/accounts/{c.account}/external-review';before=rows(c.vault)
        url=base+'/options/WALLET_SWAP/'+c.entries[1].current.source.id
        assert c.client.post(url,headers=c.headers,json={'address':c.meta['owner']}).status_code==409
        url=base+'/options/WALLET_SWAP/'+c.entries[0].current.source.id
        original=workspace.capture_external_effects
        def changed(*args,**kw):
            result=original(*args,**kw)
            # A generation change can occur independently of the bearer token.
            monkeypatch.setattr(type(c.vault),'session_generation',property(lambda self:987654321))
            return result
        monkeypatch.setattr(workspace,'capture_external_effects',changed)
        response=c.client.post(url,headers=c.headers,json={'address':c.meta['owner']})
        assert response.status_code==409 and 'observation_id'not in response.text
        assert rows(c.vault)==before


def test_saved_catalog_is_private_and_can_recover_after_unlock_without_wallet_proofs(tmp_path):
    with api_case(tmp_path)as c:
        base=f'/real/accounts/{c.account}/external-review/saved'
        assert c.client.get(base).status_code==401
        assert c.client.get(base,headers=c.headers).json()['sources']==[]
        prove_all(c);response=preview(c);assert response.status_code==200,response.text
        saved=c.client.post(c.url+'/confirm',headers=c.headers,json=confirmation(c,response.json()))
        assert saved.status_code==200,saved.text
        after=rows(c.vault);c.client.post('/real/vault/lock',headers=c.headers)
        headers={'X-Orbis-Vault-Token':c.client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        response=c.client.get(base,headers=headers);assert response.status_code==200,response.text
        assert len(response.json()['sources'])==2 and {item['accepted_id']for item in response.json()['sources']}=={saved.json()['id']}
        assert response.headers['Cache-Control']=='no-store'and rows(c.vault)==after and not c.reads
