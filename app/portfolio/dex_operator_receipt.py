"""Exact operator signature, program, events, owner token flow and actual gas."""
from collections import Counter
from datetime import datetime, timedelta
import json

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_keys.exceptions import BadSignature
from eth_utils import keccak, ValidationError

from app.live_execution.dex_artifact import DexExecutorError, executor_address, expected_runtime
from app.live_execution.dex_trade import _swap_data
from app.live_execution.evm_transaction import recover_wallet_call, quantity, word
from app.portfolio.dex_deployments import deployment_record
from app.portfolio.dex_deployment_receipt import finalized_block
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_funding import _uint, _time
from app.portfolio.wallet_swap import TOKENS


SWAPPED = '0x'+keccak(text='SwapExecuted(uint256,uint256,bool,uint256,uint256,uint256,uint256)').hex()
STOPPED = '0x'+keccak(text='Stopped(uint256,uint256,uint256)').hex()
TRANSFER = '0x'+keccak(text='Transfer(address,address,uint256)').hex()
FINANCIAL = {'amount_out_units','owned_wbnb','cost_usdt','assigned_cost_usdt','realized_pnl_usdt','new_policy_id'}
BASE = {'status','source','block_number','block_hash','block_at','gas_limit','gas_used','gas_price','gas_fee_wei','observation_ref'}


def _event_result(terms: dict, output: int, owned: int, cost: int, policy: int) -> dict:
    prior = terms['anchor']['counters']
    before_owned,before_cost = int(prior['owned_wbnb']),int(prior['cost_usdt'])
    if terms['action'] == 'STOP':
        if policy <= int(terms['policy_id']) or (owned == 0) != (cost == 0):raise ValueError('STOP state')
        return dict(amount_out_units=None,owned_wbnb=str(owned),cost_usdt=str(cost),assigned_cost_usdt=None,
                    realized_pnl_usdt=None,new_policy_id=str(policy))
    amount = int(terms['trade']['amount'])
    if policy != int(terms['policy_id']) or output < int(terms['trade']['minimum']):raise ValueError('swap output/policy')
    assigned = None
    if terms['action'] == 'BUY':
        expected_owned,expected_cost = before_owned+output,before_cost+amount
    else:
        assigned = before_cost if amount == before_owned else before_cost*amount//before_owned
        expected_owned,expected_cost = before_owned-amount,before_cost-assigned
    if (owned,cost) != (expected_owned,expected_cost):raise ValueError('position/cost accounting')
    return dict(amount_out_units=str(output),owned_wbnb=str(owned),cost_usdt=str(cost),
                assigned_cost_usdt=str(assigned) if assigned is not None else None,
                realized_pnl_usdt=str(output-assigned) if assigned is not None else None,new_policy_id=str(policy))


def _topic_address(value: str) -> str:
    integer = int(word(value),16)
    if integer >= 2**160:raise ValueError('address topic')
    return '0x'+integer.to_bytes(20,'big').hex()


