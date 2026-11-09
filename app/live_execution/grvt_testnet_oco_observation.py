"""Recuperación de ambas órdenes por ID sin persistir sus firmas completas."""

from decimal import Decimal, localcontext
import json
import re
import time
from typing import Callable
from uuid import uuid4

from sqlalchemy import select

from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient, GrvtTestnetLimits, _ORDER_FIELDS
from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument
from app.live_execution.grvt_testnet_protection import (
    GrvtTestnetTrigger, observe_testnet_protection, verify_testnet_protection, validate_testnet_protection,
)
from app.live_execution.grvt_testnet_oco import (GrvtTestnetOcoError, _verified, _encode, _stamp, _number,
    _verified_ack, canonical_oco_order)
from app.portfolio.models import GrvtTestnetOcoGroup, GrvtTestnetOcoClaim, GrvtTestnetOcoObservation
from app.portfolio.vault import RealVault


_FIELDS = {"role","client_order_id","order_id","status","traded_size","create_time_ns","update_time_ns","wire_digest"}
_TERMINAL = {"FILLED","CANCELLED","REJECTED"}


def _ns(value: object) -> int:
    if not isinstance(value,str) or not re.fullmatch(r"[0-9]{1,19}",value) or int(value) <= 0:
        raise ValueError("invalid order time")
    return int(value)


def _check_orders(plan: dict, orders: object, at_ns: int, previous: dict | None) -> None:
    if not isinstance(orders,list) or len(orders) != 2:
        raise ValueError("missing OCO order state")
    total = Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 100
        for i,(actual,expected) in enumerate(zip(orders,plan["legs"])):
            if (not isinstance(actual,dict) or set(actual) != _FIELDS
                    or any(actual[key] != expected[key] for key in ("role","client_order_id","wire_digest"))
                    or not isinstance(actual["order_id"],str) or not re.fullmatch(r"[A-Za-z0-9x]{1,80}",actual["order_id"])
                    or actual["status"] not in {"PENDING","OPEN",*_TERMINAL}
                    or not isinstance(actual["traded_size"],str)
                    or not re.fullmatch(r"[0-9]{1,30}(?:\.[0-9]{1,18})?",actual["traded_size"])):
                raise ValueError("wrong observed contract")
            filled,size = Decimal(actual["traded_size"]),Decimal(plan["size"])
            created,updated = _ns(actual["create_time_ns"]),_ns(actual["update_time_ns"])
            if (filled > size or created < plan["created_at_ns"]-1_000_000_000
                    or not created <= updated <= at_ns+2_000_000_000
                    or actual["status"] == "FILLED" and filled != size
                    or actual["status"] == "REJECTED" and filled != 0):
                raise ValueError("inconsistent order state")
            if previous is not None:
                before = previous["orders"][i]
                if (before["order_id"] != actual["order_id"] or before["create_time_ns"] != actual["create_time_ns"]
                        or _ns(before["update_time_ns"]) > updated or Decimal(before["traded_size"]) > filled
                        or before["status"] in _TERMINAL and (before["status"] != actual["status"]
                            or Decimal(before["traded_size"]) != filled)
                        or before["status"] == "OPEN" and actual["status"] == "PENDING"):
                    raise ValueError("observed order regressed")
            total += filled
        if total > Decimal(plan["size"]) or orders[0]["order_id"] == orders[1]["order_id"]:
            raise ValueError("OCO fill total exceeds source")


def latest_observation(db, group: GrvtTestnetOcoGroup, plan: dict) -> dict | None:
    """Valida la cadena completa, sin convertir estados de órdenes en fills."""
    try:
        records = list(db.scalars(select(GrvtTestnetOcoObservation).where(
            GrvtTestnetOcoObservation.group_id == group.id).order_by(GrvtTestnetOcoObservation.sequence)))
        previous, digest = None, None
        claim = db.get(GrvtTestnetOcoClaim,group.id)
        ack = _verified_ack(group,claim,plan) if claim is not None and claim.ack_json is not None else None
        for seq,record in enumerate(records,1):
            payload = json.loads(record.payload_json)
            if (group.status not in ("POST_CLAIMED","ACKNOWLEDGED") or claim is None
                    or record.sequence != seq or _encode(payload) != (record.payload_json,record.payload_digest)
                    or set(payload) != {"schema","group_id","plan_digest","sequence","previous_digest",
                        "observed_at_ns","scope","orders","can_submit","protection_guaranteed"}
                    or payload["schema"] != "orbis.grvt.testnet.oco.observation.v1"
                    or payload["group_id"] != group.id or payload["plan_digest"] != group.payload_digest
                    or type(payload["sequence"]) is not int or payload["sequence"] != seq
                    or payload["previous_digest"] != digest
                    or type(payload["observed_at_ns"]) is not int
                    or payload["observed_at_ns"] < int(claim.claimed_at_ns)
                    or payload["scope"] != "ORDER_STATE_ONLY" or payload["can_submit"] is not False
                    or payload["protection_guaranteed"] is not False
                    or previous is not None and payload["observed_at_ns"] < previous["observed_at_ns"]):
                raise ValueError("invalid observation chain")
            _stamp(payload["observed_at_ns"])
            _check_orders(plan,payload["orders"],payload["observed_at_ns"],previous)
            if ack is not None and any(actual["order_id"] != expected["order_id"]
                                      for actual,expected in zip(payload["orders"],ack["orders"])):
                raise ValueError("ack order changed")
            previous,digest = payload,record.payload_digest
        return previous
    except Exception:
        raise GrvtTestnetOcoError("Observaciones OCO no verificables") from None


