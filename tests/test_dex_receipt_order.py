"""Actual mined receipts, sealed source ports and hostile ordering observations."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from uuid import uuid4

import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_receipt_order import collect_order,verify_order,validate_order,ordered_sources,_seal
from app.portfolio.dex_receipt_order_source import order_source,order_reference
from app.portfolio import wallet_swap as swaps,dex_call_resolution as resolutions
from app.portfolio.wallet_funding import _encoded
from tests.test_dex_call_booking import resolved_case
from tests.test_dex_call_resolution import prepare,now


def source(case,kind='CALL_RESOLUTION',identifier=None):
    vault,_,_,meta,_,_,_,resolved=case
    with vault.session() as db:return order_source(db,kind,identifier or resolved['id'],wallet_address=meta['owner'])


def evidence(case,original):
    rpc=case[4];receipt=json.loads(original.receipt_json)
    block=rpc('eth_getBlockByNumber',[hex(int(receipt['block_number'])),False])
    # Tests discover their own hash from the exact mined block, not user input.
    index=original.transaction_index or 0
    return collect_order(rpc,block['transactions'][index])


def verify(case,original,observed):
    with case[0].session() as db:
        return verify_order(original,observed,reference=order_reference(db,original),
            now=max(now(),original.updated_at+timedelta(seconds=1)))


@pytest.mark.parametrize('typed',[False,True])
def test_mined_resolved_trade_keeps_signed_index_and_no_private_values_or_writes(tmp_path,typed):
    with resolved_case(tmp_path,typed=typed) as case:
        vault,_,_,meta,_,parent,_,resolved=case
        with vault.session() as db:
            before=resolutions.resolution_record(db,resolved['id'])[0].payload_json
            parent_before=swaps.swap_record(db,parent.id)[0].state_json
        original=source(case);observed=evidence(case,original);proof=verify(case,original,observed)
        assert proof['transaction_index']==str(original.transaction_index)
        assert proof['source_id']==resolved['id'] and proof['receipt_digest']==original.receipt_digest
        assert observed['transaction']['type']==('0x2' if typed else '0x0')
        with vault.session() as db:
            current=order_source(db,'CALL_RESOLUTION',resolved['id'],wallet_address=meta['owner'])
            reference=order_reference(db,current);validate_order(current,proof,reference=reference)
            assert ordered_sources([(current,proof,reference)])==(current.key,)
            assert resolutions.resolution_record(db,resolved['id'])[0].payload_json==before
            assert swaps.swap_record(db,parent.id)[0].state_json==parent_before
        for value in (meta['owner'],meta['operator'],observed['tx_hash'],observed['transaction']['input']):
            assert value.lower() not in json.dumps(proof).lower()
            assert value.lower() not in repr(original).lower()
        vault.backup(tmp_path/'order-source-backup.db');vault.lock()
        from tests.test_wallet_swap import PASSWORD
        vault.unlock(PASSWORD)
        with vault.session() as db:
            current=order_source(db,'CALL_RESOLUTION',resolved['id'],wallet_address=meta['owner'])
            validate_order(current,proof,reference=order_reference(db,current))


def test_original_wallet_receipt_without_saved_index_gains_only_a_complementary_proof(tmp_path):
    from app.live_execution.dex_call_resolution import collect_call_resolution
    with resolved_case(tmp_path) as case:
        vault,account,_,meta,rpc,_,_,_=case
        plan,call,_=prepare(vault,account,meta,rpc)
        sent=rpc('eth_sendTransaction',[plan['transaction']]);read=collect_call_resolution(rpc,call,sent)
        with vault.session() as db:swaps.report_swap(db,plan['id'],account_id=account,tx_hash=sent,now=now())
        legacy={key:read[key] for key in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
        with vault.session() as db:swaps.check_swap(db,plan['id'],account_id=account,evidence=legacy,now=now())
        original=source(case,'WALLET_SWAP',plan['id'])
        assert original.transaction_index is None
        with vault.session() as db:before=swaps.swap_record(db,plan['id'])[0].state_json
        proof=verify(case,original,collect_order(rpc,sent))
        assert proof['transaction_index']==str(int(read['transaction']['transactionIndex'],16))
        with vault.session() as db:
            assert swaps.swap_record(db,plan['id'])[0].state_json==before
            assert 'transaction_index' not in swaps.swap_record(db,plan['id'])[2]['receipt']


def test_wrong_indices_signature_block_status_gas_and_source_are_never_accepted(tmp_path):
    with resolved_case(tmp_path) as case:
        original=source(case);clean=evidence(case,original)
        mutations=[('transaction','transactionIndex','0xffff'),('receipt','transactionIndex','0xffff'),
            ('transaction','nonce','0xffff'),('transaction','s','0x1'),('transaction','hash','0x'+'1'*64),
            ('transaction','input','0xdeadbeef'),('transaction','blockHash','0x'+'2'*64),
            ('receipt','status','0x0'),('receipt','blockHash','0x'+'2'*64),('receipt','gasUsed','0x5208'),
            ('receipt','effectiveGasPrice','0x1'),('block','hash','0x'+'2'*64),('block','transactions',[]),
            ('finalized','number','0x0')]
        for section,key,value in mutations:
            bad=deepcopy(clean);bad[section][key]=value
            with pytest.raises(DexExecutorError):verify(case,original,bad)
        for altered in (replace(original,transaction_ref='f'*64),replace(original,account_id=str(uuid4())),replace(original,signer_digest='f'*64),replace(original,owner_digest='f'*64),
                        replace(original,receipt_digest='f'*64),replace(original,transaction_index=999)):
            with pytest.raises(DexExecutorError):verify(case,altered,clean)
        bad=deepcopy(clean);bad['block']['transactions'].append(bad['tx_hash'])
        with pytest.raises(DexExecutorError):verify(case,original,bad)


def test_archive_seal_binds_actual_source_even_if_public_digest_is_recalculated(tmp_path):
    with resolved_case(tmp_path) as case:
        original=source(case);proof=verify(case,original,evidence(case,original))
        with case[0].session() as db:
            reference=order_reference(db,original)
            for key,value in [('transaction_index','999'),('source_id',str(uuid4())),('receipt_digest','f'*64),
                              ('nonce','999'),('common_tx_ref','f'*64),('block_transactions_ref','f'*64),('block_transaction_count','999')]:
                bad={**proof,key:value};_encoded(bad)  # a public digest cannot attest an RPC observation
                with pytest.raises(DexExecutorError):validate_order(original,bad,reference=reference)
            for bad in ({**proof,'unexpected':False},{**proof,'transaction_index':False},
                        {**proof,'block_transaction_count':'01'},{**proof,'chain_id':True}):
                with pytest.raises(DexExecutorError):validate_order(original,bad,reference=reference)


@pytest.mark.parametrize('mode',['APPROVE','CANCELLED','REVERTED'])
def test_gas_only_outcomes_and_foreign_wallet_cannot_become_economic_order_sources(tmp_path,mode):
    with resolved_case(tmp_path,mode) as case:
        vault,_,_,meta,_,_,_,resolved=case
        with vault.session() as db:
            with pytest.raises(DexExecutorError):order_source(db,'CALL_RESOLUTION',resolved['id'],wallet_address=meta['owner'])
            with pytest.raises(DexExecutorError):order_source(db,'CALL_RESOLUTION',resolved['id'],wallet_address=meta['operator'])


def test_collector_preserves_unconfirmed_and_refuses_changed_block_without_more_requests(tmp_path):
    with resolved_case(tmp_path) as case:
        original=source(case);clean=evidence(case,original);calls=[]
        def changed(method,params):
            calls.append((method,params))
            if method=='eth_getBlockByNumber' and params[0]!='finalized' and sum(m==method and p[0]!='finalized' for m,p in calls)==2:
                result=deepcopy(case[4](method,params));result['transactions']=list(reversed(result['transactions']))+['0x'+'2'*64];return result
            return case[4](method,params)
        with pytest.raises(DexExecutorError):collect_order(changed,clean['tx_hash'])
        assert len(calls)==6
        def pending(method,params):return '0x38' if method=='eth_chainId' else None
        assert collect_order(pending,clean['tx_hash']) is None


def test_ordering_checks_authenticated_coordinates_not_uuid_or_http_arrival(tmp_path):
    # Counterfactual ordering algebra only. No fabricated source/certificate
    # is accepted by the database port or persisted as economic evidence.
    with resolved_case(tmp_path) as case:
        original=source(case);proof=verify(case,original,evidence(case,original))
        with case[0].session() as db:
            reference=order_reference(db,original)
            later_receipt={**json.loads(original.receipt_json),'block_number':str(int(proof['block_number'])+1)}
            later=replace(original,id=str(uuid4()),receipt_json=_encoded(later_receipt)[0],receipt_digest=_encoded(later_receipt)[1],
                transaction_ref='f'*64,call=replace(original.call,terms={**original.call.terms,'nonce':str(original.call.nonce+1)}))
            later_proof={**proof,'source_id':later.id,'receipt_digest':later.receipt_digest,'tx_ref':later.transaction_ref,
                'nonce':str(later.call.nonce),'block_number':later_receipt['block_number'],
                'common_tx_ref':'e'*64,'call_terms_digest':_encoded(later.call.terms)[1]}
            later_proof['certificate_ref']=_seal(later_proof,reference)
            assert ordered_sources([(later,later_proof,reference),(original,proof,reference)])==(original.key,later.key)
            with pytest.raises(DexExecutorError):ordered_sources([(original,proof,reference)]*2)
            with pytest.raises(DexExecutorError):ordered_sources([])
            with pytest.raises(DexExecutorError):ordered_sources([(original,proof,reference)]*10001)


def test_finalized_operator_fill_also_uses_the_original_signature_domain(tmp_path,monkeypatch):
    from app.portfolio import dex_receipt_booking as booking
    from tests.test_dex_receipt_booking import case as operator_case
    with operator_case(tmp_path,monkeypatch) as (values,_,worker):
        data,_,_,_,clock,mine,_=values;vault,account,_,_,meta,rpc,_,_=data
        clock[0]+=timedelta(seconds=1);mine();assert worker.run_once()['last_action']=='BUY'
        assert worker.run_once()['last_receipt_status']=='FINALIZED';worker.stop()
        with vault.session() as db:
            row=next(row for row in booking.list_receipts(db,account_id=account,wallet_address=meta['owner']) if row['source_kind']=='OPERATOR')
            original=order_source(db,'OPERATOR',row['source_id'],wallet_address=meta['owner'])
        receipt=json.loads(original.receipt_json);block=rpc('eth_getBlockByNumber',[hex(int(receipt['block_number'])),False])
        assert len(block['transactions'])==1
        observed=collect_order(rpc,block['transactions'][0])
        with vault.session() as db:
            reference=order_reference(db,original)
            proof=verify_order(original,observed,reference=reference,now=max(now(),original.updated_at+timedelta(seconds=1)))
            validate_order(original,proof,reference=reference)
            assert original.transaction_index is None and proof['transaction_index']=='0'


def test_two_actual_emitters_in_one_block_recover_the_missing_global_order(tmp_path,monkeypatch):
    from app.live_execution.dex_call_resolution import collect_call_resolution
    from app.portfolio import dex_receipt_booking as booking
    from tests.test_dex_receipt_booking import case as operator_case
    from tests.test_dex_operator import write
    # The frozen wallet v1 verifier requires chainId in transaction responses;
    # Anvil supplies that field, unlike Ganache. No production guard is relaxed.
    with operator_case(tmp_path,monkeypatch,evm_backend='anvil') as (values,_,worker):
        data,_,_,_,clock,mine,_=values;vault,account,_,_,meta,rpc,_,_=data
        write(rpc,meta,meta['wbnb'],'mint(address,uint256)',['address','uint256'],[meta['pool'],10**18])
        write(rpc,{**meta,'operator':meta['owner']},meta['usdt'],'approve(address,uint256)',
            ['address','uint256'],[meta['router'],10**18])
        plan,call,_=prepare(vault,account,meta,rpc)
        clock[0]+=timedelta(seconds=1);mine();rpc('miner_stop',[])
        assert worker.run_once()['last_action']=='BUY'
        sent=rpc('eth_sendTransaction',[plan['transaction']]);rpc('evm_mine',[]);rpc('miner_start',[1])
        assert worker.run_once()['last_receipt_status']=='FINALIZED';worker.stop()
        read=collect_call_resolution(rpc,call,sent)
        with vault.session() as db:swaps.report_swap(db,plan['id'],account_id=account,tx_hash=sent,now=clock[0])
        legacy={key:read[key] for key in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
        with vault.session() as db:swaps.check_swap(db,plan['id'],account_id=account,evidence=legacy,now=clock[0])
        with vault.session() as db:
            operator=next(row for row in booking.list_receipts(db,account_id=account,wallet_address=meta['owner']) if row['source_kind']=='OPERATOR')
            sources=[order_source(db,kind,identifier,wallet_address=meta['owner']) for kind,identifier in
                [('OPERATOR',operator['source_id']),('WALLET_SWAP',plan['id'])]]
        assert all(item.transaction_index is None for item in sources)
        receipts=[json.loads(item.receipt_json) for item in sources]
        assert receipts[0]['block_number']==receipts[1]['block_number'] and receipts[0]['block_hash']==receipts[1]['block_hash']
        assert sources[0].signer_digest!=sources[1].signer_digest
        block=rpc('eth_getBlockByNumber',[hex(int(receipts[0]['block_number'])),False]);assert len(block['transactions'])==2
        observations={}
        for tx in block['transactions']:
            observed=collect_order(rpc,tx)
            with vault.session() as db:
                for item in sources:
                    reference=order_reference(db,item)
                    if reference('tx',tx)==item.transaction_ref:
                        proof=verify_order(item,observed,reference=reference,now=clock[0]+timedelta(seconds=1))
                        observations[item.key]=(item,proof)
        assert len(observations)==2
        with vault.session() as db:
            supplied=[(item,proof,order_reference(db,item)) for item,proof in observations.values()]
            actual=ordered_sources(list(reversed(supplied)))
        expected=tuple(item.key for item,proof in sorted(observations.values(),key=lambda pair:int(pair[1]['transaction_index'])))
        assert actual==expected
