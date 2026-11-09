"""Read-only signed call outcomes. No grant, signer, DB writes or resend.

Known wallet/router and operator/executor calls share their original economic
validators. Fees are authenticated independently; no adapted/fake signed bytes.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json,re
from types import SimpleNamespace
from typing import Callable,Literal

from eth_abi import encode
from eth_abi.exceptions import DecodingError
from eth_keys.exceptions import BadSignature
from eth_utils import ValidationError,keccak

from app.live_execution.dex_artifact import DexExecutorError,executor_address,expected_runtime
from app.live_execution.dex_owner_observation import _pool,_same_block
from app.live_execution.dex_trade import _swap_data
from app.live_execution.evm_transaction import quantity,word,recover_wallet_call,recover_wallet_cancellation,recover_reverted_call
from app.portfolio.dex_deployment_receipt import finalized_block,validate_archived_receipt,require_receipt_before_observation
from app.portfolio.dex_operator_receipt import operator_effects,_event_result,FINANCIAL
from app.portfolio.wallet_swap_receipt import wallet_effects
from app.portfolio.wallet_swap import transaction,ROUTER
from app.portfolio.wallet_funding import _uint,_time,_encoded
from app.portfolio.wallet_proof import canonical_wallet_address


@dataclass(frozen=True,slots=True,repr=False)
class CallSource:
    kind:Literal['WALLET_SWAP','OPERATOR']
    id:str
    owner:str
    signer_digest:str
    terms:dict
    grant:dict|None
    created_at:datetime
    original_tx_ref:str|None
    observation:dict|None
    fee:int

    @property
    def nonce(self)->int:return int(self.terms['nonce'])

    def expected_call(self)->dict:
        if self.kind=='WALLET_SWAP':return transaction(self.terms,self.owner)
        terms=self.terms;trade=terms['trade']
        data=(keccak(text='stop()')[:4] if trade is None else _swap_data(int(terms['policy_id']),int(terms['contract_nonce']),
            terms['action']=='BUY',int(trade['amount']),int(trade['minimum']),int(trade['deadline'])))
        return {'to':executor_address(self.owner,int(terms['creation_nonce'])),'data':'0x'+data.hex(),'value':'0x0'}


def wallet_source(row,terms:dict,state:dict,wallet:str)->CallSource:
    return CallSource('WALLET_SWAP',row.id,canonical_wallet_address(wallet),terms['wallet_digest'],deepcopy(terms),None,
        _time(row.created_at),state['tx_digest'],None,terms['fee'])


def operator_source(row,terms:dict,state:dict,grant:dict,wallet:str,fee:int)->CallSource:
    return CallSource('OPERATOR',row.id,canonical_wallet_address(wallet),terms['wallet_digest'],deepcopy(terms),deepcopy(grant),
        _time(row.created_at),state['tx_ref'],deepcopy(state['nonce_observation']),fee)


def _call_reference(reference,*,target,value,data):
    # Calldata can embed a private owner address. A plain public hash could be
    # matched against chain data, so retain only an account-local keyed reference.
    raw=json.dumps({'to':target,'value':str(value),'data':data},sort_keys=True,separators=(',',':'))
    return reference('call-resolution-data',raw)


def observation_reference(source:CallSource,proof:dict,reference:Callable[[str,str],str])->str:
    """Attest verified canonical facts bound to their exact original definition.

    Provider annotations and advancing finalized heads cannot change the fixed
    receipt. The journal verifies this keyed attestation again when reopening.
    """
    body={'source_kind':source.kind,'source_id':source.id,'source_terms_digest':_encoded(source.terms)[1],
        'source_grant_digest':_encoded(source.grant)[1] if source.grant is not None else None,
        'proof':{**proof,'receipt':{key:value for key,value in proof['receipt'].items() if key!='observation_ref'}}}
    return reference('call-resolution-receipt',_encoded(body)[0])


def collect_call_resolution(rpc,source:CallSource,tx_hash:str)->dict|None:
    """Bounded observation at the receipt's block; None is still unconfirmed."""
    try:
        tx_hash=word(tx_hash)
        if quantity(rpc('eth_chainId',[]))!=56:raise ValueError('chain')
        receipt=rpc('eth_getTransactionReceipt',[tx_hash])
        if receipt is None:return None
        final=rpc('eth_getBlockByNumber',['finalized',False]);tag=hex(quantity(receipt['blockNumber']))
        if quantity(final['number'])<quantity(receipt['blockNumber']):return None
        block=rpc('eth_getBlockByNumber',[tag,False]);tx=rpc('eth_getTransactionByHash',[tx_hash])
        sender=canonical_wallet_address(tx['from']);target=None if tx.get('to') is None else canonical_wallet_address(tx['to'])
        source_code=rpc('eth_getCode',[sender,tag]);target_code=rpc('eth_getCode',[target,tag]) if target else None
        source_nonce=rpc('eth_getTransactionCount',[sender,tag]);pool=runtime=allowance=None
        expected=source.expected_call()
        matches=(target==expected['to'] and tx['input'].lower()==expected['data'] and quantity(tx['value'])==0)
        if quantity(receipt['status'])==1 and matches:
            if source.kind=='OPERATOR':
                pool=_pool(rpc,source.fee,tag);runtime=target_code
            elif source.terms['action']=='SWAP':pool=_pool(rpc,source.fee,tag)
            else:
                raw=rpc('eth_call',[{'to':target,'data':'0xdd62ed3e'+encode(['address','address'],[source.owner,ROUTER]).hex()},tag])
                word(raw);allowance=raw
        _same_block(rpc,block,tag)
        return {'chain_id':'0x38','tx_hash':tx_hash,'transaction':tx,'receipt':receipt,'block':block,'finalized':final,
            'source_code':source_code,'target_code':target_code,'source_nonce':source_nonce,'pool':pool,'runtime':runtime,'post_allowance':allowance}
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,DecodingError):
        raise DexExecutorError('No se pudo leer el resultado finalizado de la llamada DEX') from None


