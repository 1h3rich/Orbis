"""Grupo OTOCO → fills auditados → evaluación puntual de salidas TESTNET.

Lee la definición sellada antes del envío; no vuelve a depender de LAB/PAPER.
La evidencia no es un permiso operativo ni una conciliación financiera.
"""

from dataclasses import asdict
from collections.abc import Callable
from decimal import Decimal
import time

import httpx

from app.live_execution.grvt_bracket_strategy_snapshot import read_bracket_strategy
from app.live_execution.grvt_filled_strategy_exit import evaluate_filled_strategy_exit,GrvtStrategyExitError
from app.live_execution.grvt_testnet_bracket_journal import read_bracket
from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
from app.live_execution.grvt_testnet_exit_market import fetch_testnet_exit_market,GrvtTestnetExitMarket
from app.live_execution.grvt_filled_strategy_exit import GrvtExitMark
from app.live_execution.grvt_testnet_fills import audit_testnet_fills
from app.live_execution.grvt_testnet_oco import _encode
from app.live_execution.grvt_testnet_oco_preflight import _safe
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient
from app.live_execution.grvt_testnet_strategy_close import _exclusive
from app.live_execution.grvt_native_exit import classify_native_exit, NativeExitObserved
from app.portfolio.vault import RealVault
from app.portfolio.models import GrvtTestnetBracketClaim,GrvtTestnetBracketObservation
from sqlalchemy import select


def collect_bracket_exit_evidence(vault: RealVault,client: GrvtTestnetOrderClient,parent: str,*,
        public_post: Callable[[str,dict],httpx.Response],clock_ns: Callable[[],int]=time.time_ns,
        mark: GrvtExitMark|None=None,market_cache: GrvtTestnetExitMarket|None=None) -> tuple[dict,dict]:
    """No modifica órdenes, STOP, permisos ni capital y no solicita una clave de firma."""
    generation=vault.session_generation
    if generation is None: raise GrvtStrategyExitError('Bóveda bloqueada')
    try:
        with _exclusive(vault),vault.guard_session_generation(generation):
            snapshot=read_bracket_strategy(vault,parent)
            if snapshot is None: raise ValueError('no definition captured before entry')
            group=read_bracket(vault,parent)
            if group['emergency_attempts'] or group['reconciliation'] is not None:
                raise ValueError('group already reconciled or reduced')
            before=observe_bracket_orders(vault,client,parent,clock_ns=clock_ns)
            native=classify_native_exit(parent,before)
            if native is not None: raise NativeExitObserved(native)
            if before['orders'][0]['status']!='FILLED' or any(
                    Decimal(row['traded_size'])!=0 for row in before['orders'][1:]):
                raise ValueError('entry not filled or native exit has filled')
            entry=client.lookup_order(group['sub_account_id'],parent)
            if any(getattr(entry,key)!=before['orders'][0][key] for key in
                    ('order_id','client_order_id','status','size','traded_size','create_time_ns','update_time_ns')):
                raise ValueError('entry changed during review')
            audit=audit_testnet_fills(client,entry,now_ns=clock_ns())
            if audit.other_fills: raise ValueError('other activity needs reconciliation')
            market=fetch_testnet_exit_market(entry.instrument.replace('_USDT_Perp','/USDT:PERP'),
                snapshot['timeframe'],history_start_t=snapshot['history_start_t'],
                public_post=public_post,clock_ns=clock_ns,mark=mark,previous=market_cache)
            after=observe_bracket_orders(vault,client,parent,clock_ns=clock_ns)
            native=classify_native_exit(parent,after)
            if native is not None: raise NativeExitObserved(native)
            if after['orders']!=before['orders']: raise ValueError('group changed during review')
            evaluated_at=clock_ns()
            decision=evaluate_filled_strategy_exit(snapshot['definition'],timeframe=snapshot['timeframe'],
                engine_version=snapshot['engine_version'],order=entry,audit=audit,candles=market.candles,
                mark=market.mark,now_ns=evaluated_at)
            with vault.session() as db:
                claim=db.get(GrvtTestnetBracketClaim,parent)
                observation=db.scalar(select(GrvtTestnetBracketObservation).where(
                    GrvtTestnetBracketObservation.parent_client_order_id==parent,
                    GrvtTestnetBracketObservation.sequence==claim.observation_count))
                sequence=observation.sequence; digest=observation.payload_digest
            history={'source':market.source,'symbol':entry.instrument.replace('_USDT_Perp','/USDT:PERP'),
                'timeframe':snapshot['timeframe'],'start_t':market.history_start_t,'end_t':market.history_end_t,
                'candles':[bar.json() for bar in market.candles]}
            fills=[{'sub_account_id':entry.sub_account_id,'order_id':entry.order_id,
                'client_order_id':entry.client_order_id,'instrument':entry.instrument,
                'is_buyer':entry.side=='BUY','is_liquidation':False,
                **{key:value for key,value in _safe(asdict(fill)).items() if key!='event_time_ns'},
                'event_time':str(fill.event_time_ns)} for fill in audit.fills]
            evidence=_safe({'schema':'orbis.grvt.testnet.filled.exit.evidence.v1','parent_client_order_id':parent,
                'strategy_digest':_encode(snapshot)[1],'native_observation_sequence':sequence,
                'native_observation_digest':digest,'entry_order':asdict(entry),'fills':fills,
                'market':{'history':history,'history_digest':market.history_digest,'mark':asdict(market.mark)},
                'decision':asdict(decision),'evaluated_at_ns':evaluated_at})
            review={'network':'TESTNET','review_only':True,'authorization':False,'parent_client_order_id':parent,
                'strategy_digest':_encode(snapshot)[1],'strategy_version_id':snapshot['signal_source']['version_id'],
                'market_source':market.source,'market_history_digest':market.history_digest,
                'history_start_t':market.history_start_t,'history_end_t':market.history_end_t,
                'entry_fills':[asdict(fill) for fill in audit.fills],'decision':asdict(decision),
                'mainnet_order_sent':False}
            return review,evidence
    except NativeExitObserved:
        raise
    except Exception:
        raise GrvtStrategyExitError('Salidas de estrategia no verificables; conserva la protección nativa') from None


def review_bracket_strategy_exit(vault: RealVault,client: GrvtTestnetOrderClient,parent: str,*,
        public_post: Callable[[str,dict],httpx.Response],clock_ns: Callable[[],int]=time.time_ns) -> dict:
    review,_=collect_bracket_exit_evidence(vault,client,parent,public_post=public_post,clock_ns=clock_ns)
    return review
