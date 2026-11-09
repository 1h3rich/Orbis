"""Verify an already mined native replacement. No signing, retry or RPC URLs."""
from datetime import datetime
import json,re
from types import SimpleNamespace
from eth_keys.exceptions import BadSignature
from eth_utils import ValidationError
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.evm_transaction import quantity,word,recover_wallet_transfer,recover_wallet_cancellation
from app.live_execution.dex_owner_observation import _same_block
from app.portfolio.dex_deployment_receipt import finalized_block,validate_archived_receipt,require_receipt_before_observation
from app.portfolio.wallet_funding import _encoded,_uint
from app.portfolio.wallet_proof import canonical_wallet_address


def collect_resolution(rpc,tx_hash):
    try:
        tx_hash=word(tx_hash)
        if quantity(rpc('eth_chainId',[]))!=56:raise ValueError('chain')
        receipt=rpc('eth_getTransactionReceipt',[tx_hash])
        if receipt is None:return None
        final=rpc('eth_getBlockByNumber',['finalized',False]);tag=hex(quantity(receipt['blockNumber']))
        if quantity(final['number'])<quantity(receipt['blockNumber']):return None
        block=rpc('eth_getBlockByNumber',[tag,False]);tx=rpc('eth_getTransactionByHash',[tx_hash])
        source=canonical_wallet_address(tx['from']);target=canonical_wallet_address(tx['to'])
        codes=[rpc('eth_getCode',[address,tag]) for address in (source,target)]
        nonce=rpc('eth_getTransactionCount',[source,tag]);_same_block(rpc,block,tag)
        return {'chain_id':'0x38','tx_hash':tx_hash,'transaction':tx,'receipt':receipt,'block':block,'finalized':final,
            'source_code':codes[0],'target_code':codes[1],'source_nonce':nonce}
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):raise DexExecutorError('No se pudo leer el reemplazo BNB finalizado') from None


def signer(terms,kind):return terms['wallet_digest'] if kind=='GAS_FUNDING' else terms['operator_digest']


def verify_resolution(db,row,terms,state,kind,evidence,now):
    from app.portfolio.dex_deployments import _identity
    try:
        raw=json.dumps(evidence,sort_keys=True,separators=(',',':'),allow_nan=False)
        if (len(raw)>150000 or set(evidence)!={'chain_id','tx_hash','transaction','receipt','block','finalized','source_code','target_code','source_nonce'}
                or evidence['chain_id']!='0x38' or evidence['source_code']!='0x' or evidence['target_code']!='0x'):raise ValueError('shape/code')
        tx,receipt,block,final=[evidence[key] for key in ('transaction','receipt','block','finalized')]
        source=canonical_wallet_address(tx['from']);target=canonical_wallet_address(tx['to']);amount=quantity(tx['value']);tx_hash=word(evidence['tx_hash'])
        cancel=amount==0
        recovered,typed,limit,cap,priority=(recover_wallet_cancellation(tx,expected_owner=source) if cancel
            else recover_wallet_transfer(tx,expected_to=target,expected_value=amount))
        recipient=terms['operator_digest'] if kind=='GAS_FUNDING' else terms['wallet_digest']
        if (recovered!=source or _identity(db,'wallet',source)!=signer(terms,kind) or quantity(tx['nonce'])!=int(terms['nonce'])
                or not 21000<=limit<=100000 or not 0<cap<=10**12):raise ValueError('sender/nonce/gas')
        if not cancel and (_identity(db,'wallet',target)!=recipient or source==target):raise ValueError('native effect')
        height,block_hash,at=finalized_block(block,final,created_at=row.created_at,now=now)
        success=quantity(receipt['status']);index=quantity(tx['transactionIndex'])
        if (height<int(terms['anchor']['block_number']) or word(tx['hash'])!=tx_hash or word(receipt['transactionHash'])!=tx_hash
                or canonical_wallet_address(receipt['from'])!=source or canonical_wallet_address(receipt['to'])!=target
                or receipt['contractAddress'] is not None or receipt['logs']!=[] or quantity(receipt['type'])!=typed
                or index!=quantity(receipt['transactionIndex']) or success not in (0,1) or cancel and success!=1
                or any(quantity(item['blockNumber'])!=height or word(item['blockHash'])!=block_hash for item in (tx,receipt))):raise ValueError('receipt linkage')
        base=quantity(block['baseFeePerGas']) if typed==2 else None;price=min(cap,base+priority) if typed==2 else cap
        if quantity(receipt['gasUsed'])!=21000 or quantity(receipt['effectiveGasPrice'])!=price or typed==2 and cap<base:raise ValueError('fee')
        observed={'status':'FINALIZED' if success else 'REVERTED','source':'WALLET_RPC_OBSERVED','block_number':str(height),'block_hash':block_hash,
            'block_at':at.isoformat(),'transaction_type':typed,'gas_limit':str(limit),'gas_used':'21000','fee_cap':str(cap),
            'priority_fee':str(priority) if priority is not None else None,'base_fee':str(base) if base is not None else None,
            'effective_gas_price':str(price),'gas_fee_wei':str(21000*price),'transferred_wei':str(amount if success and not cancel else 0)}
        reference=_identity(db,'tx',tx_hash)
        observed['observation_ref']=_identity(db,'native-resolution-receipt',_encoded({'receipt':observed,'tx_ref':reference,
            'signer_digest':signer(terms,kind),'nonce':terms['nonce'],'index':str(index)})[0])
        proof={'outcome':'CANCELLED' if cancel else 'TRANSFERRED' if success else 'REVERTED','receipt':observed,'tx_ref':reference,
            'nonce':terms['nonce'],'transaction_index':str(index),'source_nonce':str(quantity(evidence['source_nonce'])),
            'maximum_wei':str(limit*cap),'signed_amount_wei':str(amount),'fee_overrun':limit*cap>int(terms['max_gas_wei']) or cap>int(terms['gas_price']),
            'amount_changed':not cancel and amount!=int(terms['amount_wei']),'source_code_empty':True,'target_code_empty':True}
        validate_proof(proof,terms,state,kind,row,now);return proof
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,BadSignature,ValidationError):
        raise DexExecutorError('El reemplazo BNB no corresponde a esta solicitud; conserva el pendiente') from None