def observe_operator_receipt(db, row, terms: dict, grant: dict, state: dict, evidence: dict, now: datetime) -> dict:
    from app.portfolio.dex_operator_journal import _identity
    try:
        raw = json.dumps(evidence,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(raw) > 500000 or set(evidence) != {'chain_id','wallet_address','tx_hash','transaction','receipt','block','finalized','pool','runtime'}:
            raise ValueError('evidence shape')
        tx_hash = word(evidence['tx_hash'])
        owner = canonical_wallet_address(evidence['wallet_address'])
        if (evidence['chain_id'] != '0x38' or _identity(db,'tx',tx_hash) != state['tx_ref']
                or _identity(db,'wallet',owner) != grant['wallet_digest']):raise ValueError('original hash/owner')
        executor = executor_address(owner,int(terms['creation_nonce']))
        tx,receipt,block,finalized = [evidence[key] for key in ('transaction','receipt','block','finalized')]
        sender,kind,gas_limit,price,_ = recover_wallet_call(tx,expected_to=executor)
        if (kind != 0 or _identity(db,'wallet',sender) != grant['operator_digest'] or gas_limit != int(terms['gas'])
                or price != int(terms['gas_price']) or quantity(tx['nonce']) != int(terms['nonce'])):raise ValueError('operator transaction')
        trade = terms['trade']
        expected_data = (keccak(text='stop()')[:4] if trade is None else _swap_data(int(terms['policy_id']),
            int(terms['contract_nonce']),terms['action'] == 'BUY',int(trade['amount']),int(trade['minimum']),int(trade['deadline'])))
        if tx['input'].lower() != '0x'+expected_data.hex():raise ValueError('exact reviewed call')
        height,block_hash,at = finalized_block(block,finalized,created_at=row.created_at,now=now)
        if height < int(terms['anchor']['block_number']):raise ValueError('receipt predates observation')
        if (word(tx['hash']) != tx_hash or word(receipt['transactionHash']) != tx_hash or quantity(receipt['type']) != 0
                or canonical_wallet_address(receipt['from']) != sender or canonical_wallet_address(receipt['to']) != executor
                or receipt['contractAddress'] is not None or quantity(tx['transactionIndex']) != quantity(receipt['transactionIndex'])
                or any(quantity(value['blockNumber']) != height or word(value['blockHash']) != block_hash for value in (tx,receipt))):
            raise ValueError('receipt linkage')
        deployment = deployment_record(db,grant['deployment_id'])[1]
        if evidence['runtime'].lower() != expected_runtime(owner,evidence['pool'],deployment['fee']):raise ValueError('program')
        used,status = quantity(receipt['gasUsed']),quantity(receipt['status'])
        if not 21000 <= used <= gas_limit or quantity(receipt['effectiveGasPrice']) != price or status not in (0,1):raise ValueError('gas')
        result = operator_effects(terms, grant, owner, executor, receipt['logs'], status=status, height=height,
            block_hash=block_hash, tx_hash=tx_hash, transaction_index=quantity(tx['transactionIndex']), at=at)
        return {'status':'FINALIZED' if status else 'REVERTED','source':'PUBLIC_RPC_OBSERVED',
            'block_number':str(height),'block_hash':block_hash,'block_at':at.isoformat(),
            'gas_limit':str(gas_limit),'gas_used':str(used),'gas_price':str(price),'gas_fee_wei':str(used*price),
            'observation_ref':_identity(db,'receipt',raw),**result}
    except DexExecutorError:
        raise
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,DecodingError,BadSignature,ValidationError):
        raise DexExecutorError('El recibo del operador no coincide con el original; no se repetirá la operación') from None


def validate_operator_receipt(receipt: dict, terms: dict, grant: dict, row) -> None:
    if (not isinstance(receipt,dict) or set(receipt) != BASE|FINANCIAL or receipt['status'] != row.status
            or receipt['source'] != 'PUBLIC_RPC_OBSERVED' or not isinstance(receipt['observation_ref'],str)
            or len(receipt['observation_ref']) != 64):raise ValueError('receipt identity')
    from app.portfolio.dex_deployments import _hex_ref
    if not _hex_ref(receipt['observation_ref']):raise ValueError('observation reference')
    if _uint(receipt['block_number'],'Bloque') < int(terms['anchor']['block_number']):raise ValueError('receipt block order')
    word(receipt['block_hash'])
    used = _uint(receipt['gas_used'],'Gas usado',maximum=int(terms['gas']))
    price = _uint(receipt['gas_price'],'Precio gas')
    at = _time(datetime.fromisoformat(receipt['block_at']))
    if (used < 21000 or price != int(terms['gas_price']) or receipt['gas_limit'] != terms['gas']
            or receipt['gas_fee_wei'] != str(used*price) or at.isoformat() != receipt['block_at']
            or at < row.created_at-timedelta(seconds=15) or at > row.updated_at+timedelta(seconds=15)):
        raise ValueError('receipt gas/time')
    if row.status == 'REVERTED':
        if any(receipt[key] is not None for key in FINANCIAL):raise ValueError('reverted financial result')
    else:
        owned,cost = [_uint(receipt[key],key,positive=False) for key in ('owned_wbnb','cost_usdt')]
        output = _uint(receipt['amount_out_units'],'Salida') if terms['action'] != 'STOP' else 0
        expected = _event_result(terms,output,owned,cost,_uint(receipt['new_policy_id'],'Política'))
        if expected != {key:receipt[key] for key in FINANCIAL}:raise ValueError('financial arithmetic')
        if terms['trade'] is not None and (int(at.timestamp()) > int(terms['trade']['deadline']) or int(at.timestamp()) >= grant['limits']['expires_at']):
            raise ValueError('execution time')


