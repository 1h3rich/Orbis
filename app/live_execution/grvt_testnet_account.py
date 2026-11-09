"""Snapshot posterior GRVT testnet; evidencia, no liquidación conciliada."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

from app.live_execution.grvt_testnet_fills import GrvtTestnetFillAudit
from app.live_execution.grvt_testnet_funding import GrvtTestnetFundingAudit
from app.live_execution.grvt_testnet_transfers import GrvtTestnetTransferAudit
from app.live_execution.grvt_testnet_withdrawals import GrvtTestnetWithdrawalAudit
from app.live_execution.grvt_testnet_interest import GrvtTestnetInterestAudit
from app.live_execution.grvt_testnet_order import (
    GrvtTestnetOrderClient, GrvtTestnetOrderObservation,
)


_DECIMAL = re.compile(r"-?[0-9]{1,30}(?:\.[0-9]{1,18})?\Z")
_INSTRUMENT = re.compile(r"[A-Z0-9]+_[A-Z0-9]+_(?:Perp|Future)\Z")


class GrvtTestnetAccountError(RuntimeError):
    """Snapshot testnet ausente, antiguo o contradictorio."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetAccountSnapshot:
    sub_account_id: str
    event_time_ns: int
    settle_currency: str
    settle_balance: Decimal
    total_equity: Decimal
    initial_margin: Decimal
    maintenance_margin: Decimal
    positions: tuple[tuple[str, Decimal], ...]
    open_order_count: int
    complete_for_execution: bool = False
    margin_type: str | None = None
    sub_account_mode: str | None = None
    other_balances: tuple[tuple[str, Decimal], ...] | None = None
    unrealized_pnl: Decimal | None = None


def _number(raw: object, *, nonnegative: bool = False) -> Decimal:
    if not isinstance(raw, str) or not _DECIMAL.fullmatch(raw):
        raise GrvtTestnetAccountError("Importe de cuenta testnet no verificable")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise GrvtTestnetAccountError("Importe de cuenta testnet no verificable") from None
    if not value.is_finite() or nonnegative and value < 0:
        raise GrvtTestnetAccountError("Importe de cuenta testnet no verificable")
    return value


