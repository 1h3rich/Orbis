"""Joint signed bot/wallet replacements, real idle admission and causal costs."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.live_execution.dex_external_order import common_reference,verify_external_order
from app.live_execution.dex_external_observation import source_reference
from app.portfolio import dex_external_observation as observations,dex_external_order as orders
from app.portfolio import wallet_swap as swaps,dex_operator_journal as operators,dex_bot_journal as bots
from app.portfolio.api import create_real_router
from app.portfolio.dex_external_outcomes import external_outcome
from app.portfolio.dex_operator_outcomes import operator_outcome
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.repository import account_snapshot
from tests.dex_external_bot_fixture import external_bot_case
from tests.dex_evm_fixture import wait_receipt
from tests.test_dex_call_resolution import prepare
from tests.test_dex_external_observation import calldata
from tests.test_dex_external_observation_journal import proposal,save
from tests.test_dex_deployments import PASSWORD
from tests.test_wallet_funding_api import prove
from tests.test_economic_external_group_acceptance import rows


@pytest.mark.parametrize('kind',['LONG','DCA'])
def test_signed_joint_bot_and_wallet_costs_need_idle_and_never_create_strategy_fills(tmp_path,monkeypatch,kind):
    with external_bot_case(tmp_path,monkeypatch,kind)as c:
        def clock():
            block=c.rpc('eth_getBlockByNumber',['latest',False])
            c.clock[0]=max(datetime.now(timezone.utc),c.clock[0],datetime.fromtimestamp(int(block['timestamp'],16),timezone.utc))+timedelta(seconds=1)
            return c.clock[0]
        monkeypatch.setattr('tests.test_dex_call_resolution.now',clock)
        planned,source,before=prepare(c.vault,c.account,c.meta,c.rpc)
        tx={**planned['transaction'],'gasPrice':hex(2*10**9),'to':c.meta['usdt'],
            'data':calldata('transfer(address,uint256)',['address','uint256'],[c.meta['owner'],5*10**15])}
        sent=c.rpc('eth_sendTransaction',[tx]);wait_receipt(c.rpc,sent);evidence=collect_call_resolution(c.rpc,source,sent)
        with c.vault.session()as db:salt=_existing_salt(db)
        wallet=SimpleNamespace(vault=c.vault,account=c.account,meta=c.meta,source=source,evidence=evidence,
            reference=source_reference('WALLET_SWAP',salt),now=clock(),hash=sent)
        observed=save(wallet,proposal(wallet))
        with c.vault.session()as db:
            row,payload=observations.observation_record(db,observed['id']);proof=payload['proof']
            certificate=verify_external_order(source,row.id,row.payload_digest,proof,evidence,
                reference=wallet.reference,common=common_reference(salt),now=clock())
            reviewed=orders.preview(row,certificate)
        with c.vault.session()as db:orders.save_order(db,observed['id'],account_id=c.account,proof=certificate,
            preview_digest=reviewed['preview_digest'],now=clock())
        declarations=[];fees={}
        for kind_of_source,identifier,observed_id in(('OPERATOR',c.source.id,c.observed['id']),('WALLET_SWAP',source.id,observed['id'])):
            with c.vault.session()as db:body=observations.observation_record(db,observed_id)[1]['proof']
            gas=str(uuid4());fees[kind_of_source]=int(body['receipt']['gas_fee_wei'])
            refs=['native:'+body['call_ref']]if kind_of_source=='OPERATOR'else['token:'+body['transfers'][0]['log_ref']]
            declarations.append({'source_kind':kind_of_source,'source_id':identifier,'context_account_id':c.account,
                'review':{'address':c.meta['owner'],'review_id':str(uuid4()),'new_events':[{'key':gas,'account_id':c.account,
                    'kind':'FEE','quote_currency':'BNB','amount':str(D(fees[kind_of_source]).scaleb(-18)),'note':'Gas externo observado'}],
                    'members':[{'event':{'new_key':gas},'role':'GAS'}],
                    'allocations':[{'effect_refs':refs,'events':[],'classification':'NO_LEDGER_CHANGE',
                        'description':'Traslado entre actores de la misma custodia'}],
                    'annotations':[{'reference':'unobserved-effects','disposition':'MANUAL_COMPLETE',
                        'note':'Cobertura restante revisada manualmente'}]}})
        recorded_at=clock()+timedelta(seconds=1);holder={};reads=[]
        @contextmanager
        def no_rpc():
            reads.append(True);raise AssertionError('No RPC in financial group review');yield
        def supervisor(v,connection):
            holder['vault']=v;holder['supervisor']=DexBotSupervisor(v,connection,clock=lambda:recorded_at+timedelta(seconds=1))
            return holder['supervisor']
        app=FastAPI();app.include_router(create_real_router(c.vault.path,dex_rpc_factory=no_rpc,dex_bot_supervisor_factory=supervisor))
        with TestClient(app)as client:
            headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
            try:
                prove(client,headers,c.account,Account.from_key('0x'+'11'*32))
                url=f'/real/accounts/{c.account}/external-effect-groups/{uuid4()}'
                body={'recorded_at':recorded_at.isoformat(),'declarations':declarations};original_rows=rows(holder['vault'])
                response=client.post(url+'/preview',headers=headers,json=body);assert response.status_code==200,response.text
                preview=response.json();assert preview['can_accept']and preview['events_to_create']==2
                confirmed={**body,'preview_digest':preview['preview_digest'],'confirmation':'ACEPTAR_EFECTOS_EXTERNOS_CONJUNTOS'}
                registry=holder['supervisor']
                with registry._lock:registry._starting['owned-start-reservation']=Event()
                try:
                    assert client.post(url+'/preview',headers=headers,json=body).status_code==409
                    assert client.post(url+'/confirm',headers=headers,json=confirmed).status_code==409
                    assert rows(holder['vault'])==original_rows
                finally:
                    with registry._lock:registry._starting.clear()
                response=client.post(url+'/confirm',headers=headers,json=confirmed);assert response.status_code==200,response.text
                saved=response.json();assert saved['created']and not saved['strategy_executed']and not saved['trading_authority']
                with holder['vault'].session()as db:
                    current=account_snapshot(db,c.account)
                    assert current.cash['BNB']==D(1)-D(sum(fees.values())).scaleb(-18)
                    assert current.cash['USDT']==D(100)and not current.positions
                    assert bots.run_record(db,c.started['id'])[0].status=='STOPPED'
                    old=operators.intent_record(db,c.source.id)[0]
                    assert(old.payload_json,old.state_json,old.updated_at)==c.before
                    old=swaps.swap_record(db,source.id)[0];assert(old.payload_json,old.state_json,old.updated_at)==before
                    op=operator_outcome(db,c.source.id,include_external=True)
                    assert op.outcome=='EXTERNAL_REVIEWED'and not op.filled and op.gas_fee_wei==fees['OPERATOR']
                    fact=external_outcome(db,'WALLET_SWAP',source.id)
                    assert fact.acceptance_id==saved['id']and fact.gas_fee_wei==fees['WALLET_SWAP']
                assert not registry._workers and not registry._starting and not reads
                viewed=registry.view(c.started['id'],account_id=c.account)
                displayed=next(item for item in viewed['intents']if item['id']==c.source.id)
                assert displayed['effective_outcome']=='EXTERNAL_REVIEWED'and not displayed['strategy_executed']
                assert displayed['external_review']['external_review']['acceptance_id']==saved['id']
                assert viewed['stored_status']=='STOPPED'
                after=rows(holder['vault']);assert not client.post(url+'/confirm',headers=headers,json=confirmed).json()['created']
                assert rows(holder['vault'])==after
                holder['vault'].backup(tmp_path/'mixed-cold.db')
                client.post('/real/vault/lock',headers=headers)
                headers={'X-Orbis-Vault-Token':client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']}
                restored=client.get(url,headers=headers);assert restored.status_code==200,restored.text
                assert restored.json()['id']==saved['id']and not reads
            finally:client.post('/real/vault/lock',headers=headers)
