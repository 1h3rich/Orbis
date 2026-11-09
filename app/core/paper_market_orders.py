"""Cotización pública causal para órdenes PAPER manuales gestionadas por Orbis.

La propuesta del cliente no contiene precio. El servidor elige ask/bid cuando
existen o usa el último precio como aproximación explícita; nunca usa la vela
bajo el cursor ni una cotización manual/de respaldo atrasada.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json

from app.core.paper_trading import PaperTradeResult
from app.database.models import PaperMarketEvidence
from app.marketdata.normalize import canonical_symbol


class MarketQuoteUnavailable(ValueError):
    """No hay una cotización pública suficientemente reciente para operar."""


def _decimal_text(value: Decimal) -> str:
    """Texto canónico exacto sin redondeo implícito del contexto Decimal."""
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in ("-0", "") else rendered


@dataclass(frozen=True)
class MarketPaperQuote:
    symbol: str
    provider: str
    source: str
    received_at: str
    market_time_at: str | None
    market_time_status: str
    price_basis: str
    price: Decimal
    approximate: bool


def _positive_price(value: object) -> Decimal:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise MarketQuoteUnavailable("Precio de mercado inválido") from None
    if not price.is_finite() or price <= 0:
        raise MarketQuoteUnavailable("Precio de mercado inválido")
    with localcontext() as context:
        context.prec = 80
        price = price.normalize()
    # El libro PAPER existente usa Numeric(30, 12); rechazar en vez de
    # truncar silenciosamente un precio que esa columna no representa.
    if price.as_tuple().exponent < -12 or price.adjusted() > 17:
        raise MarketQuoteUnavailable("Precio fuera de la precisión del libro PAPER")
    return price


def quote_from_snapshot(
    snapshot: dict, *, asset: str, quote_currency: str, side: str,
    requested_provider: str, now: datetime | None = None,
) -> MarketPaperQuote:
    """Valida cobertura, procedencia y tiempo antes de decidir el precio."""
    symbol = canonical_symbol(asset, quote_currency)
    if not isinstance(snapshot, dict) or not snapshot.get("available") or snapshot.get("symbol") != symbol:
        raise MarketQuoteUnavailable("No hay cotización pública para este par")
    provider = str(snapshot.get("resolved_provider") or "").lower()
    if not provider or provider == "grvt" or (
        requested_provider != "auto" and provider != requested_provider
    ):
        raise MarketQuoteUnavailable("La fuente no coincide con el mercado solicitado")
    source = snapshot.get("source")
    # Los adaptadores Spot públicos declaran su nombre como procedencia.
    # Nunca convertir una cotización manual o de otro venue en una de éste.
    if not isinstance(source, str) or source != provider:
        raise MarketQuoteUnavailable("La procedencia del precio no coincide con el proveedor")
    status = snapshot.get("market_time_status")
    if status not in ("fresh", "unknown"):
        raise MarketQuoteUnavailable("La cotización del mercado no está vigente")
    received_text = snapshot.get("received_at")
    try:
        received = datetime.fromisoformat(str(received_text).replace("Z", "+00:00"))
        if received.tzinfo is None:
            raise ValueError("naive timestamp")
        age = ((now or datetime.now(timezone.utc)) - received).total_seconds()
    except (TypeError, ValueError):
        raise MarketQuoteUnavailable("No se conoce la hora de recepción") from None
    if age < -5 or age > 60:
        raise MarketQuoteUnavailable("La cotización recibida ha caducado")
    market_time_at = snapshot.get("market_time_at")
    if status == "fresh":
        try:
            event = datetime.fromisoformat(str(market_time_at).replace("Z", "+00:00"))
            if event.tzinfo is None:
                raise ValueError("naive event time")
            event_age = ((now or datetime.now(timezone.utc)) - event).total_seconds()
        except (TypeError, ValueError):
            raise MarketQuoteUnavailable("No se pudo verificar la hora del mercado") from None
        if event_age < -120 or event_age > 60:
            raise MarketQuoteUnavailable("La cotización del mercado no está vigente")
    basis = "ask" if side == "BUY" else "bid"
    raw = snapshot.get(basis)
    if raw is None:
        basis, raw = "last", snapshot.get("price")
    price = _positive_price(raw)
    if len(source) > 64:
        raise MarketQuoteUnavailable("Procedencia de precio inválida")
    return MarketPaperQuote(
        symbol=symbol, provider=provider, source=source,
        received_at=str(received_text), market_time_at=str(market_time_at) if market_time_at else None,
        market_time_status=status, price_basis=basis, price=price,
        approximate=basis == "last" or status == "unknown",
    )


def _request_digest(
    *, side: str, asset: str, quote_currency: str, size: Decimal,
    fee: Decimal, provider: str, legacy: bool,
) -> str:
    if legacy:
        with localcontext() as context:
            context.prec = 28  # Formato exacto usado antes de esta corrección.
            size_text, fee_text = str(size.normalize()), str(fee.normalize())
    else:
        size_text, fee_text = _decimal_text(size), _decimal_text(fee)
    payload = {
        "kind": "orbis-paper-public-market-v1", "side": side,
        "symbol": canonical_symbol(asset, quote_currency),
        "size": size_text, "fee": fee_text,
        "provider": provider,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def market_request_digest(
    *, side: str, asset: str, quote_currency: str, size: Decimal,
    fee: Decimal, provider: str,
) -> str:
    """Identidad exacta del intento: no depende del precio cambiante."""
    return _request_digest(
        side=side, asset=asset, quote_currency=quote_currency,
        size=size, fee=fee, provider=provider, legacy=False,
    )


def legacy_market_request_digest(
    *, side: str, asset: str, quote_currency: str, size: Decimal,
    fee: Decimal, provider: str,
) -> str | None:
    """Sólo leer digestos previos cuyo normalizado de 28 dígitos era fiel."""
    if any(len(value.as_tuple().digits) > 28 for value in (size, fee)):
        return None
    return _request_digest(
        side=side, asset=asset, quote_currency=quote_currency,
        size=size, fee=fee, provider=provider, legacy=True,
    )


def evidence_for(request_id: str, quote: MarketPaperQuote) -> PaperMarketEvidence:
    return PaperMarketEvidence(
        request_id=request_id, symbol=quote.symbol, provider=quote.provider,
        source=quote.source, received_at=quote.received_at,
        market_time_at=quote.market_time_at,
        market_time_status=quote.market_time_status, price_basis=quote.price_basis,
        price=str(quote.price), approximate=quote.approximate,
    )


def market_trade_response(result: PaperTradeResult, evidence: PaperMarketEvidence) -> dict:
    residual = Decimal("0")
    if result.executed:
        with localcontext() as context:
            context.prec = 80
            gross = result.asset_received * result.price
            if result.side == "BUY":
                residual = result.amount_spent - result.fee - gross
            else:
                residual = gross - result.fee - result.amount_spent

    return {
        "executed": result.executed, "reason": result.reason, "side": result.side,
        "asset": result.asset, "amount_spent": _decimal_text(result.amount_spent),
        "asset_received": _decimal_text(result.asset_received), "price": _decimal_text(result.price),
        "fee": _decimal_text(result.fee), "operation_id": result.operation_id,
        "rounding_residual_quote": _decimal_text(residual),
        "quote": {
            "symbol": evidence.symbol, "provider": evidence.provider,
            "source": evidence.source, "received_at": evidence.received_at,
            "market_time_at": evidence.market_time_at,
            "market_time_status": evidence.market_time_status,
            "price_basis": evidence.price_basis, "price": evidence.price,
            "approximate": evidence.approximate,
        },
    }
