"""Definición causal de entrada OTOCO: persistencia sin credenciales.

Los grupos anteriores a v43 conservan snapshot ausente; nunca se deduce
retroactivamente una definición nueva a partir del estado actual de LAB.
"""

import json
from typing import TypedDict,Literal,cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.lab.engine import INTERVALS,supports_engine_version,validate_strategy,validate_rule_timeframes
from app.live_execution.grvt_filled_strategy_exit import required_exit_warmup
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_journal import _verified
from app.live_execution.grvt_testnet_oco import _encode
from app.portfolio.models import GrvtTestnetBracketGroup,GrvtTestnetBracketStrategy
from app.portfolio.vault import RealVault


class BracketStrategySnapshot(TypedDict):
    schema: Literal['orbis.grvt.testnet.bracket.strategy.v1']
    parent_client_order_id: str
    plan_digest: str
    signal_source: dict[str,str]
    definition: dict
    timeframe: str
    engine_version: str
    history_start_t: int
    captured_at_ns: int


def _payload(group,plan,context: dict) -> BracketStrategySnapshot:
    definition=validate_strategy(context['definition'])
    timeframe=context['timeframe']; step=INTERVALS[timeframe]
    validate_rule_timeframes(definition,timeframe)
    if (set(context)!={'definition','timeframe','engine_version'}
            or definition['direction']!=plan['signal_source']['direction']
            or not supports_engine_version(context['engine_version'],definition)):
        raise ValueError('incompatible strategy snapshot')
    # Semilla fija, ligada a esta entrada; no se desplaza entre evaluaciones.
    warmup=max(500,required_exit_warmup(definition['exit'],step) if 'exit' in definition else 0)
    return {'schema':'orbis.grvt.testnet.bracket.strategy.v1',
        'parent_client_order_id':group.parent_client_order_id,'plan_digest':group.payload_digest,
        'signal_source':plan['signal_source'],'definition':definition,'timeframe':timeframe,
        'engine_version':context['engine_version'],
        'history_start_t':max(step,(plan['created_at_ns']//1_000_000_000//step-warmup)*step),
        'captured_at_ns':plan['created_at_ns']}


def verified_bracket_strategy(db: Session,row: GrvtTestnetBracketStrategy) -> BracketStrategySnapshot:
    try:
        group=db.get(GrvtTestnetBracketGroup,row.parent_client_order_id); plan=_verified(db,group)
        value=json.loads(row.payload_json)
        context={key:value[key] for key in ('definition','timeframe','engine_version')}
        if _encode(_payload(group,plan,context))!=(row.payload_json,row.payload_digest):
            raise ValueError('altered strategy snapshot')
        return cast(BracketStrategySnapshot,value)
    except Exception:
        raise GrvtTestnetBracketError('Definición OTOCO archivada no verificable') from None


def verify_all_bracket_strategies(db: Session) -> None:
    for row in db.scalars(select(GrvtTestnetBracketStrategy)): verified_bracket_strategy(db,row)


def read_bracket_strategy(vault: RealVault,parent: str) -> BracketStrategySnapshot|None:
    with vault.session() as db:
        row=db.get(GrvtTestnetBracketStrategy,parent)
        return verified_bracket_strategy(db,row) if row is not None else None


def save_bracket_strategy(vault: RealVault,parent: str,context: dict) -> BracketStrategySnapshot:
    """Sólo antes del claim; releer/reintentar nunca cambia la definición."""
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            group=db.get(GrvtTestnetBracketGroup,parent); plan=_verified(db,group)
            encoded,digest=_encode(_payload(group,plan,context))
            row=db.get(GrvtTestnetBracketStrategy,parent)
            if row is not None:
                result=verified_bracket_strategy(db,row)
                if (row.payload_json,row.payload_digest)!=(encoded,digest):
                    raise ValueError('a different definition already exists')
            else:
                if group.status!='PREPARED': raise ValueError('entry already claimed')
                row=GrvtTestnetBracketStrategy(parent_client_order_id=parent,payload_json=encoded,payload_digest=digest)
                db.add(row); db.flush(); result=verified_bracket_strategy(db,row)
            db.commit(); return result
        except Exception:
            db.rollback()
            raise GrvtTestnetBracketError('No se pudo archivar la definición antes del envío') from None
