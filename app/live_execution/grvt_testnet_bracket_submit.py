"""Puente causal PAPER → único bulk OTOCO TESTNET, con revisión manual y sin worker."""

from decimal import Decimal
import json
import time
from typing import Callable

import httpx

from app.derivatives.paper_store import PaperPerpStore
from app.lab.store import LabStore
from app.live_execution.grvt_testnet_bracket import (
    GrvtTestnetBracketError,validate_testnet_bracket,verify_testnet_bracket,
)
from app.live_execution.grvt_testnet_bracket_journal import (
    prepare_bracket,claim_bracket,record_bracket_ack,match_bracket_orders,_verified,
)
from app.live_execution.grvt_testnet_bracket_preflight import make_bracket_preflight,verify_bracket_preflight
from app.live_execution.grvt_testnet_continuity import check_testnet_history_continuity
from app.live_execution.grvt_testnet_oco import _encode,_stamp,instrument_terms
from app.live_execution.grvt_testnet_protection import GrvtTestnetTrigger
from app.live_execution.grvt_testnet_quote import fetch_testnet_quote
from app.live_execution.grvt_testnet_risk import fetch_testnet_risk,evaluate_testnet_risk,_public_post
from app.live_execution.grvt_testnet_signal import _submit_verified_signal_once,GrvtTestnetSignalBinding
from app.live_execution.grvt_testnet_signer import GrvtTestnetLimitDraft,GrvtTestnetInstrument,fetch_testnet_instrument
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient,GrvtTestnetLimits
from app.portfolio.models import GrvtTestnetBracketGroup,GrvtTestnetBracketClaim,GrvtTestnetStop
from app.portfolio.vault import RealVault


def submit_bracket_signal_once(paper: PaperPerpStore,lab: LabStore,vault: RealVault,
                              client: GrvtTestnetOrderClient,orders: list[dict],entry_limits: GrvtTestnetLimits,
                              exit_limits: GrvtTestnetLimits,instrument: GrvtTestnetInstrument,*,
                              session_id: str,action_request_id: str,max_spread_bps: int=25,max_slippage_bps: int=25,
                              clock_ns: Callable[[],int]=time.time_ns,
                              public_post: Callable[[str,dict],httpx.Response]=_public_post
                              ) -> tuple[GrvtTestnetSignalBinding,dict]:
    """Comparte el replay IOC sin ampliar su contrato; sólo OPEN con FOK/SL/TP.

    La transacción PAPER permanece estable hasta reservar el POST. El llamador
    local sigue necesitando sesión, confirmación y ámbito testnet: no hay una
    autoridad operativa persistente en este módulo. La API manual TESTNET
    conserva la revisión y confirmación por intento.
    La causalidad es de entrada; SL/TP son términos revisados independientes.
    No implementa todavía CLOSE, trailing, permanencia ni el ciclo del bot.
    """
    orders=json.loads(_encode(orders)[0])
    def signature_verifier(parent,policy,*,now_ns):
        triggers=tuple(GrvtTestnetTrigger(order['metadata']['trigger']['trigger_type'],
            Decimal(order['metadata']['trigger']['tpsl']['trigger_price'])) for order in orders[1:])
        return verify_testnet_bracket(orders,policy,exit_limits,instrument,expected_triggers=triggers,now_ns=now_ns)
    def submitter(vault,client,parent,policy,**kwargs):
        return _submit_bracket_once(vault,client,orders,policy,exit_limits,instrument,
            public_post=public_post,max_spread_bps=max_spread_bps,max_slippage_bps=max_slippage_bps,**kwargs)
    return _submit_verified_signal_once(paper,lab,vault,client,orders[0],entry_limits,
        session_id=session_id,action_request_id=action_request_id,clock_ns=clock_ns,
        contract_validator=lambda parent,policy,now:validate_testnet_bracket(orders,policy,exit_limits,now_ns=now)[0],
        signature_verifier=signature_verifier,submitter=submitter,include_signal_timing=True,
        include_strategy_context=True)


