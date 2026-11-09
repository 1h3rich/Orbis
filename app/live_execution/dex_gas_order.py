"""Supplemental order of authenticated gas-only receipts; no execution authority.

The source port authenticates original journals and constructs live call specs.
Archive reopening needs only originals and the keyed certificate, never a full
wallet address, transaction hash, raw calldata, RPC or signing key.
"""
from __future__ import annotations

from dataclasses import dataclass,field
from datetime import datetime
import hmac,json,re
from typing import Literal
from uuid import UUID

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_keys.exceptions import BadSignature
from eth_utils import ValidationError

from app.live_execution.dex_artifact import DexExecutorError,executor_address
from app.live_execution.dex_receipt_order import MAX_TRANSACTIONS,Reference,collect_order
from app.live_execution.evm_transaction import (
    quantity,word,recover_wallet_call,recover_wallet_cancellation,recover_wallet_transfer,recover_reverted_call,
)
from app.portfolio.dex_deployment_receipt import finalized_block
from app.portfolio.economic_projection import GAS_SOURCE_KINDS
from app.portfolio.wallet_funding import _encoded,_time
from app.portfolio.wallet_proof import canonical_wallet_address


SCHEMA='orbis.dex.gas.order.proof.v1'
SignatureKind=Literal['CREATE','CALL','CONTROL','NATIVE','CANCEL','FAILED']


@dataclass(frozen=True,slots=True,repr=False)
class GasCall:
    kind: SignatureKind
    owner: str=field(repr=False)
    target: str|None=None
    data: str|None=field(default=None,repr=False)
    value: int=0
    target_ref: str|None=None
    call_digest: str|None=None
    control_terms_json: str|None=field(default=None,repr=False)


@dataclass(frozen=True,slots=True,repr=False)
class GasSource:
    kind: str
    id: str
    account_id: str
    definition_digest: str
    receipt_digest: str
    source_snapshot_digest: str
    owner_digest: str
    signer_digest: str
    original_signer_digest: str
    parent_kind: str
    parent_id: str
    action: str
    outcome: str
    signature_kind: SignatureKind
    nonce: int
    gas_limit: int
    fee_cap: int
    receipt_json: str=field(repr=False)
    transaction_ref: str
    created_at: datetime
    updated_at: datetime
    transaction_index: int|None=None
    call: GasCall|None=field(default=None,repr=False)

    @property
    def key(self)->tuple[str,str]:return self.kind,self.id


def _receipt(source:GasSource)->dict:
    if (source.kind not in GAS_SOURCE_KINDS or source.parent_kind not in GAS_SOURCE_KINDS
            or any(str(UUID(value))!=value for value in (source.id,source.account_id,source.parent_id))
            or source.signature_kind not in ('CREATE','CALL','CONTROL','NATIVE','CANCEL','FAILED')
            or source.outcome not in ('EXECUTED','DEPLOYED','TRANSFERRED','CANCELLED','REVERTED')):
        raise ValueError('source identity')
    for value in (source.definition_digest,source.receipt_digest,source.source_snapshot_digest,source.owner_digest,
            source.signer_digest,source.original_signer_digest,source.transaction_ref):
        if not isinstance(value,str) or re.fullmatch(r'[a-f0-9]{64}',value)is None:raise ValueError('source reference')
    if (type(source.nonce)is not int or not 0<=source.nonce<2**64
            or type(source.gas_limit)is not int or not 21000<=source.gas_limit<=10_000_000
            or type(source.fee_cap)is not int or not 0<source.fee_cap<=10**12
            or source.transaction_index is not None and (type(source.transaction_index)is not int
                or not 0<=source.transaction_index<MAX_TRANSACTIONS)):
        raise ValueError('source coordinates')
    receipt=json.loads(source.receipt_json)
    status='REVERTED'if source.outcome=='REVERTED'else 'FINALIZED'
    outcomes={'CREATE':('DEPLOYED','REVERTED'),'CALL':('EXECUTED','REVERTED'),'CONTROL':('EXECUTED','REVERTED'),
        'NATIVE':('TRANSFERRED','REVERTED'),'CANCEL':('CANCELLED',),'FAILED':('REVERTED',)}
    if (_encoded(receipt)[1]!=source.receipt_digest or receipt['status']!=status
            or source.outcome not in outcomes[source.signature_kind]
            or source.kind in ('WALLET_SWAP','OPERATOR','CALL_RESOLUTION')and source.outcome=='EXECUTED'and source.action in ('BUY','SELL','SWAP')):
        raise ValueError('source receipt/outcome')
    return receipt