def audit_testnet_account(client: GrvtTestnetOrderClient,
                          order: GrvtTestnetOrderObservation,
                          fills: GrvtTestnetFillAudit, *,
                          now_ns: int,
                          funding: GrvtTestnetFundingAudit | None = None,
                          transfers: GrvtTestnetTransferAudit | None = None,
                          withdrawals: GrvtTestnetWithdrawalAudit | None = None,
                          interest: GrvtTestnetInterestAudit | None = None,
                          ) -> GrvtTestnetAccountSnapshot:
    """Lee el estado posterior sin atribuir sus cambios a una sola orden.

    Ni la ausencia de órdenes abiertas ni un saldo actual prueban que no hubo
    actividad externa o que el historial de fills sea completo.
    """
    if (type(now_ns) is not int or now_ns <= 0
            or not isinstance(order, GrvtTestnetOrderObservation)
            or not isinstance(order.update_time_ns, str)
            or not order.update_time_ns.isdecimal()
            or not 0 < int(order.update_time_ns) <= now_ns + 2_000_000_000
            or not isinstance(fills, GrvtTestnetFillAudit)
            or fills.client_order_id != order.client_order_id
            or fills.order_id != order.order_id):
        raise GrvtTestnetAccountError("Identidad testnet no verificable")
    try:
        account = client.read_testnet("account_summary", order.sub_account_id).get("result")
        orders = client.read_testnet("open_orders", order.sub_account_id).get("result")
    except Exception:
        raise GrvtTestnetAccountError("Cuenta testnet no verificable") from None
    if (not isinstance(account, dict)
            or account.get("sub_account_id") != order.sub_account_id
            or not isinstance(orders, list) or len(orders) > 500
            or not isinstance(account.get("positions"), list)
            or len(account["positions"]) > 100
            or not isinstance(account.get("spot_balances"), list)
            or len(account["spot_balances"]) > 100):
        raise GrvtTestnetAccountError("Cuenta testnet incompleta")
    event_raw = account.get("event_time")
    if not isinstance(event_raw, str) or not event_raw.isdecimal():
        raise GrvtTestnetAccountError("Hora de cuenta testnet inválida")
    event_ns = int(event_raw)
    latest_funding_ns = funding.latest_payment_time_ns if funding is not None else None
    latest_transfer_ns = (transfers.latest_transfer_time_ns
                          if transfers is not None else None)
    latest_withdrawal_ns = (withdrawals.latest_withdrawal_time_ns
                            if withdrawals is not None else None)
    latest_interest_ns = (interest.latest_payment_time_ns
                          if interest is not None else None)
    latest_external_event = max(int(order.update_time_ns),
                                fills.latest_any_fill_time_ns or 0,
                                latest_funding_ns or 0,
                                latest_transfer_ns or 0,
                                latest_withdrawal_ns or 0,
                                latest_interest_ns or 0)
    if (event_ns <= 0 or event_ns > now_ns + 2_000_000_000
            or event_ns < latest_external_event):
        raise GrvtTestnetAccountError("Cuenta testnet anterior a la última actividad")
    currency = account.get("settle_currency")
    if (not isinstance(currency, str)
            or not re.fullmatch(r"[A-Z0-9]{2,16}", currency)
            or currency != order.instrument.split("_")[1]):
        raise GrvtTestnetAccountError("Divisa testnet no verificable")
    balances: dict[str, Decimal] = {}
    for row in account["spot_balances"]:
        if (not isinstance(row, dict)
                or not isinstance(row.get("currency"), str)
                or not re.fullmatch(r"[A-Z0-9]{2,16}", row["currency"])
                or row["currency"] in balances):
            raise GrvtTestnetAccountError("Saldo testnet no verificable")
        balances[row["currency"]] = _number(row.get("balance"))
    if currency not in balances:
        raise GrvtTestnetAccountError("Saldo de liquidación testnet ausente")
    positions: dict[str, Decimal] = {}
    for row in account["positions"]:
        if (not isinstance(row, dict)
                or row.get("sub_account_id") != order.sub_account_id
                or not isinstance(row.get("instrument"), str)
                or not _INSTRUMENT.fullmatch(row["instrument"])
                or row["instrument"] in positions):
            raise GrvtTestnetAccountError("Posición testnet no verificable")
        positions[row["instrument"]] = _number(row.get("size"))
    for row in orders:
        state = row.get("state") if isinstance(row, dict) else None
        if (not isinstance(row, dict)
                or row.get("sub_account_id") != order.sub_account_id
                or not isinstance(state, dict)
                or state.get("status") not in ("PENDING", "OPEN")):
            raise GrvtTestnetAccountError("Órdenes abiertas testnet no verificables")
        if order.status in ("FILLED", "REJECTED", "CANCELLED") and (
                row.get("order_id") == order.order_id
                or (isinstance(row.get("metadata"), dict)
                    and row["metadata"].get("client_order_id") == order.client_order_id)):
            raise GrvtTestnetAccountError("Orden terminal aún figura abierta")
    return GrvtTestnetAccountSnapshot(
        order.sub_account_id, event_ns, currency, balances[currency],
        _number(account.get("total_equity")),
        _number(account.get("initial_margin"), nonnegative=True),
        _number(account.get("maintenance_margin"), nonnegative=True),
        tuple(sorted(positions.items())), len(orders),
        margin_type=account.get("margin_type") if isinstance(account.get("margin_type"), str) else None,
        sub_account_mode=account.get("sub_account_mode") if isinstance(account.get("sub_account_mode"), str) else None,
        other_balances=tuple(sorted((code, balance) for code, balance in balances.items() if code != currency)),
        unrealized_pnl=_number(account["unrealized_pnl"]) if "unrealized_pnl" in account else None,
    )
