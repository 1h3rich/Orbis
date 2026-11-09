"""Read and verify an already mined replacement; never signs or sends."""
from datetime import datetime
import json
from types import SimpleNamespace
from eth_keys.exceptions import BadSignature
from eth_abi.exceptions import DecodingError
from eth_utils import ValidationError
from app.live_execution.dex_artifact import DexExecutorError,executor_address
from app.live_execution.dex_owner_observation import _pool,_same_block
from app.live_execution.evm_transaction import quantity,word,recover_wallet_call,recover_wallet_cancellation
from app.portfolio.dex_deployment_receipt import finalized_block,observe_receipt,validate_archived_receipt,observe_code,validate_archived_code
from app.portfolio.wallet_proof import canonical_wallet_address


def collect_resolution(rpc,wallet,nonce,fee,tx_hash):
    try:
        owner=canonical_wallet_address(wallet);tx_hash=word(tx_hash)
        if quantity(rpc('eth_chainId',[]))!=56:raise ValueError('chain')
        receipt=rpc('eth_getTransactionReceipt',[tx_hash])
        if receipt is None:return None
        tag=hex(quantity(receipt['blockNumber']));final=rpc('eth_getBlockByNumber',['finalized',False])
        if quantity(final['number'])<quantity(receipt['blockNumber']):return None
        block=rpc('eth_getBlockByNumber',[tag,False]);tx=rpc('eth_getTransactionByHash',[tx_hash])
        code=rpc('eth_getCode',[executor_address(owner,nonce),tag]);owner_code=rpc('eth_getCode',[owner,tag]);owner_nonce=rpc('eth_getTransactionCount',[owner,tag])
        pool=_pool(rpc,fee,tag) if tx.get('to') is None else None
        _same_block(rpc,block,tag)
        return {'chain_id':'0x38','tx_hash':tx_hash,'transaction':tx,'receipt':receipt,'block':block,'finalized':final,
            'runtime':code,'pool':pool,'owner_code':owner_code,'owner_nonce':owner_nonce}
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,DecodingError):raise DexExecutorError('No se pudo leer el reemplazo finalizado') from None