def verify_call_resolution(source:CallSource,evidence:dict,*,reference:Callable[[str,str],str],now:datetime)->dict:
    try:
        raw=json.dumps(evidence,sort_keys=True,separators=(',',':'),allow_nan=False)
        if (len(raw)>750000 or set(evidence)!={'chain_id','tx_hash','transaction','receipt','block','finalized',
                'source_code','target_code','source_nonce','pool','runtime','post_allowance'}
                or evidence['chain_id']!='0x38' or evidence['source_code']!='0x'):raise ValueError('shape/EOA')
        tx,receipt,block,final=[evidence[k] for k in ('transaction','receipt','block','finalized')]
        tx_hash=word(evidence['tx_hash']);sender=canonical_wallet_address(tx['from'])
        target=None if tx.get('to') is None else canonical_wallet_address(tx['to'])
        success=quantity(receipt['status'])
        if success not in (0,1):raise ValueError('status')
        expected=source.expected_call();matches=target==expected['to'] and tx['input'].lower()==expected['data'] and quantity(tx['value'])==0
        cancel=target==sender and tx['input']=='0x' and quantity(tx['value'])==0
        if cancel and success!=1:raise ValueError('empty EOA self-call cannot revert')
        if cancel:recovered,typed,limit,cap,priority=recover_wallet_cancellation(tx,expected_owner=sender)
        elif matches:recovered,typed,limit,cap,priority=recover_wallet_call(tx,expected_to=target)
        elif success==0:recovered,typed,limit,cap,priority=recover_reverted_call(tx,receipt_status=success)
        else:raise ValueError('successful external call requires effect reconciliation')
        if (recovered!=sender or reference('wallet',sender)!=source.signer_digest or quantity(tx['nonce'])!=source.nonce
                or not 21000<=limit<=2000000 or not 0<cap<=10**12):raise ValueError('signer/nonce/gas')
        owner_digest=source.terms['wallet_digest'] if source.kind=='WALLET_SWAP' else source.terms['owner_digest']
        if reference('wallet',source.owner)!=owner_digest:raise ValueError('owner')
        height,block_hash,at=finalized_block(block,final,created_at=source.created_at,now=now)
        index=quantity(tx['transactionIndex']);block_transactions=block['transactions']
        if (not isinstance(block_transactions,list) or len(block_transactions)>100000 or index>=len(block_transactions)
                or word(block_transactions[index])!=tx_hash or word(tx['hash'])!=tx_hash or word(receipt['transactionHash'])!=tx_hash
                or quantity(receipt['type'])!=typed or canonical_wallet_address(receipt['from'])!=sender
                or (None if receipt.get('to') is None else canonical_wallet_address(receipt['to']))!=target
                or quantity(receipt['transactionIndex'])!=index
                or any(quantity(item['blockNumber'])!=height or word(item['blockHash'])!=block_hash for item in (tx,receipt))):raise ValueError('receipt/block/index')
        if receipt['contractAddress'] is not None and (target is not None or success or
                canonical_wallet_address(receipt['contractAddress'])!=executor_address(sender,source.nonce)):raise ValueError('contract creation')
        consumed=quantity(evidence['source_nonce'])
        if not source.nonce<consumed<=2**64-1:raise ValueError('unconsumed nonce')
        base=quantity(block['baseFeePerGas']) if typed==2 else None
        price=min(cap,base+priority) if typed==2 else cap
        used=quantity(receipt['gasUsed'])
        if not 21000<=used<=limit or quantity(receipt['effectiveGasPrice'])!=price or typed==2 and cap<base:raise ValueError('fee')
        if target is None and used<53000:raise ValueError('creation intrinsic gas')
        effect=None
        if not success:
            if receipt['logs']!=[]:raise ValueError('reverted logs')
            outcome='REVERTED'
        elif cancel:
            if evidence['target_code']!='0x' or receipt['logs']!=[] or used!=21000:raise ValueError('self cancellation')
            outcome='CANCELLED'
        else:
            if not isinstance(evidence['target_code'],str) or evidence['target_code']=='0x':raise ValueError('known target code')
            outcome='EXECUTED'
            if source.kind=='WALLET_SWAP':
                if source.terms['action']=='SWAP' and int(at.timestamp())>source.terms['deadline']:raise ValueError('deadline')
                output,approved=wallet_effects(source.terms,source.owner,receipt['logs'],evidence['pool'],evidence['post_allowance'],height,block_hash,tx_hash,index,1)
                effect={'amount_out_units':output,'approved_units':approved}
            else:
                if evidence['post_allowance'] is not None or evidence['runtime']!=expected_runtime(source.owner,evidence['pool'],source.fee):raise ValueError('executor runtime')
                effect=operator_effects(source.terms,source.grant,source.owner,target,receipt['logs'],1,height,block_hash,tx_hash,index,at)
        observed={'status':'FINALIZED' if success else 'REVERTED','source':'WALLET_RPC_OBSERVED',
            'block_number':str(height),'block_hash':block_hash,'block_at':at.isoformat(),'transaction_type':typed,
            'gas_limit':str(limit),'gas_used':str(used),'fee_cap':str(cap),'priority_fee':str(priority) if priority is not None else None,
            'base_fee':str(base) if base is not None else None,'effective_gas_price':str(price),'gas_fee_wei':str(used*price)}
        proof={'outcome':outcome,'call_kind':'KNOWN' if matches else 'SELF_CANCEL' if cancel else 'CREATE' if target is None else 'OTHER_CALL',
            'receipt':observed,'effect':effect,'nonce':str(source.nonce),'transaction_index':str(index),
            'source_nonce':str(consumed),'tx_ref':reference('tx',tx_hash),'matches_original':None if source.original_tx_ref is None else reference('tx',tx_hash)==source.original_tx_ref,
            'matches_call':matches,'call_digest':_call_reference(reference,target=target,value=quantity(tx['value']),data=tx['input'].lower()),
            'expected_call_digest':_call_reference(reference,target=expected['to'],value=0,data=expected['data']),
            'fee_changed':typed!=0 or limit!=int(source.terms['gas']) or cap!=int(source.terms['gas_price']),
            'fee_overrun':limit*cap>int(source.terms['max_gas_wei']) or cap>int(source.terms['gas_price'])}
        observed['observation_ref']=observation_reference(source,proof,reference)
        validate_call_proof(proof,source,now=now);return proof
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,DecodingError,BadSignature,ValidationError):
        raise DexExecutorError('El reemplazo no demuestra el resultado de esta llamada DEX; conserva el pendiente') from None


