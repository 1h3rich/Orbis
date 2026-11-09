"""Pinned BNB executor, canonical creation and immutable runtime bindings.

No compiler, wallet keys, RPC or arbitrary caller-supplied bytecode at runtime.
Wallet addresses and encoded constructor inputs are transient return values.
"""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from eth_abi import encode
from eth_utils import keccak
import rlp

from app.portfolio.domain import PortfolioError
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_swap import TOKENS, ROUTER


ARTIFACT_SHA256='acd44b437da4c3d9b73afe33d1b4d857e7ec47f42a2a9c1ed189e8d8bd0a3a37'
SOURCE_SHA256='e8db4e1a9bee09f2d596fabee7e5956272e38600ef8cdce628dfbebfc6a3915c'
FACTORY='0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7'
PROTOCOL='ORBIS_BOUNDED_SWAP_BSC_V01'
FEES=(100,500,3000,10000)


class DexExecutorError(PortfolioError):
    pass


@dataclass(frozen=True,slots=True)
class ExecutorArtifact:
    artifact_sha256:str
    source_sha256:str
    creation:bytes
    runtime:bytes
    slots:tuple[tuple[str,tuple[tuple[int,int],...]],...]


@lru_cache(maxsize=1)
def executor_artifact()->ExecutorArtifact:
    try:
        raw=(Path(__file__).with_name('artifacts')/'bnb_executor_v01.json').read_bytes()
        if hashlib.sha256(raw).hexdigest()!=ARTIFACT_SHA256:raise ValueError('artifact hash')
        value=json.loads(raw)
        if (value['name']!='BoundedSwapExecutor' or value['sourceSha256']!=SOURCE_SHA256
                or value['compiler']!='0.8.38+commit.1835d247.Emscripten.clang'
                or value['settings']['evmVersion']!='paris' or value['settings']['optimizer']!={'enabled':True,'runs':200}
                or set(value['immutableSlots'])!={'owner','pool','poolFee'}):raise ValueError('artifact identity')
        creation=bytes.fromhex(value['bytecode'][2:]);runtime=bytes.fromhex(value['runtimeTemplate'][2:])
        if not 0<len(creation)<=49152 or not 0<len(runtime)<=24576:raise ValueError('artifact size')
        slots=tuple((name,tuple((item['start'],item['length']) for item in entries))
                    for name,entries in sorted(value['immutableSlots'].items()))
        ranges=[]
        for _name,entries in slots:
            if not entries:raise ValueError('missing immutable')
            for offset,length in entries:
                if type(offset) is not int or type(length) is not int or length!=32 or not 0<=offset<=len(runtime)-32:
                    raise ValueError('immutable range')
                if runtime[offset:offset+length]!=bytes(length):raise ValueError('immutable placeholder')
                ranges.append((offset,offset+length))
        ranges.sort()
        if any(left[1]>right[0] for left,right in zip(ranges,ranges[1:])):raise ValueError('overlapping bindings')
        references=sorted((item['start'],item['start']+item['length']) for entries in value['immutableReferences'].values() for item in entries)
        if references!=ranges:raise ValueError('unmapped immutable')
        return ExecutorArtifact(ARTIFACT_SHA256,SOURCE_SHA256,creation,runtime,slots)
    except (OSError,ValueError,TypeError,KeyError):
        raise DexExecutorError('El artefacto DEX no coincide con la versión verificada') from None


def _integer(value,name,maximum,*,zero=False):
    if type(value) is not int or not (0 if zero else 1)<=value<=maximum:
        raise DexExecutorError(name+': entero fuera de rango')
    return value


def _owner(value):
    address=canonical_wallet_address(value)
    if int(address,16)==0 or address in {*TOKENS.values(),ROUTER,FACTORY}:
        raise DexExecutorError('Wallet propietaria no válida')
    return address


def _fee(value):
    if type(value) is not int or value not in FEES:raise DexExecutorError('Pool DEX no compatible')
    return value


def creation_input(wallet:str,fee:int)->bytes:
    return executor_artifact().creation+encode(['address','uint24'],[_owner(wallet),_fee(fee)])


def creation_transaction(wallet:str,fee:int,*,nonce:int,gas:int,gas_price:int)->dict:
    _integer(nonce,'Nonce',2**64-1,zero=True);_integer(gas,'Gas',10_000_000);_integer(gas_price,'Gas price',10**12)
    return {'from':_owner(wallet),'chainId':'0x38','type':'0x0','value':'0x0','nonce':hex(nonce),
        'gas':hex(gas),'gasPrice':hex(gas_price),'data':'0x'+creation_input(wallet,fee).hex()}


def executor_address(wallet:str,nonce:int)->str:
    _integer(nonce,'Nonce',2**64-1,zero=True)
    serial=nonce.to_bytes((nonce.bit_length()+7)//8,'big')
    return '0x'+keccak(rlp.encode([bytes.fromhex(_owner(wallet)[2:]),serial]))[-20:].hex()


def expected_runtime(wallet:str,pool:str,fee:int)->str:
    owner=_owner(wallet);pool=canonical_wallet_address(pool);_fee(fee)
    if int(pool,16)==0 or pool in {owner,*TOKENS.values(),ROUTER,FACTORY}:raise DexExecutorError('Pool DEX no válido')
    artifact=executor_artifact();code=bytearray(artifact.runtime)
    values={'owner':int(owner,16),'pool':int(pool,16),'poolFee':fee}
    for name,entries in artifact.slots:
        word=values[name].to_bytes(32,'big')
        for offset,length in entries:code[offset:offset+length]=word
    return '0x'+bytes(code).hex()