def _submit_bracket_once(vault,client,orders,entry_limits,exit_limits,instrument,*,signal_source,
                         signal_recorded_at_ns,strategy_context,clock_ns,public_post,max_spread_bps,max_slippage_bps):
    parent=orders[0]['metadata']['client_order_id']
    with vault.session() as db:
        row=db.get(GrvtTestnetBracketGroup,parent)
        if row is not None and _verified(db,row) and row.status!='PREPARED':
            raise GrvtTestnetBracketError('Intento OTOCO ya consumido; consulta sus tres IDs')
    last=clock_ns(); _stamp(last)
    def checked():
        nonlocal last
        now=clock_ns()
        if type(now) is not int or now<last:
            raise GrvtTestnetBracketError('Reloj OTOCO retrocedió')
        last=now; return now
    actual=fetch_testnet_instrument(entry_limits.instrument,http_post=public_post)
    if instrument_terms(actual)!=instrument_terms(instrument):
        raise GrvtTestnetBracketError('Reglas del instrumento OTOCO cambiadas')
    leg=orders[0]['legs'][0]
    draft=GrvtTestnetLimitDraft(entry_limits.sub_account_id,entry_limits.instrument,
        'BUY' if leg['is_buying_asset'] else 'SELL',Decimal(leg['size']),Decimal(leg['limit_price']),
        False,orders[0]['builder'],parent,orders[0]['signature']['nonce'],int(orders[0]['signature']['expiration']))
    first,rules=fetch_testnet_risk(client,draft.instrument,draft.sub_account_id,public_post=public_post,clock_ns=checked)
    if not evaluate_testnet_risk(draft,entry_limits,first,rules,now_ns=checked()).allowed:
        raise GrvtTestnetBracketError('Primer control de riesgo OTOCO rechazado')
    history=check_testnet_history_continuity(vault,client,draft.sub_account_id,now_ns=checked())
    state,new_rules=fetch_testnet_risk(client,draft.instrument,draft.sub_account_id,public_post=public_post,clock_ns=checked)
    stable=('sub_account_id','settle_currency','margin_type','sub_account_mode','settle_balance','positions',
        'open_order_count','total_equity','initial_margin','maintenance_margin','unrealized_pnl')
    if (any(getattr(first,key)!=getattr(state,key) for key in stable)
            or state.event_time_ns<first.event_time_ns or state.observed_at_ns<first.observed_at_ns
            or (rules.instrument,rules.max_size,rules.tiers)!=(new_rules.instrument,new_rules.max_size,new_rules.tiers)
            or new_rules.fetched_at_ns<rules.fetched_at_ns):
        raise GrvtTestnetBracketError('Cuenta o reglas OTOCO cambiadas durante la revisión')
    quote=fetch_testnet_quote(entry_limits.instrument,http_post=public_post,clock_ns=checked)
    # No reserva identidades si el replay/señal ya venció mientras se leían datos.
    prepared_ns=checked()
    plan=prepare_bracket(vault,orders,entry_limits,exit_limits,instrument,signal_source=signal_source,
        now_ns=prepared_ns,max_spread_bps=max_spread_bps,max_slippage_bps=max_slippage_bps)
    from app.live_execution.grvt_bracket_strategy_snapshot import save_bracket_strategy
    save_bracket_strategy(vault,parent,strategy_context)
    proof=make_bracket_preflight(plan,state,new_rules,quote,history,
        checked_at_ns=checked(),signal_recorded_at_ns=signal_recorded_at_ns)
    claim_bracket(vault,parent,orders,entry_limits,exit_limits,preflight=proof,now_ns=checked())
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row=db.get(GrvtTestnetBracketGroup,parent)
            p=_verified(db,row)
            claim=db.get(GrvtTestnetBracketClaim,parent)
            stop=db.get(GrvtTestnetStop,entry_limits.sub_account_id)
            if row.status!='POST_CLAIMED' or stop is not None and stop.stopped:
                raise GrvtTestnetBracketError('Concesión OTOCO revocada')
            now=checked()
            match_bracket_orders(p,orders,entry_limits,exit_limits,now_ns=now)
            verify_bracket_preflight(db,p,json.loads(claim.preflight_json),now_ns=now)
            acks=client.submit_signed_bracket(orders,entry_limits,exit_limits)
            db.commit()
        except Exception:
            db.rollback(); raise
    # El acuse es evidencia incluso si venció la señal durante la respuesta.
    try: stamp=checked()
    except Exception: stamp=last
    return record_bracket_ack(vault,parent,acks,now_ns=stamp)
