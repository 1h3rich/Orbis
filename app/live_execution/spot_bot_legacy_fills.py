"""Recover a terminal legacy order and its accounting from matching originals."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError
from app.live_execution.journal import (
    _canonical, _stored_policy, intent_request_digest, recover_spot_intent_in_session,
)
from app.live_execution.mexc_booking import book_spot_order_in_transaction
from app.live_execution.mexc_fill_event import booked_event_matches
from app.live_execution.mexc_order_observer import ObservedMexcOrder, archive_spot_observation_in_transaction
from app.live_execution.mexc_settlement import _balance_evidence
from app.live_execution.mexc_uid_guard import MexcUidGuardError, assert_known_mexc_uid
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, _utc
from app.live_execution.reconciliation_view import _project_fills
from app.live_execution.spot_bot_legacy_review import LegacySpotDecision, read_legacy_spot_decision
from app.live_execution.spot_bot_store import (
    LEGACY_CANCEL_REVIEWS, LEGACY_FILL_RECOVERIES, SpotBotStoreError, bot_authorizations, bot_decision_payload,
    bot_record, decoded, encoded,
)
from app.portfolio.models import (
    MexcObservedFill, RealEvent, RealLiveOrderObservation, RealLiveSpotIntent, RealLiveSpotSettlement,
    RealMexcAccountLink, RealMexcFillBooking, RealSpotBot, RealSpotBotDecision,
)
from app.portfolio.reconciliation import compare_mexc_spot_inventory
from app.portfolio.repository import account_book_revision, account_snapshot, verify_account_event_digests
from app.portfolio.vault import RealVault


@dataclass(frozen=True, slots=True)
class LegacyFillRecovery:
    original: LegacySpotDecision
    intent: LiveSpotIntent
    observation: ObservedMexcOrder
    book_revision: str
    digest: str
    base_asset: str
    quote_asset: str


def _exact_request(db: Session, config: dict, decision: LegacySpotDecision, payload: dict) -> LiveSpotIntent | None:
    order = decision.order
    signal = _utc(datetime.fromisoformat(payload["signal_at"]), "signal_at")
    if signal > decision.created_at:
        raise SpotBotStoreError("La señal antigua es posterior a la decisión")
    for item in bot_authorizations(config):
        if item["source"] != payload["source"] or datetime.fromisoformat(item["started_at"]) > decision.created_at:
            continue
        policy = _stored_policy(db, item["policy_id"])
        if (policy.account_ref != decision.account_ref or policy.symbol != order.symbol
                or policy.strategy_version_id != payload["source"]["version_id"]
                or not policy.enabled or policy.expires_at <= decision.created_at):
            continue
        with localcontext() as context:
            context.prec = 300
            fee = (order.quantity_base * order.limit_price_quote * (policy.max_fee_bps / Decimal(10000))).normalize()
            # The policy can give the exact rate. The archived digest must also
            # authenticate its original decimal representation, including zeros.
            exponent = min(fee.as_tuple().exponent, 100)
            minimum = max(fee.adjusted() - 39, -100) if fee else -100
            for scale in range(exponent, minimum - 1, -1):
                original_fee = fee.quantize(Decimal(1).scaleb(scale))
                intent = LiveSpotIntent(order.intent_id, policy.policy_id, policy.strategy_version_id,
                    decision.account_ref, order.symbol, order.side, order.quantity_base, order.limit_price_quote,
                    original_fee, signal)
                if intent_request_digest(intent) == payload["request_digest"]:
                    return intent
    return None


def preview_legacy_fill_recovery(db: Session, account_id: str, bot_id: str, decision: LegacySpotDecision,
                                 observed: ObservedMexcOrder | None, *, now: datetime) -> LegacyFillRecovery | None:
    if (decision.bot_status != "PAUSED" or observed is None or not observed.fills or not observed.final
            or observed.intent_id != decision.order.intent_id):
        return None
    current = read_legacy_spot_decision(db, account_id, bot_id, decision.order.intent_id)
    if current != decision:
        raise SpotBotStoreError("La decisión cambió durante la consulta")
    row, config, _ = bot_record(db, bot_id)
    source = db.get(RealSpotBotDecision, decision.order.intent_id)
    payload = bot_decision_payload(db, source)
    try:
        intent = _exact_request(db, config, decision, payload)
        if intent is None:
            return None
        policy = _stored_policy(db, intent.policy_id)
        if any(asset not in (policy.base_asset, policy.quote_asset) and amount > 0
               for asset, amount in observed.fees_by_asset):
            return None
        earliest = int((decision.created_at - timedelta(seconds=2)).timestamp() * 1000)
        latest = int((now + timedelta(seconds=2)).timestamp() * 1000)
        fills = sorted(observed.fills, key=lambda fill: fill["id"])
        if any(not earliest <= fill["time_ms"] <= latest or
               (intent.side == "BUY" and Decimal(fill["price"]) > intent.limit_price_quote) or
               (intent.side == "SELL" and Decimal(fill["price"]) < intent.limit_price_quote) for fill in fills):
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", observed.external_order_id):
            return None
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None
    book_revision = account_book_revision(db, account_id)
    review = {"protocol": "MEXC_LEGACY_FILL_RECOVERY_V1", "decision_digest": decision.digest,
              "intent_digest": intent_request_digest(intent), "pause_revision": decision.bot_revision,
              "book_revision": book_revision, "account_id": row.account_id, "order": observed.external_order_id,
              "status": observed.status, "executed_base": str(observed.executed_base),
              "cumulative_quote": str(observed.cumulative_quote), "fills": fills}
    return LegacyFillRecovery(decision, intent, observed, book_revision, encoded(review)[1],
                              policy.base_asset, policy.quote_asset)


def fetch_legacy_recovery_balances(vault: RealVault, view: MexcReadOnlyClient,
                                    proposal: LegacyFillRecovery) -> dict:
    ref = proposal.intent.account_ref
    try:
        uid = view.account_uid()
        if view.account_reference() != ref or not assert_known_mexc_uid(vault, ref, uid):
            raise MexcUidGuardError("View fuera del ámbito de la decisión")
        balances = view.balances()
        if (view.account_reference() != ref or view.account_uid() != uid
                or not assert_known_mexc_uid(vault, ref, uid)):
            raise MexcUidGuardError("View cambió durante la consulta")
        return balances
    except (MexcReadOnlyError, MexcUidGuardError):
        raise SpotBotStoreError("El saldo View de la orden antigua no es verificable") from None


def legacy_fills_replay(db: Session, account_id: str, bot_id: str, identifier: str, digest: str | None) -> dict | None:
    if db.get(RealLiveSpotIntent, identifier) is None:
        return None
    row, config, _ = bot_record(db, bot_id)
    if row.account_id != account_id:
        raise SpotBotStoreError("La decisión no pertenece a esta cuenta")
    return _legacy_fills_result(db, bot_id, config, identifier, digest)


def _legacy_fills_result(db: Session, bot_id: str, config: dict, identifier: str, digest: str | None) -> dict | None:
    reviews = config.get(LEGACY_FILL_RECOVERIES, {})
    if not isinstance(reviews, dict):
        raise SpotBotStoreError("El archivo de conciliaciones antiguas cambió")
    record = reviews.get(identifier)
    if record is None:
        return None
    if (not isinstance(record, dict) or set(record) != {"decision_digest", "review_digest", "observation_id"}
            or any(not isinstance(value, str) for value in record.values())):
        raise SpotBotStoreError("El archivo de conciliaciones antiguas cambió")
    decision = db.get(RealSpotBotDecision, identifier)
    intent = db.get(RealLiveSpotIntent, identifier)
    settlement = db.get(RealLiveSpotSettlement, identifier)
    observed = db.get(RealLiveOrderObservation, record["observation_id"])
    if (decision is None or decision.bot_id != bot_id or intent is None or intent.status != "RESERVATION_CLOSED"
            or intent.reason != "LEGACY_RECOVERY_RECONCILED" or settlement is None or observed is None
            or observed.intent_id != identifier or settlement.observation_id != observed.id
            or settlement.external_order_id != observed.external_order_id or observed.fill_count <= 0
            or settlement.external_status != observed.status or settlement.account_ref != intent.account_ref
            or observed.account_ref != intent.account_ref or observed.symbol != intent.symbol
            or intent.account_ref != config.get("account_ref")
            or record.get("decision_digest") != decision.payload_digest
            or "preparation_protocol" in decoded(decision.payload_json, decision.payload_digest)
            or bot_decision_payload(db, decision).get("request_digest") != intent.request_digest
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("review_digest", ""))):
        raise SpotBotStoreError("La conciliación antigua perdió sus originales")
    recover_spot_intent_in_session(db, identifier)
    fills = db.scalars(select(MexcObservedFill).where(MexcObservedFill.account_ref == intent.account_ref,
        MexcObservedFill.symbol == intent.symbol, MexcObservedFill.order_id == observed.external_order_id)).all()
    if _project_fills(intent, observed, fills)["status"] != "NEEDS_ACCOUNT_LINK_AND_BOOKING":
        raise SpotBotStoreError("La conciliación antigua perdió sus ejecuciones verificadas")
    link = db.get(RealMexcAccountLink, intent.account_ref)
    if link is None or link.account_id != config.get("account_id"):
        raise SpotBotStoreError("La conciliación antigua cambió de cuenta")
    for fill in fills:
        booking = db.get(RealMexcFillBooking, fill.id)
        event = db.get(RealEvent, booking.event_id) if booking is not None else None
        if not booked_event_matches(fill, intent, booking, event, link):
            raise SpotBotStoreError("La conciliación antigua perdió su asiento original")
    if digest is not None and digest != record["review_digest"]:
        raise SpotBotStoreError("La decisión ya se concilió con otra revisión")
    return {"state": "LEGACY_FILLS_RECONCILED", "decision_id": identifier, "fill_count": observed.fill_count,
            "replayed": True}


def validate_legacy_fill_recoveries(db: Session, bot_id: str, config: dict) -> None:
    reviews = config[LEGACY_FILL_RECOVERIES]
    if not isinstance(reviews, dict) or not reviews:
        raise SpotBotStoreError("El archivo de conciliaciones antiguas cambió")
    for identifier in reviews:
        if _legacy_fills_result(db, bot_id, config, identifier, None) is None:
            raise SpotBotStoreError("La conciliación antigua perdió su archivo")


def _reject_possible_manual_duplicates(db: Session, account_id: str, policy: LiveSpotPolicy,
                                       observation: ObservedMexcOrder) -> None:
    padding = timedelta(seconds=2)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for fill in observation.fills:
        at = epoch + timedelta(milliseconds=fill["time_ms"])
        candidates = db.scalars(select(RealEvent).outerjoin(RealMexcFillBooking, RealMexcFillBooking.event_id == RealEvent.id)
            .where(RealEvent.account_id == account_id, RealEvent.asset == policy.base_asset,
                   RealEvent.quote_currency == policy.quote_asset, RealEvent.kind == fill["side"],
                   RealEvent.occurred_at.between(at-padding, at+padding), RealMexcFillBooking.fill_id.is_(None))).all()
        if any(event.quantity == Decimal(fill["quantity"]) and event.gross_amount == Decimal(fill["quote_quantity"])
               for event in candidates):
            raise SpotBotStoreError("Posible duplicado manual. Revisa el asiento existente antes de conciliar.")


def archive_legacy_fills(db: Session, account_id: str, bot_id: str, proposal: LegacyFillRecovery,
                         balances: dict, *, observed_at: datetime, now: datetime) -> dict:
    now = _utc(now, "now")
    decision, intent, order = proposal.original, proposal.intent, proposal.observation
    current = read_legacy_spot_decision(db, account_id, bot_id, intent.intent_id)
    if current != decision or account_book_revision(db, account_id) != proposal.book_revision:
        raise SpotBotStoreError("El bot o la cartera cambiaron. Repite la revisión de la orden antigua.")
    bot, config, _ = bot_record(db, bot_id)
    siblings = db.scalars(select(RealSpotBot).where(RealSpotBot.account_ref == intent.account_ref)).all()
    if any(sibling.status == "ACTIVE" for sibling in siblings) or db.scalar(select(RealLiveSpotIntent.id).where(
            RealLiveSpotIntent.account_ref == intent.account_ref,
            RealLiveSpotIntent.status.in_(("RESERVED", "SUBMISSION_UNKNOWN"))).limit(1)):
        raise SpotBotStoreError("Detén los bots y revisa las órdenes pendientes de esta cuenta")
    for sibling in siblings:
        reviews = decoded(sibling.config_json, sibling.config_digest).get(LEGACY_CANCEL_REVIEWS, {})
        if any(item["external_order_id"] == order.external_order_id and item["symbol"] == intent.symbol
               for item in reviews.values()):
            raise SpotBotStoreError("La orden contradice una cancelación ya archivada")
    verify_account_event_digests(db, account_id)
    policy = _stored_policy(db, intent.policy_id)
    _reject_possible_manual_duplicates(db, account_id, policy, order)
    balance_at, amounts, balance_digest = _balance_evidence(balances, policy.base_asset, policy.quote_asset, now=now)
    if balance_at < observed_at:
        raise SpotBotStoreError("El saldo es anterior a la observación de la orden")
    with localcontext() as context:
        context.prec = 300
        notional = intent.quantity_base * intent.limit_price_quote
    row = RealLiveSpotIntent(id=intent.intent_id, policy_id=intent.policy_id, account_ref=intent.account_ref,
        symbol=intent.symbol, base_asset=policy.base_asset, quote_asset=policy.quote_asset, side=intent.side,
        request_digest=intent_request_digest(intent), request_json=_canonical(intent),
        status="SUBMISSION_UNKNOWN", reason="LEGACY_RECOVERY_IN_TRANSACTION", notional_quote=notional,
        limit_price_quote=intent.limit_price_quote, quantity_base=intent.quantity_base,
        reserved_quote=Decimal(0), reserved_base=Decimal(0), created_at=decision.created_at, updated_at=now)
    db.add(row)
    db.flush()
    recover_spot_intent_in_session(db, row.id)
    archived = archive_spot_observation_in_transaction(db, intent, order, now=observed_at)
    booked = book_spot_order_in_transaction(db, row.id, account_id, booked_at=now)
    db.flush()
    comparison = compare_mexc_spot_inventory(account_snapshot(db, account_id), balances, now=now)
    if not comparison["all_quantities_equal"] or comparison["negative_book_cash_assets"]:
        raise SpotBotStoreError("Los asientos propuestos no cuadran con MEXC. Revisa la cartera antes de conciliar.")
    observation = db.get(RealLiveOrderObservation, archived["observation_id"])
    base_free, base_locked = amounts[policy.base_asset]
    quote_free, quote_locked = amounts[policy.quote_asset]
    db.add(RealLiveSpotSettlement(intent_id=row.id, observation_id=observation.id, account_ref=intent.account_ref,
        external_order_id=order.external_order_id, external_status=order.status, balance_observed_at=balance_at,
        free_base=base_free, locked_base=base_locked, free_quote=quote_free, locked_quote=quote_locked,
        balance_digest=balance_digest, settled_at=now))
    row.status, row.reason = "RESERVATION_CLOSED", "LEGACY_RECOVERY_RECONCILED"
    config.setdefault(LEGACY_FILL_RECOVERIES, {})[row.id] = {
        "decision_digest": decision.digest, "review_digest": proposal.digest, "observation_id": observation.id,
    }
    bot.config_json, bot.config_digest = encoded(config)
    bot.updated_at = max(bot.updated_at, now)
    db.flush()
    bot_record(db, bot_id)
    return {"state": "LEGACY_FILLS_RECONCILED", "decision_id": row.id, "fill_count": booked["fill_count"],
            "replayed": False}
