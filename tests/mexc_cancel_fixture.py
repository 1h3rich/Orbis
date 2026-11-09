"""Proveedor controlado que puede mantener abierta y cancelar una compra LIMIT."""

from decimal import Decimal, localcontext
import hashlib
import hmac

from app.live_execution.mexc_live_order import (
    MexcCancelAcknowledgement, MexcOrderAcknowledgement, MexcOrderSendError,
)
from tests.test_mexc_execution import ExchangeOrders
from tests.test_mexc_execution_state import SOURCE


class ReplacementView:
    def __init__(self, venue, key, secret, *, same_uid):
        self._venue = venue
        self._ref = hmac.new(secret.encode(), key.encode(), hashlib.sha256).hexdigest()
        self._same_uid = same_uid

    def account_reference(self):
        return self._ref

    def account_uid(self):
        return self._venue.account_uid() if self._same_uid else "987654321"

    def __getattr__(self, method):
        return getattr(self._venue, method)


def controlled_view(venue, key, secret):
    if key in ("fixture-renewed-key", "fixture-renewed-key-2", "fixture-wrong-view"):
        return ReplacementView(venue, key, secret, same_uid=key!="fixture-wrong-view")
    return venue


class CancelableExchangeOrders(ExchangeOrders):
    def __init__(self, venue, clock):
        super().__init__(venue, clock)
        self.open_next = None
        self.cancellations = []
        self.lose_cancel_reply = False
        self.hide_order = False

    def place_limit_order(self, intent):
        mode = self.open_next
        if mode is None:
            return super().place_limit_order(intent)
        assert intent.side == "BUY" and Decimal(self.venue.locked) == 0
        identifier = intent.intent_id
        self.posts.append(identifier)
        order_id = "order-" + identifier
        executed = Decimal("0.0001") if mode == "partial" else Decimal(0)
        fee = Decimal("0.02") if executed else Decimal(0)
        self.open_next = None
        with localcontext() as context:
            context.prec = 300
            filled_quote = executed * intent.limit_price_quote
            locked = (intent.quantity_base - executed) * intent.limit_price_quote
            self.venue.base = str(Decimal(self.venue.base) + executed)
            self.venue.quote = str(Decimal(self.venue.quote) - filled_quote - fee - locked)
            self.venue.locked = str(locked)
        if executed:
            self.venue.trades.append({
                "id": "partial-" + identifier, "order_id": order_id, "side": intent.side,
                "time_ms": int(self.clock().timestamp() * 1000), "price": str(intent.limit_price_quote),
                "quantity": str(executed), "quote_quantity": str(filled_quote),
                "commission": str(fee), "commission_asset": "USDT",
            })
        client_id = identifier.replace("-", "")
        self.orders[client_id] = {
            **SOURCE, "symbol": intent.symbol, "client_order_id": client_id, "order_id": order_id,
            "type": "LIMIT", "side": intent.side, "price": str(intent.limit_price_quote),
            "quantity": str(intent.quantity_base), "status": "PARTIALLY_FILLED" if executed else "NEW",
            "executed_quantity": str(executed), "cumulative_quote": str(filled_quote),
        }
        if mode == "cancelled":
            self.finish(identifier)
        return MexcOrderAcknowledgement(identifier, client_id, order_id)

    def cancel_limit_order(self, intent):
        identifier = intent.intent_id
        client_id = identifier.replace("-", "")
        order = self.orders[client_id]
        assert order["status"] in ("NEW", "PARTIALLY_FILLED") and identifier not in self.cancellations
        self.cancellations.append(identifier)
        self.finish(intent.intent_id)
        if self.lose_cancel_reply:
            self.hide_order = True
            raise MexcOrderSendError("Resultado de cancelación MEXC incierto")
        return MexcCancelAcknowledgement(identifier, client_id, order["order_id"], order["status"])

    def finish(self, identifier):
        order = self.orders[identifier.replace("-", "")]
        assert order["status"] in ("NEW", "PARTIALLY_FILLED")
        order["status"] = "PARTIALLY_CANCELED" if Decimal(order["executed_quantity"]) else "CANCELED"
        with localcontext() as context:
            context.prec = 300
            self.venue.quote = str(Decimal(self.venue.quote) + Decimal(self.venue.locked))
            self.venue.locked = "0"

    def order_by_client_id(self, symbol, client_id):
        if self.hide_order:
            raise TimeoutError("Controlled MEXC read outage")
        return super().order_by_client_id(symbol, client_id)