def observe_oco_orders(vault: RealVault, client: GrvtTestnetOrderClient, group_id: str, *,
                        clock_ns: Callable[[],int] = time.time_ns) -> dict:
    """GET lógico por ambos IDs; ninguna creación/cancelación ni reenvío."""
    with vault.session() as db:
        group = db.get(GrvtTestnetOcoGroup,group_id)
        if group is None or group.status not in ("POST_CLAIMED","ACKNOWLEDGED"):
            raise GrvtTestnetOcoError("No hay concesión OCO para consultar")
        plan = _verified(db,group)
        plan_digest = group.payload_digest
        latest_observation(db,group,plan)
    try:
        start = clock_ns(); _stamp(start)
        rules = plan["instrument_terms"]
        instrument = GrvtTestnetInstrument(plan["instrument"],int(rules["asset_id"]),rules["base_decimals"],
            _number(rules["tick_size"]),_number(rules["min_size"]),_number(rules["min_notional"]))
        limits = GrvtTestnetLimits(plan["sub_account_id"],plan["instrument"],_number(plan["max_order_notional"]))
        states = []
        for leg in plan["legs"]:
            row = client._lookup_order_payload(limits.sub_account_id,leg["client_order_id"])
            candidate = {key:row[key] for key in _ORDER_FIELDS}
            candidate["metadata"] = {key:row["metadata"][key] for key in ("client_order_id","trigger")}
            validate_testnet_protection(candidate,limits,now_ns=plan["created_at_ns"])
            candidate = canonical_oco_order(candidate)
            if _encode(candidate)[1] != leg["wire_digest"]:
                raise ValueError("original signed contract changed")
            trigger = GrvtTestnetTrigger(leg["role"],Decimal(leg["trigger_price"]))
            verify_testnet_protection(candidate,limits,instrument,expected_trigger=trigger,now_ns=plan["created_at_ns"])
            at = clock_ns(); _stamp(at)
            if at < start: raise ValueError("clock regressed")
            normalised_row = {**row,**candidate,"metadata":{**row["metadata"],**candidate["metadata"]}}
            state = observe_testnet_protection(normalised_row,candidate,limits,expected_trigger=trigger,
                created_at_ns=plan["created_at_ns"],now_ns=at).order
            states.append({"role":leg["role"],"client_order_id":state.client_order_id,"order_id":state.order_id,
                "status":state.status,"traded_size":state.traded_size,"create_time_ns":state.create_time_ns,
                "update_time_ns":state.update_time_ns,"wire_digest":leg["wire_digest"]})
        at = clock_ns(); _stamp(at)
        if at < start: raise ValueError("clock regressed")
    except Exception:
        raise GrvtTestnetOcoError("No se verificaron ambas órdenes OCO; conserva los IDs y no reenvíes") from None
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            group = db.get(GrvtTestnetOcoGroup,group_id)
            p = _verified(db,group)
            if group.payload_digest != plan_digest:
                raise GrvtTestnetOcoError("Contrato OCO cambiado durante la consulta")
            previous = latest_observation(db,group,p)
            if previous is not None and at < previous["observed_at_ns"]:
                raise GrvtTestnetOcoError("Observación OCO más antigua que la guardada")
            if previous is not None and previous["observed_at_ns"] == at and previous["orders"] == states:
                db.commit(); return previous
            _check_orders(p,states,at,previous)
            claim = db.get(GrvtTestnetOcoClaim,group_id)
            if at < int(claim.claimed_at_ns):
                raise GrvtTestnetOcoError("Reloj OCO anterior al envío")
            seq = previous["sequence"]+1 if previous else 1
            payload = {"schema":"orbis.grvt.testnet.oco.observation.v1","group_id":group_id,"plan_digest":plan_digest,
                "sequence":seq,"previous_digest":_encode(previous)[1] if previous else None,
                "observed_at_ns":at,"scope":"ORDER_STATE_ONLY","orders":states,
                "can_submit":False,"protection_guaranteed":False}
            encoded,digest = _encode(payload)
            db.add(GrvtTestnetOcoObservation(id=str(uuid4()),group_id=group_id,sequence=seq,
                payload_json=encoded,payload_digest=digest))
            db.flush()
            result = latest_observation(db,group,p)
            db.commit(); return result
        except GrvtTestnetOcoError:
            db.rollback(); raise
        except Exception:
            db.rollback()
            raise GrvtTestnetOcoError("Estados OCO incompatibles con el historial guardado") from None
