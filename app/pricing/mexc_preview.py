"""Valoración informativa de saldos Spot MEXC observados, nunca P/L ni riesgo REAL.

Sólo se publica un total cuando cada activo distinto de cero tiene un bid
directo, reciente y en la misma divisa. Ningún dato se incorpora al libro.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Callable

from app.live_execution.mexc_live_observation import (
    MexcLiveObservationError, _quote_get, fetch_live_quote,
)


_ASSET = re.compile(r"[A-Z0-9]{2,24}\Z")
_MAX_QUOTE_REQUESTS = 24
_MAX_AGE_SECONDS = 30


class MexcPreviewError(ValueError):
    """Entrada u observación inválida, sin datos privados en el mensaje."""


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MexcPreviewError("Hora de observación MEXC no verificable")
    return value.astimezone(timezone.utc)


def _amount(raw: object) -> Decimal:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 100:
        raise MexcPreviewError("Saldo MEXC no verificable")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise MexcPreviewError("Saldo MEXC no verificable") from None
    if (not value.is_finite() or value < 0 or len(value.as_tuple().digits) > 40
            or abs(value.as_tuple().exponent) > 100):
        raise MexcPreviewError("Saldo MEXC no verificable")
    return value


def preview_spot_valuation(
    observed: dict, quote_asset: str, *,
    clock: Callable[[], datetime] | None = None,
    quote_get: Callable[[str], object] = _quote_get,
) -> dict:
    """Devuelve total sólo con cobertura completa; los parciales no tienen total.

    Se limita a 24 consultas públicas por petición. El exceso queda sin
    valorar, de forma explícita, para no saturar MEXC ni la interfaz local.
    """
    if not isinstance(quote_asset, str) or quote_asset not in ("USDT", "EUR"):
        raise MexcPreviewError("Divisa de valoración no admitida")
    if (not isinstance(observed, dict)
            or observed.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
            or observed.get("accounting") != "OBSERVED_NOT_IMPORTED"
            or not isinstance(observed.get("balances"), list)):
        raise MexcPreviewError("Observación MEXC sin procedencia válida")
    current = clock or (lambda: datetime.now(timezone.utc))
    started = _utc(current())
    try:
        observed_at = _utc(datetime.fromisoformat(observed["observed_at"]))
    except (KeyError, TypeError, ValueError):
        raise MexcPreviewError("Observación MEXC sin hora válida") from None
    if (observed_at > started or (started - observed_at).total_seconds() > _MAX_AGE_SECONDS):
        raise MexcPreviewError("Saldos MEXC caducados")

    values = []
    unpriced = []
    seen: set[str] = set()
    requests = 0
    with localcontext() as context:
        context.prec = 100
        priced_total = Decimal(0)
        for row in observed["balances"]:
            if (not isinstance(row, dict) or not isinstance(row.get("asset"), str)
                    or not _ASSET.fullmatch(row["asset"]) or row["asset"] in seen):
                raise MexcPreviewError("Inventario MEXC malformado o duplicado")
            asset = row["asset"]
            seen.add(asset)
            free, locked, total = (_amount(row.get(key)) for key in ("free", "locked", "total"))
            if total != free + locked:
                raise MexcPreviewError("Saldo MEXC no concuerda")
            if total == 0:
                continue
            item = {"asset": asset, "free": format(free, "f"),
                    "locked": format(locked, "f"), "total": format(total, "f"),
                    "bid_quote": None, "value_quote": None, "price_at": None}
            if asset == quote_asset:
                bid, price_at = Decimal(1), observed_at
            elif requests >= _MAX_QUOTE_REQUESTS or len(asset + quote_asset) > 30:
                unpriced.append(asset)
                values.append(item)
                continue
            else:
                requests += 1
                try:
                    bid, _, _, price_at = fetch_live_quote(
                        asset + quote_asset, now=_utc(current()),
                        max_age_seconds=_MAX_AGE_SECONDS, http_get=quote_get,
                    )
                except MexcLiveObservationError:
                    unpriced.append(asset)
                    values.append(item)
                    continue
            value = total * bid
            priced_total += value
            item.update({"bid_quote": format(bid, "f"), "value_quote": format(value, "f"),
                         "price_at": price_at.isoformat()})
            values.append(item)
        finished = _utc(current())
        if (finished < started or observed_at > finished
                or (finished - observed_at).total_seconds() > _MAX_AGE_SECONDS):
            raise MexcPreviewError("Saldos MEXC caducados durante la valoración")
        for item in values:
            if item["price_at"] is not None:
                price_at = datetime.fromisoformat(item["price_at"])
                if price_at > finished or (finished - price_at).total_seconds() > _MAX_AGE_SECONDS:
                    item.update({"bid_quote": None, "value_quote": None, "price_at": None})
                    unpriced.append(item["asset"])
        complete = not unpriced
        if complete:
            total_quote = format(priced_total, "f")
        else:
            total_quote = None
    return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
            "quote_asset": quote_asset, "observed_at": observed_at.isoformat(),
            "valued_at": finished.isoformat(), "complete": complete,
            "total_quote": total_quote, "unpriced_assets": unpriced,
            "balances": values, "max_quote_requests": _MAX_QUOTE_REQUESTS}
