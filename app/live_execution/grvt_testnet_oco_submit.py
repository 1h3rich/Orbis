"""Un bulk OCO TESTNET tras preflight, concesión duradera y control final."""

from decimal import Decimal
import json
import time
from typing import Callable

import httpx

from app.live_execution.grvt_testnet_oco import (GrvtTestnetOcoError, read_oco, _parent,
    _match_orders, _verified, _encode, _stamp, claim_oco, record_oco_ack, instrument_terms)
from app.live_execution.grvt_testnet_oco_preflight import make_oco_preflight, verify_oco_preflight
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient, GrvtTestnetLimits
from app.live_execution.grvt_testnet_signer import fetch_testnet_instrument, GrvtTestnetLimitDraft
from app.live_execution.grvt_testnet_quote import fetch_testnet_quote
from app.live_execution.grvt_testnet_risk import fetch_testnet_risk, evaluate_testnet_risk, _public_post
from app.live_execution.grvt_testnet_continuity import check_testnet_history_continuity
from app.portfolio.models import GrvtTestnetOcoGroup, GrvtTestnetOcoClaim, GrvtTestnetStop
from app.portfolio.vault import RealVault


def submit_oco_once(vault: RealVault, client: GrvtTestnetOrderClient, group_id: str,
                    orders: list[dict], limits: GrvtTestnetLimits, *,
                    clock_ns: Callable[[],int] = time.time_ns,
                    public_post: Callable[[str,dict],httpx.Response] = _public_post) -> dict:
    """No reenvía ni reanuda tras POST_CLAIMED, aunque no haya acuse local."""
    orders = json.loads(_encode(orders)[0])
    plan = read_oco(vault,group_id)
    if plan["status"] != "PREPARED":
        raise GrvtTestnetOcoError("Intento OCO ya consumido; revisa ambos IDs")
    last = clock_ns()
    _stamp(last)
    def checked() -> int:
        nonlocal last
        now = clock_ns()
        if type(now) is not int or now < last:
            raise GrvtTestnetOcoError("Reloj OCO retrocedió")
        last = now
        return now
    _match_orders(plan,orders,limits,now_ns=checked())
    current = fetch_testnet_instrument(limits.instrument,http_post=public_post)
    terms = instrument_terms(current)
    if terms != plan["instrument_terms"]:
        raise GrvtTestnetOcoError("Reglas del instrumento OCO cambiadas")
    first,rules = fetch_testnet_risk(client,limits.instrument,limits.sub_account_id,
                                   public_post=public_post,clock_ns=checked)
    for order in orders:
        leg = order["legs"][0]
        draft = GrvtTestnetLimitDraft(limits.sub_account_id,limits.instrument,
            "BUY" if leg["is_buying_asset"] else "SELL",Decimal(leg["size"]),Decimal(leg["limit_price"]),
            True,order["builder"],order["metadata"]["client_order_id"],
            order["signature"]["nonce"],int(order["signature"]["expiration"]))
        if not evaluate_testnet_risk(draft,limits,first,rules,now_ns=checked()).allowed:
            raise GrvtTestnetOcoError("Primer control de riesgo OCO rechazado")
    history = check_testnet_history_continuity(vault,client,limits.sub_account_id,now_ns=checked())
    state,new_rules = fetch_testnet_risk(client,limits.instrument,limits.sub_account_id,
                                       public_post=public_post,clock_ns=checked)
    stable = ("sub_account_id","settle_currency","margin_type","sub_account_mode","settle_balance",
              "positions","open_order_count")
    if (any(getattr(first,key) != getattr(state,key) for key in stable)
            or state.event_time_ns < first.event_time_ns or state.observed_at_ns < first.observed_at_ns
            or (rules.instrument,rules.max_size,rules.tiers) != (new_rules.instrument,new_rules.max_size,new_rules.tiers)
            or new_rules.fetched_at_ns < rules.fetched_at_ns):
        raise GrvtTestnetOcoError("Cuenta o reglas OCO cambiadas durante la revisión")
    quote = fetch_testnet_quote(limits.instrument,http_post=public_post,clock_ns=checked)
    proof = make_oco_preflight(plan,state,new_rules,quote,history,checked_at_ns=checked())
    with vault.session() as db:
        verify_oco_preflight(db,plan,proof,now_ns=checked())
    claim_oco(vault,group_id,orders,limits,now_ns=checked(),preflight=proof)
    with vault.session() as db:
        try:
            # STOP y bloqueo de la bóveda comparten esta exclusión. Una vez en
            # red el resultado puede ser incierto, pero nunca se repite el bulk.
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetOcoGroup,group_id)
            p = _verified(db,row)
            stop = db.get(GrvtTestnetStop,limits.sub_account_id)
            claim = db.get(GrvtTestnetOcoClaim,group_id)
            if row.status != "POST_CLAIMED" or stop is not None and stop.stopped or claim.preflight_json is None:
                raise GrvtTestnetOcoError("Concesión OCO revocada o no verificable")
            _parent(db,row.parent_client_order_id,latest=True)
            _match_orders(p,orders,limits,now_ns=checked())
            verify_oco_preflight(db,p,json.loads(claim.preflight_json),now_ns=checked())
            acks = client.submit_signed_oco(orders,limits)
            db.commit()
        except Exception:
            db.rollback(); raise
    # No descartar un acuse ya recibido si el reloj se corrige tras la red.
    try:
        stamp = checked()
    except GrvtTestnetOcoError:
        stamp = last
    return record_oco_ack(vault,group_id,acks,now_ns=stamp)
