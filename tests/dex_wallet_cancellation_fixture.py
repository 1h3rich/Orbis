"""A claimed native funding on the identified disposable Anvil only."""
from contextlib import contextmanager
from uuid import uuid4
from app.live_execution.dex_wallet_cancellation import read_wallet_cancellation_state,plan_wallet_cancellation
from tests.test_dex_gas_funding import case,prepare,claim,now


@contextmanager
def pending_funding(directory,*,arrived=True):
    with case(directory,evm_backend='anvil') as values:
        _,_,_,_,_,rpc,_,_=values
        source=prepare(values);assert claim(values,source)['claim_granted'];rpc('miner_stop',[])
        tx_hash=rpc('eth_sendTransaction',[source['transaction']]) if arrived else None
        yield values,source,tx_hash


def state(values,source,tx_hash):
    _,_,_,draft,meta,rpc,_,_=values
    return read_wallet_cancellation_state(rpc,meta['owner'],draft['operator_address'],nonce=int(source['nonce']),
        amount_wei=int(source['amount_wei']),original_hash=tx_hash,now=now())


def review(values,source,tx_hash,*,price=1200000000,accept_unknown=False):
    from app.portfolio import dex_wallet_cancellation as journal
    vault,account,_,_,meta,_,_,_=values
    current=state(values,source,tx_hash)
    with vault.session() as db:
        return journal.prepare_cancellation(db,identifier=str(uuid4()),funding_id=source['id'],account_id=account,
            wallet_address=meta['owner'],snapshot=current,gas_price=price,bump_percent=10,
            max_gas_wei=10**14,accept_unknown_fee=accept_unknown,now=now())


def plan(values,source,tx_hash,record):
    return plan_wallet_cancellation(state(values,source,tx_hash),gas_price=record['gas_price'],bump_percent=record['bump_percent'],
        max_gas_wei=int(record['max_gas_wei']),accept_unknown_fee=record['accept_unknown_fee'],
        previous_prices=tuple(record['previous_prices']),previous_fees_unknown=record['previous_fees_unknown'],now=now())
