"""Contabilidad Decimal de una posición perpetua aislada.

No es el motor de liquidación de GRVT: la exigencia de mantenimiento debe
proceder de las reglas vigentes del instrumento y la ejecución intravela
necesita un motor separado. ``margin_breached`` es una señal de riesgo, no una
orden ni la afirmación de que el exchange habría liquidado a ese precio.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


def _finite(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} debe ser Decimal finito")


@dataclass(frozen=True)
class IsolatedPerpPosition:
    instrument: str
    size: Decimal  # positivo LONG; negativo SHORT, unidades base
    entry_price: Decimal
    collateral: Decimal
    entry_fee: Decimal = Decimal("0")
    funding_paid: Decimal = Decimal("0")  # positivo = coste, negativo = cobro

    def __post_init__(self) -> None:
        if not self.instrument.endswith("_Perp"):
            raise ValueError("Se requiere un instrumento perpetuo explícito")
        for name in ("size", "entry_price", "collateral", "entry_fee", "funding_paid"):
            _finite(getattr(self, name), name)
        if self.size == 0 or self.entry_price <= 0 or self.collateral <= 0 or self.entry_fee < 0:
            raise ValueError("Posición, precio, colateral o comisión inválidos")
        if self.entry_fee > self.collateral:
            raise ValueError("La comisión supera el colateral")

    @property
    def side(self) -> str:
        return "LONG" if self.size > 0 else "SHORT"

    @property
    def entry_notional(self) -> Decimal:
        return abs(self.size) * self.entry_price

    def initial_margin(self, leverage: Decimal) -> Decimal:
        _finite(leverage, "leverage")
        if leverage < 1:
            raise ValueError("El apalancamiento debe ser al menos 1")
        return self.entry_notional / leverage

    def can_open(self, leverage: Decimal) -> bool:
        """Control local; el límite máximo de GRVT se valida aparte."""
        return self.collateral >= self.initial_margin(leverage) + self.entry_fee

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        _finite(mark_price, "mark_price")
        if mark_price <= 0:
            raise ValueError("El mark price debe ser positivo")
        return (mark_price - self.entry_price) * self.size

    def equity(self, mark_price: Decimal) -> Decimal:
        return self.collateral + self.unrealized_pnl(mark_price) - self.entry_fee - self.funding_paid

    def margin_breached(self, mark_price: Decimal, maintenance_requirement: Decimal) -> bool:
        """¿La equity ha caído hasta la exigencia externa de mantenimiento?"""
        _finite(maintenance_requirement, "maintenance_requirement")
        if maintenance_requirement < 0:
            raise ValueError("El mantenimiento no puede ser negativo")
        return self.equity(mark_price) <= maintenance_requirement

    def funding_payment(self, mark_price: Decimal, funding_rate: Decimal) -> Decimal:
        """Pago firmado al siguiente intervalo: LONG paga tasa positiva."""
        _finite(funding_rate, "funding_rate")
        self.unrealized_pnl(mark_price)  # valida el mark price
        return self.size * mark_price * funding_rate

    def with_funding(self, payment: Decimal) -> "IsolatedPerpPosition":
        _finite(payment, "payment")
        return IsolatedPerpPosition(
            instrument=self.instrument,
            size=self.size,
            entry_price=self.entry_price,
            collateral=self.collateral,
            entry_fee=self.entry_fee,
            funding_paid=self.funding_paid + payment,
        )
