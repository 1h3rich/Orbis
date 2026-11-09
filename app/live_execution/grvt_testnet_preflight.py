"""Evidencia cifrada de riesgo GRVT testnet antes del punto de POST."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
import json
import re

from sqlalchemy.orm import Session

from app.derivatives.paper_math import MarginRules
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits, _validate_order
from app.live_execution.grvt_testnet_risk import (
    GrvtTestnetRiskDecision, GrvtTestnetRiskState, evaluate_testnet_risk,
)
from app.live_execution.grvt_testnet_signer import GrvtTestnetLimitDraft
from app.live_execution.grvt_testnet_quote import (
    GrvtTestnetPriceGuard, GrvtTestnetQuote, GrvtTestnetQuoteError,
    guard_testnet_limit_price,
)
from app.live_execution.policy import _utc
from app.portfolio.models import GrvtTestnetOrderIntent, GrvtTestnetPreflightEvidence
from app.portfolio.vault import RealVault


class GrvtTestnetPreflightError(RuntimeError):
    """El estado anterior al envío no es demostrable o no coincide."""


def client_id_for_signal(session_id: str, action_request_id: str) -> str:
    """Identidad estable, dentro del espacio uint64 reservado a ORBIS."""
    if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 64
            or not isinstance(action_request_id, str) or not 1 <= len(action_request_id) <= 64):
        raise GrvtTestnetPreflightError("Identidad de señal PAPER inválida")
    material = f"orbis-grvt-testnet-signal-v1:{session_id}:{action_request_id}".encode("utf-8")
    return str(2**63 | (int.from_bytes(hashlib.sha256(material).digest()[:8], "big") & (2**63 - 1)))


def _signal_source(source: object, client_order_id: str) -> dict:
    fields = {"kind", "session_id", "action_request_id", "action", "direction",
              "version_id", "dataset_id", "dataset_hash", "client_order_id"}
    if not isinstance(source, dict) or set(source) != fields or source.get("kind") != "GRVT_AUTO_PAPER":
        raise GrvtTestnetPreflightError("Procedencia PAPER testnet inválida")
    if (source.get("action") not in ("OPEN", "CLOSE")
            or source.get("direction") not in ("LONG", "SHORT")
            or any(not isinstance(source.get(key), str) or not 1 <= len(source[key]) <= 64
                   for key in ("session_id", "action_request_id", "version_id", "dataset_id"))
            or not isinstance(source.get("dataset_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", source["dataset_hash"])
            or source.get("client_order_id") != client_order_id
            or client_id_for_signal(source["session_id"], source["action_request_id"]) != client_order_id):
        raise GrvtTestnetPreflightError("Procedencia PAPER testnet inválida")
    return source


def _order_digest(order: dict) -> str:
    data = json.dumps(order, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def _decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise GrvtTestnetPreflightError("Importe de preflight inválido")
    return format(value, "f")


_PRICE_GUARD_FIELDS = frozenset({
    "source", "instrument", "side", "limit_price", "event_time_ns",
    "observed_at_ns", "bid", "ask", "bid_size", "ask_size",
    "max_spread_bps", "max_slippage_bps",
})


def _price_guard_payload(value: GrvtTestnetPriceGuard | dict, *,
                         instrument: str, side: str, limit_price: Decimal,
                         now_ns: int) -> dict:
    """Normaliza y revalida la evidencia ligada al instrumento y orden exactos."""
    try:
        if isinstance(value, GrvtTestnetPriceGuard):
            quote = value.quote
            spread = value.max_spread_bps
            slippage = value.max_slippage_bps
        elif isinstance(value, dict) and set(value) == _PRICE_GUARD_FIELDS:
            if (value["source"] != "GRVT_TESTNET_PUBLIC_TICKER"
                    or value["instrument"] != instrument
                    or value["side"] != side
                    or value["limit_price"] != _decimal(limit_price)
                    or any(type(value[key]) is not int for key in
                           ("event_time_ns", "observed_at_ns", "max_spread_bps", "max_slippage_bps"))
                    or any(not isinstance(value[key], str)
                           or not re.fullmatch(r"[0-9]{1,30}(?:\.[0-9]{1,18})?", value[key])
                           for key in ("bid", "ask", "bid_size", "ask_size"))):
                raise ValueError("invalid price guard")
            quote = GrvtTestnetQuote(
                instrument, value["event_time_ns"], value["observed_at_ns"],
                Decimal(value["bid"]), Decimal(value["ask"]),
                Decimal(value["bid_size"]), Decimal(value["ask_size"]),
            )
            spread = value["max_spread_bps"]
            slippage = value["max_slippage_bps"]
        else:
            raise ValueError("invalid price guard")
        if quote.instrument != instrument:
            raise ValueError("instrument mismatch")
        guard_testnet_limit_price(
            side, limit_price, quote, max_spread_bps=spread,
            max_slippage_bps=slippage, now_ns=now_ns,
        )
        return {"source": "GRVT_TESTNET_PUBLIC_TICKER",
                "instrument": instrument, "side": side,
                "limit_price": _decimal(limit_price),
                "event_time_ns": quote.event_time_ns,
                "observed_at_ns": quote.observed_at_ns,
                "bid": _decimal(quote.bid), "ask": _decimal(quote.ask),
                "bid_size": _decimal(quote.bid_size),
                "ask_size": _decimal(quote.ask_size),
                "max_spread_bps": spread, "max_slippage_bps": slippage}
    except (TypeError, ValueError, KeyError, GrvtTestnetQuoteError,
            GrvtTestnetPreflightError):
        raise GrvtTestnetPreflightError("Precio testnet de preflight no verificable") from None


def _encode(payload: dict) -> tuple[str, str]:
    data = json.dumps(payload, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False)
    return data, hashlib.sha256(data.encode("ascii")).hexdigest()


def _verified(row: GrvtTestnetPreflightEvidence) -> dict:
    try:
        digest = hashlib.sha256(row.payload_json.encode("ascii")).hexdigest()
        payload = json.loads(row.payload_json)
    except (AttributeError, UnicodeEncodeError, ValueError, TypeError):
        raise GrvtTestnetPreflightError("Preflight testnet alterado") from None
    if (digest != row.payload_digest or not isinstance(payload, dict)
            or payload.get("schema") not in ("orbis.grvt.testnet.preflight.v1", "orbis.grvt.testnet.preflight.v2", "orbis.grvt.testnet.preflight.v3")
            or payload.get("schema") in ("orbis.grvt.testnet.preflight.v2", "orbis.grvt.testnet.preflight.v3") and "history_continuity" not in payload
            or payload.get("schema") == "orbis.grvt.testnet.preflight.v3" and "emergency_scope" not in payload
            or payload.get("client_order_id") != row.client_order_id
            or payload.get("sub_account_id") != row.sub_account_id
            or payload.get("allowed") is not True):
        raise GrvtTestnetPreflightError("Preflight testnet alterado")
    if "signal_source" in payload:
        _signal_source(payload["signal_source"], row.client_order_id)
    if "price_guard" in payload:
        price = payload["price_guard"]
        try:
            if not isinstance(price, dict) or not isinstance(price.get("limit_price"), str):
                raise ValueError("invalid price evidence")
            _price_guard_payload(price, instrument=price.get("instrument"),
                                 side=price.get("side"),
                                 limit_price=Decimal(price["limit_price"]),
                                 now_ns=payload.get("observed_at_ns"))
        except (ValueError, TypeError, GrvtTestnetPreflightError):
            raise GrvtTestnetPreflightError("Preflight testnet alterado") from None
    return payload


def verified_preflight_for_claim(db: Session, intent: GrvtTestnetOrderIntent,
                                 *, now_ns: int) -> dict:
    """Comprobación dentro de la misma transacción que reclama el POST."""
    row = db.get(GrvtTestnetPreflightEvidence, intent.client_order_id)
    if row is None or row.sub_account_id != intent.sub_account_id:
        raise GrvtTestnetPreflightError("Preflight testnet no archivado")
    payload = _verified(row)
    observed = payload.get("observed_at_ns")
    event = payload.get("account_event_time_ns")
    if (payload.get("request_digest") != intent.request_digest
            or not isinstance(observed, int) or type(observed) is not int
            or not isinstance(event, int) or type(event) is not int
            or observed > now_ns + 2_000_000_000
            or event > now_ns + 2_000_000_000
            or now_ns - observed > 5_000_000_000
            or now_ns - event > 5_000_000_000):
        raise GrvtTestnetPreflightError("Preflight testnet obsoleto o ajeno")
    if "price_guard" in payload:
        _price_guard_payload(payload["price_guard"], instrument=intent.instrument,
                             side=intent.side, limit_price=intent.limit_price,
                             now_ns=now_ns)
    _require_continuity(db, intent.sub_account_id, payload["state"], now_ns=now_ns)
    _require_history(db, intent.sub_account_id, intent.client_order_id, payload, now_ns=now_ns)
    _verify_emergency_reference(db,row,payload)
    return payload


def record_testnet_preflight(vault: RealVault, order: dict,
                             limits: GrvtTestnetLimits,
                             state: GrvtTestnetRiskState,
                             rules: MarginRules,
                             decision: GrvtTestnetRiskDecision, *,
                             now: datetime, now_ns: int,
                             signal_source: dict | None = None,
                             price_guard: GrvtTestnetPriceGuard | None = None,
                             history_continuity: dict | None = None) -> str:
    """Archiva sólo una decisión positiva para un intento aún PREPARED."""
    now = _utc(now, "now")
    client_id = _validate_order(order, limits, now_ns)
    if (not isinstance(state, GrvtTestnetRiskState)
            or not isinstance(rules, MarginRules)
            or not isinstance(decision, GrvtTestnetRiskDecision)):
        raise GrvtTestnetPreflightError("Preflight testnet inválido")
    leg = order["legs"][0]
    draft = GrvtTestnetLimitDraft(
        order["sub_account_id"], leg["instrument"],
        "BUY" if leg["is_buying_asset"] else "SELL",
        Decimal(leg["size"]), Decimal(leg["limit_price"]),
        order["reduce_only"], order["builder"], client_id,
        order["signature"]["nonce"], int(order["signature"]["expiration"]),
    )
    checked = evaluate_testnet_risk(draft, limits, state, rules, now_ns=now_ns)
    if not checked.allowed or checked != decision:
        raise GrvtTestnetPreflightError("Riesgo testnet no aprobado")
    payload = {
        "schema": "orbis.grvt.testnet.preflight.v2",
        "history_continuity": history_continuity,
        "client_order_id": client_id, "sub_account_id": limits.sub_account_id,
        "request_digest": _order_digest(order), "observed_at_ns": now_ns,
        "account_event_time_ns": state.event_time_ns,
        "allowed": True, "reason": checked.reason, "mode": checked.mode,
        "notional": _decimal(checked.notional),
        "state": {"settle_currency": state.settle_currency,
                  "margin_type": state.margin_type,
                  "sub_account_mode": state.sub_account_mode,
                  "settle_balance": _decimal(state.settle_balance),
                  "total_equity": _decimal(state.total_equity),
                  "initial_margin": _decimal(state.initial_margin),
                  "maintenance_margin": _decimal(state.maintenance_margin),
                  "unrealized_pnl": _decimal(state.unrealized_pnl),
                  "positions": [[name, _decimal(size)] for name, size in state.positions],
                  "open_order_count": state.open_order_count},
        "rules": {"instrument": rules.instrument,
                  "max_size": _decimal(rules.max_size),
                  "fetched_at_ns": rules.fetched_at_ns,
                  "tiers": [{"floor": _decimal(tier.floor),
                             "cap": _decimal(tier.cap) if tier.cap is not None else None,
                             "initial_rate": _decimal(tier.initial_rate),
                             "maintenance_rate": _decimal(tier.maintenance_rate),
                             "max_leverage": _decimal(tier.max_leverage)}
                            for tier in rules.tiers]},
    }
    if signal_source is not None:
        payload["signal_source"] = _signal_source(signal_source, client_id)
    if price_guard is not None:
        payload["price_guard"] = _price_guard_payload(
            price_guard, instrument=draft.instrument, side=draft.side,
            limit_price=draft.limit_price, now_ns=now_ns,
        )
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            intent = db.get(GrvtTestnetOrderIntent, client_id)
            _require_continuity(db, limits.sub_account_id, payload["state"], now_ns=now_ns)
            _require_history(db, limits.sub_account_id, client_id, payload, now_ns=now_ns)
            if (intent is None or intent.status != "PREPARED"
                    or intent.sub_account_id != limits.sub_account_id
                    or intent.request_digest != payload["request_digest"]):
                raise GrvtTestnetPreflightError("Intento testnet no preparado")
            from app.live_execution.grvt_testnet_bracket_emergency import has_emergency_table
            from app.portfolio.models import GrvtTestnetBracketEmergency
            permit=db.get(GrvtTestnetBracketEmergency,client_id) if has_emergency_table(db) else None
            if permit is not None:
                payload['schema']='orbis.grvt.testnet.preflight.v3'
                payload['emergency_scope']={'parent_client_order_id':permit.parent_client_order_id,
                    'digest':permit.payload_digest}
            data,digest=_encode(payload)
            row = db.get(GrvtTestnetPreflightEvidence, client_id)
            if row is not None:
                _verified(row)
                if row.payload_digest != digest:
                    raise GrvtTestnetPreflightError("Preflight testnet ya archivado")
                db.commit()
                return digest
            db.add(GrvtTestnetPreflightEvidence(
                client_order_id=client_id, sub_account_id=limits.sub_account_id,
                observed_at=now, payload_json=data, payload_digest=digest,
            ))
            db.commit()
            return digest
        except Exception:
            db.rollback()
            raise


def _require_continuity(db: Session, sub_account_id: str, state: dict, *, now_ns: int) -> None:
    from app.live_execution.grvt_testnet_reconciliation import (
        require_reconciled_continuity, GrvtTestnetReconciliationError)
    try:
        require_reconciled_continuity(db, sub_account_id, state, now_ns=now_ns)
    except GrvtTestnetReconciliationError:
        raise GrvtTestnetPreflightError("Saldo o posición no continúan la conciliación testnet") from None


def _require_history(db: Session, sub_account_id: str, client_id: str, payload: dict, *, now_ns: int) -> None:
    from app.live_execution.grvt_testnet_continuity import validate_testnet_history_continuity
    from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskError
    try:
        validate_testnet_history_continuity(db, sub_account_id, payload.get("history_continuity"),
            now_ns=now_ns, account_event_time_ns=payload["account_event_time_ns"], client_order_id=client_id)
    except GrvtTestnetRiskError:
        raise GrvtTestnetPreflightError("Historial entre operaciones GRVT testnet no verificable") from None


def _verify_emergency_reference(db,row,payload) -> None:
    from app.live_execution.grvt_testnet_bracket_emergency import has_emergency_table,verified_emergency_scope
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.portfolio.models import GrvtTestnetBracketEmergency
    try:
        permit=db.get(GrvtTestnetBracketEmergency,row.client_order_id) if has_emergency_table(db) else None
        if permit is None:
            if payload['schema']=='orbis.grvt.testnet.preflight.v3' or 'emergency_scope' in payload:
                raise ValueError('missing emergency authority')
        else:
            if (payload['schema']!='orbis.grvt.testnet.preflight.v3'
                    or payload.get('emergency_scope')!={'parent_client_order_id':permit.parent_client_order_id,
                        'digest':permit.payload_digest}):
                raise ValueError('changed emergency authority')
            verified_emergency_scope(db,db.get(GrvtTestnetOrderIntent,row.client_order_id),historical=True)
    except (GrvtTestnetBracketError,ValueError,TypeError,KeyError):
        raise GrvtTestnetPreflightError('Ámbito de emergencia del preflight no verificable') from None


def verified_archived_preflight(db: Session, row: GrvtTestnetPreflightEvidence) -> dict:
    """Comprueba la evidencia histórica sin renovarla ni autorizar un envío."""
    payload = _verified(row)
    _verify_emergency_reference(db,row,payload)
    if payload["schema"] in ("orbis.grvt.testnet.preflight.v2", "orbis.grvt.testnet.preflight.v3"):
        from app.live_execution.grvt_testnet_continuity import validate_testnet_history_continuity
        from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskError
        try:
            validate_testnet_history_continuity(db,row.sub_account_id,payload["history_continuity"],
                now_ns=payload["observed_at_ns"],account_event_time_ns=payload["account_event_time_ns"],
                client_order_id=row.client_order_id,historical=True)
        except (GrvtTestnetRiskError,KeyError,TypeError,ValueError):
            raise GrvtTestnetPreflightError("Historial entre operaciones GRVT testnet alterado") from None
    return payload


def read_testnet_preflight(vault: RealVault, sub_account_id: str,
                           client_order_id: str) -> dict:
    """Reabre una sola evidencia de preflight, verificando su huella."""
    with vault.session() as db:
        row = db.get(GrvtTestnetPreflightEvidence, client_order_id)
        if row is None or row.sub_account_id != sub_account_id:
            raise GrvtTestnetPreflightError("Preflight testnet no archivado")
        return verified_archived_preflight(db,row)


def optional_testnet_preflight(vault: RealVault, sub_account_id: str,
                                client_order_id: str) -> dict | None:
    """Legados v11 pueden no tener preflight; corrupción existente sí falla."""
    with vault.session() as db:
        row = db.get(GrvtTestnetPreflightEvidence, client_order_id)
        if row is None:
            return None
        if row.sub_account_id != sub_account_id:
            raise GrvtTestnetPreflightError("Preflight testnet ajeno")
        return verified_archived_preflight(db,row)
