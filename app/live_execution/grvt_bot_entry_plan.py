"""Términos automáticos TESTNET desde libro actual y porcentajes de estrategia.

El límite de entrada es nocional, no margen ni capital PAPER. El contrato
FOK/OTOCO y su coordinador conservan la validación final de riesgo y liquidez.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext

from app.lab.engine import validate_strategy
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_preview import MAX_ENTRY_NOTIONAL, MAX_EXIT_NOTIONAL
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote, guard_testnet_limit_price
from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument


@dataclass(frozen=True, slots=True)
class AutomaticEntryTerms:
    size: Decimal
    limit_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal


class GrvtBotConfigurationError(GrvtTestnetBracketError):
    """Mensaje local seguro que puede mostrarse sin revelar respuestas remotas."""


def automatic_entry_terms(definition: dict, instrument: GrvtTestnetInstrument,
        quote: GrvtTestnetQuote, *, entry_cap: Decimal, max_spread_bps: int,
        max_slippage_bps: int, now_ns: int, exit_cap: Decimal | None = None) -> AutomaticEntryTerms:
    """Toma ask/bid sin ampliar el precio; redondea cantidad siempre hacia abajo.

    Stop hacia la entrada para no aumentar el riesgo porcentual. Objetivo
    hacia fuera para no reducir el rendimiento exigido. Se rechazan bandas
    que el tick, el LAST o los mínimos hacen imposibles; nunca se corrigen
    silenciosamente aumentando capital o relajando el stop.
    """
    try:
        strategy = validate_strategy(definition)
        if (type(entry_cap) is not Decimal or not entry_cap.is_finite()
                or not 0 < entry_cap <= MAX_ENTRY_NOTIONAL
                or exit_cap is not None and (type(exit_cap) is not Decimal or not exit_cap.is_finite()
                    or not 0 < exit_cap <= MAX_EXIT_NOTIONAL)
                or quote.instrument != instrument.name):
            raise ValueError('invalid automatic entry scope')
        buying = strategy['direction'] == 'LONG'
        with localcontext() as context:
            context.prec = 100
            tick = instrument.tick_size
            unit = Decimal(1).scaleb(-instrument.base_decimals)
            price = ((quote.ask if buying else quote.bid) / tick).to_integral_value(
                rounding=ROUND_CEILING if buying else ROUND_FLOOR) * tick
            guard_testnet_limit_price('BUY' if buying else 'SELL', price, quote,
                max_spread_bps=max_spread_bps, max_slippage_bps=max_slippage_bps, now_ns=now_ns)
            size = (entry_cap / price).quantize(unit, rounding=ROUND_FLOOR)
            sign = 1 if buying else -1
            stop = (price * (1-sign*Decimal(strategy['stop_loss'])) / tick).to_integral_value(
                rounding=ROUND_CEILING if buying else ROUND_FLOOR) * tick
            target = (price * (1+sign*Decimal(strategy['take_profit'])) / tick).to_integral_value(
                rounding=ROUND_CEILING if buying else ROUND_FLOOR) * tick
            if (size < instrument.min_size or min(price, stop, target)*size < instrument.min_notional
                    or size*price > entry_cap
                    or not (stop < min(price, quote.last_price) <= max(price, quote.last_price) < target
                        if buying else target < min(price, quote.last_price) <= max(price, quote.last_price) < stop)):
                raise ValueError('unexecutable automatic terms')
            for trigger in (stop, target):
                multiplier = Decimal(1)+Decimal(max_slippage_bps)/10000*(-1 if buying else 1)
                limit = (trigger*multiplier/tick).to_integral_value(
                    rounding=ROUND_CEILING if buying else ROUND_FLOOR)*tick
                if (limit <= 0 or size*limit < instrument.min_notional
                        or exit_cap is not None and size*max(quote.last_price, trigger, limit) > exit_cap):
                    raise ValueError('exit minimum or budget')
            return AutomaticEntryTerms(size, price, stop, target)
    except Exception:
        raise GrvtTestnetBracketError('Tamaño o protección automática TESTNET no ejecutable') from None
