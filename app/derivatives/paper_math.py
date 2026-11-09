"""Contratos de precio y límites para PAPER manual de perpetuos GRVT.

Las cotizaciones proceden del mercado actual; el precio bid/ask más un
deslizamiento configurable es una aproximación, no una promesa de ejecución.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class PaperPerpError(ValueError):
    pass


def _exact_integer(value: object,name: str) -> int:
    # Public tier/max-leverage fields and timestamps are integers. The stored
    # rule codec also emits integer text; preserve it without int(float/bool)
    # truncation. Valid existing rule snapshots retain their interpretation.
    if type(value) is int:return value
    if type(value) is str and 1<=len(value)<=80 and value.isascii() and value.isdecimal():
        return int(value)
    raise PaperPerpError(name+' debe ser un entero exacto')


def decimal_string(value: object, name: str, *, positive: bool = False, allow_zero: bool = True) -> Decimal:
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        raise PaperPerpError(f"{name} debe ser una cadena decimal")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise PaperPerpError(f"{name} inválido") from exc
    if (not number.is_finite() or abs(number) > Decimal("1e18")
            or len(number.as_tuple().digits) > 40
            or not -40 <= number.as_tuple().exponent <= 40):
        raise PaperPerpError(f"{name} fuera de rango")
    if positive and number <= 0:
        raise PaperPerpError(f"{name} debe ser positivo")
    if not positive and not allow_zero and number == 0:
        raise PaperPerpError(f"{name} no puede ser cero")
    return number


@dataclass(frozen=True)
class PerpQuote:
    instrument: str
    event_time_ns: int
    mark: Decimal
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, str) or not self.instrument:
            raise PaperPerpError("Instrumento GRVT inválido")
        if type(self.event_time_ns) is not int or self.event_time_ns <= 0:
            raise PaperPerpError("Cotización sin hora verificable")
        for name in ("mark", "bid", "ask"):
            value = getattr(self, name)
            # Los enteros son exactos; float y bool nunca deben llegar al motor.
            if type(value) not in (Decimal, int):
                raise PaperPerpError(f"{name} debe ser decimal exacto")
            object.__setattr__(self, name, decimal_string(str(value), name, positive=True))
        if self.bid > self.ask:
            raise PaperPerpError("Libro GRVT invertido")

    @classmethod
    def from_ticker(cls, ticker: dict | None, instrument: str, now_ns: int) -> "PerpQuote":
        if not isinstance(ticker, dict) or ticker.get("instrument") != instrument:
            raise PaperPerpError("Cotización GRVT ausente o de otro instrumento")
        raw_time = ticker.get("event_time_ns")
        if (type(raw_time) is not int
                and not (isinstance(raw_time, str) and 1 <= len(raw_time) <= 20
                         and raw_time.isascii() and raw_time.isdecimal())):
            raise PaperPerpError("Cotización sin hora verificable")
        try:
            event_time_ns = int(raw_time)
        except (ValueError, TypeError) as exc:
            raise PaperPerpError("Cotización sin hora verificable") from exc
        if event_time_ns > now_ns + 2_000_000_000 or now_ns - event_time_ns > 15_000_000_000:
            raise PaperPerpError("Cotización GRVT obsoleta o futura")
        mark = decimal_string(ticker.get("mark_price"), "mark_price", positive=True)
        bid = decimal_string(ticker.get("bid"), "bid", positive=True)
        ask = decimal_string(ticker.get("ask"), "ask", positive=True)
        if bid > ask:
            raise PaperPerpError("Libro GRVT invertido")
        return cls(instrument, event_time_ns, mark, bid, ask)

    def fill_price(self, side: str, action: str, slippage_bps: Decimal) -> Decimal:
        if side not in ("LONG", "SHORT") or action not in ("OPEN", "CLOSE"):
            raise PaperPerpError("Lado o acción no admitidos")
        if not slippage_bps.is_finite() or not Decimal("0") <= slippage_bps <= Decimal("500"):
            raise PaperPerpError("Deslizamiento fuera de rango")
        buying = (side == "LONG") == (action == "OPEN")
        base = self.ask if buying else self.bid
        factor = Decimal("1") + slippage_bps / Decimal("10000") * (1 if buying else -1)
        return base * factor


@dataclass(frozen=True)
class MarginTier:
    floor: Decimal
    cap: Decimal | None
    initial_rate: Decimal
    maintenance_rate: Decimal
    cumulative_maintenance_amount: Decimal
    max_leverage: Decimal


@dataclass(frozen=True)
class MarginRules:
    instrument: str
    max_size: Decimal
    fetched_at_ns: int
    tiers: tuple[MarginTier, ...]

    @classmethod
    def from_payload(cls, raw: dict | None, instrument: str, now_ns: int) -> "MarginRules":
        if not isinstance(raw, dict) or raw.get("instrument") != instrument:
            raise PaperPerpError("Reglas GRVT ausentes o de otro instrumento")
        try:
            fetched_at = _exact_integer(raw["fetched_at_ns"],"fetched_at_ns")
            rows = raw["risk_brackets"]
            max_size = decimal_string(raw["max_position_size"], "max_position_size", positive=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise PaperPerpError("Reglas GRVT incompletas") from exc
        if fetched_at > now_ns + 2_000_000_000 or now_ns - fetched_at > 60_000_000_000:
            raise PaperPerpError("Reglas GRVT obsoletas o futuras")
        if not isinstance(rows, list) or not rows:
            raise PaperPerpError("Reglas GRVT sin tramos")
        tiers = []
        for index, row in enumerate(rows):
            try:
                if _exact_integer(row["tier"],"tier") != index + 1:
                    raise PaperPerpError("Tramos GRVT desordenados")
                floor = decimal_string(row["notional_floor"], "notional_floor")
                cap = (decimal_string(row["notional_cap"], "notional_cap", positive=True)
                       if row.get("notional_cap") is not None else None)
                initial = decimal_string(row["initial_margin_rate"], "initial_margin_rate", positive=True)
                maintenance = decimal_string(row["maintenance_margin_rate"], "maintenance_margin_rate", positive=True)
                cumulative = decimal_string(row["cumulative_maintenance_amount"],
                                            "cumulative_maintenance_amount")
                leverage = Decimal(_exact_integer(row["max_leverage"],"max_leverage"))
            except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
                raise PaperPerpError("Tramo GRVT inválido") from exc
            if floor < 0 or (cap is not None and cap <= floor) or not 0 < maintenance < initial <= 1 or leverage < 1:
                raise PaperPerpError("Tramo GRVT fuera de rango")
            if cumulative < 0 or (index == 0 and cumulative != 0):
                raise PaperPerpError("Importe acumulado de mantenimiento inválido")
            if floor and cumulative > floor * maintenance:
                raise PaperPerpError("Mantenimiento negativo en el inicio del tramo")
            if index == 0 and floor != 0:
                raise PaperPerpError("Primer tramo GRVT no empieza en cero")
            if index and tiers[-1].cap != floor:
                raise PaperPerpError("Tramos GRVT con hueco o solape")
            tiers.append(MarginTier(floor, cap, initial, maintenance, cumulative, leverage))
        return cls(instrument, max_size, fetched_at, tuple(tiers))

    def tier_for(self, notional: Decimal) -> MarginTier:
        if not notional.is_finite() or notional <= 0:
            raise PaperPerpError("Nocional inválido")
        for tier in self.tiers:
            if tier.floor <= notional and (tier.cap is None or notional < tier.cap):
                return tier
        raise PaperPerpError("Nocional fuera de tramos GRVT")

    def maintenance_requirement(self, notional: Decimal) -> Decimal:
        tier = self.tier_for(notional)
        return notional * tier.maintenance_rate - tier.cumulative_maintenance_amount

    def json(self) -> dict:
        return {"instrument": self.instrument, "max_position_size": str(self.max_size),
                "fetched_at_ns": self.fetched_at_ns,
                "risk_brackets": [{"tier": index + 1, "notional_floor": str(t.floor),
                                   "notional_cap": str(t.cap) if t.cap is not None else None,
                                   "initial_margin_rate": str(t.initial_rate),
                                   "maintenance_margin_rate": str(t.maintenance_rate),
                                   "cumulative_maintenance_amount": str(t.cumulative_maintenance_amount),
                                   "max_leverage": str(t.max_leverage)} for index, t in enumerate(self.tiers)]}


@dataclass(frozen=True)
class InstrumentSpec:
    instrument: str
    fetched_at_ns: int
    min_size: Decimal
    size_step: Decimal
    min_notional: Decimal
    tick_size: Decimal
    max_size: Decimal | None

    @classmethod
    def from_payload(cls, raw: dict | None, instrument: str, now_ns: int) -> "InstrumentSpec":
        if not isinstance(raw, dict) or raw.get("instrument") != instrument:
            raise PaperPerpError("Especificación GRVT ausente o de otro instrumento")
        try:
            fetched_at = _exact_integer(raw["fetched_at_ns"],"fetched_at_ns")
            decimals = raw["base_decimals"]
            min_size = decimal_string(raw["min_size"], "min_size", positive=True)
            min_notional = decimal_string(raw["min_notional"], "min_notional", positive=True)
            tick_size = decimal_string(raw["tick_size"], "tick_size", positive=True)
            max_size = (decimal_string(raw["max_position_size"], "max_position_size", positive=True)
                        if raw.get("max_position_size") is not None else None)
        except (KeyError, ValueError, TypeError) as exc:
            raise PaperPerpError("Especificación GRVT incompleta") from exc
        if (raw.get("kind") != "PERPETUAL" or not isinstance(raw.get("venues"), list)
                or "ORDERBOOK" not in raw["venues"]):
            raise PaperPerpError("Instrumento GRVT no operable como perpetuo de libro")
        if type(decimals) is not int or not -12 <= decimals <= 18:
            raise PaperPerpError("Granularidad GRVT inválida")
        if fetched_at > now_ns + 2_000_000_000 or now_ns - fetched_at > 60_000_000_000:
            raise PaperPerpError("Especificación GRVT obsoleta o futura")
        step = Decimal(1).scaleb(-decimals)
        if min_size < step or min_size % step or (max_size is not None and max_size < min_size):
            raise PaperPerpError("Tamaño GRVT incoherente con su granularidad")
        return cls(instrument, fetched_at, min_size, step, min_notional, tick_size, max_size)

    def validate_open(self, size: Decimal, reference_price: Decimal) -> None:
        if not size.is_finite() or size < self.min_size:
            raise PaperPerpError(f"Cantidad inferior al mínimo GRVT ({self.min_size})")
        if size % self.size_step:
            raise PaperPerpError(f"Cantidad no múltiplo del paso GRVT ({format(self.size_step, 'f')})")
        if self.max_size is not None and size > self.max_size:
            raise PaperPerpError("Cantidad superior al máximo GRVT")
        if not reference_price.is_finite() or reference_price <= 0:
            raise PaperPerpError("Precio de referencia inválido")
        if size * reference_price < self.min_notional:
            raise PaperPerpError(f"Nocional inferior al mínimo GRVT ({self.min_notional})")

    def json(self) -> dict:
        return {"instrument": self.instrument, "fetched_at_ns": self.fetched_at_ns,
                "min_size": str(self.min_size), "size_step": format(self.size_step, "f"),
                "min_notional": str(self.min_notional), "tick_size": str(self.tick_size),
                "max_position_size": str(self.max_size) if self.max_size is not None else None}