def _recover(source:GasSource,tx:dict,observed:dict,reference:Reference):
    spec=source.call
    if spec is None or spec.kind!=source.signature_kind:raise ValueError('live source required')
    if not hmac.compare_digest(reference('gas-order-wallet',canonical_wallet_address(spec.owner)),source.owner_digest):
        raise ValueError('source owner')
    target=None if tx.get('to')is None else canonical_wallet_address(tx['to'])
    sender=canonical_wallet_address(tx['from']);success=quantity(observed['status'])
    if spec.kind=='CANCEL':
        result=recover_wallet_cancellation(tx,expected_owner=sender)
    elif spec.kind=='NATIVE':
        if (type(spec.value)is not int or spec.value<=0 or target is None or spec.target_ref is None
                or not hmac.compare_digest(reference('gas-order-wallet',target),spec.target_ref)):
            raise ValueError('native recipient/value')
        result=recover_wallet_transfer(tx,expected_to=target,expected_value=spec.value)
    elif spec.kind=='FAILED':
        result=recover_reverted_call(tx,receipt_status=success)
        from app.live_execution.dex_call_resolution import _call_reference
        actual=_call_reference(reference,target=target,value=quantity(tx['value']),data=tx['input'].lower())
        if spec.call_digest is None or not hmac.compare_digest(actual,spec.call_digest):raise ValueError('failed call identity')
    elif spec.kind=='CONTROL':
        from app.live_execution.dex_policy import POLICY_ABI
        from app.portfolio.dex_controls import _compile
        from app.portfolio.wallet_swap import TOKENS
        terms=json.loads(spec.control_terms_json);owner=canonical_wallet_address(spec.owner);operator=None
        expected_target=TOKENS[terms['allowance']['token']]if terms['action']in ('APPROVE','REVOKE')else executor_address(owner,int(terms['creation_nonce']))
        result=recover_wallet_call(tx,expected_to=expected_target)
        if terms['action']=='CONFIGURE':
            reviewed=decode(['uint256',POLICY_ABI],bytes.fromhex(tx['input'][10:]),strict=True)
            operator=canonical_wallet_address(reviewed[1][0])
            if not hmac.compare_digest(reference('control-operator',operator),terms['operator_ref']):raise ValueError('control operator')
        expected=_compile(terms,owner,operator,now=source.created_at)
        if tx['input'].lower()!=expected['data']:raise ValueError('control call')
    else:
        if (spec.value!=0 or (spec.kind=='CREATE')!=(spec.target is None)
                or spec.data is None or tx['input'].lower()!=spec.data):raise ValueError('exact call/creation')
        result=recover_wallet_call(tx,expected_to=spec.target)
    recovered=result[0]
    if (recovered!=sender or not hmac.compare_digest(reference('wallet',sender),source.original_signer_digest)
            or not hmac.compare_digest(reference('gas-order-wallet',sender),source.signer_digest)
            or quantity(tx['nonce'])!=source.nonce):raise ValueError('original signer/nonce')
    return result,target


def _fixed(source:GasSource,receipt:dict)->dict:
    return {'schema':SCHEMA,'chain_id':56,'purpose':'NATIVE_GAS_ONLY','source_kind':source.kind,'source_id':source.id,
        'context_account_id':source.account_id,'definition_digest':source.definition_digest,'receipt_digest':source.receipt_digest,
        'source_snapshot_digest':source.source_snapshot_digest,'owner_digest':source.owner_digest,'signer_ref':source.signer_digest,
        'original_signer_ref':source.original_signer_digest,'parent_kind':source.parent_kind,'parent_id':source.parent_id,
        'action':source.action,'outcome':source.outcome,'signature_kind':source.signature_kind,'nonce':str(source.nonce),
        'tx_ref':source.transaction_ref,'source_created_at':_time(source.created_at).isoformat(),
        'source_updated_at':_time(source.updated_at).isoformat(),'gas_limit':str(source.gas_limit),'fee_cap':str(source.fee_cap),
        'block_number':receipt['block_number'],'block_hash':receipt['block_hash'],'block_at':receipt['block_at'],
        'gas_fee_wei':receipt['gas_fee_wei'],'internal_principal_wei':receipt.get('transferred_wei','0')}


def _seal(proof:dict,reference:Reference)->str:
    return reference('dex-gas-order-certificate',_encoded({key:value for key,value in proof.items()if key!='certificate_ref'})[0])


