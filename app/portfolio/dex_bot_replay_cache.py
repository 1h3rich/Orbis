"""Vault-scoped verified prefixes. No persisted cache and no trusted row hash.

Every reuse fingerprints actual bytes and relational metadata again, including
the finalized receipts referenced by that prefix. An edit or deletion forces
semantic replay. Frozen values prevent callers mutating a cached read result.
"""
from dataclasses import dataclass
import hashlib

from app.lab.engine import Candle
from app.live_execution.dex_bot_plan import DexBotCursor
from app.portfolio.models import RealDexOperatorIntent


def witness(row)->tuple:
    values=[]
    for column in row.__table__.columns:
        value=getattr(row,column.name)
        if column.name.endswith('_json'):value=hashlib.sha256(value.encode()).hexdigest()
        values.append((column.name,value))
    return tuple(values)


def receipt_witnesses(db,identifiers:tuple[str,...])->tuple:
    values=[]
    for identifier in identifiers:
        row=db.get(RealDexOperatorIntent,identifier)
        if row is None:raise ValueError('missing prefix receipt')
        values.append(witness(row))
        if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=71:
            from app.portfolio.models import RealDexCallResolution
            from app.portfolio.dex_call_resolution import identifier as resolution_id
            resolution=db.get(RealDexCallResolution,resolution_id('OPERATOR',identifier))
            values.append(('call_resolution',identifier,witness(resolution) if resolution is not None else None))
    return tuple(values)


@dataclass(frozen=True,slots=True)
class VerifiedPrefix:
    context:tuple
    decisions:tuple
    receipts:tuple
    bars:tuple[Candle,...]
    cursor:DexBotCursor
    settled:tuple[str,...]
    digest:str
    sequence:int
    last_action:str|None
    last_reason:str|None

    def matches(self,db,context,items)->bool:
        count=len(self.decisions)
        return (self.context==context and len(items)>=count
            and self.decisions==tuple(witness(row) for row in items[:count])
            and self.receipts==receipt_witnesses(db,self.settled))


def remember(cache:dict,identifier:str,prefix:VerifiedPrefix)->None:
    cache.pop(identifier,None)
    while len(cache)>=2:cache.pop(next(iter(cache)))
    cache[identifier]=prefix
