"""Cancelación expresa OCO TESTNET, distinta de STOP y nunca reintentable."""

from __future__ import annotations

import json
import time
from typing import Callable

from app.live_execution.grvt_testnet_oco import (
    GrvtTestnetOcoError, _encode, _stamp, _verified, read_oco,
)
from app.live_execution.grvt_testnet_oco_observation import latest_observation, observe_oco_orders
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient
from app.live_execution.grvt_testnet_reconciliation import _ns
from app.portfolio.models import (
    GrvtTestnetOcoCancel, GrvtTestnetOcoClaim, GrvtTestnetOcoGroup,
    GrvtTestnetOcoObservation, GrvtTestnetStop,
)
from app.portfolio.vault import RealVault
from sqlalchemy import select


def verified_cancel_attempt(db, group: GrvtTestnetOcoGroup, plan: dict) -> dict | None:
    """Recalcula alcance/observación/STOP; la huella sola no concede permiso."""
    row = db.get(GrvtTestnetOcoCancel,group.id)
    if row is None:
        return None
    try:
        p = json.loads(row.payload_json)
        if (_encode(p) != (row.payload_json,row.payload_digest)
                or set(p) != {"schema","group_id","plan_digest","observation_sequence","observation_digest",
                              "target_client_order_id","claimed_at_ns","stop_at_ns"}
                or p["schema"] != "orbis.grvt.testnet.oco.cancel.v1"
                or p["group_id"] != group.id or p["plan_digest"] != group.payload_digest
                or group.status not in ("POST_CLAIMED","ACKNOWLEDGED")
                or db.get(GrvtTestnetOcoClaim,group.id) is None
                or any(type(p[key]) is not int for key in ("observation_sequence","claimed_at_ns","stop_at_ns"))):
            raise ValueError("invalid cancel contract")
        _stamp(p["claimed_at_ns"]); _stamp(p["stop_at_ns"])
        latest_observation(db,group,plan)
        observation = db.scalar(select(GrvtTestnetOcoObservation).where(
            GrvtTestnetOcoObservation.group_id == group.id,
            GrvtTestnetOcoObservation.sequence == p["observation_sequence"]))
        if observation is None or observation.payload_digest != p["observation_digest"]:
            raise ValueError("missing observed pair")
        observed = json.loads(observation.payload_json)
        stop = db.get(GrvtTestnetStop,group.sub_account_id)
        targets = [leg["client_order_id"] for leg in observed["orders"] if leg["status"] in ("PENDING","OPEN")]
        if (p["target_client_order_id"] not in targets
                or not 0 <= p["claimed_at_ns"]-observed["observed_at_ns"] <= 5_000_000_000
                or stop is None or not stop.stopped
                or not p["stop_at_ns"] <= p["claimed_at_ns"]
                or _ns(stop.updated_at) < p["stop_at_ns"]):
            raise ValueError("cancel scope not verified")
        status, received_ns = "CLAIMED", None
        if row.ack_json is not None or row.ack_digest is not None:
            ack = json.loads(row.ack_json)
            if (_encode(ack) != (row.ack_json,row.ack_digest)
                    or set(ack) != {"cancel_digest","received_at_ns","acknowledgement_received"}
                    or ack["cancel_digest"] != row.payload_digest
                    or ack["acknowledgement_received"] is not True
                    or type(ack["received_at_ns"]) is not int or ack["received_at_ns"] < p["claimed_at_ns"]):
                raise ValueError("invalid cancel acknowledgement")
            _stamp(ack["received_at_ns"])
            status, received_ns = "ACKNOWLEDGED", ack["received_at_ns"]
        return {**p,"status":status,"acknowledgement_received_at_ns":received_ns,
                "financially_reconciled":False,"can_submit":False}
    except Exception:
        raise GrvtTestnetOcoError("Cancelación OCO no verificable") from None


