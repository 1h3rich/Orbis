"""Actual signed disposable-chain gas receipts; no owner data or financial I/O."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone
import json
from uuid import uuid4

import pytest
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_order import collect_order,verify_gas_order,validate_gas_order
from app.portfolio.dex_gas_order_source import gas_source,gas_reference,archived_gas_source
from app.portfolio import dex_deployments as deployments
from tests.dex_evm_fixture import evm
from tests.test_dex_deployments import setup,PASSWORD
from tests.test_dex_deployment_receipt import deployment_on_evm
from tests.test_dex_call_booking import resolved_case


def now():return datetime.now(timezone.utc)


def checked(vault,kind,identifier,meta,rpc,tx_hash):
    evidence=collect_order(rpc,tx_hash)
    assert evidence is not None
    with vault.session()as db:
        changes=db.connection().exec_driver_sql('SELECT total_changes()').scalar_one()
        source=gas_source(db,kind,identifier,wallet_address=meta['owner'])
        stamp=max(now(),source.updated_at)
        proof=verify_gas_order(source,evidence,reference=gas_reference(db,source),now=stamp)
        assert db.connection().exec_driver_sql('SELECT total_changes()').scalar_one()==changes
        reopened=archived_gas_source(db,kind,identifier,proof=proof)
        assert reopened.call is None and replace(source,call=None)==reopened
        validate_gas_order(reopened,proof,reference=gas_reference(db,reopened))
        with pytest.raises(DexExecutorError):
            verify_gas_order(reopened,evidence,reference=gas_reference(db,reopened),now=now())
    encoded=json.dumps(proof).lower()
    assert proof['purpose']=='NATIVE_GAS_ONLY'and proof['gas_fee_wei']==str(int(evidence['receipt']['gasUsed'],16)*int(evidence['receipt']['effectiveGasPrice'],16))
    for secret in (meta['owner'],meta['operator'],tx_hash,evidence['transaction']['r'],evidence['transaction']['s']):
        assert secret.lower()not in encoded
    return source,proof,evidence


@pytest.mark.parametrize('typed',[False,True])
def test_signed_creation_has_real_creation_shape_and_reopens_without_wallet(tmp_path,typed):
    vault,account,at=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil')as (meta,rpc):
            plan,evidence=deployment_on_evm(vault,account,at,meta,rpc,typed=typed)
            with vault.session()as db:deployments.check_deployment(db,plan['id'],account_id=account,evidence=evidence,now=now())
            source,proof,_=checked(vault,'DEPLOYMENT',plan['id'],meta,rpc,evidence['tx_hash'])
            assert proof['signature_kind']=='CREATE'and proof['outcome']=='DEPLOYED'and proof['internal_principal_wei']=='0'
            assert source.call.target is None and source.call.data==evidence['transaction']['input'].lower()
            vault.lock();vault.unlock(PASSWORD)
            with vault.session()as db:assert archived_gas_source(db,'DEPLOYMENT',plan['id'],proof=proof).call is None
    finally:vault.lock()


@pytest.mark.parametrize('mode',['APPROVE','REVOKE','CANCELLED','REVERTED'])
@pytest.mark.parametrize('typed',[False,True])
def test_resolved_wallet_gas_keeps_actual_known_cancel_or_failed_signed_form(tmp_path,mode,typed):
    with resolved_case(tmp_path,mode,typed=typed)as case:
        vault,account,owner,meta,rpc,parent,_,resolved=case
        # This fixture's replacement is the original authenticated resolved
        # block member; no hash is taken from user input or a generated effect.
        with vault.session()as db:
            from app.portfolio.dex_call_resolution import resolution_record
            payload=resolution_record(db,resolved['id'])[1]
        block=rpc('eth_getBlockByNumber',[hex(int(payload['proof']['receipt']['block_number'])),False])
        tx_hash=block['transactions'][int(payload['proof']['transaction_index'])]
        source,proof,evidence=checked(vault,'CALL_RESOLUTION',resolved['id'],meta,rpc,tx_hash)
        assert proof['signature_kind']==('CANCEL'if mode=='CANCELLED'else 'FAILED'if mode=='REVERTED'else 'CALL')
        assert proof['outcome']==('CANCELLED'if mode=='CANCELLED'else 'REVERTED'if mode=='REVERTED'else 'EXECUTED')
        assert proof['internal_principal_wei']=='0'
        with vault.session()as db:
            with pytest.raises(DexExecutorError):gas_source(db,'CALL_RESOLUTION',resolved['id'],wallet_address=meta['operator'])
            changed=deepcopy(evidence);changed['transaction']['nonce']=hex(source.nonce+1)
            with pytest.raises(DexExecutorError):verify_gas_order(source,changed,reference=gas_reference(db,source),now=now())
            changed=deepcopy(proof);changed['gas_fee_wei']=str(int(changed['gas_fee_wei'])+1)
            with pytest.raises(DexExecutorError):archived_gas_source(db,'CALL_RESOLUTION',resolved['id'],proof=changed)


def test_executed_swap_cannot_be_downgraded_to_gas_only_source(tmp_path):
    with resolved_case(tmp_path,'BUY')as case:
        with case[0].session()as db:
            with pytest.raises(DexExecutorError,match='sin compra'):
                gas_source(db,'CALL_RESOLUTION',case[-1]['id'],wallet_address=case[3]['owner'])


def discover(vault,kind,identifier,meta,rpc):
    with vault.session()as db:
        source=gas_source(db,kind,identifier,wallet_address=meta['owner']);reference=gas_reference(db,source)
        block=rpc('eth_getBlockByNumber',[hex(int(json.loads(source.receipt_json)['block_number'])),False])
        matches=[tx for tx in block['transactions']if reference('tx',tx)==source.transaction_ref]
    assert len(matches)==1
    return matches[0]


def test_configuration_approval_revocation_and_stop_are_real_control_calls(tmp_path):
    from app.portfolio import dex_controls as controls
    from app.portfolio.models import RealDexControlIntent
    from app.live_execution.dex_state import read_executor_state
    from tests.test_dex_allowance_journal import ready
    from sqlalchemy import select
    vault,account,at=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil')as (meta,rpc):
            deployment=ready(vault,account,at,meta,rpc)
            with vault.session()as db:
                config=next(identifier for identifier in db.scalars(select(RealDexControlIntent.id))
                    if controls.control_record(db,identifier)[1]['action']=='CONFIGURE')
            _,proof,_=checked(vault,'CONTROL',config,meta,rpc,discover(vault,'CONTROL',config,meta,rpc))
            assert proof['action']=='CONFIGURE'and proof['signature_kind']=='CONTROL'
            for action in ('APPROVE','REVOKE','STOP'):
                state=read_executor_state(rpc,meta['owner'],int(deployment['nonce']),500,now=now())
                with vault.session()as db:
                    plan=controls.prepare_control(db,identifier=str(uuid4()),account_id=account,deployment_id=deployment['id'],
                        wallet_address=meta['owner'],action=action,operator=None,limits=None,snapshot=state,
                        gas='1000000',gas_price='1000000000',max_gas_bnb='0.002',now=now(),
                        **({'token':'USDT','allowance_units':'1'if action=='APPROVE'else '0'}if action!='STOP'else {}))
                with vault.session()as db:controls.claim_control(db,plan['id'],account_id=account,wallet_address=meta['owner'],operator=None,
                    plan_digest=plan['plan_digest'],snapshot=state,now=now())
                sent=rpc('eth_sendTransaction',[plan['transaction']]);raw=collect_order(rpc,sent)
                evidence={**raw,'pool':state.pool,'runtime':rpc('eth_getCode',[state.executor_address,raw['block']['number']])}
                with vault.session()as db:controls.report_control(db,plan['id'],account_id=account,tx_hash=sent,now=now())
                with vault.session()as db:controls.check_control(db,plan['id'],account_id=account,evidence=evidence,now=now())
                _,proof,_=checked(vault,'CONTROL',plan['id'],meta,rpc,sent)
                assert proof['action']==action and proof['outcome']=='EXECUTED'and proof['internal_principal_wei']=='0'
    finally:vault.lock()


@pytest.mark.parametrize('typed',[False,True])
def test_native_funding_retains_positive_signed_principal_separately_from_gas(tmp_path,typed):
    from tests.test_dex_gas_funding import case,prepare,claim
    from app.portfolio import dex_gas_funding as funding
    with case(tmp_path,evm_backend='anvil')as values:
        vault,account,_,draft,meta,rpc,_,_=values;plan=prepare(values);claim(values,plan)
        tx=dict(plan['transaction'])
        if typed:
            tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(1200000000),maxPriorityFeePerGas=hex(100000000),accessList=[])
        sent=rpc('eth_sendTransaction',[tx]);evidence=collect_order(rpc,sent)
        with vault.session()as db:funding.report_funding(db,plan['id'],account_id=account,tx_hash=sent,now=now())
        with vault.session()as db:funding.check_funding(db,plan['id'],account_id=account,evidence=evidence,now=now())
        source,proof,_=checked(vault,'GAS_FUNDING',plan['id'],meta,rpc,sent)
        assert proof['internal_principal_wei']==str(10**15)and proof['gas_fee_wei']!=proof['internal_principal_wei']
        assert source.call.kind=='NATIVE'and source.call.value==10**15
        assert draft['operator_address'].lower()not in json.dumps(proof).lower()
        from app.portfolio.wallet_funding import _encoded
        altered=json.loads(source.receipt_json);altered['transferred_wei']=str(2*10**15)
        receipt_json,receipt_digest=_encoded(altered)
        with vault.session()as db:
            with pytest.raises(DexExecutorError):verify_gas_order(replace(source,receipt_json=receipt_json,receipt_digest=receipt_digest),
                evidence,reference=gas_reference(db,source),now=now())


@pytest.mark.parametrize('typed',[False,True])
@pytest.mark.parametrize('cancel',[False,True])
def test_native_resolution_uses_accepted_changed_amount_or_actual_self_cancel(tmp_path,typed,cancel):
    from tests.test_dex_gas_funding import case,prepare,claim
    from app.live_execution.dex_native_resolution import collect_resolution
    from app.portfolio import dex_native_resolution as resolution
    with case(tmp_path,evm_backend='anvil')as values:
        vault,account,_,draft,meta,rpc,_,_=values;plan=prepare(values);claim(values,plan)
        amount=0 if cancel else 8*10**14
        tx={'from':meta['owner'],'to':meta['owner']if cancel else draft['operator_address'],'data':'0x','value':hex(amount),
            'nonce':hex(int(plan['nonce'])),'gas':'0x5208','gasPrice':hex(10**9)}
        if typed:
            tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(1200000000),maxPriorityFeePerGas=hex(100000000),accessList=[])
        sent=rpc('eth_sendTransaction',[tx]);evidence=collect_resolution(rpc,sent)
        scope={'account_id':account,'wallet_address':meta['owner'],'evidence':evidence,'accept_changes':True,'now':now()}
        with vault.session()as db:proposal=resolution.preview_resolution(db,'GAS_FUNDING',plan['id'],**scope)
        with vault.session()as db:result=resolution.confirm_resolution(db,'GAS_FUNDING',plan['id'],preview_digest=proposal['preview_digest'],
            source_state_digest=proposal['source_state_digest'],**scope)
        source,proof,_=checked(vault,'NATIVE_RESOLUTION',result['id'],meta,rpc,sent)
        assert proof['internal_principal_wei']==str(amount)
        assert proof['signature_kind']==('CANCEL'if cancel else 'NATIVE')
        if not cancel:assert source.call.value==amount and source.call.value!=int(plan['amount_wei'])


@pytest.mark.parametrize('typed',[False,True])
@pytest.mark.parametrize('cancel',[False,True])
def test_deployment_resolution_preserves_actual_create_or_cancellation_shape(tmp_path,typed,cancel):
    from tests.test_dex_deployment_resolution import replacement_case,review,commit
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil')as (meta,rpc):
            plan,sent,evidence=replacement_case(vault,account,meta,rpc,cancel=cancel,typed=typed)
            proposal=review(vault,account,meta,plan,evidence)
            result=commit(vault,account,meta,plan,evidence,proposal)
            _,proof,_=checked(vault,'DEPLOYMENT_RESOLUTION',result['id'],meta,rpc,sent)
            assert proof['signature_kind']==('CANCEL'if cancel else 'CREATE')
            assert proof['outcome']==('CANCELLED'if cancel else 'DEPLOYED')
    finally:vault.lock()


@pytest.mark.parametrize('typed',[False,True])
@pytest.mark.parametrize('action',['APPROVE','REVOKE','REVERTED_SWAP'])
def test_original_wallet_permission_or_reverted_swap_is_not_a_fabricated_trade(tmp_path,typed,action):
    from tests.test_dex_call_resolution import prepare
    from app.live_execution.dex_call_resolution import collect_call_resolution
    from app.portfolio import wallet_swap as swaps
    from tests.test_dex_operator import write
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil')as (meta,rpc):
            if action=='REVERTED_SWAP':write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,True])
            plan,call,_=prepare(vault,account,meta,rpc,action='SWAP'if action=='REVERTED_SWAP'else action)
            tx=dict(plan['transaction'])
            if typed:
                tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(1200000000),maxPriorityFeePerGas=hex(100000000),accessList=[])
            sent=rpc('eth_sendTransaction',[tx]);observed=collect_call_resolution(rpc,call,sent)
            evidence={name:observed[name]for name in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
            with vault.session()as db:swaps.report_swap(db,plan['id'],account_id=account,tx_hash=sent,now=now())
            if typed:
                # Legacy v1 stays legacy-only. Observe the actual typed bytes
                # through the existing explicit resolution path instead.
                from app.portfolio import dex_call_resolution as resolution
                with vault.session()as db:
                    with pytest.raises(swaps.WalletSwapError):swaps.check_swap(db,plan['id'],account_id=account,evidence=evidence,now=now())
                scope={'account_id':account,'wallet_address':meta['owner'],'evidence':observed,'accept_changes':True,'now':now()}
                with vault.session()as db:proposal=resolution.preview_resolution(db,'WALLET_SWAP',plan['id'],**scope)
                with vault.session()as db:result=resolution.confirm_resolution(db,'WALLET_SWAP',plan['id'],preview_digest=proposal['preview_digest'],
                    source_state_digest=proposal['source_state_digest'],**scope)
                _,proof,_=checked(vault,'CALL_RESOLUTION',result['id'],meta,rpc,sent)
            else:
                with vault.session()as db:swaps.check_swap(db,plan['id'],account_id=account,evidence=evidence,now=now())
                _,proof,_=checked(vault,'WALLET_SWAP',plan['id'],meta,rpc,sent)
            assert proof['signature_kind']==('FAILED'if typed and action=='REVERTED_SWAP'else 'CALL')
            assert proof['outcome']==('REVERTED'if action=='REVERTED_SWAP'else 'EXECUTED')
    finally:vault.lock()


@pytest.mark.parametrize('stop',[False,True])
def test_original_operator_stop_or_failed_buy_has_actual_operator_signature(tmp_path,stop):
    from tests.test_dex_operator_journal import case,prepared_execution,evidence
    from tests.test_dex_operator import write
    from app.live_execution.dex_trade import read_stop_plan
    from app.portfolio import dex_operator_journal as journal
    with case(tmp_path,evm_backend='anvil')as (vault,account,meta,rpc,deployment,grant,trade):
        if stop:trade=read_stop_plan(rpc,meta['owner'],int(deployment['nonce']),500,gas=100000,gas_price=10**9,max_gas_wei=10**14,now=now())
        session,prepared=prepared_execution(vault,account,grant,trade)
        try:
            if not stop:write(rpc,{**meta,'operator':meta['owner']},meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,True])
            hashes=[]
            def post(raw):
                sent=rpc('eth_sendRawTransaction',['0x'+raw.hex()]);hashes.append(sent);return sent
            prepared.broadcast_once(post,now=now())
            with vault.session()as db:journal.check_intent(db,prepared.identifier,account_id=account,
                evidence=evidence(meta,rpc,trade,hashes[0]),now=now())
            source,proof,_=checked(vault,'OPERATOR',prepared.identifier,meta,rpc,hashes[0])
            assert source.signer_digest!=source.owner_digest
            assert proof['outcome']==('EXECUTED'if stop else 'REVERTED')and proof['signature_kind']=='CALL'
        finally:session.revoke()


@pytest.mark.parametrize('variant',['ORIGINAL','CANCELLED','TRANSFERRED'])
def test_native_return_binds_operator_sender_and_owner_recipient_without_conflating_principal(tmp_path,variant):
    from contextlib import nullcontext
    from datetime import timedelta
    from eth_account import Account
    from eth_utils import to_checksum_address
    from tests.dex_bot_worker_fixture import running_case
    from app.live_execution.dex_bot_worker import DexBotWorker
    from app.live_execution.dex_gas_return import read_return_state,plan_return
    from app.portfolio import dex_gas_return as journal
    with running_case(tmp_path,evm_backend='anvil')as (values,started,claim,connection,clock,mine,market):
        vault,account,manager,draft,meta,rpc,_,binding=values
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0]);worker.stop()
        clock[0]+=timedelta(seconds=1);mine()
        current=plan_return(read_return_state(rpc,meta['owner'],draft['operator_address'],now=clock[0]),
            gas_price=10**9,max_gas_wei=21000*10**9,now=clock[0]);identifier=str(uuid4())
        with vault.session()as db:prepared=journal.prepare_return(db,identifier=identifier,account_id=account,binding_id=binding['id'],
            draft_id=draft['id'],wallet_address=meta['owner'],drafts=manager,plan=current,now=clock[0])
        with vault.session()as db:journal.reserve_return(db,identifier,account_id=account,wallet_address=meta['owner'],
            drafts=manager,draft_id=draft['id'],plan=current,plan_digest=prepared['plan_digest'],now=clock[0])
        tx=current.transaction();fields={key:to_checksum_address(value)if key=='to'else value if key=='data'else int(value,16)
            for key,value in tx.items()if key not in ('from','type')}
        with manager._copy(draft['id'],account,meta['owner'],clock[0])as (_,key):signed=Account.sign_transaction(fields,bytes(key))
        tx_hash='0x'+signed.hash.hex()
        with vault.session()as db:journal.record_signature(db,identifier,account_id=account,plan=current,
            raw=bytes(signed.raw_transaction),tx_hash=tx_hash,now=clock[0])
        with vault.session()as db:journal.mark_broadcast(db,identifier,account_id=account,
            plan_digest=prepared['plan_digest'],tx_hash=tx_hash,now=clock[0])
        if variant=='ORIGINAL':
            assert rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])==tx_hash
            with vault.session()as db:journal.check_return(db,identifier,account_id=account,evidence=collect_order(rpc,tx_hash),now=clock[0])
            kind,source_id,amount='GAS_RETURN',identifier,current.amount_wei
        else:
            from app.live_execution.dex_native_resolution import collect_resolution
            from app.portfolio import dex_native_resolution as resolution
            amount=0 if variant=='CANCELLED'else current.amount_wei//2
            replacement={**fields,'to':to_checksum_address(draft['operator_address']if variant=='CANCELLED'else meta['owner']),'value':amount}
            with manager._copy(draft['id'],account,meta['owner'],clock[0])as (_,key):
                other=Account.sign_transaction(replacement,bytes(key))
            tx_hash=rpc('eth_sendRawTransaction',['0x'+other.raw_transaction.hex()])
            scope={'account_id':account,'wallet_address':meta['owner'],'evidence':collect_resolution(rpc,tx_hash),'accept_changes':True,'now':clock[0]}
            with vault.session()as db:preview=resolution.preview_resolution(db,'GAS_RETURN',identifier,**scope)
            with vault.session()as db:result=resolution.confirm_resolution(db,'GAS_RETURN',identifier,preview_digest=preview['preview_digest'],
                source_state_digest=preview['source_state_digest'],**scope)
            kind,source_id='NATIVE_RESOLUTION',result['id']
        source,proof,_=checked(vault,kind,source_id,meta,rpc,tx_hash)
        assert source.signer_digest!=source.owner_digest and proof['internal_principal_wei']==str(amount)
        assert proof['signature_kind']==('CANCEL'if variant=='CANCELLED'else 'NATIVE')
        assert proof['gas_fee_wei']==str(21000*10**9)


def test_signature_block_status_gas_and_certificate_tampering_fail_closed(tmp_path):
    vault,account,at=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil')as (meta,rpc):
            plan,evidence=deployment_on_evm(vault,account,at,meta,rpc,typed=True)
            with vault.session()as db:deployments.check_deployment(db,plan['id'],account_id=account,evidence=evidence,now=now())
            source,proof,raw=checked(vault,'DEPLOYMENT',plan['id'],meta,rpc,evidence['tx_hash'])
            changes=[('transaction','input','0xdeadbeef'),('transaction','value','0x1'),('transaction','r','0x1'),
                ('transaction','nonce',hex(source.nonce+1)),('transaction','transactionIndex','0x7'),
                ('receipt','gasUsed','0x5208'),('receipt','effectiveGasPrice','0x1'),('receipt','status','0x0'),
                ('receipt','transactionIndex','0x1'),('receipt','blockHash','0x'+'ff'*32),
                ('receipt','contractAddress',meta['operator']),('finalized','number','0x1')]
            with vault.session()as db:
                reference=gas_reference(db,source)
                for section,key,value in changes:
                    changed=deepcopy(raw);changed[section][key]=value
                    with pytest.raises(DexExecutorError):verify_gas_order(source,changed,reference=reference,now=now())
                changed=deepcopy(raw);changed['block']['transactions']=[raw['tx_hash'],raw['tx_hash']]
                with pytest.raises(DexExecutorError):verify_gas_order(source,changed,reference=reference,now=now())
                for key,value in [('owner_digest','f'*64),('gas_fee_wei','1'),('signature_kind','CALL'),('outcome','TRANSFERRED')]:
                    changed=deepcopy(proof);changed[key]=value
                    with pytest.raises(DexExecutorError):archived_gas_source(db,'DEPLOYMENT',plan['id'],proof=changed)
    finally:vault.lock()


def test_read_port_never_creates_missing_wallet_identity(tmp_path):
    from app.portfolio.models import RealWalletIdentitySalt
    vault,_,_=setup(tmp_path)
    try:
        with vault.session()as db:
            assert db.get(RealWalletIdentitySalt,1)is None
            with pytest.raises(DexExecutorError):gas_source(db,'CONTROL',str(uuid4()),wallet_address='0x'+'12'*20)
            assert db.get(RealWalletIdentitySalt,1)is None and not db.new and not db.dirty
    finally:vault.lock()


def test_certificate_cannot_follow_a_source_moved_to_another_context(tmp_path):
    from app.portfolio.models import RealDexDeploymentIntent
    from app.portfolio.repository import create_account
    from app.portfolio.wallet_funding import _encoded
    vault,account,at=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil')as (meta,rpc):
            plan,evidence=deployment_on_evm(vault,account,at,meta,rpc)
            with vault.session()as db:deployments.check_deployment(db,plan['id'],account_id=account,evidence=evidence,now=now())
            _,proof,_=checked(vault,'DEPLOYMENT',plan['id'],meta,rpc,evidence['tx_hash'])
            with vault.session()as db:other=create_account(db,'Otro contexto','USDT').id
            with vault.session()as db:
                row=db.get(RealDexDeploymentIntent,plan['id']);original=(row.account_id,row.payload_json,row.payload_digest)
                terms=json.loads(row.payload_json);terms['account_id']=other
                row.account_id=other;row.payload_json,row.payload_digest=_encoded(terms);db.commit()
            try:
                with vault.session()as db:
                    # Its own legacy structure remains internally valid; the
                    # old certificate must still reject the changed context.
                    assert deployments.deployment_record(db,plan['id'])[0].account_id==other
                    with pytest.raises(DexExecutorError):archived_gas_source(db,'DEPLOYMENT',plan['id'],proof=proof)
            finally:
                with vault.session()as db:
                    row=db.get(RealDexDeploymentIntent,plan['id']);row.account_id,row.payload_json,row.payload_digest=original;db.commit()
            with vault.session()as db:assert archived_gas_source(db,'DEPLOYMENT',plan['id'],proof=proof).account_id==account
    finally:vault.lock()
