from copy import deepcopy

import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_nonce_search import MAX_SEARCH_BLOCKS, find_finalized_nonce


SENDER = '0x'+'11'*20
OTHER = '0x'+'22'*20
ANCHOR = 15
FOUND = 901
FINAL = 100_000
NONCE = 9
TX_HASH = '0x'+'ab'*32


def block_hash(height):
    return '0x'+format(height,'064x')


class Chain:
    def __init__(self):
        self.calls = []
        self.final = FINAL
        self.fault = None

    def __call__(self, method, params):
        self.calls.append((method,params))
        if method == 'eth_chainId':
            return '0x38'
        if method == 'eth_getTransactionCount':
            assert params[0] == SENDER
            height = int(params[1],16)
            return hex(NONCE+2 if height >= FOUND else NONCE)
        assert method == 'eth_getBlockByNumber'
        height = self.final if params[0] == 'finalized' else int(params[0],16)
        result = {'number':hex(height),'hash':block_hash(height)}
        if params[1]:
            assert height == FOUND
            item = {'from':SENDER,'nonce':hex(NONCE),'hash':TX_HASH,
                'blockHash':result['hash'],'blockNumber':hex(height),'transactionIndex':'0x2'}
            result['transactions'] = [{**item,'from':OTHER}, {**item,'nonce':hex(NONCE+1)}, item]
            if self.fault == 'duplicate':
                result['transactions'][0] = item
            elif self.fault == 'unmatched':
                result['transactions'].pop()
            elif self.fault == 'index':
                item['transactionIndex'] = '0x1'
        if self.fault == 'changed' and height == FOUND and not params[1]:
            result['hash'] = '0x'+'33'*32
        return result

    def find(self):
        return find_finalized_nonce(self,SENDER,NONCE,anchor_height=ANCHOR,anchor_hash=block_hash(ANCHOR))


def test_nonce_search_is_bounded_and_checks_the_receipt_position():
    chain = Chain()
    match = chain.find()
    assert (match.tx_hash,match.block_number,match.block_hash,match.index) == (TX_HASH,FOUND,block_hash(FOUND),2)
    assert len(chain.calls) <= 26
    assert sum(method == 'eth_getBlockByNumber' and params[1] is True for method,params in chain.calls) == 1
    receipt = {'transactionHash':TX_HASH,'blockHash':block_hash(FOUND),'blockNumber':hex(FOUND),'transactionIndex':'0x2'}
    match.check_receipt({'receipt':receipt})
    for key in ('transactionHash','blockHash','blockNumber','transactionIndex'):
        bad = deepcopy(receipt)
        bad[key] = '0x0'
        with pytest.raises(DexExecutorError,match='bloque localizado'):
            match.check_receipt({'receipt':bad})


@pytest.mark.parametrize('fault',['changed','duplicate','unmatched','index'])
def test_unconfirmed_or_ambiguous_candidate_is_refused(fault):
    chain = Chain()
    chain.fault = fault
    with pytest.raises(DexExecutorError,match='bloque verificable'):
        chain.find()


def test_nonce_not_finalized_is_distinct_from_an_unsearchable_window():
    chain = Chain()
    chain.final = FOUND-1
    assert chain.find() is None
    assert not any(method == 'eth_getBlockByNumber' and params[1] is True for method,params in chain.calls)

    chain.final = ANCHOR+MAX_SEARCH_BLOCKS+500

    def beyond_window(method,params):
        if method == 'eth_getTransactionCount':
            return hex(NONCE)
        return chain(method,params)

    with pytest.raises(DexExecutorError,match='tramo consultado'):
        find_finalized_nonce(beyond_window,SENDER,NONCE,anchor_height=ANCHOR,anchor_hash=block_hash(ANCHOR))


@pytest.mark.parametrize('head',[FOUND,FINAL])
def test_legacy_plans_search_from_finalized_head_without_a_saved_anchor(head):
    chain=Chain();chain.final=head
    match=find_finalized_nonce(chain,SENDER,NONCE)
    assert (match.tx_hash,match.block_number,match.index)==(TX_HASH,FOUND,2)
    counts=[int(args[1],16) for method,args in chain.calls if method=='eth_getTransactionCount']
    assert counts[:2]==[head,head-1] and len(chain.calls)<=44
    if head==FOUND:
        assert len(counts)==2


def test_legacy_search_keeps_the_manual_path_if_receipt_is_outside_its_window():
    chain=Chain();chain.final=FOUND+MAX_SEARCH_BLOCKS+3
    with pytest.raises(DexExecutorError,match='tramo consultado'):
        find_finalized_nonce(chain,SENDER,NONCE)
    assert len(chain.calls)<=44
