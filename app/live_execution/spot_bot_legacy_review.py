"""Verify legacy decisions without creating a reservation or giving send authority."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError
from app.live_execution.mexc_order_observer import (
    MexcOrderObservationError, MexcSpotOrderKey, ObservedMexcOrder, observe_spot_intent,
)
from app.live_execution.mexc_uid_guard import MexcUidGuardError, assert_known_mexc_uid
from app.live_execution.policy import _money, _utc
from app.live_execution.spot_bot_store import (
    LEGACY_CANCEL_REVIEWS, LEGACY_CANCEL_STATE, SpotBotStoreError, bot_authorizations,
    bot_decision_payload, bot_record, decoded, encoded,
)
from app.portfolio.models import (
    MexcObservedFill, RealLiveOrderObservation, RealLiveSpotIntent, RealMexcAccountLink,
    RealSpotBot, RealSpotBotDecision,
)
from app.portfolio.vault import RealVault


_LOOKUP_WINDOW = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class LegacySpotDecision:
    account_ref: str
    digest: str
    created_at: datetime
    order: MexcSpotOrderKey
    bot_status: str
    bot_revision: str
    archived_digest: str | None


def read_legacy_spot_decision(db: Session, account_id: str, bot_id: str,
                             decision_id: str) -> LegacySpotDecision:
    row, config, _ = bot_record(db, bot_id)
    return _legacy_spot_decision(db, row, config, account_id, decision_id)


def _legacy_spot_decision(db: Session, row: RealSpotBot, config: dict, account_id: str,
                          decision_id: str) -> LegacySpotDecision:
    decision = db.get(RealSpotBotDecision, decision_id)
    link = db.get(RealMexcAccountLink, row.account_ref)
    if (row.account_id != account_id or link is None or link.account_id != account_id
            or decision is None or decision.bot_id != row.id or decision.intent_id != decision_id
            or db.get(RealLiveSpotIntent, decision_id) is not None):
        raise SpotBotStoreError("La decisión sin reserva no pertenece a este bot y cuenta")
    payload = bot_decision_payload(db, decision)
    if "preparation_protocol" in payload:
        raise SpotBotStoreError("La decisión no corresponde a una preparación antigua")
    try:
        source = payload["source"]
        base, quote = source["symbol"].split("/")
        side = payload["side"]
        if (not any(item["source"] == source for item in bot_authorizations(config))
                or any(not re.fullmatch(r"[A-Z0-9]{2,16}", asset) for asset in (base, quote))
                or len(base + quote) > 30 or side not in ("BUY", "SELL")
                or not re.fullmatch(r"[0-9a-f]{64}", payload["request_digest"])
                or not all(isinstance(payload[key], str) for key in ("quantity_base", "limit_price_quote"))):
            raise ValueError
        quantity = _money(Decimal(payload["quantity_base"]), "quantity_base", positive=True)
        price = _money(Decimal(payload["limit_price_quote"]), "limit_price_quote", positive=True)
        created = _utc(decision.created_at, "created_at")
    except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation):
        raise SpotBotStoreError("Los términos archivados de la decisión no son verificables") from None
    revision = encoded({"config_digest": row.config_digest, "state_digest": row.state_digest,
                        "status": row.status, "cycle_id": row.cycle_id, "updated_at": row.updated_at.isoformat()})[1]
    archived = config.get(LEGACY_CANCEL_REVIEWS, {}).get(decision_id)
    return LegacySpotDecision(row.account_ref, decision.payload_digest, created,
                              MexcSpotOrderKey(decision_id, base + quote, side, quantity, price), row.status, revision,
                              archived.get("review_digest") if isinstance(archived, dict) else None)


def legacy_spot_order_review(vault: RealVault, decision: LegacySpotDecision,
                             view: MexcReadOnlyClient | None, *, now: datetime) -> dict:
    return observe_legacy_spot_order(vault, decision, view, now=now)[0]


def observe_legacy_spot_order(vault: RealVault, decision: LegacySpotDecision,
                              view: MexcReadOnlyClient | None, *, now: datetime
                              ) -> tuple[dict, ObservedMexcOrder | None]:
    at = _utc(now, "now")
    if decision.created_at > at:
        raise SpotBotStoreError("La fecha de la decisión está en el futuro")
    order = decision.order
    scope = {"client_order_id": UUID(order.intent_id).hex, "symbol": order.symbol,
             "decision_at": decision.created_at.isoformat(), "checked_at": at.isoformat()}
    if at - decision.created_at >= _LOOKUP_WINDOW:
        return {**scope, "state": "OUTSIDE_LOOKUP_WINDOW"}, None
    if view is None:
        raise SpotBotStoreError("Conecta MEXC View para consultar la decisión")
    try:
        if view.account_reference() != decision.account_ref:
            raise MexcUidGuardError("View fuera del ámbito de la decisión")
        uid = view.account_uid()
        if not assert_known_mexc_uid(vault, decision.account_ref, uid):
            raise MexcUidGuardError("Sin UID archivado")
    except (MexcReadOnlyError, MexcUidGuardError):
        raise SpotBotStoreError("MEXC View no acredita la cuenta original de esta decisión") from None

    try:
        observed = observe_spot_intent(view, order)
    except MexcOrderObservationError:
        observed = None
    try:
        if (view.account_reference() != decision.account_ref or view.account_uid() != uid
                or not assert_known_mexc_uid(vault, decision.account_ref, uid)):
            raise MexcUidGuardError("View cambió durante la consulta")
    except (MexcReadOnlyError, MexcUidGuardError):
        raise SpotBotStoreError("MEXC View cambió durante la consulta") from None
    if (observed is None or observed.status not in (
            "NEW", "FILLED", "PARTIALLY_FILLED", "CANCELED", "PARTIALLY_CANCELED")
            or observed.executed_base > order.quantity_base
            or (observed.executed_base == 0) != (observed.cumulative_quote == 0)
            or observed.status == "NEW" and observed.executed_base != 0
            or observed.status == "FILLED" and observed.executed_base != order.quantity_base
            or observed.status in ("PARTIALLY_FILLED", "PARTIALLY_CANCELED")
                and not 0 < observed.executed_base < order.quantity_base):
        return {**scope, "state": "UNVERIFIED"}, None
    result = {**scope, "state": "MATCHED", "external_order_id": observed.external_order_id,
              "external_status": observed.status, "executed_base": format(observed.executed_base, "f"),
              "cumulative_quote": format(observed.cumulative_quote, "f"), "fill_count": len(observed.fills)}
    if (decision.bot_status == "PAUSED" and observed.status == "CANCELED" and not observed.fills
            and observed.executed_base == observed.cumulative_quote == 0):
        result["resolution_digest"] = _cancel_evidence(decision, observed.external_order_id, decision.bot_revision)["review_digest"]
    return result, observed


def _cancel_evidence(decision: LegacySpotDecision, external_id: str, revision: str) -> dict:
    order = decision.order
    evidence = {"protocol": "MEXC_VIEW_CANCEL_NO_FILLS_V1", "decision_id": order.intent_id,
                "decision_digest": decision.digest, "account_ref": decision.account_ref,
                "pause_revision": revision, "client_order_id": UUID(order.intent_id).hex,
                "external_order_id": external_id, "symbol": order.symbol, "side": order.side,
                "quantity_base": format(order.quantity_base, "f"),
                "limit_price_quote": format(order.limit_price_quote, "f"),
                "external_status": "CANCELED", "executed_base": "0", "cumulative_quote": "0", "fill_count": 0}
    return {**evidence, "review_digest": encoded(evidence)[1]}


def _check_cancel_evidence(db: Session, decision: LegacySpotDecision, record: dict, latest_at: datetime) -> None:
    try:
        external_id, revision = record["external_order_id"], record["pause_revision"]
        checked_at = _utc(datetime.fromisoformat(record["checked_at"]), "checked_at")
        expected = {**_cancel_evidence(decision, external_id, revision), "checked_at": checked_at.isoformat()}
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", external_id)
                or not re.fullmatch(r"[0-9a-f]{64}", revision)
                or not timedelta(0) <= checked_at - decision.created_at < _LOOKUP_WINDOW or checked_at > latest_at
                or encoded(record) != encoded(expected)):
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError):
        raise SpotBotStoreError("La evidencia de cancelación antigua cambió") from None
    if (db.scalar(select(MexcObservedFill.id).where(MexcObservedFill.account_ref == decision.account_ref,
            MexcObservedFill.symbol == decision.order.symbol, MexcObservedFill.order_id == external_id).limit(1))
            or db.scalar(select(RealLiveOrderObservation.id).where(
                RealLiveOrderObservation.account_ref == decision.account_ref,
                RealLiveOrderObservation.symbol == decision.order.symbol,
                RealLiveOrderObservation.external_order_id == external_id).limit(1))):
        raise SpotBotStoreError("La orden antigua tiene evidencia local que requiere revisión")


def validate_legacy_cancel_reviews(db: Session, row: RealSpotBot, config: dict) -> None:
    reviews = config[LEGACY_CANCEL_REVIEWS]
    if not isinstance(reviews, dict) or not reviews:
        raise SpotBotStoreError("El archivo de cancelaciones antiguas no es verificable")
    external_ids = set()
    for identifier, record in reviews.items():
        decision = _legacy_spot_decision(db, row, config, row.account_id, identifier)
        _check_cancel_evidence(db, decision, record, row.updated_at)
        key = (record["symbol"], record["external_order_id"])
        if key in external_ids:
            raise SpotBotStoreError("La orden antigua ya se asignó a otra decisión")
        external_ids.add(key)


def legacy_cancel_replay(decision: LegacySpotDecision, digest: str | None = None) -> dict | None:
    if decision.archived_digest is None:
        return None
    if digest is not None and decision.archived_digest != digest:
        raise SpotBotStoreError("La decisión ya tiene otra revisión de cancelación")
    return {"state": LEGACY_CANCEL_STATE, "decision_id": decision.order.intent_id, "replayed": True}


def archive_legacy_cancel(db: Session, account_id: str, bot_id: str, original: LegacySpotDecision,
                          review: dict, digest: str) -> dict:
    """Write under the caller's immediate transaction and current REAL authorization."""
    row, config, _ = bot_record(db, bot_id)
    identifier = original.order.intent_id
    decision = _legacy_spot_decision(db, row, config, account_id, identifier)
    existing = legacy_cancel_replay(decision, digest)
    if existing is not None:
        return existing
    if (decision != original or row.status != "PAUSED" or review.get("state") != "MATCHED"
            or review.get("resolution_digest") != digest):
        raise SpotBotStoreError("Repite la consulta de cancelación con el bot pausado")
    evidence = _cancel_evidence(decision, review["external_order_id"], decision.bot_revision)
    if (evidence["review_digest"] != digest or review.get("external_status") != "CANCELED"
            or review.get("fill_count") != 0 or Decimal(review["executed_base"]) != 0
            or Decimal(review["cumulative_quote"]) != 0):
        raise SpotBotStoreError("MEXC no confirma una cancelación sin ejecuciones")
    evidence["checked_at"] = review["checked_at"]
    neighbors = db.execute(select(RealSpotBot.config_json, RealSpotBot.config_digest).where(
        RealSpotBot.account_ref == row.account_ref, RealSpotBot.id != row.id)).all()
    for raw, fingerprint in neighbors:
        reviews = decoded(raw, fingerprint).get(LEGACY_CANCEL_REVIEWS, {})
        if not isinstance(reviews, dict) or any(not isinstance(other, dict) for other in reviews.values()):
            raise SpotBotStoreError("El archivo de cancelaciones de la cuenta no es verificable")
        if any(other.get("symbol") == evidence["symbol"] and other.get("external_order_id") == evidence["external_order_id"]
               for other in reviews.values()):
            raise SpotBotStoreError("La orden antigua ya se asignó a otro bot")
    config.setdefault(LEGACY_CANCEL_REVIEWS, {})[identifier] = evidence
    row.config_json, row.config_digest = encoded(config)
    row.updated_at = max(row.updated_at, datetime.fromisoformat(evidence["checked_at"]))
    validate_legacy_cancel_reviews(db, row, config)
    return {"state": LEGACY_CANCEL_STATE, "decision_id": identifier, "replayed": False}