def cancel_oco_once(vault: RealVault, client: GrvtTestnetOrderClient, group_id: str, *,
                    acknowledge_unprotected_position: bool,
                    clock_ns: Callable[[],int] = time.time_ns) -> dict:
    """Retira el grupo sólo por acción expresa con STOP. Nunca cierra posición.

    La pérdida de protección se reconoce aparte; un STOP no llama a este
    servicio. Si el bulk o la consulta posterior falla, se conserva el intento
    y se permite consultar, nunca reenviar ni liberar la subcuenta.
    """
    plan = read_oco(vault,group_id)
    if acknowledge_unprotected_position is not True:
        raise GrvtTestnetOcoError("Confirma que cancelar retira ambas protecciones y no cierra la posición")
    if plan["cancel_attempt"] is not None:
        raise GrvtTestnetOcoError("Cancelación OCO ya consumida; consulta ambos IDs")
    with vault.session() as db:
        stop = db.get(GrvtTestnetStop,plan["sub_account_id"])
        if stop is None or not stop.stopped:
            raise GrvtTestnetOcoError("Detén la subcuenta antes de cancelar el grupo OCO")
    observed = observe_oco_orders(vault,client,group_id,clock_ns=clock_ns)
    targets = [row["client_order_id"] for row in observed["orders"] if row["status"] in ("OPEN","PENDING")]
    if not targets:
        return read_oco(vault,group_id)
    claimed_ns = clock_ns(); _stamp(claimed_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            group = db.get(GrvtTestnetOcoGroup,group_id)
            p = _verified(db,group)
            stop = db.get(GrvtTestnetStop,group.sub_account_id)
            current = latest_observation(db,group,p)
            if (group.payload_digest != _encode({key:plan[key] for key in p})[1]
                    or current != observed or stop is None or not stop.stopped
                    or db.get(GrvtTestnetOcoCancel,group_id) is not None):
                raise GrvtTestnetOcoError("Estado OCO cambió; no se cancela ni reenvía")
            payload = {"schema":"orbis.grvt.testnet.oco.cancel.v1","group_id":group_id,
                "plan_digest":group.payload_digest,"observation_sequence":observed["sequence"],
                "observation_digest":_encode(observed)[1],"target_client_order_id":targets[0],
                "claimed_at_ns":claimed_ns,"stop_at_ns":_ns(stop.updated_at)}
            encoded,digest = _encode(payload)
            db.add(GrvtTestnetOcoCancel(group_id=group_id,payload_json=encoded,payload_digest=digest))
            db.flush(); verified_cancel_attempt(db,group,p)
            db.commit()
        except Exception:
            db.rollback(); raise
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            group = db.get(GrvtTestnetOcoGroup,group_id)
            p = _verified(db,group)
            attempt = verified_cancel_attempt(db,group,p)
            now = clock_ns(); _stamp(now)
            if (attempt["status"] != "CLAIMED" or not 0 <= now-claimed_ns <= 5_000_000_000
                    or _encode(latest_observation(db,group,p))[1] != attempt["observation_digest"]):
                raise GrvtTestnetOcoError("Cancelación OCO sin revalidación final; consulta ambos IDs")
            client.cancel_oco_by_client_id(group.sub_account_id,attempt["target_client_order_id"])
            # Un acuse recibido sigue siendo archivado si el reloj retrocede
            # después de la red, pero no se inventa un estado terminal.
            received_ns = clock_ns()
            if type(received_ns) is not int or received_ns < now:
                received_ns = now
            row = db.get(GrvtTestnetOcoCancel,group_id)
            row.ack_json,row.ack_digest = _encode({"cancel_digest":row.payload_digest,
                "received_at_ns":received_ns,"acknowledgement_received":True})
            db.flush(); verified_cancel_attempt(db,group,p)
            db.commit()
        except Exception:
            db.rollback(); raise
    observe_oco_orders(vault,client,group_id,clock_ns=clock_ns)
    return read_oco(vault,group_id)
