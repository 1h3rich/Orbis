"""Private joint HTTP acceptance on two signed owned EVM replacements."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.portfolio.api import create_real_router
from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.portfolio.economic_external_plan import event_id
from app.portfolio import economic_external_group_api as module
from app.portfolio import economic_acceptance as archive,models as m
from app.portfolio.repository import append_event
from app.portfolio.domain import EventInput
from tests.external_batch_fixture import batch_case
from tests.test_economic_external_group_acceptance import rows
from tests.test_wallet_swap import PASSWORD
from tests.test_wallet_funding_api import prove


@contextmanager
def api_case(path):
    with batch_case(path)as c:
        declarations=[]
        for index,item in enumerate(c.declarations):
            keys={event_id(item['review_id'],value.key):value.key for value in item['new_events']}
            def ref(identifier):return {'new_key':keys[identifier]}
            proposed=[]
            for value in item['new_events']:
                event=value.input;body={'key':value.key,'account_id':value.account_id,
                    'kind':event.kind,'quote_currency':event.quote_currency}
                for name in('asset','fee_asset','note'):
                    if getattr(event,name)is not None:body[name]=getattr(event,name)
                for name in('quantity','unit_price','amount','fee'):
                    if getattr(event,name)is not None:body[name]=str(getattr(event,name))
                if value.pair_key is not None:body['pair_key']=value.pair_key
                proposed.append(body)
            review={'address':c.entries[index].current.meta['owner'],'review_id':item['review_id'],
                'wallets':[{'context_account_id':c.entries[1-index].current.account,'address':c.entries[1-index].current.meta['owner']}],
                'new_events':proposed,'members':[{'event':ref(value.event_id),'role':value.role}for value in item['members']],
                'allocations':[{'effect_refs':list(value.effect_refs),'events':[ref(identifier)for identifier in value.event_ids],
                    'classification':value.classification,'description':value.description}for value in item['allocations']],
                'annotations':[asdict(value)for value in item['annotations']]}
            declarations.append({'source_kind':'WALLET_SWAP','source_id':c.entries[index].current.source.id,
                'context_account_id':c.entries[index].current.account,'review':review})
        body={'recorded_at':c.recorded_at.isoformat(),'declarations':declarations}
        c.vault.lock();holder={};reads=[];started=datetime.now(timezone.utc)
        @contextmanager
        def rpc():
            reads.append('UNEXPECTED');raise AssertionError('The group API must use only saved receipts');yield
        def supervisor(v,context):
            holder['vault']=v
            return DexBotSupervisor(v,context,clock=lambda:c.recorded_at+timedelta(seconds=1)+(datetime.now(timezone.utc)-started))
        app=FastAPI();app.include_router(create_real_router(c.vault.path,dex_rpc_factory=rpc,dex_bot_supervisor_factory=supervisor))
        with TestClient(app)as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            try:yield SimpleNamespace(**{**vars(c),'vault':holder['vault']},client=client,headers=headers,body=body,reads=reads,
                url=f'/real/accounts/{c.account}/external-effect-groups/{uuid4()}')
            finally:client.post('/real/vault/lock',headers=headers)


def prove_all(c):
    for context,key in((c.account,'11'),(c.peer,'22')):prove(c.client,c.headers,context,Account.from_key('0x'+key*32))


def preview(c,body=None):return c.client.post(c.url+'/preview',headers=c.headers,json=body or c.body)


def confirmation(c,result):
    return {**c.body,'recorded_at':result['recorded_at'],'preview_digest':result['preview_digest'],
        'confirmation':'ACEPTAR_EFECTOS_EXTERNOS_CONJUNTOS'}


def test_all_eoa_contexts_are_required_and_joint_preview_is_private_read_only_without_rpc(tmp_path):
    with api_case(tmp_path)as c:
        before=rows(c.vault)
        assert c.client.post(c.url+'/preview',json=c.body).status_code==401
        assert c.client.post(c.url+'/confirm',json={**c.body,'preview_digest':'0'*64,
            'confirmation':'ACEPTAR_EFECTOS_EXTERNOS_CONJUNTOS'}).status_code==401
        assert preview(c).status_code==409
        prove(c.client,c.headers,c.account,Account.from_key('0x'+'11'*32));assert preview(c).status_code==409
        prove_all(c);response=preview(c);assert response.status_code==200,response.text
        data=response.json();assert data['manual_review_complete']and data['can_accept']and data['events_to_create']==6
        assert not data['saved']and not data['trading_authority']and response.headers['cache-control']=='no-store'
        assert rows(c.vault)==before and not c.reads
        for raw in(c.meta['owner'],c.meta['operator'],*(item.current.hash for item in c.entries)):assert raw.lower()not in response.text.lower()
        inverted=deepcopy(c.body);inverted['declarations'].reverse()
        assert preview(c,inverted).json()['preview_digest']==data['preview_digest']


def test_confirm_lost_reply_and_cold_get_recover_one_group_without_wallet_proofs_or_rpc(tmp_path):
    with api_case(tmp_path)as c:
        prove_all(c);response=preview(c);assert response.status_code==200,response.text
        body=confirmation(c,response.json());saved=c.client.post(c.url+'/confirm',headers=c.headers,json=body)
        assert saved.status_code==200,saved.text
        assert saved.json()['created']and saved.json()['joint_review'];after=rows(c.vault)
        retried=c.client.post(c.url+'/confirm',headers=c.headers,json=body)
        assert retried.status_code==200,retried.text
        assert not retried.json()['created']and retried.json()['id']==saved.json()['id']and rows(c.vault)==after
        assert c.client.get(c.url).status_code==401
        assert c.client.post('/real/vault/lock',headers=c.headers).status_code==200
        c.headers={'X-Orbis-Vault-Token':c.client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
        recovered=c.client.get(c.url,headers=c.headers);assert recovered.status_code==200,recovered.text
        assert recovered.json()['id']==saved.json()['id']and not recovered.json()['created']and not c.reads
        foreign=c.url.replace('/accounts/'+c.account+'/', '/accounts/'+c.peer+'/')
        assert c.client.get(foreign,headers=c.headers).status_code==409
        assert rows(c.vault)==after


@pytest.mark.parametrize('mode',['DUPLICATE','EXTRA','CHILD_TIME','ORIGIN','WRONG_CONFIRMATION'])
def test_strict_joint_port_rejects_ambiguous_or_untrusted_requests(tmp_path,mode):
    with api_case(tmp_path)as c:
        prove_all(c);body=deepcopy(c.body);headers=c.headers;url=c.url+'/preview';before=rows(c.vault)
        if mode=='DUPLICATE':body['declarations'][1]=body['declarations'][0]
        elif mode=='EXTRA':body['evidence']={'authority':True}
        elif mode=='CHILD_TIME':body['declarations'][0]['review']['recorded_at']=body['recorded_at']
        elif mode=='ORIGIN':headers={**headers,'Origin':'https://outside.invalid'}
        else:
            url=c.url+'/confirm';body.update(preview_digest='0'*64,confirmation='EXECUTE')
        response=c.client.post(url,headers=headers,json=body)
        assert response.status_code==(403 if mode=='ORIGIN'else 422),response.text
        assert rows(c.vault)==before and not c.reads


def test_detached_joint_calculation_releases_vault_and_access_lock_and_cannot_escape_later_lock(tmp_path,monkeypatch):
    with api_case(tmp_path)as c:
        prove_all(c);original=module.prepare_external_batch
        with ThreadPoolExecutor(max_workers=1)as pool:
            def calculate(*args,**kw):
                result=original(*args,**kw)
                assert pool.submit(c.client.post,'/real/vault/lock',headers=c.headers).result(timeout=5).status_code==200
                return result
            monkeypatch.setattr(module,'prepare_external_batch',calculate)
            response=preview(c)
        assert response.status_code==401,response.text
        assert not c.vault.unlocked and not c.reads


def test_unknown_joint_effects_keep_acceptance_unavailable_and_cannot_write(tmp_path):
    with api_case(tmp_path)as c:
        prove_all(c);c.body['declarations'][1]['review']['annotations'][0]['disposition']='UNKNOWN';before=rows(c.vault)
        response=preview(c);assert response.status_code==200,response.text
        data=response.json();assert not data['manual_review_complete']and not data['acceptance_available']and not data['can_accept']
        refused=c.client.post(c.url+'/confirm',headers=c.headers,json=confirmation(c,data))
        assert refused.status_code==409 and rows(c.vault)==before and not c.reads


@pytest.mark.parametrize('change',['LINK','HEAD_NAME','EVENT'])
def test_joint_captures_are_revalidated_after_pure_calculation(tmp_path,monkeypatch,change):
    with api_case(tmp_path)as c:
        prove_all(c);original=module.prepare_external_batch
        def calculate(*args,**kw):
            result=original(*args,**kw)
            with c.vault.session()as db:
                if change=='LINK':db.delete(db.scalar(select(m.RealWalletLink).where(m.RealWalletLink.account_id==c.peer)));db.commit()
                elif change=='HEAD_NAME':db.get(m.RealAccount,c.peer).name='Nombre cambiado';db.commit()
                else:append_event(db,c.peer,str(uuid4()),EventInput('CONTRIBUTION','USDT',c.recorded_at,amount=D(1)))
            return result
        monkeypatch.setattr(module,'prepare_external_batch',calculate)
        response=preview(c);assert response.status_code==409,response.text
        assert 'preview_digest'not in response.text and rows(c.vault)[1:]==(0,0,0,0,0,0)


@pytest.mark.parametrize('stage',['DRAFT','COMMIT'])
def test_vault_lock_after_staging_or_immediately_before_commit_wins(tmp_path,monkeypatch,stage):
    with api_case(tmp_path)as c:
        prove_all(c)
        if stage=='COMMIT':
            response=preview(c);assert response.status_code==200,response.text
            body=confirmation(c,response.json());target=module.acceptance;name='confirm_group'
        else:body=c.body;target=archive;name='_draft'
        original=getattr(target,name)
        with ThreadPoolExecutor(max_workers=1)as pool:
            def wrapped(*args,**kw):
                if stage=='DRAFT':result=original(*args,**kw)
                assert pool.submit(c.client.post,'/real/vault/lock',headers=c.headers).result(timeout=5).status_code==200
                return result if stage=='DRAFT'else original(*args,**kw)
            monkeypatch.setattr(target,name,wrapped)
            response=c.client.post(c.url+('/preview'if stage=='DRAFT'else'/confirm'),headers=c.headers,json=body)
        assert response.status_code==401,response.text
        assert not c.vault.unlocked and not c.reads


def test_confirm_digest_and_saved_selection_are_bound_to_all_declared_receipts(tmp_path):
    with api_case(tmp_path)as c:
        prove_all(c);response=preview(c);assert response.status_code==200,response.text
        body=confirmation(c,response.json());before=rows(c.vault)
        assert c.client.post(c.url+'/confirm',headers=c.headers,json={**body,'preview_digest':'0'*64}).status_code==409
        assert rows(c.vault)==before
        response=c.client.post(c.url+'/confirm',headers=c.headers,json=body);assert response.status_code==200,response.text
        saved=rows(c.vault);changed=deepcopy(body);changed['declarations'][1]['review']['annotations'][0]['note']='Otra declaración'
        assert c.client.post(c.url+'/confirm',headers=c.headers,json=changed).status_code==409
        assert rows(c.vault)==saved