def validate_proof(proof,terms,state,kind,row,at):
    keys={'outcome','receipt','tx_ref','nonce','transaction_index','source_nonce','maximum_wei','signed_amount_wei','fee_overrun','amount_changed','source_code_empty','target_code_empty'}
    if (set(proof)!=keys or proof['outcome'] not in {'CANCELLED','TRANSFERRED','REVERTED'} or proof['nonce']!=terms['nonce']
            or not re.fullmatch(r'[0-9a-f]{64}',proof['tx_ref']) or proof['source_code_empty'] is not True or proof['target_code_empty'] is not True
            or type(proof['fee_overrun']) is not bool or type(proof['amount_changed']) is not bool):raise ValueError('proof')
    _uint(proof['transaction_index'],'Índice',positive=False)
    if _uint(proof['source_nonce'],'Nonce',maximum=2**64-1)<=int(terms['nonce']):raise ValueError('unconsumed nonce')
    receipt=proof['receipt'];maximum=_uint(proof['maximum_wei'],'Gas máximo');amount=_uint(proof['signed_amount_wei'],'BNB',positive=False)
    cancel=proof['outcome']=='CANCELLED';status='REVERTED' if proof['outcome']=='REVERTED' else 'FINALIZED'
    validate_archived_receipt({key:value for key,value in receipt.items() if key!='transferred_wei'},
        {'max_gas_wei':str(maximum)},SimpleNamespace(status=status,created_at=row.created_at,updated_at=at),creation=False)
    if (receipt['gas_used']!='21000' or not 21000<=int(receipt['gas_limit'])<=100000 or int(receipt['fee_cap'])>10**12
            or maximum!=int(receipt['gas_limit'])*int(receipt['fee_cap']) or int(receipt['block_number'])<int(terms['anchor']['block_number'])
            or proof['fee_overrun']!=(maximum>int(terms['max_gas_wei']) or int(receipt['fee_cap'])>int(terms['gas_price']))
            or proof['amount_changed']!=(not cancel and amount!=int(terms['amount_wei']))
            or cancel and amount!=0 or not cancel and amount==0
            or receipt['transferred_wei']!=(str(amount) if proof['outcome']=='TRANSFERRED' else '0')):raise ValueError('effect/fee')
    nonce_observed=state.get('nonce_observation')
    require_receipt_before_observation(receipt,nonce_observed['anchor'] if nonce_observed else None)
