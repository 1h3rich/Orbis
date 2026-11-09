"""Diario nativo OCO TESTNET. Preparar no habilita ni envía una orden.

La identidad de cada salida se reserva para siempre; una marca de POST no
puede volver a preparación. Términos económicos y huellas sí se guardan,
pero no claves, firmas, cookies ni direcciones de firmante/builder.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.grvt_testnet_order import GrvtTestnetLimits, GrvtTestnetOrderAck, _ORDER_FIELDS
from app.live_execution.grvt_testnet_protection import (
    PROTECTION_LIFETIME_NS, GrvtTestnetTrigger, validate_testnet_protection, verify_testnet_protection,
)
from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument, _exact_uint, _UINT64_MAX
from app.live_execution.grvt_testnet_reconciliation import verified_reconciliation
from app.live_execution.grvt_testnet_preflight import verified_archived_preflight
from app.portfolio.models import (GrvtTestnetOrderIntent, GrvtTestnetPreflightEvidence,
    GrvtTestnetReconciliation, GrvtTestnetOcoGroup, GrvtTestnetOcoLeg, GrvtTestnetOcoClaim,
    GrvtTestnetStop)
from app.portfolio.vault import RealVault


class GrvtTestnetOcoError(RuntimeError):
    """Contrato o reserva OCO incompatibles; sin secretos ni respuesta remota."""


def _uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or len(value) != 36 or str(UUID(value)) != value:
            raise ValueError("invalid group")
        return value
    except (ValueError,TypeError,AttributeError):
        raise GrvtTestnetOcoError("Identidad OCO inválida") from None


def oco_client_ids(group_id: str) -> tuple[str, str]:
    _uuid(group_id)
    def identifier(role: str) -> str:
        digest = hashlib.sha256(f"orbis-grvt-testnet-oco-v1:{group_id}:{role}".encode()).digest()
        return str(2**63 | (int.from_bytes(digest[:8], "big") & (2**63-1)))
    ids = (identifier("STOP_LOSS"), identifier("TAKE_PROFIT"))
    if ids[0] == ids[1]:
        raise GrvtTestnetOcoError("Colisión de identidades OCO")
    return ids


def _encode(value: object) -> tuple[str, str]:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return text, hashlib.sha256(text.encode("ascii")).hexdigest()


def _stamp(now_ns: int) -> datetime:
    if type(now_ns) is not int or not 0 < now_ns < 2**63:
        raise GrvtTestnetOcoError("Reloj OCO inválido")
    seconds, nanos = divmod(now_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=nanos//1000)


def _parent(db: Session, client_id: str, *, latest: bool = False) -> tuple[dict, str]:
    row = db.get(GrvtTestnetOrderIntent, client_id)
    receipt = db.get(GrvtTestnetReconciliation, client_id)
    pre = db.get(GrvtTestnetPreflightEvidence, client_id)
    if row is None or receipt is None or pre is None:
        raise GrvtTestnetOcoError("Entrada OCO no conciliada")
    payload = verified_reconciliation(db, row)
    before = verified_archived_preflight(db, pre)
    if (before["mode"] != "OPEN" or not payload["result"]["positions"]
            or payload.get("signal_source") is not None and payload["signal_source"]["action"] != "OPEN"):
        raise GrvtTestnetOcoError("Origen OCO no es una entrada")
    if latest:
        newer = db.scalar(select(GrvtTestnetOrderIntent.client_order_id).where(
            GrvtTestnetOrderIntent.sub_account_id == row.sub_account_id,
            GrvtTestnetOrderIntent.status == "RECONCILED").order_by(
                GrvtTestnetOrderIntent.updated_at.desc(), GrvtTestnetOrderIntent.client_order_id.desc()).limit(1))
        if newer != client_id:
            raise GrvtTestnetOcoError("Hay una conciliación posterior al origen OCO")
    return payload, receipt.payload_digest


def validate_oco_orders(orders: object, limits: GrvtTestnetLimits, *, now_ns: int) -> tuple[str, str]:
    """Dos salidas del mismo tamaño/lado: SL primero y TP después."""
    try:
        if not isinstance(orders, (list, tuple)) or len(orders) != 2:
            raise ValueError("pair required")
        ids = tuple(validate_testnet_protection(order, limits, now_ns=now_ns) for order in orders)
        a, b = orders
        if (ids[0] == ids[1] or a["metadata"]["trigger"]["trigger_type"] != "STOP_LOSS"
                or b["metadata"]["trigger"]["trigger_type"] != "TAKE_PROFIT"
                or any(a[key] != b[key] for key in ("sub_account_id", "builder_fee"))
                or a["builder"].lower() != b["builder"].lower()
                or any(a["legs"][0][key] != b["legs"][0][key] for key in ("instrument", "is_buying_asset"))
                or Decimal(a["legs"][0]["size"]) != Decimal(b["legs"][0]["size"])
                or a["signature"]["signer"].lower() != b["signature"]["signer"].lower()
                or int(a["signature"]["expiration"]) != int(b["signature"]["expiration"])
                or a["signature"]["nonce"] == b["signature"]["nonce"]):
            raise ValueError("different OCO legs")
        stop = Decimal(a["metadata"]["trigger"]["tpsl"]["trigger_price"])
        target = Decimal(b["metadata"]["trigger"]["tpsl"]["trigger_price"])
        if (stop <= target if a["legs"][0]["is_buying_asset"] else stop >= target):
            raise ValueError("inverted OCO triggers")
        return ids
    except Exception:
        raise GrvtTestnetOcoError("Contrato de órdenes OCO no verificable") from None


def _number(value: object) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,30}(?:\.[0-9]{1,18})?", value):
        raise ValueError("invalid amount")
    amount = Decimal(value)
    if amount <= 0:
        raise ValueError("invalid amount")
    return amount


def canonical_amount(value: str) -> str:
    """Representación exacta sin usar normalize(), que depende del contexto."""
    text = format(_number(value),"f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def canonical_oco_order(order: dict) -> dict:
    """Sólo normaliza representación decimal/hex; nunca bool, lado o reglas.

    Requiere validación del contrato antes de usarse como evidencia. No firma
    ni autoriza; evita que un View con '0.010000000' pierda el vínculo con '0.01'.
    """
    body = json.loads(_encode(order)[0])
    for leg in body["legs"]:
        for key in ("size","limit_price"): leg[key] = canonical_amount(leg[key])
    tpsl = body["metadata"]["trigger"]["tpsl"]
    tpsl["trigger_price"] = canonical_amount(tpsl["trigger_price"])
    body["builder"] = body["builder"].lower()
    for key in ("signer","r","s"): body["signature"][key] = body["signature"][key].lower()
    body["signature"]["expiration"] = str(int(body["signature"]["expiration"]))
    return body


def oco_ack_from_result(result: object, order: dict, limits: GrvtTestnetLimits, *, now_ns: int) -> GrvtTestnetOrderAck:
    if not isinstance(result,dict): raise GrvtTestnetOcoError("Acuse OCO ausente")
    candidate = {key:result[key] for key in _ORDER_FIELDS}
    candidate["metadata"] = {key:result["metadata"][key] for key in ("client_order_id","trigger")}
    validate_testnet_protection(candidate,limits,now_ns=now_ns)
    if _encode(canonical_oco_order(candidate))[1] != _encode(canonical_oco_order(order))[1]:
        raise GrvtTestnetOcoError("Acuse de otro contrato OCO")
    order_id = result.get("order_id")
    if not isinstance(order_id,str) or not re.fullmatch(r"[A-Za-z0-9x]{1,80}",order_id):
        raise GrvtTestnetOcoError("ID de acuse OCO inválido")
    return GrvtTestnetOrderAck(limits.sub_account_id,order["metadata"]["client_order_id"],order_id,True)


def instrument_terms(instrument: GrvtTestnetInstrument) -> dict:
    return {"asset_id":str(instrument.asset_id),"base_decimals":instrument.base_decimals,
        "tick_size":canonical_amount(format(instrument.tick_size,"f")),
        "min_size":canonical_amount(format(instrument.min_size,"f")),
        "min_notional":canonical_amount(format(instrument.min_notional,"f"))}


def _leg_terms(orders: list[dict]) -> list[dict]:
    return [{"role": order["metadata"]["trigger"]["trigger_type"],
        "client_order_id": order["metadata"]["client_order_id"],
        "trigger_price": canonical_amount(order["metadata"]["trigger"]["tpsl"]["trigger_price"]),
        "limit_price": canonical_amount(order["legs"][0]["limit_price"]),
        "expiration_ns": int(order["signature"]["expiration"]), "nonce": order["signature"]["nonce"],
        "wire_digest": _encode(canonical_oco_order(order))[1]} for order in orders]


def _verified(db: Session, row: GrvtTestnetOcoGroup) -> dict:
    try:
        p = json.loads(row.payload_json)
        if (_encode(p) != (row.payload_json, row.payload_digest)
                or set(p) != {"schema", "group_id", "parent_client_order_id", "parent_receipt_digest",
                    "sub_account_id", "instrument", "size", "side", "max_order_notional", "created_at_ns",
                    "instrument_terms", "legs"}
                or p["schema"] != "orbis.grvt.testnet.oco.v1" or _uuid(p["group_id"]) != row.id
                or p["parent_client_order_id"] != row.parent_client_order_id
                or p["sub_account_id"] != row.sub_account_id
                or row.status not in ("PREPARED", "POST_CLAIMED", "ACKNOWLEDGED", "ABANDONED")
                or _stamp(p["created_at_ns"]) != row.created_at or row.updated_at < row.created_at):
            raise ValueError("invalid group")
        source, digest = _parent(db, row.parent_client_order_id)
        position = source["result"]["positions"]
        size, cap = _number(p["size"]), _number(p["max_order_notional"])
        if (digest != p["parent_receipt_digest"] or source["sub_account_id"] != row.sub_account_id
                or row.created_at < db.get(GrvtTestnetReconciliation, row.parent_client_order_id).reconciled_at
                or len(position) != 1 or position[0][0] != p["instrument"]
                or Decimal(position[0][1]).copy_abs() != size
                or p["side"] != ("SELL" if Decimal(position[0][1]) > 0 else "BUY")):
            raise ValueError("source position mismatch")
        terms = p["instrument_terms"]
        if set(terms) != {"asset_id", "base_decimals", "tick_size", "min_size", "min_notional"}:
            raise ValueError("instrument contract")
        instrument = GrvtTestnetInstrument(p["instrument"], int(terms["asset_id"]), terms["base_decimals"],
            _number(terms["tick_size"]), _number(terms["min_size"]), _number(terms["min_notional"]))
        ids = oco_client_ids(row.id)
        if not isinstance(p["legs"], list) or len(p["legs"]) != 2:
            raise ValueError("leg count")
        with localcontext() as ctx:
            ctx.prec = 100
            _exact_uint(size, instrument.base_decimals, _UINT64_MAX)
            for i, leg in enumerate(p["legs"]):
                if (set(leg) != {"role", "client_order_id", "trigger_price", "limit_price", "expiration_ns", "nonce", "wire_digest"}
                        or leg["role"] != ("STOP_LOSS", "TAKE_PROFIT")[i]
                        or leg["client_order_id"] != ids[i]
                        or type(leg["nonce"]) is not int or not 0 <= leg["nonce"] < 2**32
                        or type(leg["expiration_ns"]) is not int
                        or not p["created_at_ns"] < leg["expiration_ns"] <= p["created_at_ns"]+PROTECTION_LIFETIME_NS
                        or not isinstance(leg["wire_digest"], str) or not re.fullmatch("[a-f0-9]{64}", leg["wire_digest"])):
                    raise ValueError("leg identity")
                trigger, price = _number(leg["trigger_price"]), _number(leg["limit_price"])
                GrvtTestnetTrigger(leg["role"], trigger)
                _exact_uint(price, 9, _UINT64_MAX)
                deviation = (price-trigger)*10_000/trigger
                if (trigger % instrument.tick_size or price % instrument.tick_size or size < instrument.min_size
                        or size*price < instrument.min_notional or size*max(price,trigger) > cap
                        or not (0 <= deviation <= 100 if p["side"] == "BUY" else -100 <= deviation <= 0)):
                    raise ValueError("leg amount")
        a,b = p["legs"]
        if (a["expiration_ns"] != b["expiration_ns"] or a["nonce"] == b["nonce"]
                or (Decimal(a["trigger_price"]) <= Decimal(b["trigger_price"]) if p["side"] == "BUY"
                    else Decimal(a["trigger_price"]) >= Decimal(b["trigger_price"]))):
            raise ValueError("pair shape")
        stored = list(db.scalars(select(GrvtTestnetOcoLeg).where(GrvtTestnetOcoLeg.group_id == row.id)))
        if sorted((leg.client_order_id,leg.role) for leg in stored) != sorted((leg["client_order_id"],leg["role"]) for leg in p["legs"]):
            raise ValueError("reserved ids mismatch")
        claim = db.get(GrvtTestnetOcoClaim, row.id)
        if (row.status in ("PREPARED", "ABANDONED")) != (claim is None):
            raise ValueError("claim/status mismatch")
        if claim is not None and (claim.payload_digest != row.payload_digest or not claim.claimed_at_ns.isdecimal()
                or not p["created_at_ns"] <= int(claim.claimed_at_ns) < min(leg["expiration_ns"] for leg in p["legs"])):
            raise ValueError("invalid claim")
        if claim is not None:
            if _stamp(int(claim.claimed_at_ns)) > row.updated_at:
                raise ValueError("claim timestamp")
            if row.status == "ACKNOWLEDGED":
                _verified_ack(row, claim, p)
            elif claim.ack_json is not None or claim.ack_digest is not None:
                raise ValueError("unexpected ack")
            if claim.preflight_json is not None or claim.preflight_digest is not None:
                from app.live_execution.grvt_testnet_oco_preflight import verify_oco_preflight
                proof = json.loads(claim.preflight_json)
                if _encode(proof) != (claim.preflight_json,claim.preflight_digest):
                    raise ValueError("preflight digest")
                verify_oco_preflight(db,p,proof,now_ns=int(claim.claimed_at_ns),historical=True)
        return p
    except Exception:
        raise GrvtTestnetOcoError("Diario OCO no verificable") from None


def _public(db: Session, row: GrvtTestnetOcoGroup, *, include_cancels: bool = True, include_finance: bool = True) -> dict:
    from app.live_execution.grvt_testnet_oco_observation import latest_observation
    from app.live_execution.grvt_testnet_oco_cancel import verified_cancel_attempt
    p = _verified(db,row)
    claim = db.get(GrvtTestnetOcoClaim,row.id)
    acks = _verified_ack(row,claim,p)["orders"] if row.status == "ACKNOWLEDGED" else []
    reconciliation, audits = None, []
    if include_finance:
        from app.live_execution.grvt_testnet_oco_finance import verified_oco_audits, verified_oco_reconciliation
        audits = verified_oco_audits(db,row,p)
        reconciliation = verified_oco_reconciliation(db,row,p)
    return {**p, "status": row.status, "acknowledged_orders": acks,
            "last_observation": latest_observation(db,row,p),
            "cancel_attempt": verified_cancel_attempt(db,row,p) if include_cancels else None,
            "financial_audit_count":len(audits),"reconciliation":reconciliation,
            "preflight_available": claim is not None and claim.preflight_json is not None,
            "can_submit": False, "protection_guaranteed": False}


def _verified_ack(row: GrvtTestnetOcoGroup, claim: GrvtTestnetOcoClaim, p: dict) -> dict:
    try:
        ack = json.loads(claim.ack_json)
        if (_encode(ack) != (claim.ack_json,claim.ack_digest)
                or set(ack) != {"group_id","plan_digest","observed_at_ns","orders"}
                or ack["group_id"] != row.id or ack["plan_digest"] != row.payload_digest
                or type(ack["observed_at_ns"]) is not int
                or ack["observed_at_ns"] < int(claim.claimed_at_ns)
                or _stamp(ack["observed_at_ns"]) != row.updated_at
                or not isinstance(ack["orders"],list) or len(ack["orders"]) != 2):
            raise ValueError("invalid ack")
        for received, expected in zip(ack["orders"],p["legs"]):
            if (set(received) != {"client_order_id","order_id"}
                    or received["client_order_id"] != expected["client_order_id"]
                    or not isinstance(received["order_id"],str)
                    or not re.fullmatch(r"[A-Za-z0-9x]{1,80}",received["order_id"])):
                raise ValueError("wrong ack scope")
        if ack["orders"][0]["order_id"] == ack["orders"][1]["order_id"]:
            raise ValueError("duplicate ack")
        return ack
    except Exception:
        raise GrvtTestnetOcoError("Acuse OCO no verificable") from None


def require_no_pending_oco(db: Session, sub_account_id: str) -> None:
    """Guardia compartida con IOC, dentro de la transacción del llamador."""
    from app.live_execution.grvt_testnet_bracket_journal import require_no_pending_brackets
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    try:
        require_no_pending_brackets(db,sub_account_id)
    except GrvtTestnetBracketError:
        raise GrvtTestnetOcoError('Entrada OTOCO pendiente o no verificable') from None
    for row in db.scalars(select(GrvtTestnetOcoGroup).where(GrvtTestnetOcoGroup.sub_account_id == sub_account_id)):
        state = _public(db,row)
        if row.status != "ABANDONED" and state['reconciliation'] is None:
            raise GrvtTestnetOcoError("Protección OCO pendiente; no abras otra orden")


def verify_all_oco(db: Session, *, include_cancels: bool = True, include_finance: bool = True) -> None:
    """Verificación semántica de copia, incluyendo hijos/claims sin grupo."""
    for row in db.scalars(select(GrvtTestnetOcoGroup)):
        _public(db,row,include_cancels=include_cancels,include_finance=include_finance)
    from app.portfolio.models import GrvtTestnetOcoObservation, GrvtTestnetOcoCancel
    models = (GrvtTestnetOcoLeg, GrvtTestnetOcoClaim, GrvtTestnetOcoObservation)
    for model in (*models, *((GrvtTestnetOcoCancel,) if include_cancels else ())):
        for child in db.scalars(select(model)):
            if db.get(GrvtTestnetOcoGroup, child.group_id) is None:
                raise GrvtTestnetOcoError("Referencia OCO huérfana")
    if include_finance:
        from app.portfolio.models import GrvtTestnetOcoAudit, GrvtTestnetOcoReconciliation
        for model in (GrvtTestnetOcoAudit,GrvtTestnetOcoReconciliation):
            for child in db.scalars(select(model)):
                if db.get(GrvtTestnetOcoGroup,child.group_id) is None:
                    raise GrvtTestnetOcoError("Auditoría o recibo OCO huérfanos")


def read_oco(vault: RealVault, group_id: str) -> dict:
    with vault.session() as db:
        row = db.get(GrvtTestnetOcoGroup, _uuid(group_id))
        if row is None:
            raise GrvtTestnetOcoError("Grupo OCO no encontrado")
        return _public(db,row)


def prepare_oco(vault: RealVault, group_id: str, parent_client_id: str, orders: list[dict],
                 limits: GrvtTestnetLimits, instrument: GrvtTestnetInstrument, *, now_ns: int) -> dict:
    """Reserva ambas identidades y contrato antes de usar red; no concede POST."""
    stamp = _stamp(now_ns)
    if validate_oco_orders(orders, limits, now_ns=now_ns) != oco_client_ids(group_id):
        raise GrvtTestnetOcoError("Identidades de salidas ajenas al grupo")
    for order in orders:
        raw = order["metadata"]["trigger"]
        verify_testnet_protection(order, limits, instrument,
            expected_trigger=GrvtTestnetTrigger(raw["trigger_type"], Decimal(raw["tpsl"]["trigger_price"])), now_ns=now_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            source, digest = _parent(db, parent_client_id)
            legs = _leg_terms(orders)
            existing = db.get(GrvtTestnetOcoGroup, group_id)
            if existing is not None:
                old = _verified(db, existing)
                if old["parent_client_order_id"] != parent_client_id or old["legs"] != legs or _number(old["max_order_notional"]) != limits.max_order_notional:
                    raise GrvtTestnetOcoError("Grupo OCO reutilizado con otro contrato")
                result = _public(db,existing); db.commit(); return result
            _parent(db,parent_client_id,latest=True)
            stop = db.get(GrvtTestnetStop, limits.sub_account_id)
            if stop is not None and stop.stopped:
                raise GrvtTestnetOcoError("Subcuenta testnet detenida")
            require_no_pending_oco(db,limits.sub_account_id)
            if db.scalar(select(GrvtTestnetOrderIntent.client_order_id).where(
                    GrvtTestnetOrderIntent.sub_account_id == limits.sub_account_id,
                    GrvtTestnetOrderIntent.status.in_(("PREPARED", "POST_CLAIMED", "ACKNOWLEDGED", "OBSERVED"))).limit(1)) is not None:
                raise GrvtTestnetOcoError("Otra orden testnet sigue pendiente")
            if any(db.get(GrvtTestnetOrderIntent,leg["client_order_id"]) is not None
                    or db.get(GrvtTestnetOcoLeg,leg["client_order_id"]) is not None for leg in legs):
                raise GrvtTestnetOcoError("Identidad OCO ya reservada")
            from app.live_execution.grvt_testnet_bracket_journal import require_bracket_ids_unreserved
            from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
            try:
                require_bracket_ids_unreserved(db,[leg['client_order_id'] for leg in legs])
            except GrvtTestnetBracketError:
                raise GrvtTestnetOcoError('Identidad OCO ya reservada para OTOCO') from None
            payload = {"schema": "orbis.grvt.testnet.oco.v1", "group_id": group_id,
                "parent_client_order_id": parent_client_id, "parent_receipt_digest": digest,
                "sub_account_id": limits.sub_account_id, "instrument": limits.instrument,
                "size": canonical_amount(orders[0]["legs"][0]["size"]), "side": "BUY" if orders[0]["legs"][0]["is_buying_asset"] else "SELL",
                "max_order_notional": format(limits.max_order_notional,"f"), "created_at_ns": now_ns, "legs": legs,
                "instrument_terms": instrument_terms(instrument)}
            encoded, hashed = _encode(payload)
            row = GrvtTestnetOcoGroup(id=group_id,parent_client_order_id=parent_client_id,
                sub_account_id=limits.sub_account_id,status="PREPARED",payload_json=encoded,payload_digest=hashed,
                created_at=stamp,updated_at=stamp)
            db.add(row); db.flush()
            for leg in legs:
                db.add(GrvtTestnetOcoLeg(client_order_id=leg["client_order_id"],group_id=group_id,role=leg["role"]))
            db.flush()
            result = _public(db,row)
            db.commit(); return result
        except Exception:
            db.rollback(); raise


def _match_orders(p: dict, orders: list[dict], limits: GrvtTestnetLimits, *, now_ns: int) -> None:
    if (validate_oco_orders(orders, limits, now_ns=now_ns) != oco_client_ids(p["group_id"])
            or p["sub_account_id"] != limits.sub_account_id or p["instrument"] != limits.instrument
            or Decimal(p["max_order_notional"]) != limits.max_order_notional
            or _leg_terms(orders) != p["legs"]
            or Decimal(orders[0]["legs"][0]["size"]) != Decimal(p["size"])
            or ("BUY" if orders[0]["legs"][0]["is_buying_asset"] else "SELL") != p["side"]):
        raise GrvtTestnetOcoError("Órdenes ajenas al contrato OCO")


def claim_oco(vault: RealVault, group_id: str, orders: list[dict], limits: GrvtTestnetLimits, *, now_ns: int,
                preflight: dict | None = None) -> dict:
    """Reserva irreversible de un intento, no un permiso de ejecución externa."""
    stamp = _stamp(now_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetOcoGroup, _uuid(group_id))
            if row is None or row.status != "PREPARED":
                raise GrvtTestnetOcoError("Intento OCO no preparado o ya consumido")
            p = _verified(db,row)
            _match_orders(p, orders, limits, now_ns=now_ns)
            _parent(db,row.parent_client_order_id,latest=True)
            stop = db.get(GrvtTestnetStop, row.sub_account_id)
            if stop is not None and stop.stopped:
                raise GrvtTestnetOcoError("Subcuenta testnet detenida")
            if not p["created_at_ns"] <= now_ns <= p["created_at_ns"]+120_000_000_000:
                raise GrvtTestnetOcoError("Preparación OCO caducada")
            proof_json, proof_digest = None, None
            if preflight is not None:
                from app.live_execution.grvt_testnet_oco_preflight import verify_oco_preflight
                proof_json, proof_digest = _encode(preflight)
                verify_oco_preflight(db,p,json.loads(proof_json),now_ns=now_ns)
            db.add(GrvtTestnetOcoClaim(group_id=row.id,claimed_at_ns=str(now_ns),payload_digest=row.payload_digest,
                preflight_json=proof_json,preflight_digest=proof_digest))
            row.status = "POST_CLAIMED"; row.updated_at = stamp
            db.flush()
            result = _public(db,row)
            db.commit(); return result
        except Exception:
            db.rollback(); raise


def abandon_oco(vault: RealVault, group_id: str, *, now_ns: int) -> dict:
    """Sólo descarta una preparación sin concesión de POST; conserva sus IDs."""
    stamp = _stamp(now_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetOcoGroup, _uuid(group_id))
            if row is None or row.status != "PREPARED":
                raise GrvtTestnetOcoError("Sólo puede descartarse un OCO sin POST")
            _verified(db,row)
            if stamp < row.updated_at or db.get(GrvtTestnetOcoClaim,row.id) is not None:
                raise GrvtTestnetOcoError("Concesión OCO no descartable")
            row.status = "ABANDONED"; row.updated_at = stamp
            result = _public(db,row)
            db.commit(); return result
        except Exception:
            db.rollback(); raise


def record_oco_ack(vault: RealVault, group_id: str, acks: tuple[GrvtTestnetOrderAck, GrvtTestnetOrderAck],
                     *, now_ns: int) -> dict:
    """Archiva sólo los dos acuses del bulk; nunca ejecución ni liberación."""
    stamp = _stamp(now_ns)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetOcoGroup, _uuid(group_id))
            if row is None or row.status not in ("POST_CLAIMED", "ACKNOWLEDGED"):
                raise GrvtTestnetOcoError("Grupo OCO sin concesión de POST")
            p = _verified(db,row)
            if (not isinstance(acks,tuple) or len(acks) != 2
                    or any(not isinstance(ack,GrvtTestnetOrderAck)
                           or ack.sub_account_id != row.sub_account_id
                           or ack.accepted_by_testnet is not True or ack.execution_unverified is not True
                           for ack in acks)):
                raise GrvtTestnetOcoError("Acuses OCO incompletos o ajenos")
            orders = [{"client_order_id":ack.client_order_id,"order_id":ack.order_id} for ack in acks]
            claim = db.get(GrvtTestnetOcoClaim,row.id)
            if row.status == "ACKNOWLEDGED":
                if _verified_ack(row,claim,p)["orders"] != orders:
                    raise GrvtTestnetOcoError("Acuse OCO ya registrado con otros IDs")
                result = _public(db,row); db.commit(); return result
            if stamp < row.updated_at or now_ns < int(claim.claimed_at_ns):
                raise GrvtTestnetOcoError("Reloj OCO retrocedió")
            ack = {"group_id":row.id,"plan_digest":row.payload_digest,"observed_at_ns":now_ns,"orders":orders}
            claim.ack_json, claim.ack_digest = _encode(ack)
            row.status = "ACKNOWLEDGED"; row.updated_at = stamp
            result = _public(db,row)
            db.commit(); return result
        except Exception:
            db.rollback(); raise