def operator_effects(terms, grant, owner, executor, logs, status, height, block_hash, tx_hash, transaction_index, at):
    trade = terms['trade']
    if not isinstance(logs,list) or len(logs) > 100 or not status and logs:raise ValueError('logs')
    result = {key:None for key in FINANCIAL}
    if status:
        if trade is not None and (int(at.timestamp()) > int(trade['deadline']) or int(at.timestamp()) >= grant['limits']['expires_at']):
            raise ValueError('execution deadline')
        executor_events = []
        owner_moves = []
        executor_net = Counter()
        indices = []
        for event in logs:
            address = canonical_wallet_address(event['address'])
            index = quantity(event['logIndex']);indices.append(index)
            topics = [word(topic) for topic in event['topics']]
            if (event['removed'] is not False or word(event['transactionHash']) != tx_hash
                    or quantity(event['transactionIndex']) != transaction_index
                    or quantity(event['blockNumber']) != height or word(event['blockHash']) != block_hash):raise ValueError('event linkage')
            if address == executor:executor_events.append((topics,event['data']))
            if address in TOKENS.values() and topics and topics[0] == TRANSFER:
                if len(topics) != 3:raise ValueError('transfer topics')
                source,target = _topic_address(topics[1]),_topic_address(topics[2])
                amount = int(word(event['data']),16)
                if source == owner:owner_moves.append((address,-amount))
                if target == owner:owner_moves.append((address,amount))
                if source == executor:executor_net[address] -= amount
                if target == executor:executor_net[address] += amount
        if len(set(indices)) != len(indices) or indices != sorted(indices) or len(executor_events) != 1:
            raise ValueError('unique executor event')
        topics,data = executor_events[0]
        if not isinstance(data,str) or not data.startswith('0x') or len(data) != (130 if trade is None else 322):
            raise ValueError('exact event ABI')
        if trade is None:
            if len(topics) != 2 or topics[0] != STOPPED or owner_moves or any(executor_net.values()):raise ValueError('STOP event')
            owned,cost = decode(['uint256','uint256'],bytes.fromhex(data[2:]),strict=True)
            result = _event_result(terms,0,owned,cost,int(topics[1],16))
        else:
            if topics != [SWAPPED,'0x'+int(terms['policy_id']).to_bytes(32,'big').hex(),
                    '0x'+int(terms['contract_nonce']).to_bytes(32,'big').hex()]:raise ValueError('swap event identity')
            buy,amount,output,owned,cost = decode(['bool','uint256','uint256','uint256','uint256'],bytes.fromhex(data[2:]),strict=True)
            if buy is not (terms['action'] == 'BUY') or amount != int(trade['amount']):raise ValueError('swap side/input')
            input_token,output_token = (TOKENS['USDT'],TOKENS['WBNB']) if buy else (TOKENS['WBNB'],TOKENS['USDT'])
            if Counter(owner_moves) != Counter([(input_token,-amount),(output_token,output)]) or any(executor_net.values()):
                raise ValueError('owner/executor token flows')
            if buy and int(terms['anchor']['counters']['acquired_wbnb'])+output > int(grant['limits']['max_wbnb_acquired']):
                raise ValueError('acquisition quota')
            result = _event_result(terms,output,owned,cost,int(topics[1],16))
    return result
