"""Signed disposable-chain observations, detached verification and hostile inputs."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from eth_abi import encode
from eth_utils import keccak
import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_call_resolution import collect_call_resolution, verify_call_resolution, wallet_source, operator_source
from app.live_execution.dex_external_observation import verify_external_call, validate_external_proof, source_reference, MAX_LOGS
from app.portfolio import wallet_swap as swaps, dex_operator_journal as operators
from app.portfolio.models import RealWalletIdentitySalt
from app.portfolio.vault import RealVault
from tests.dex_evm_fixture import evm, wait_receipt
from tests.test_wallet_swap import setup, PASSWORD
from tests.test_dex_call_resolution import prepare
from tests.test_dex_operator import write, KEY


def calldata(signature, types, values):
    return '0x'+(keccak(text=signature)[:4]+encode(types,values)).hex()


def reference(vault, kind='WALLET_SWAP'):
    with vault.session() as db:
        salt=bytes.fromhex(db.get(RealWalletIdentitySalt,1).salt_hex)
        result=source_reference(kind,salt)
        original=swaps._identity if kind=='WALLET_SWAP'else operators._identity
        for label,value in [('wallet','0x'+'11'*20),('tx','0x'+'12'*32),('external-observation-v1:log','{"ASSET":"USDT"}')]:
            assert result(label,value)==original(db,label,value)
    return result


@contextmanager
def external_case(directory, mode='TRANSFER', typed=False, *, zero_gas=False):
    vault,account,_=setup(directory)
    try:
      with evm(directory/'evm',backend='anvil')as(meta,rpc),pytest.MonkeyPatch.context()as patch:
        write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
        for token,recipient in [('usdt',meta['owner']),('wbnb',meta['pool'])]:
            write(rpc,meta,meta[token],'mint(address,uint256)',['address','uint256'],[recipient,10**18])
        write(rpc,{**meta,'operator':meta['owner']},meta['usdt'],'approve(address,uint256)',
            ['address','uint256'],[meta['router'],10**18])
        if mode == 'WBNB_TRANSFER':
            write(rpc,meta,meta['wbnb'],'mint(address,uint256)',['address','uint256'],[meta['owner'],10**18])
        def clock():
            block=rpc('eth_getBlockByNumber',['latest',False])
            return max(datetime.now(timezone.utc),datetime.fromtimestamp(int(block['timestamp'],16),timezone.utc))+timedelta(seconds=2)
        patch.setattr('tests.test_dex_call_resolution.now',clock)
        plan,source,before=prepare(vault,account,meta,rpc)
        tx={**plan['transaction'],'gasPrice':hex(2*10**9)}
        amount=0 if mode=='ZERO'else 5*10**15
        if mode in ('TRANSFER','ZERO','SELF','WBNB_TRANSFER'):
            tx.update(to=meta['wbnb' if mode=='WBNB_TRANSFER' else 'usdt'],data=calldata('transfer(address,uint256)',
                ['address','uint256'],[meta['owner']if mode=='SELF'else meta['operator'],amount]))
        elif mode=='APPROVAL':
            tx.update(to=meta['usdt'],data=calldata('approve(address,uint256)',['address','uint256'],[meta['operator'],7*10**15]))
        elif mode in ('MINT','UNKNOWN_TOKEN'):
            target=meta['wbnb']
            if mode=='UNKNOWN_TOKEN':
                target='0x'+'77'*20;rpc('anvil_setCode',[target,rpc('eth_getCode',[meta['usdt'],'latest'])])
            tx.update(to=target,data=calldata('mint(address,uint256)',['address','uint256'],[meta['owner'],amount]))
        elif mode=='NATIVE':tx.update(to=meta['operator'],data='0x',value=hex(10**15),gas=hex(21000))
        elif mode=='CREATE':tx.pop('to');tx.update(data='0x6001600c60003960016000f300')
        elif mode=='NO_LOGS':tx.update(data=calldata('set(uint256,bool)',['uint256','bool'],[9999,False]))
        elif mode=='CANCEL':tx.update(to=meta['owner'],data='0x',gas=hex(21000))
        elif mode=='REVERTED':tx.update(to=meta['usdt'],data='0xdeadbeef')
        elif mode!='KNOWN':raise AssertionError(mode)
        if typed:
            tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(2*10**9),maxPriorityFeePerGas=hex(10**9))
        if zero_gas:
            assert not typed
            rpc('anvil_setNextBlockBaseFeePerGas',['0x0']);tx['gasPrice']='0x0'
        sent=rpc('eth_sendTransaction',[tx]);wait_receipt(rpc,sent)
        evidence=collect_call_resolution(rpc,source,sent)
        yield SimpleNamespace(vault=vault,account=account,meta=meta,rpc=rpc,plan=plan,source=source,
            before=before,evidence=evidence,reference=reference(vault),now=clock(),hash=sent)
    finally:vault.lock()


@pytest.mark.parametrize('typed',[False,True])
@pytest.mark.parametrize('mode',['TRANSFER','ZERO','SELF','APPROVAL','MINT','UNKNOWN_TOKEN','NATIVE','CREATE','NO_LOGS'])
def test_actual_external_success_keeps_exact_facts_without_authority_or_resolution(tmp_path,typed,mode):
    with external_case(tmp_path,mode,typed)as case:
        with case.vault.session()as db:changes=db.connection().connection.driver_connection.total_changes
        result=verify_external_call(case.source,case.evidence,reference=case.reference,now=case.now)
        assert result['outcome']=='EXTERNAL_SUCCEEDED'
        assert result['gas_payer_role']=='OWNER'
        assert result['native_coverage']=='TOP_LEVEL_VALUE_AND_GAS_ONLY'
        assert result['token_coverage']=='REGISTERED_TOKEN_EVENTS_ONLY'
        assert all(result[name]is False for name in ('resolves_intent','trading_authority','effects_complete','accounting_complete'))
        assert result['receipt']['transaction_type']==(2 if typed else 0)
        expected_gas=int(case.evidence['receipt']['gasUsed'],16)*int(case.evidence['receipt']['effectiveGasPrice'],16)
        assert result['receipt']['gas_fee_wei']==str(expected_gas)
        if mode in ('TRANSFER','ZERO','SELF','MINT'):
            assert len(result['transfers'])==1 and not result['approvals']and not result['uninterpreted_logs']
            effect=result['transfers'][0]
            assert effect['units']==str(0 if mode=='ZERO'else 5*10**15)
            assert effect['left_role']==('ZERO'if mode=='MINT'else'OWNER')
            assert effect['right_role']==('OWNER'if mode in ('MINT','SELF')else'OTHER')
        elif mode=='APPROVAL':
            assert not result['transfers']and len(result['approvals'])==1
            assert result['approvals'][0]['units']==str(7*10**15)
        elif mode=='UNKNOWN_TOKEN':
            assert not result['transfers']and len(result['uninterpreted_logs'])==1
        elif mode=='NATIVE':
            assert result['native_value_wei']==str(10**15)and result['call_kind']=='NATIVE_TRANSFER'
        elif mode=='CREATE':assert result['created_ref']and result['target_ref']is None and result['call_kind']=='CREATE'
        elif mode=='NO_LOGS':assert not any(result[key]for key in ('transfers','approvals','uninterpreted_logs'))
        with case.vault.session()as db:
            row,_,_=swaps.swap_record(db,case.plan['id'])
            assert (row.payload_json,row.state_json,row.updated_at)==case.before
            assert db.connection().connection.driver_connection.total_changes==changes
        text=json.dumps(result).lower()
        for raw in (case.meta['owner'],case.meta['operator'],case.evidence['transaction']['to']or'',case.hash):
            if raw:assert raw.lower()not in text
        assert 'price'not in result and 'trade'not in result


@pytest.mark.parametrize('mode',['KNOWN','CANCEL','REVERTED'])
def test_existing_outcome_profiles_remain_distinct(tmp_path,mode):
    with external_case(tmp_path,mode)as case:
        with pytest.raises(DexExecutorError):verify_external_call(case.source,case.evidence,reference=case.reference,now=case.now)
        result=verify_call_resolution(case.source,case.evidence,reference=case.reference,now=case.now)
        assert result['outcome']=={'KNOWN':'EXECUTED','CANCEL':'CANCELLED','REVERTED':'REVERTED'}[mode]


def test_signature_nonce_block_log_and_fee_mutations_never_become_observations(tmp_path):
    with external_case(tmp_path)as case:
        mutations=[('transaction','nonce','0xffff'),('transaction','s','0x1'),('transaction','hash','0x'+'12'*32),
            ('transaction','input','0xdeadbeef'),('transaction','value','0x1'),('transaction','chainId','0x1'),
            ('transaction','transactionIndex','0xffff'),('receipt','transactionIndex','0xffff'),
            ('receipt','status','0x0'),('receipt','blockHash','0x'+'12'*32),('receipt','effectiveGasPrice','0x1'),
            ('receipt','gasUsed','0x1'),('block','transactions',[]),('finalized','number','0x0')]
        for section,key,value in mutations:
            bad=deepcopy(case.evidence);bad[section][key]=value
            with pytest.raises(DexExecutorError):verify_external_call(case.source,bad,reference=case.reference,now=case.now)
        for key,value in [('removed',True),('blockNumber','0x0'),('transactionHash','0x'+'12'*32),
                          ('topics',[case.evidence['receipt']['logs'][0]['topics'][0]]),('data','0x01')]:
            bad=deepcopy(case.evidence);bad['receipt']['logs'][0][key]=value
            with pytest.raises(DexExecutorError):verify_external_call(case.source,bad,reference=case.reference,now=case.now)
        for altered in (replace(case.source,signer_digest='f'*64),replace(case.source,terms={**case.source.terms,'nonce':'999'})):
            with pytest.raises(DexExecutorError):verify_external_call(altered,case.evidence,reference=case.reference,now=case.now)


def test_archive_reopens_without_live_address_and_rejects_tampering_and_source_changes(tmp_path):
    with external_case(tmp_path)as case:
        proof=verify_external_call(case.source,case.evidence,reference=case.reference,now=case.now)
        proof=json.loads(json.dumps(proof))
        backup=case.vault.backup(tmp_path/'external-cold.db');cold=RealVault(backup);cold.unlock(PASSWORD)
        try:
            with cold.session()as db:
                row,terms,state=swaps.swap_record(db,case.plan['id'])
                detached=wallet_source(row,terms,state,'0x'+'01'*20)
            cold.lock()
            validate_external_proof(proof,detached,reference=case.reference,now=case.now)
        finally:cold.lock()
        for key,value in [('resolves_intent',True),('trading_authority',True),('effects_complete',True),('native_value_wei','01'),
                          ('gas_payer_role','SIGNER'),('token_registry','current-unversioned'),
                          ('signer_is_owner',False),('observation_ref','f'*64),('source_id','f'*36)]:
            bad=deepcopy(proof);bad[key]=value
            with pytest.raises(ValueError):validate_external_proof(bad,case.source,reference=case.reference,now=case.now)
        for key,value in [('gas_fee_wei','1'),('effective_gas_price','1'),('block_number','0'),('transaction_type',True)]:
            bad=deepcopy(proof);bad['receipt'][key]=value
            with pytest.raises(ValueError):validate_external_proof(bad,case.source,reference=case.reference,now=case.now)
        for source in (replace(case.source,created_at=case.source.created_at+timedelta(seconds=1)),
                       replace(case.source,observation={'block_number':proof['receipt']['block_number'],'block_hash':proof['receipt']['block_hash'],'operator_nonce':proof['source_nonce']})):
            with pytest.raises((ValueError,DexExecutorError)):validate_external_proof(proof,source,reference=case.reference,now=case.now)


def test_limits_and_other_sender_owner_roles_are_not_silently_truncated(tmp_path):
    with external_case(tmp_path)as case:
        bad=deepcopy(case.evidence);bad['receipt']['logs']*=MAX_LOGS+1
        with pytest.raises(DexExecutorError):verify_external_call(case.source,bad,reference=case.reference,now=case.now)
        bad=deepcopy(case.evidence);bad['receipt']['logs']*=2
        with pytest.raises(DexExecutorError):verify_external_call(case.source,bad,reference=case.reference,now=case.now)
        proof=verify_external_call(case.source,case.evidence,reference=case.reference,now=case.now)
        bad=deepcopy(proof);bad['transfers'][0]['right_role']='OWNER'
        with pytest.raises(ValueError):validate_external_proof(bad,case.source,reference=case.reference,now=case.now)


def test_real_operator_signature_retains_distinct_owner_and_gas_payer(tmp_path):
    from app.live_execution.dex_operator import DexOperatorSession
    from tests.test_dex_operator_journal import case as operator_case, reserve
    with operator_case(tmp_path,evm_backend='anvil')as(vault,account,meta,rpc,deployment,grant,trade):
        intent=reserve(vault,account,grant,trade);session=DexOperatorSession(vault)
        try:
            now=datetime.now(timezone.utc)
            session.connect(private_key=KEY,snapshot=trade.snapshot,ttl_seconds=120,gas_budget_wei=2*10**15,backup_confirmed=True,now=now)
            signed=session.sign_swap(trade,now=now)
            with vault.session()as db:operators.record_signature(db,intent['id'],account_id=account,signed=signed,plan=trade,now=now)
            with vault.session()as db:
                row,terms,state=operators.intent_record(db,intent['id']);parent,grant_body,_=operators.grant_record(db,row.grant_id)
                source=operator_source(row,terms,state,grant_body,meta['owner'],500);original=row.state_json
            tx={'from':meta['operator'],'to':meta['owner'],'data':'0x','value':hex(10**15),
                'nonce':hex(source.nonce),'chainId':'0x38','gas':hex(21000),'gasPrice':hex(2*10**9)}
            sent=rpc('eth_sendTransaction',[tx]);observed=collect_call_resolution(rpc,source,sent)
            now=max(datetime.now(timezone.utc),datetime.fromtimestamp(int(observed['block']['timestamp'],16),timezone.utc))+timedelta(seconds=2)
            proof=verify_external_call(source,observed,reference=reference(vault,'OPERATOR'),now=now)
            assert not proof['signer_is_owner']and proof['native_target_role']=='OWNER'
            assert proof['gas_payer_role']=='SIGNER'
            assert proof['native_value_wei']==str(10**15)and proof['receipt']['gas_fee_wei']==str(21000*2*10**9)
            with vault.session()as db:assert operators.intent_record(db,intent['id'])[0].state_json==original
        finally:session.revoke()


def test_advancing_finalized_head_and_provider_annotations_do_not_change_receipt_identity(tmp_path):
    with external_case(tmp_path)as case:
        first=verify_external_call(case.source,case.evidence,reference=case.reference,now=case.now)
        for _ in range(3):case.rpc('evm_mine',[])
        later=collect_call_resolution(case.rpc,case.source,case.hash)
        assert int(later['finalized']['number'],16)>int(case.evidence['finalized']['number'],16)
        later['transaction']['provider_label']='not persisted';later['receipt']['provider_label']='not persisted'
        now=max(case.now,datetime.fromtimestamp(int(later['finalized']['timestamp'],16),timezone.utc))+timedelta(seconds=2)
        assert verify_external_call(case.source,later,reference=case.reference,now=now)==first
        assert 'provider_label'not in json.dumps(first)


@pytest.mark.parametrize('matches',[True,False])
def test_reported_transaction_hash_is_separate_from_expected_call_identity(tmp_path,matches):
    with external_case(tmp_path)as case:
        source=replace(case.source,original_tx_ref=case.reference('tx',case.hash)if matches else 'a'*64)
        proof=verify_external_call(source,case.evidence,reference=case.reference,now=case.now)
        assert proof['matches_reported_tx']is matches and proof['outcome']=='EXTERNAL_SUCCEEDED'
        validate_external_proof(proof,source,reference=case.reference,now=case.now)


def test_same_log_fact_cannot_claim_another_owner_even_with_recomputed_attestation(tmp_path):
    from app.live_execution.dex_external_observation import _attestation
    with external_case(tmp_path)as case:
        proof=verify_external_call(case.source,case.evidence,reference=case.reference,now=case.now)
        bad=deepcopy(proof);bad['transfers'][0]['right_role']='OWNER'
        bad['observation_ref']=_attestation(case.source,bad,case.reference)
        with pytest.raises(ValueError,match='role binding'):
            validate_external_proof(bad,case.source,reference=case.reference,now=case.now)
        bad=deepcopy(proof);bad['transfers'][0]['asset']='WBNB'
        bad['observation_ref']=_attestation(case.source,bad,case.reference)
        with pytest.raises(ValueError,match='registry binding'):
            validate_external_proof(bad,case.source,reference=case.reference,now=case.now)
