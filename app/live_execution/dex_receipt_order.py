"""Complementary chain ordering for already authenticated economic receipts.

Read-only signature/block observation, never an execution/booking authority.
The source port must authenticate the original record before constructing it.
Original receipt formats and financial semantics remain unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable,Sequence
import hmac,json,re
from uuid import UUID

from eth_keys.exceptions import BadSignature
from eth_utils import ValidationError

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_call_resolution import CallSource
from app.live_execution.evm_transaction import quantity,word,recover_wallet_call
from app.portfolio.dex_deployment_receipt import finalized_block
from app.portfolio.wallet_funding import _encoded,_time
from app.portfolio.wallet_proof import canonical_wallet_address


SCHEMA='orbis.dex.receipt.order.proof.v1'
MAX_TRANSACTIONS=100000
MAX_SOURCES=10000
Reference=Callable[[str,str],str]


@dataclass(frozen=True,slots=True,repr=False)
class OrderSource:
    kind: str
    id: str
    account_id: str
    definition_digest: str
    receipt_digest: str
    owner_digest: str
    signer_digest: str
    call: CallSource
    receipt_json: str
    transaction_ref: str
    updated_at: datetime
    transaction_index: int|None=None

    @property
    def key(self) -> tuple[str,str]:return self.kind,self.id


def _receipt(source: OrderSource) -> dict:
    if (source.kind not in ('WALLET_SWAP','OPERATOR','CALL_RESOLUTION') or str(UUID(source.id))!=source.id
            or str(UUID(source.account_id))!=source.account_id):
        raise ValueError('source identity')
    for value in (source.definition_digest,source.receipt_digest,source.owner_digest,source.signer_digest,source.transaction_ref,source.call.signer_digest):
        if not isinstance(value,str) or re.fullmatch(r'[a-f0-9]{64}',value) is None:raise ValueError('source reference')
    receipt=json.loads(source.receipt_json)
    if _encoded(receipt)[1]!=source.receipt_digest or receipt['status']!='FINALIZED':raise ValueError('source receipt')
    if source.call.kind not in ('WALLET_SWAP','OPERATOR'):raise ValueError('source call kind')
    if (str(UUID(source.call.id))!=source.call.id or source.call.terms['id']!=source.call.id
            or source.account_id!=source.call.terms['account_id']):
        raise ValueError('source parent identity')
    if source.kind!='CALL_RESOLUTION' and (source.kind!=source.call.kind or source.id!=source.call.id
            or source.definition_digest!=_encoded(source.call.terms)[1]):raise ValueError('source definition')
    if source.call.kind=='WALLET_SWAP' and source.owner_digest!=source.signer_digest:
        raise ValueError('wallet owner/signer mapping')
    action=source.call.terms['action']
    if (source.call.kind=='WALLET_SWAP' and action!='SWAP'
            or source.call.kind=='OPERATOR' and action not in ('BUY','SELL')):raise ValueError('not a trade')
    if source.transaction_index is not None and (type(source.transaction_index) is not int
            or not 0<=source.transaction_index<MAX_TRANSACTIONS):raise ValueError('source index')
    return receipt


def collect_order(rpc,tx_hash: str) -> dict|None:
    """Observe one finalized block with bounded data and an exact second read."""
    try:
        tx_hash=word(tx_hash)
        if quantity(rpc('eth_chainId',[]))!=56:raise ValueError('chain')
        receipt=rpc('eth_getTransactionReceipt',[tx_hash])
        if receipt is None:return None
        final=rpc('eth_getBlockByNumber',['finalized',False])
        height=quantity(receipt['blockNumber'])
        if final is None or quantity(final['number'])<height:return None
        tag=hex(height);block=rpc('eth_getBlockByNumber',[tag,False])
        transactions=block['transactions']
        if not isinstance(transactions,list) or not 1<=len(transactions)<=MAX_TRANSACTIONS:raise ValueError('block size')
        transaction=rpc('eth_getTransactionByHash',[tx_hash])
        repeated=rpc('eth_getBlockByNumber',[tag,False])
        if any(block.get(key)!=repeated.get(key) for key in ('number','hash','timestamp','transactions','baseFeePerGas')):
            raise ValueError('block changed')
        return {'chain_id':'0x38','tx_hash':tx_hash,'transaction':transaction,'receipt':receipt,'block':block,'finalized':final}
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('No se pudo comprobar el orden finalizado del recibo DEX') from None


def _fixed(source: OrderSource,receipt: dict) -> dict:
    return {'schema':SCHEMA,'chain_id':56,'source_kind':source.kind,'source_id':source.id,'context_account_id':source.account_id,
        'definition_digest':source.definition_digest,'receipt_digest':source.receipt_digest,'owner_digest':source.owner_digest,
        'parent_kind':source.call.kind,'parent_id':source.call.id,'call_terms_digest':_encoded(source.call.terms)[1],
        'call_grant_digest':_encoded(source.call.grant)[1] if source.call.grant is not None else None,
        'tx_ref':source.transaction_ref,'signer_ref':source.signer_digest,'original_signer_ref':source.call.signer_digest,'nonce':str(source.call.nonce),
        'block_number':receipt['block_number'],'block_hash':receipt['block_hash'],'block_at':receipt['block_at']}


def _seal(proof: dict,reference: Reference) -> str:
    return reference('dex-receipt-order-proof',_encoded({key:value for key,value in proof.items() if key!='certificate_ref'})[0])


def verify_order(source: OrderSource,evidence: dict,*,reference: Reference,now: datetime) -> dict:
    """Recover signed bytes and add an index without changing receipt effects."""
    try:
        receipt=_receipt(source);now=_time(now)
        if now<_time(source.updated_at):raise ValueError('observation predates source')
        raw=json.dumps(evidence,sort_keys=True,separators=(',',':'),allow_nan=False)
        if (len(raw)>8_000_000 or set(evidence)!={'chain_id','tx_hash','transaction','receipt','block','finalized'}
                or evidence['chain_id']!='0x38'):raise ValueError('shape/chain')
        tx,observed,block,final=[evidence[key] for key in ('transaction','receipt','block','finalized')]
        tx_hash=word(evidence['tx_hash']);expected=source.call.expected_call()
        sender,typed,limit,cap,priority=recover_wallet_call(tx,expected_to=expected['to'])
        if (not hmac.compare_digest(reference('tx',tx_hash),source.transaction_ref)
                or not hmac.compare_digest(reference('wallet',sender),source.call.signer_digest)
                or not hmac.compare_digest(reference('dex-order-signer',sender),source.signer_digest)
                or not hmac.compare_digest(reference('dex-order-owner',source.call.owner),source.owner_digest)
                or canonical_wallet_address(tx['from'])!=sender or quantity(tx['nonce'])!=source.call.nonce
                or tx['input'].lower()!=expected['data']):raise ValueError('signed original')
        height,block_hash,at=finalized_block(block,final,created_at=source.call.created_at,now=now)
        if (height!=int(receipt['block_number']) or block_hash!=receipt['block_hash']
                or at!=_time(datetime.fromisoformat(receipt['block_at']))):raise ValueError('original block')
        transactions=block['transactions']
        if not isinstance(transactions,list) or not 1<=len(transactions)<=MAX_TRANSACTIONS:raise ValueError('block size')
        hashes=[word(value) for value in transactions]
        index=quantity(tx['transactionIndex'])
        if (len(set(hashes))!=len(hashes) or index>=len(hashes) or hashes[index]!=tx_hash
                or quantity(observed['transactionIndex'])!=index
                or source.transaction_index is not None and source.transaction_index!=index
                or word(tx['hash'])!=tx_hash or word(observed['transactionHash'])!=tx_hash
                or any(quantity(item['blockNumber'])!=height or word(item['blockHash'])!=block_hash for item in (tx,observed))):
            raise ValueError('receipt inclusion/index')
        used,price=quantity(observed['gasUsed']),quantity(observed['effectiveGasPrice'])
        original_price=receipt.get('effective_gas_price',receipt.get('gas_price'))
        if (quantity(observed['status'])!=1 or quantity(observed['type'])!=typed
                or canonical_wallet_address(observed['from'])!=sender or canonical_wallet_address(observed['to'])!=expected['to']
                or observed['contractAddress'] is not None or typed!=receipt.get('transaction_type',0)
                or limit!=int(receipt.get('gas_limit',source.call.terms['gas']))
                or cap!=int(receipt.get('fee_cap',source.call.terms['gas_price']))
                or priority!=(int(receipt['priority_fee']) if receipt.get('priority_fee') is not None else None)
                or not 21000<=used<=limit or used!=int(receipt['gas_used']) or price!=int(original_price)
                or used*price!=int(receipt['gas_fee_wei'])):raise ValueError('original gas/type/status')
        if typed==2:
            base=quantity(block['baseFeePerGas'])
            if base!=int(receipt['base_fee']) or cap<base or price!=min(cap,base+priority):raise ValueError('typed fee')
        elif price!=cap:raise ValueError('legacy fee')
        proof={**_fixed(source,receipt),'transaction_index':str(index),'block_transaction_count':str(len(hashes)),
            'common_tx_ref':reference('dex-order-tx',tx_hash),
            'block_transactions_ref':reference('dex-receipt-order-block',_encoded(hashes)[0])}
        proof['certificate_ref']=_seal(proof,reference)
        validate_order(source,proof,reference=reference)
        return proof
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,BadSignature,ValidationError):
        raise DexExecutorError('La prueba de orden no coincide con el recibo DEX original') from None


def validate_order(source: OrderSource,proof: dict,*,reference: Reference) -> None:
    """Authenticate archived facts against a freshly verified source port."""
    try:
        fixed=_fixed(source,_receipt(source))
        extra={'transaction_index','block_transaction_count','common_tx_ref','block_transactions_ref','certificate_ref'}
        if (not isinstance(proof,dict) or set(proof)!=set(fixed)|extra
                or any(proof[key]!=value or type(proof[key]) is not type(value) for key,value in fixed.items())):
            raise ValueError('proof identity')
        for key in ('transaction_index','block_transaction_count'):
            if not isinstance(proof[key],str) or re.fullmatch(r'0|[1-9][0-9]{0,5}',proof[key]) is None:raise ValueError('index shape')
        index,count=int(proof['transaction_index']),int(proof['block_transaction_count'])
        if not 0<=index<count<=MAX_TRANSACTIONS or source.transaction_index is not None and index!=source.transaction_index:
            raise ValueError('index bounds')
        for key in ('certificate_ref','block_transactions_ref','common_tx_ref'):
            if not isinstance(proof[key],str) or re.fullmatch(r'[a-f0-9]{64}',proof[key]) is None:raise ValueError('proof reference')
        if not hmac.compare_digest(proof['certificate_ref'],_seal(proof,reference)):raise ValueError('proof seal')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('La prueba de orden DEX cambió o perdió su origen') from None


def ordered_sources(items: Sequence[tuple[OrderSource,dict,Reference]]) -> tuple[tuple[str,str],...]:
    """Bounded O(n log n) order, with all sources authenticated and no ledger I/O."""
    if not 1<=len(items)<=MAX_SOURCES:raise DexExecutorError('Número de recibos fuera del límite de ordenación')
    positions={};identities=set();transactions=set();blocks={};owner=None;result=[]
    for source,proof,reference in items:
        validate_order(source,proof,reference=reference)
        if owner is not None and owner!=source.owner_digest:raise DexExecutorError('La secuencia mezcla wallets distintas')
        owner=source.owner_digest;height=int(proof['block_number']);index=int(proof['transaction_index'])
        position=(height,index)
        tx_key=proof['common_tx_ref']
        if source.key in identities or tx_key in transactions or position in positions:
            raise DexExecutorError('La secuencia repite un recibo o una posición de bloque')
        # The live certificate explicitly attests the common transaction domain;
        # original wallet/operator references retain their separate domains.
        anchor=(proof['block_hash'],proof['block_at'],proof['block_transaction_count'],proof['block_transactions_ref'])
        if height in blocks and blocks[height]!=anchor:raise DexExecutorError('La secuencia contiene versiones distintas de un bloque')
        blocks[height]=anchor;positions[position]=source;identities.add(source.key);transactions.add(tx_key)
        result.append((position,source.key,source.signer_digest,source.call.nonce))
    result.sort(key=lambda item:item[0]);last_nonce={}
    for _,_,signer,nonce in result:
        if signer in last_nonce and nonce<=last_nonce[signer]:raise DexExecutorError('El orden de bloque contradice los nonces del emisor')
        last_nonce[signer]=nonce
    return tuple(item[1] for item in result)
