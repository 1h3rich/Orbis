"""Owned bot replacement, signed with its disposable generated fixture key."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
import json

from eth_account import Account
from sqlalchemy import select

from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_operator_transport import BnbOperatorSender
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.live_execution.dex_external_order import common_reference, verify_external_order
from app.live_execution.dex_external_observation import source_reference
from app.portfolio import dex_operator_journal as operators, dex_call_resolution as calls, dex_bot_journal as bots
from app.portfolio import dex_external_observation as observations, dex_external_order as orders
from app.portfolio import economic_external_acceptance as accepted
from app.portfolio.domain import EventInput
from app.portfolio.economic_external_capture import capture_external_effects
from app.portfolio.economic_external_api import ExternalReview, _prepare
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.models import RealDexOperatorIntent
from app.portfolio.repository import append_event
from app.portfolio.wallet_links import save_wallet_link
from tests.dex_bot_worker_fixture import running_case
from tests.dex_dca_worker_fixture import dca_running_case
from tests.dex_evm_fixture import wait_receipt
from tests.test_dex_external_observation_journal import proposal, save


@contextmanager
def external_bot_case(path, monkeypatch, kind='LONG'):
    factory=dca_running_case if kind=='DCA'else running_case
    with factory(path,evm_backend='anvil')as(values,started,claim,connection,clock,mine,market):
        vault,account,manager,draft,meta,rpc,deployment,binding=values;hashes=[]
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        def sender(plan):
            item=BnbOperatorSender(plan,clock=lambda:clock[0]);send=item.send
            def post(raw):
                tx=send(raw);hashes.append(tx);return tx
            item.send=post;return item
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,
            wallet_address=meta['owner'],market=market,rpc_factory=lambda:nullcontext(rpc),sender_factory=sender,clock=lambda:clock[0])
        try:
            clock[0]+=timedelta(seconds=1);mine();rpc('miner_stop',[])
            first=worker.run_once();assert first['last_action']=='BUY'and len(hashes)==1,first
            with vault.session()as db:
                row=db.scalar(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==binding['grant_id']))
                row,terms,state=operators.intent_record(db,row.id)
                source=calls.source_record(db,'OPERATOR',row.id,owner=meta['owner'])[3]
                before=(row.payload_json,row.state_json,row.updated_at)
            # The key belongs ONLY to this disposable test; no owner wallet or
            # production secret is read. Real code has no raw-key signing API.
            with manager._lock:signing_key=bytes(manager._draft.key)
            tx={'chainId':56,'nonce':source.nonce,'to':meta['owner'],'value':10**13,'gas':21000,'gasPrice':2*10**9}
            signed=Account.sign_transaction(tx,signing_key);del signing_key
            sent=rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()]);rpc('evm_mine',[]);rpc('miner_start',[1]);wait_receipt(rpc,sent)
            assert rpc('eth_getTransactionReceipt',[hashes[0]])is None
            at=datetime.fromtimestamp(int(rpc('eth_getBlockByNumber',['latest',False])['timestamp'],16),timezone.utc)
            clock[0]=max(clock[0],at)+timedelta(seconds=1);worker.stop()
            with vault.session()as db:
                assert bots.run_record(db,started['id'])[0].status=='STOPPED'
                assert operators.grant_record(db,binding['grant_id'])[0].status=='ACTIVE'
                salt=_existing_salt(db)
            evidence=collect_call_resolution(rpc,source,sent)
            c=SimpleNamespace(vault=vault,account=account,meta=meta,source=source,evidence=evidence,
                reference=source_reference('OPERATOR',salt),now=clock[0],hash=sent)
            observed=save(c,proposal(c))
            with vault.session()as db:
                row,body=observations.observation_record(db,observed['id'])
                cert=verify_external_order(source,row.id,row.payload_digest,body['proof'],evidence,
                    reference=c.reference,common=common_reference(salt),now=clock[0])
                preview=orders.preview(row,cert)
            with vault.session()as db:orders.save_order(db,observed['id'],account_id=account,proof=cert,preview_digest=preview['preview_digest'],now=clock[0])
            with vault.session()as db:save_wallet_link(db,account,meta['owner'],'0x38','Principal');db.commit()
            for currency,amount in [('USDT','100'),('BNB','1')]:
                with vault.session()as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION',currency,
                    at-timedelta(hours=1),amount=Decimal(amount)))
            def capture(db):return capture_external_effects(db,'OPERATOR',source.id,context_account_id=account,owner_address=meta['owner'])
            def accept():
                clock[0]+=timedelta(seconds=1)
                with vault.session()as db:inputs=capture(db)
                gas=str(uuid4());proof=json.loads(inputs.observation_json)
                body=ExternalReview(address=meta['owner'],review_id=str(uuid4()),new_events=[{'key':gas,'account_id':account,
                    'kind':'FEE','quote_currency':'BNB','amount':str(Decimal(proof['receipt']['gas_fee_wei']).scaleb(-18)),
                    'note':'Gas externo observado'}],members=[{'event':{'new_key':gas},'role':'GAS'}],
                    allocations=[{'effect_refs':['native:'+proof['call_ref']],'events':[],'classification':'NO_LEDGER_CHANGE',
                        'description':'Retorno entre actores de la misma custodia'}],annotations=[{'reference':'unobserved-effects',
                            'disposition':'MANUAL_COMPLETE','note':'Revisión explícita de cobertura restante'}])
                plan=_prepare(inputs,body,clock[0]);generation=vault.session_generation
                prepared=accepted.prepare_external_acceptance(vault,inputs,plan,capture=capture,
                    generation=generation,selection_digest=plan.digest)
                clock[0]+=timedelta(seconds=1)
                return accepted.confirm_external_acceptance(vault,prepared,capture=capture,preview_digest=prepared.digest,
                    now=clock[0],generation=generation,wallet_addresses=(meta['owner'],))
            yield SimpleNamespace(values=values,vault=vault,account=account,manager=manager,draft=draft,meta=meta,rpc=rpc,
                deployment=deployment,binding=binding,started=started,claim=claim,connection=connection,clock=clock,mine=mine,
                market=market,source=source,before=before,observed=observed,certificate=cert,accept=accept,hashes=hashes,worker=worker)
        finally:worker.stop()
