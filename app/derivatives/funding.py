"""Estimación PAPER de financiación a partir de liquidaciones públicas GRVT.

No es un cargo de la cuenta PAPER ni el libro de pagos del usuario en GRVT.
La API pública expresa `funding_rate` en puntos porcentuales; por ello se
divide entre 100 antes de multiplicar tamaño firmado y mark de liquidación.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from app.derivatives.paper_math import decimal_string


HOUR_NS = 3_600_000_000_000
MAX_WINDOW_NS = 366 * 24 * HOUR_NS


class FundingHistoryError(ValueError):
    pass


@dataclass(frozen=True)
class FundingRecord:
    instrument: str
    time_ns: int
    rate_percent: Decimal
    mark: Decimal
    interval_hours: int

    @classmethod
    def from_payload(cls, raw: dict, instrument: str) -> "FundingRecord":
        if not isinstance(raw, dict) or raw.get("instrument") != instrument:
            raise FundingHistoryError("Funding GRVT de otro instrumento")
        try:
            time_text = raw["funding_time"]
            if not isinstance(time_text, str) or not time_text.isdecimal():
                raise ValueError("Hora no textual")
            time_ns = int(time_text)
            rate = decimal_string(raw["funding_rate"], "funding_rate")
            mark = decimal_string(raw["mark_price"], "mark_price", positive=True)
            interval = raw["funding_interval_hours"]
        except (KeyError, ValueError, TypeError) as exc:
            raise FundingHistoryError("Funding GRVT malformado") from exc
        if (time_ns <= 0 or abs(rate) > 100 or type(interval) is not int
                or interval not in (1, 4, 8)):
            raise FundingHistoryError("Funding GRVT fuera de rango")
        return cls(instrument, time_ns, rate, mark, interval)


def estimate_funding(position: dict, records: list[FundingRecord], as_of_ns: int) -> dict:
    """Pago firmado: positivo = coste del LONG; negativo = cobro.

    Se exige una liquidación anterior a la apertura y continuidad de los
    intervalos conocidos. Si una apertura/cierre coincide exactamente con
    el timestamp de funding, no se presume un orden intratick.
    """
    opened = position["opened_at_ns"]
    closed = position["closed_at_ns"]
    end = closed if closed is not None else as_of_ns
    if (type(opened) is not int or type(end) is not int or opened >= end
            or end - opened > MAX_WINDOW_NS):
        raise FundingHistoryError("Periodo PAPER inválido o superior a 366 días")
    if not records or any(record.instrument != position["instrument"] for record in records):
        raise FundingHistoryError("Histórico público de financiación no disponible")
    ordered = sorted(records, key=lambda record: record.time_ns)
    if len({record.time_ns for record in ordered}) != len(ordered):
        raise FundingHistoryError("Liquidaciones GRVT duplicadas")
    if ordered[-1].time_ns > end:
        raise FundingHistoryError("Liquidación posterior al periodo PAPER")
    if any(record.time_ns in (opened, end) for record in ordered):
        raise FundingHistoryError("Apertura o cierre coinciden con una liquidación; orden desconocido")
    anchor_index = max((index for index, record in enumerate(ordered) if record.time_ns < opened), default=None)
    if anchor_index is None:
        raise FundingHistoryError("Falta la liquidación anterior a la apertura")
    relevant = ordered[anchor_index:]
    for earlier, later in zip(relevant, relevant[1:]):
        if later.time_ns - earlier.time_ns != later.interval_hours * HOUR_NS:
            raise FundingHistoryError("Hueco o cambio temporal no verificable en funding GRVT")
    if end >= relevant[-1].time_ns + relevant[-1].interval_hours * HOUR_NS:
        raise FundingHistoryError("Falta una liquidación esperada antes del final del periodo")
    signed_size = decimal_string(position["size"], "size", positive=True)
    if position["side"] == "SHORT":
        signed_size = -signed_size
    elif position["side"] != "LONG":
        raise FundingHistoryError("Dirección PAPER inválida")
    with localcontext() as context:
        context.prec = 80
        payments = []
        for record in relevant[1:]:
            payment = signed_size * record.mark * record.rate_percent / Decimal("100")
            payments.append({"funding_time_ns": record.time_ns,
                             "rate_percent": format(record.rate_percent, "f"),
                             "mark_price": format(record.mark, "f"),
                             "interval_hours": record.interval_hours,
                             "estimated_payment": format(payment, "f")})
        total = sum((Decimal(row["estimated_payment"]) for row in payments), Decimal("0"))
    return {"position_id": position["id"], "instrument": position["instrument"],
            "side": position["side"], "opened_at_ns": opened, "through_ns": end,
            "anchor_time_ns": relevant[0].time_ns, "estimated_funding_paid": format(total, "f"),
            "settlements": payments, "source": "grvt-public-funding-interval",
            "status": "HISTORICAL_ESTIMATE_NOT_BOOKED",
            "limitations": ["No modifica el saldo ni el P/L PAPER",
                            "No demuestra el pago del libro de cuenta GRVT ni la secuencia de liquidación",
                            "Cambios de intervalo posteriores al último dato pueden no ser detectables"]}