def verify_gas_order(source:GasSource,evidence:dict,*,reference:Reference,now:datetime)->dict:
    """Exact original signature, finalized inclusion and fee; no new effects."""
    try:
        receipt=_receipt(source);now=_time(now)
        raw=json.dumps(evidence,sort_keys=True,separators=(',',':'),allow_nan=False)
        if (now<_time(source.updated_at)or len(raw)>8_000_000
                or set(evidence)!={'chain_id','tx_hash','transaction','receipt','block','finalized'}or evidence['chain_id']!='0x38'):
            raise ValueError('shape/time/chain')
        tx,observed,block,final=[evidence[key]for key in ('transaction','receipt','block','finalized')]
        tx_hash=word(evidence['tx_hash'])
        if not hmac.compare_digest(reference('tx',tx_hash),source.transaction_ref):raise ValueError('original hash')
        (sender,typed,limit,cap,priority),target=_recover(source,tx,observed,reference)
        height,block_hash,at=finalized_block(block,final,created_at=source.created_at,now=now)
        if (height!=int(receipt['block_number'])or block_hash!=receipt['block_hash']
                or at!=_time(datetime.fromisoformat(receipt['block_at']))):raise ValueError('original block')
        transactions=block['transactions']
        if type(transactions)is not list or not 1<=len(transactions)<=MAX_TRANSACTIONS:raise ValueError('block size')
        hashes=[word(value)for value in transactions];index=quantity(tx['transactionIndex'])
        if (len(set(hashes))!=len(hashes)or not 0<=index<len(hashes)or hashes[index]!=tx_hash
                or source.transaction_index is not None and source.transaction_index!=index
                or quantity(observed['transactionIndex'])!=index
                or word(tx['hash'])!=tx_hash or word(observed['transactionHash'])!=tx_hash
                or any(quantity(value['blockNumber'])!=height or word(value['blockHash'])!=block_hash for value in (tx,observed))
                or quantity(observed['status'])!=(0 if receipt['status']=='REVERTED'else 1)
                or canonical_wallet_address(observed['from'])!=sender
                or (None if observed.get('to')is None else canonical_wallet_address(observed['to']))!=target):
            raise ValueError('inclusion/status/linkage')
        contract=observed['contractAddress']
        if target is not None and contract is not None:raise ValueError('unexpected contract')
        if target is None:
            expected=executor_address(sender,source.nonce)
            if (contract is not None and canonical_wallet_address(contract)!=expected
                    or receipt['status']=='FINALIZED'and contract is None):raise ValueError('creation address')
        if (receipt['status']=='REVERTED'or source.signature_kind in ('CREATE','CANCEL','NATIVE'))and observed['logs']!=[]:
            raise ValueError('unexpected logs')
        principal=quantity(tx['value'])if source.signature_kind=='NATIVE'and receipt['status']=='FINALIZED'else 0
        if receipt.get('transferred_wei','0')!=str(principal):raise ValueError('principal is not a fee')
        used,price=quantity(observed['gasUsed']),quantity(observed['effectiveGasPrice'])
        if (typed!=receipt.get('transaction_type',0)or quantity(observed['type'])!=typed
                or limit!=source.gas_limit or cap!=source.fee_cap
                or priority!=(int(receipt['priority_fee'])if receipt.get('priority_fee')is not None else None)
                or not (53000 if target is None else 21000)<=used<=limit or used!=int(receipt['gas_used'])
                or price!=int(receipt.get('effective_gas_price',receipt.get('gas_price')))
                or used*price!=int(receipt['gas_fee_wei'])):raise ValueError('original fee')
        if typed==2:
            base=quantity(block['baseFeePerGas'])
            if base!=int(receipt['base_fee'])or cap<base or price!=min(cap,base+priority):raise ValueError('typed fee')
        elif price!=cap:raise ValueError('legacy fee')
        proof={**_fixed(source,receipt),'transaction_index':str(index),'block_transaction_count':str(len(hashes)),
            'common_tx_ref':reference('gas-order-tx',tx_hash),
            'block_transactions_ref':reference('dex-receipt-order-block',_encoded(hashes)[0])}
        proof['certificate_ref']=_seal(proof,reference);validate_gas_order(source,proof,reference=reference);return proof
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,BadSignature,ValidationError,DecodingError):
        raise DexExecutorError('El orden del gas no coincide con su transacción y recibo originales')from None


def validate_gas_order(source:GasSource,proof:dict,*,reference:Reference)->None:
    try:
        fixed=_fixed(source,_receipt(source))
        if (type(proof)is not dict or set(proof)!=set(fixed)|{'transaction_index','block_transaction_count','common_tx_ref','block_transactions_ref','certificate_ref'}
                or any(proof[key]!=value or type(proof[key])is not type(value)for key,value in fixed.items())):
            raise ValueError('proof identity')
        for key in ('transaction_index','block_transaction_count'):
            if not isinstance(proof[key],str)or re.fullmatch(r'0|[1-9][0-9]{0,5}',proof[key])is None:raise ValueError('coordinates')
        index,count=int(proof['transaction_index']),int(proof['block_transaction_count'])
        if not 0<=index<count<=MAX_TRANSACTIONS or source.transaction_index is not None and source.transaction_index!=index:
            raise ValueError('index bounds')
        for key in ('common_tx_ref','block_transactions_ref','certificate_ref'):
            if not isinstance(proof[key],str)or re.fullmatch(r'[0-9a-f]{64}',proof[key])is None:raise ValueError('proof reference')
        if not hmac.compare_digest(proof['certificate_ref'],_seal(proof,reference)):raise ValueError('certificate')
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise DexExecutorError('La prueba de orden del gas cambió o perdió su origen')from None