def verify_resolution(db,row,terms,state,wallet,evidence,now):
    from app.portfolio.dex_deployments import _identity
    try:
        raw=json.dumps(evidence,sort_keys=True,separators=(',',':'),allow_nan=False)
        if (len(raw)>500000 or set(evidence)!={'chain_id','tx_hash','transaction','receipt','block','finalized','runtime','pool','owner_code','owner_nonce'}
                or evidence['chain_id']!='0x38' or evidence['owner_code']!='0x'):raise ValueError('evidence shape or owner code')
        tx,receipt,block,final=[evidence[key] for key in ('transaction','receipt','block','finalized')]
        owner=canonical_wallet_address(wallet);tx_hash=word(evidence['tx_hash']);creation=tx.get('to') is None
        sender,kind,limit,cap,priority=(recover_wallet_call(tx,expected_to=None) if creation else recover_wallet_cancellation(tx,expected_owner=owner))
        if (sender!=owner or _identity(db,'wallet',owner)!=row.wallet_digest or quantity(tx['nonce'])!=int(terms['nonce'])
                or not 0<cap<=10**12 or not 21000<=limit<=(10000000 if creation else 100000)):raise ValueError('replacement identity or gas')
        maximum=limit*cap;reference=_identity(db,'tx',tx_hash)
        if creation:
            legacy={key:value for key,value in evidence.items() if key not in {'owner_code','owner_nonce'}}
            observed=observe_receipt(db,row,{**terms,'max_gas_wei':str(maximum)},{**state,'tx_ref':reference},legacy,now)
            outcome='DEPLOYED' if observed['status']=='FINALIZED' else 'REVERTED'
            code_observation=observe_code(db,row,terms,owner,{'chain_id':'0x38','block':block,'pool':evidence['pool'],'runtime':evidence['runtime'],
                'owner_nonce':evidence['owner_nonce'],'contract_address':executor_address(owner,int(terms['nonce']))},now) if outcome=='DEPLOYED' else None
        else:
            height,block_hash,at=finalized_block(block,final,created_at=row.created_at,now=now)
            if (word(tx['hash'])!=tx_hash or word(receipt['transactionHash'])!=tx_hash or quantity(receipt['status'])!=1
                    or quantity(receipt['type'])!=kind or canonical_wallet_address(receipt['from'])!=owner
                    or canonical_wallet_address(receipt['to'])!=owner or receipt['contractAddress'] is not None or receipt['logs']!=[]
                    or any(quantity(item['blockNumber'])!=height or word(item['blockHash'])!=block_hash for item in (tx,receipt))
                    or quantity(tx['transactionIndex'])!=quantity(receipt['transactionIndex']) or evidence['runtime']!='0x'
                    or state['code_observation'] is not None):raise ValueError('cancellation receipt')
            base=quantity(block['baseFeePerGas']) if kind==2 else None
            price=min(cap,base+priority) if kind==2 else cap
            if (quantity(receipt['gasUsed'])!=21000 or quantity(receipt['effectiveGasPrice'])!=price or kind==2 and cap<base):raise ValueError('cancellation fee')
            observed={'status':'FINALIZED','source':'WALLET_RPC_OBSERVED','block_number':str(height),'block_hash':block_hash,'block_at':at.isoformat(),
                'transaction_type':kind,'gas_limit':str(limit),'gas_used':'21000','fee_cap':str(cap),'priority_fee':str(priority) if priority is not None else None,
                'base_fee':str(base) if base is not None else None,'effective_gas_price':str(price),'gas_fee_wei':str(21000*price),
                'observation_ref':_identity(db,'receipt',raw)}
            outcome='CANCELLED';code_observation=None
        # Finality is rechecked on every read, but a newer finalized tip or
        # provider-only receipt fields must not invalidate an unchanged review.
        # References pin only the immutable, verified transaction facts.
        from app.portfolio.wallet_funding import _encoded
        observed['observation_ref']=_identity(db,'resolution-receipt',_encoded({'receipt':{key:value for key,value in observed.items() if key!='observation_ref'},
            'tx_ref':reference,'wallet_digest':row.wallet_digest,'nonce':terms['nonce'],'index':str(quantity(tx['transactionIndex']))})[0])
        if code_observation is not None:
            code_observation['observation_ref']=_identity(db,'resolution-code',_encoded({'code':{key:value for key,value in code_observation.items() if key!='observation_ref'},
                'wallet_digest':row.wallet_digest,'fee':terms['fee']})[0])
        proof={'outcome':outcome,'receipt':observed,'tx_ref':reference,'nonce':terms['nonce'],
            'transaction_index':str(quantity(tx['transactionIndex'])),'maximum_wei':str(maximum),
            'fee_overrun':maximum>int(terms['max_gas_wei']) or cap>int(terms['gas_price']),'owner_code_empty':True,'owner_nonce':str(quantity(evidence['owner_nonce'])),'code_observation':code_observation}
        validate_proof(proof,terms,row,now)
        return proof
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,BadSignature,ValidationError):
        raise DexExecutorError('El reemplazo no corresponde a esta solicitud; conserva el pendiente') from None


def validate_proof(proof,terms,row,at):
    from app.portfolio.wallet_funding import _uint
    if (set(proof)!={'outcome','receipt','tx_ref','nonce','transaction_index','maximum_wei','fee_overrun','owner_code_empty','owner_nonce','code_observation'}
            or proof['outcome'] not in {'CANCELLED','DEPLOYED','REVERTED'} or proof['nonce']!=terms['nonce']
            or type(proof['fee_overrun']) is not bool or proof['owner_code_empty'] is not True
            or not isinstance(proof['tx_ref'],str) or len(proof['tx_ref'])!=64):raise ValueError('resolution proof')
    _uint(proof['transaction_index'],'Índice',positive=False)
    if _uint(proof['owner_nonce'],'Nonce',maximum=2**64-1)<=int(terms['nonce']):raise ValueError('unconsumed nonce')
    if proof['outcome']=='DEPLOYED':validate_archived_code(proof['code_observation'],terms,row,at)
    elif proof['code_observation'] is not None:raise ValueError('unexpected code')
    maximum=_uint(proof['maximum_wei'],'Máximo gas');receipt=proof['receipt']
    if (maximum!=int(receipt['gas_limit'])*int(receipt['fee_cap']) or proof['fee_overrun']!=(maximum>int(terms['max_gas_wei']) or int(receipt['fee_cap'])>int(terms['gas_price']))):raise ValueError('maximum gas')
    expected='REVERTED' if proof['outcome']=='REVERTED' else 'FINALIZED'
    proxy=SimpleNamespace(status=expected,created_at=row.created_at,updated_at=at)
    validate_archived_receipt(receipt,{'max_gas_wei':str(maximum)},proxy,creation=proof['outcome']!='CANCELLED')
    if proof['outcome']=='CANCELLED' and receipt['gas_used']!='21000':raise ValueError('cancel gas')
