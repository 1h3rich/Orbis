"""Original owner return is pending only on our identified disposable EVM."""
from contextlib import contextmanager
from uuid import uuid4
from eth_account import Account
from eth_utils import to_checksum_address
from app.live_execution.dex_gas_return import read_return_state,plan_return
from app.live_execution.dex_gas_funding import collect_funding_receipt
from app.portfolio import dex_gas_funding as funding,dex_gas_return as returns
from tests.test_dex_gas_funding import case,prepare,claim,now


def cancellation_review(values,original,*,price=1200000000):
    from app.live_execution.dex_gas_cancellation import read_cancellation_state
    from app.portfolio import dex_gas_cancellation as journal
    vault,account,manager,draft,meta,rpc,_,_=values
    snapshot=read_cancellation_state(rpc,meta['owner'],draft['operator_address'],nonce=int(original['nonce']),now=now())
    with vault.session() as db:
        return journal.prepare_cancellation(db,identifier=str(uuid4()),return_id=original['id'],account_id=account,
            wallet_address=meta['owner'],draft_id=draft['id'],drafts=manager,snapshot=snapshot,gas_price=price,bump_percent=10,max_gas_wei=10**14,now=now())


def cancellation_plan(values,record):
    from app.live_execution.dex_gas_cancellation import read_cancellation_state,plan_cancellation
    _,_,_,draft,meta,rpc,_,_=values
    snapshot=read_cancellation_state(rpc,meta['owner'],draft['operator_address'],nonce=int(record['nonce']),now=now())
    return plan_cancellation(snapshot,original_nonce=int(record['nonce']),original_gas_price=record['base_gas_price'],
        gas_price=record['gas_price'],bump_percent=record['bump_percent'],max_gas_wei=int(record['max_gas_wei']),now=now())


@contextmanager
def pending_return(directory,*,arrived=True):
    with case(directory,evm_backend='anvil') as values:
        vault,account,manager,draft,meta,rpc,_,binding=values
        prepared=prepare(values);assert claim(values,prepared)['claim_granted']
        tx_hash=rpc('eth_sendTransaction',[prepared['transaction']])
        with vault.session() as db:funding.check_funding(db,prepared['id'],account_id=account,evidence=collect_funding_receipt(rpc,tx_hash),now=now())
        plan=plan_return(read_return_state(rpc,meta['owner'],draft['operator_address'],now=now()),gas_price=10**9,max_gas_wei=21000*10**9,now=now())
        with vault.session() as db:
            original=returns.prepare_return(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],
                wallet_address=meta['owner'],drafts=manager,plan=plan,now=now())
        with vault.session() as db:returns.reserve_return(db,original['id'],account_id=account,wallet_address=meta['owner'],drafts=manager,draft_id=draft['id'],plan=plan,plan_digest=original['plan_digest'],now=now())
        def sign(transaction):
            fields={key:to_checksum_address(value) if key=='to' else value if key=='data' else int(value,16)
                for key,value in transaction.items() if key not in ('from','type')}
            with manager._copy(draft['id'],account,meta['owner'],now()) as (_,key):return Account.sign_transaction(fields,bytes(key))
        signed=sign(plan.transaction());tx_hash='0x'+signed.hash.hex()
        with vault.session() as db:returns.record_signature(db,original['id'],account_id=account,plan=plan,raw=bytes(signed.raw_transaction),tx_hash=tx_hash,now=now())
        with vault.session() as db:returns.mark_broadcast(db,original['id'],account_id=account,plan_digest=original['plan_digest'],tx_hash=tx_hash,now=now())
        rpc('miner_stop',[])
        if arrived:assert rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])==tx_hash
        yield values,original,tx_hash,sign