def validate_call_proof(proof:dict,source:CallSource,*,now:datetime)->None:
    """Replay sanitized facts; the journal must authenticate source and proof.

    The live verifier attests both keyed call references after reconstructing
    signed bytes. Reopen compares them without needing stored owner addresses.
    """
    keys={'outcome','call_kind','receipt','effect','nonce','transaction_index','source_nonce','tx_ref','matches_original','matches_call','call_digest','expected_call_digest','fee_changed','fee_overrun'}
    if (not isinstance(proof,dict) or set(proof)!=keys or proof['outcome'] not in {'CANCELLED','EXECUTED','REVERTED'}
            or proof['nonce']!=str(source.nonce) or type(proof['matches_call']) is not bool
            or any(type(proof[k]) is not bool for k in ('fee_changed','fee_overrun'))
            or (proof['matches_original'] is not None if source.original_tx_ref is None else type(proof['matches_original']) is not bool)
            or any(not isinstance(proof[k],str) or not re.fullmatch(r'[0-9a-f]{64}',proof[k]) for k in ('tx_ref','call_digest','expected_call_digest'))):raise ValueError('call proof')
    _uint(proof['transaction_index'],'Índice',positive=False)
    if (proof['call_kind'] not in {'KNOWN','SELF_CANCEL','CREATE','OTHER_CALL'} or proof['matches_call']!=(proof['call_kind']=='KNOWN')
            or (proof['outcome']=='CANCELLED')!=(proof['call_kind']=='SELF_CANCEL')
            or proof['call_kind'] in {'CREATE','OTHER_CALL'} and proof['outcome']!='REVERTED'):raise ValueError('call kind/outcome')
    if _uint(proof['source_nonce'],'Nonce',maximum=2**64-1)<=source.nonce:raise ValueError('nonce')
    receipt=proof['receipt'];limit=int(receipt['gas_limit']);cap=int(receipt['fee_cap'])
    status='REVERTED' if proof['outcome']=='REVERTED' else 'FINALIZED'
    validate_archived_receipt(receipt,{'max_gas_wei':str(limit*cap)},SimpleNamespace(status=status,created_at=source.created_at,updated_at=_time(now)),creation=False)
    if proof['call_kind']=='CREATE' and int(receipt['gas_used'])<53000:raise ValueError('creation intrinsic gas')
    if (proof['fee_changed']!=(receipt['transaction_type']!=0 or limit!=int(source.terms['gas']) or cap!=int(source.terms['gas_price']))
            or proof['fee_overrun']!=(limit*cap>int(source.terms['max_gas_wei']) or cap>int(source.terms['gas_price']))):raise ValueError('fee review')
    if source.kind=='OPERATOR' and int(receipt['block_number'])<int(source.terms['anchor']['block_number']):raise ValueError('receipt anchor')
    if (source.kind=='OPERATOR' and receipt['block_number']==source.terms['anchor']['block_number']
            and receipt['block_hash']!=source.terms['anchor']['block_hash']):raise ValueError('anchor fork')
    require_receipt_before_observation(receipt,source.observation)
    if (source.observation is not None and receipt['block_number']==source.observation['block_number']
            and proof['source_nonce']!=source.observation['operator_nonce']):raise ValueError('nonce observation changed')
    if proof['matches_call']!=(proof['call_digest']==proof['expected_call_digest']):raise ValueError('call binding')
    if source.original_tx_ref is not None and proof['matches_original']!=(source.original_tx_ref==proof['tx_ref']):raise ValueError('original hash')
    if proof['outcome']!='EXECUTED':
        if proof['effect'] is not None or proof['outcome']=='CANCELLED' and (receipt['gas_used']!='21000' or proof['matches_call']):raise ValueError('no executed effect')
        return
    effect=proof['effect']
    if not proof['matches_call'] or not isinstance(effect,dict):raise ValueError('executed known call')
    at=_time(datetime.fromisoformat(receipt['block_at']))
    if source.kind=='WALLET_SWAP':
        if set(effect)!={'amount_out_units','approved_units'}:raise ValueError('wallet effect')
        if source.terms['action']=='SWAP':
            if (_uint(effect['amount_out_units'],'Salida')<int(source.terms['minimum_units']) or effect['approved_units'] is not None
                    or int(at.timestamp())>source.terms['deadline']):raise ValueError('swap amount/deadline')
        elif effect['amount_out_units'] is not None or effect['approved_units']!=('0' if source.terms['action']=='REVOKE' else source.terms['amount_units']):raise ValueError('approval')
    else:
        if set(effect)!=FINANCIAL:raise ValueError('operator effect')
        output=_uint(effect['amount_out_units'],'Salida') if source.terms['trade'] else 0
        expected=_event_result(source.terms,output,_uint(effect['owned_wbnb'],'Posición',positive=False),_uint(effect['cost_usdt'],'Coste',positive=False),_uint(effect['new_policy_id'],'Política'))
        if effect!=expected:raise ValueError('operator arithmetic')
        if source.terms['action']=='BUY' and int(source.terms['anchor']['counters']['acquired_wbnb'])+output>int(source.grant['limits']['max_wbnb_acquired']):raise ValueError('acquisition quota')
        if source.terms['trade'] and (int(at.timestamp())>int(source.terms['trade']['deadline']) or int(at.timestamp())>=source.grant['limits']['expires_at']):raise ValueError('operator deadline')
