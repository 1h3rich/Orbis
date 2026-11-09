"""Read a bounded MEXC route from independently archived transfer and Spot evidence."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.live_execution.journal import LiveJournalError, recover_spot_intent_in_session
from app.live_execution.mexc_fill_event import booked_event_matches
from app.live_execution.policy import MEXC_MANUAL_ORIGIN
from app.live_execution.reconciliation_view import _project_fills
from app.live_execution.spot_bot_store import bot_authorizations, bot_decision_payload, bot_record
from app.portfolio.mexc_transfer_matches import verify_mexc_transfer_match
from app.portfolio.mexc_withdrawal_booking import settlement_status as withdrawal_settlement
from app.portfolio.mexc_withdrawal_journal import amount_text, recipient_matches, withdrawal_record
from app.portfolio.models import (
    MexcObservedFill,
    RealAccount,
    RealEvent,
    RealLiveOrderObservation,
    RealLiveSpotIntent,
    RealLiveSpotSettlement,
    RealMexcAccountLink,
    RealMexcFillBooking,
    RealMexcTransferMatch,
    RealMexcWithdrawal,
    RealSpotBotDecision,
    RealWalletFundingBooking,
    RealWalletFundingIntent,
)
from app.portfolio.wallet_funding import WalletFundingError, _identity, funding_record
from app.portfolio.wallet_funding_credit import booking_record, credit_record


_MAX_ORDERS = 200
_MAX_WITHDRAWALS = 20
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SettledOrder:
    intent_id: str
    symbol: str
    origin: str
    side: str
    first_fill_ms: int
    last_fill_ms: int
    base_asset: str
    quote_asset: str
    gross_base: str
    gross_quote: str
    net_by_asset: dict[str, str]
    fee_by_asset: dict[str, str]
    fill_count: int

    def public(self) -> dict:
        return {"intent_id": self.intent_id, "symbol": self.symbol, "origin": self.origin,
                "base_asset": self.base_asset, "quote_asset": self.quote_asset, "gross_base": self.gross_base,
                "gross_quote": self.gross_quote, "net_by_asset": self.net_by_asset,
                "fee_by_asset": self.fee_by_asset, "fill_count": self.fill_count}


def _next_deposit_at(db: Session, funding: RealWalletFundingIntent | RealMexcTransferMatch,
                     start: datetime) -> datetime | None:
    # The booking stores canonical UTC ISO dates. An earlier event can be
    # linked later, so the movement's date is not the boundary.
    booked_at = func.json_extract(RealWalletFundingBooking.payload_json, "$.booked_at")
    query = (
        select(RealWalletFundingIntent.id)
        .join(RealWalletFundingBooking, RealWalletFundingBooking.funding_id == RealWalletFundingIntent.id)
        .where(RealWalletFundingIntent.account_id == funding.account_id,
               RealWalletFundingIntent.account_ref == funding.account_ref,
               booked_at >= start.isoformat())
    )
    if isinstance(funding, RealWalletFundingIntent):
        query = query.where(RealWalletFundingIntent.id != funding.id)
    candidate = db.scalar(query.order_by(booked_at, RealWalletFundingIntent.id).limit(1))
    next_wallet = None
    if candidate is not None:
        archived = booking_record(db, candidate)
        if archived is None:
            raise WalletFundingError("El siguiente ingreso perdió su evidencia contable")
        next_wallet = datetime.fromisoformat(archived[1]["booked_at"])

    # A USDT deposit confirmed from MEXC View also adds capital to this book,
    # even when ORBIS did not prepare the transfer from the wallet.
    query = (
        select(RealMexcTransferMatch)
        .join(RealEvent, RealEvent.id == RealMexcTransferMatch.event_id)
        .where(RealMexcTransferMatch.account_ref == funding.account_ref,
               RealMexcTransferMatch.account_id == funding.account_id,
               RealMexcTransferMatch.direction == "deposit",
               RealMexcTransferMatch.confirmed_at >= start,
               RealEvent.quote_currency == "USDT", RealEvent.asset.is_(None))
    )
    if isinstance(funding, RealMexcTransferMatch):
        # All direct credits confirmed in this batch belong to the same
        # starting interval. A separate paired transfer still ends it.
        query = query.where(or_(RealMexcTransferMatch.confirmed_at > start, RealEvent.kind != "CONTRIBUTION"))
    external = db.scalar(query.order_by(RealMexcTransferMatch.confirmed_at, RealMexcTransferMatch.id).limit(1))
    if external is not None and (next_wallet is None or external.confirmed_at <= next_wallet):
        verify_mexc_transfer_match(db, external, check_history=True)
        return external.confirmed_at
    return next_wallet


def _first_withdrawal(db: Session, link: RealMexcAccountLink, start: datetime,
                      end: datetime | None) -> tuple[dict, dict, datetime] | None:
    query = select(RealMexcWithdrawal).where(
        RealMexcWithdrawal.account_id == link.account_id,
        RealMexcWithdrawal.account_ref == link.account_ref,
        # A withdrawal plan expires two minutes after preparation.
        RealMexcWithdrawal.created_at >= start - timedelta(minutes=2),
        RealMexcWithdrawal.status.in_(("SUBMISSION_UNKNOWN", "ACKNOWLEDGED")),
    )
    if end is not None:
        query = query.where(RealMexcWithdrawal.created_at < end)
    first: tuple[dict, dict, datetime] | None = None
    for index, candidate in enumerate(db.scalars(query.order_by(RealMexcWithdrawal.created_at, RealMexcWithdrawal.id)
                                                .limit(_MAX_WITHDRAWALS + 1))):
        if first is not None and candidate.created_at > first[2]:
            break
        if index == _MAX_WITHDRAWALS:
            raise WalletFundingError("Hay demasiadas retiradas para verificar este recorrido")
        _, terms, journal = withdrawal_record(db, candidate.id)
        requested_at = datetime.fromisoformat(journal["history"][1]["at"])
        if requested_at < start or end is not None and requested_at >= end:
            continue
        state = withdrawal_settlement(db, candidate.id)
        if state["settlement_status"] == "REJECTED_CONFIRMED":
            continue
        if first is None or requested_at < first[2]:
            first = (terms, state, requested_at)
        elif requested_at == first[2]:
            raise WalletFundingError("Dos retiradas comparten la hora de solicitud")
    return first


def _withdrawal_view(db: Session, first: tuple[dict, dict, datetime] | None, *,
                     wallet: str | None, funding_terms: dict | None) -> tuple[str, dict | None]:
    if first is None:
        return "MISSING", None
    terms, state, _ = first
    if funding_terms is not None:
        if wallet is None:
            proof = terms.get("roundtrip_wallet")
            if proof is None:
                return "DESTINATION_UNVERIFIED", None
            same_wallet = proof["funding_digest"] == funding_terms["wallet_digest"]
        else:
            same_wallet = recipient_matches(db, terms, wallet)
        if not same_wallet:
            return "OTHER_WALLET", None
        if terms["contract"] != funding_terms["token_contract"] or terms["decimals"] != funding_terms["decimals"]:
            return "OTHER_ROUTE", None
    if not state["wallet_received"]:
        return "PENDING", None
    amounts = {key: state[key] for key in ("received", "actual_fee", "actual_debit")}
    return ("BOOKED" if state["booked"] else "RECEIVED"), amounts


def _order_origin(db: Session, intent: RealLiveSpotIntent, link: RealMexcAccountLink) -> str:
    request = recover_spot_intent_in_session(db, intent.id)
    decision = db.scalar(select(RealSpotBotDecision).where(RealSpotBotDecision.intent_id == intent.id))
    if decision is None:
        return "MANUAL" if request.strategy_version_id == MEXC_MANUAL_ORIGIN else "ORBIS"
    bot, config, _ = bot_record(db, decision.bot_id)
    data = bot_decision_payload(db, decision)
    origins = {item["policy_id"]: item["source"] for item in bot_authorizations(config)}
    if (request.strategy_version_id == MEXC_MANUAL_ORIGIN or bot.account_id != link.account_id
            or bot.account_ref != link.account_ref or request.policy_id not in origins
            or data.get("source") != origins[request.policy_id]
            or data.get("request_digest") != intent.request_digest):
        raise WalletFundingError("La decisión archivada del bot no coincide con la orden")
    return "BOT"


def _settled_orders(db: Session, link: RealMexcAccountLink, start: datetime,
                    end: datetime | None) -> list[SettledOrder]:
    query = (
        select(RealLiveSpotIntent, RealLiveSpotSettlement, RealLiveOrderObservation)
        .join(RealLiveSpotSettlement, RealLiveSpotSettlement.intent_id == RealLiveSpotIntent.id)
        .join(RealLiveOrderObservation, RealLiveOrderObservation.id == RealLiveSpotSettlement.observation_id)
        .where(RealLiveSpotIntent.account_ref == link.account_ref, RealLiveSpotIntent.quote_asset == "USDT",
               RealLiveSpotIntent.created_at >= start, RealLiveSpotSettlement.settled_at >= start,
               RealLiveOrderObservation.fill_count > 0)
    )
    if end is not None:
        query = query.where(RealLiveSpotSettlement.settled_at < end)
    orders = []
    for index, (intent, settlement, observation) in enumerate(db.execute(
            query.order_by(RealLiveSpotSettlement.settled_at, RealLiveSpotIntent.id).limit(_MAX_ORDERS + 1))):
        if index == _MAX_ORDERS:
            raise WalletFundingError("Hay demasiadas órdenes para verificar este recorrido")
        if (intent.status != "RESERVATION_CLOSED" or settlement.account_ref != link.account_ref
                or observation.intent_id != intent.id or observation.account_ref != link.account_ref
                or observation.symbol != intent.symbol or settlement.external_order_id != observation.external_order_id
                or settlement.external_status != observation.status
                or not observation.observed_at <= settlement.balance_observed_at <= settlement.settled_at):
            raise WalletFundingError("El cierre archivado de una orden no es verificable")
        fills = db.scalars(select(MexcObservedFill).where(
            MexcObservedFill.account_ref == link.account_ref, MexcObservedFill.symbol == intent.symbol,
            MexcObservedFill.order_id == observation.external_order_id,
        )).all()
        projection = _project_fills(intent, observation, fills)
        if projection["status"] != "NEEDS_ACCOUNT_LINK_AND_BOOKING":
            raise WalletFundingError("Los fills archivados no coinciden con la orden cerrada")
        for fill in fills:
            booking = db.get(RealMexcFillBooking, fill.id)
            event = db.get(RealEvent, booking.event_id) if booking is not None else None
            if not booked_event_matches(fill, intent, booking, event, link):
                raise WalletFundingError("Un fill perdió su asiento REAL")
        first, last = min(fill.time_ms for fill in fills), max(fill.time_ms for fill in fills)
        if (_EPOCH + timedelta(milliseconds=first) < start
                or _EPOCH + timedelta(milliseconds=last) > settlement.settled_at):
            raise WalletFundingError("La fecha de ejecución no encaja en el recorrido")
        try:
            origin = _order_origin(db, intent, link)
        except LiveJournalError:
            raise WalletFundingError("El origen archivado de la orden no es verificable") from None
        pair = f"{intent.base_asset}/{intent.quote_asset}"
        orders.append(SettledOrder(
            intent_id=intent.id, symbol=pair, origin=origin, side=intent.side, first_fill_ms=first, last_fill_ms=last,
            base_asset=intent.base_asset, quote_asset=intent.quote_asset,
            gross_base=format(observation.executed_base, "f"), gross_quote=format(observation.cumulative_quote, "f"),
            net_by_asset=projection["deltas"], fee_by_asset=projection["fee_by_asset"], fill_count=projection["fill_count"],
        ))
    return sorted(orders, key=lambda order: (order.last_fill_ms, order.intent_id))


def _journey_steps(db: Session, link: RealMexcAccountLink, start: datetime, end: datetime | None,
                   *, wallet: str | None = None, funding_terms: dict | None = None) -> dict:
    first = _first_withdrawal(db, link, start, end)
    withdrawal, amounts = _withdrawal_view(db, first, wallet=wallet, funding_terms=funding_terms)
    orders = _settled_orders(db, link, start, first[2] if first is not None else end)
    result = {"purchase":None, "sale":None, "withdrawal":withdrawal,
              "withdrawal_amounts":amounts, "complete":False, "next_step":None}
    if end is None:
        if withdrawal in ("PENDING", "RECEIVED"):
            result["next_step"] = "WITHDRAWAL"
        elif withdrawal == "MISSING":
            result["next_step"] = "SPOT"
    purchases: dict[str, SettledOrder] = {}
    for order in orders:
        if order.side == "BUY":
            purchases.setdefault(order.symbol, order)
        elif order.symbol in purchases and purchases[order.symbol].last_fill_ms < order.first_fill_ms:
            if result["next_step"] == "SPOT":
                result["next_step"] = "WITHDRAWAL"
            return {**result, "purchase":purchases[order.symbol].public(), "sale":order.public(),
                    "complete":withdrawal == "BOOKED"}
    if purchases:
        result["purchase"] = next(iter(purchases.values())).public()
    return result


def review_mexc_roundtrip(db: Session, account_id: str, funding_id: str, wallet: str | None = None) -> dict:
    scoped = db.get(RealWalletFundingIntent, funding_id)
    if scoped is None or scoped.account_id != account_id:
        raise WalletFundingError("El ingreso no corresponde a esta cuenta y wallet")
    funding, terms, _ = funding_record(db, funding_id)
    if wallet is not None and terms["wallet_digest"] != _identity(db, "wallet", wallet):
        raise WalletFundingError("El ingreso no corresponde a esta cuenta y wallet")
    result = {"deposit": "PENDING", "purchase": None, "sale": None, "withdrawal": "MISSING", "complete": False,
              "deposit_amounts": None, "withdrawal_amounts": None, "next_step": None}
    credit = credit_record(db, funding_id)
    if credit is None:
        return result
    result["deposit"] = "CREDITED"
    result["deposit_amounts"] = {
        "credited": amount_text(int(terms["amount_units"]), terms["decimals"]),
        "gas_bnb": amount_text(int(credit[1]["gas_fee_wei"]), 18),
    }
    booking = booking_record(db, funding_id)
    if booking is None:
        return result
    link = db.get(RealMexcAccountLink, funding.account_ref)
    if link is None or link.account_id != account_id:
        raise WalletFundingError("El ingreso perdió el vínculo con el libro MEXC")
    start = datetime.fromisoformat(booking[1]["booked_at"])
    end = _next_deposit_at(db, funding, start)
    return {**result, "deposit":"BOOKED", **_journey_steps(db, link, start, end, wallet=wallet, funding_terms=terms)}


def _direct_credits(account_id: str):
    return (
        select(RealMexcTransferMatch)
        .join(RealEvent, RealEvent.id == RealMexcTransferMatch.event_id)
        .where(RealMexcTransferMatch.account_id == account_id, RealMexcTransferMatch.direction == "deposit",
               RealEvent.account_id == account_id, RealEvent.kind == "CONTRIBUTION",
               RealEvent.asset.is_(None), RealEvent.quote_currency == "USDT")
    )


def list_mexc_direct_journeys(db: Session, account_id: str, offset: int, limit: int) -> dict:
    if db.get(RealAccount, account_id) is None:
        raise WalletFundingError("No se encontró esta cuenta REAL")
    match = RealMexcTransferMatch
    query = (_direct_credits(account_id)
        .with_only_columns(func.min(match.event_id).label("id"), match.confirmed_at, func.count().label("credits"))
        .group_by(match.account_ref, match.confirmed_at)
        .order_by(match.confirmed_at.desc(), func.min(match.event_id).desc()))
    groups = db.execute(query.limit(limit + 1).offset(offset)).all()
    return {"items":[{"id":row.id, "booked_at":row.confirmed_at.isoformat(), "credits":row.credits}
                     for row in groups[:limit]], "has_more":len(groups) > limit}


def review_mexc_direct_journey(db: Session, account_id: str, event_id: str) -> dict:
    anchor = db.scalar(_direct_credits(account_id).where(RealMexcTransferMatch.event_id == event_id))
    if anchor is None:
        raise WalletFundingError("El ingreso directo no corresponde a esta cuenta")
    link = db.get(RealMexcAccountLink, anchor.account_ref)
    if link is None or link.account_id != account_id:
        raise WalletFundingError("El ingreso perdió el vínculo con el libro MEXC")
    start = anchor.confirmed_at
    members = db.scalars(_direct_credits(account_id).where(
        RealMexcTransferMatch.account_ref == anchor.account_ref,
        RealMexcTransferMatch.confirmed_at == start,
    ).order_by(RealMexcTransferMatch.event_id).limit(21)).all()
    if not 1 <= len(members) <= 20:
        raise WalletFundingError("El grupo de ingresos directos no es verificable")
    with localcontext() as context:
        context.prec = 300
        amount = Decimal(0)
        for member in members:
            verify_mexc_transfer_match(db, member, check_history=True)
            event = db.get(RealEvent, member.event_id)
            if event.occurred_at > start:
                raise WalletFundingError("El abono archivado es posterior a su contabilización")
            amount += event.gross_amount
    end = _next_deposit_at(db, anchor, start)
    return {"deposit":"BOOKED", "deposit_amounts":{"credited":format(amount, "f")},
            "booked_at":start.isoformat(), "credits":len(members), "source_wallet":"UNKNOWN",
            **_journey_steps(db, link, start, end)}
