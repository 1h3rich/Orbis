"""Servidor de prueba REAL con una orden MEXC final archivada, sin red externa."""

import os
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI

from app.live_execution.journal import begin_spot_submission, register_spot_policy, reserve_spot_intent
from app.live_execution.mexc_uid_guard import bind_uid_for_spot_submission
from app.live_execution.mexc_order_observer import ObservedMexcOrder, archive_observed_spot_order
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, LiveSpotState
from app.portfolio.api import create_real_router
from app.portfolio.domain import EventInput
from app.portfolio.repository import append_event, create_account
from app.portfolio.vault import RealVault
from app.web.mount import register_ui


PATH = Path(os.environ["ORBIS_REAL_VAULT_PATH"])
PASSWORD = "browser booking vault password"
NOW = datetime.now(timezone.utc) - timedelta(minutes=1)
API_KEY = "browser-booking-key"
SECRET_KEY = "browser-booking-secret"
ACCOUNT_REF = hmac.new(SECRET_KEY.encode(), API_KEY.encode(), hashlib.sha256).hexdigest()
ORDER_ID = "browser-verified-order"
FILL = {"id": "browser-verified-fill", "order_id": ORDER_ID, "side": "BUY",
        "time_ms": int(NOW.timestamp() * 1000), "price": "65000", "quantity": "0.01",
        "quote_quantity": "650", "commission": "0.05", "commission_asset": "USDT"}
INTENT_ID = None


class FakeMexc:
    def __init__(self, api_key, secret_key):
        if (api_key, secret_key) != (API_KEY, SECRET_KEY):
            raise ValueError("Credencial de prueba inválida")

    def account_reference(self):
        return ACCOUNT_REF

    def account_uid(self):
        return "123456789"

    def balances(self):
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": datetime.now(timezone.utc).isoformat(), "balances": [
                    {"asset": "BTC", "free": "0.01", "locked": "0", "total": "0.01"},
                    {"asset": "USDT", "free": "49.95", "locked": "0", "total": "49.95"},
                ]}

    def order_by_client_id(self, symbol, client_order_id):
        assert symbol == "BTCUSDT" and client_order_id == INTENT_ID.replace("-", "")
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "type": "LIMIT", "client_order_id": client_order_id,
                "symbol": symbol, "side": "BUY", "order_id": ORDER_ID,
                "price": "65000", "quantity": "0.01",
                "status": "FILLED", "executed_quantity": "0.01", "cumulative_quote": "650"}

    def trades_for_order(self, symbol, order_id):
        assert symbol == "BTCUSDT" and order_id == ORDER_ID
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "ORDER_ID_BELOW_PAGE_LIMIT", "symbol": symbol,
                "order_id": order_id, "trades": [FILL]}

    def trades_window(self, symbol, *, days):
        assert symbol == "BTCUSDT" and days in (1, 7, 30)
        observed_at = datetime.now(timezone.utc)
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT", "symbol": symbol,
                "limit": 1000, "pages": 1,
                "start_ms": int((observed_at - timedelta(days=days)).timestamp() * 1000),
                "end_ms": int(observed_at.timestamp() * 1000),
                "observed_at": observed_at.isoformat(), "trades": [FILL]}

    def transfers_recent(self, *, days):
        assert days in (1, 7, 30, 90)
        observed_at = datetime.now(timezone.utc)
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
                "start_ms": int((observed_at - timedelta(days=days)).timestamp() * 1000),
                "end_ms": int(observed_at.timestamp() * 1000),
                "observed_at": observed_at.isoformat(), "deposits": [], "withdrawals": []}


def seed() -> None:
    global INTENT_ID
    if PATH.exists():
        return
    vault = RealVault(PATH)
    vault.unlock(PASSWORD, create=True)
    with vault.session() as db:
        account = create_account(db, "MEXC prueba", "USDT")
        append_event(db, account.id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("700"),
            occurred_at=NOW - timedelta(days=1),
        ))
    policy = LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="browser-v1", account_ref=ACCOUNT_REF,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("1000"), max_daily_turnover_quote=Decimal("5000"),
        max_exposure_quote=Decimal("5000"), max_daily_loss_quote=Decimal("500"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=60, expires_at=NOW + timedelta(hours=1), enabled=True,
    )
    intent = LiveSpotIntent(
        intent_id=str(uuid4()), policy_id=policy.policy_id, strategy_version_id="browser-v1",
        account_ref=ACCOUNT_REF, symbol="BTCUSDT", side="BUY",
        quantity_base=Decimal("0.01"), limit_price_quote=Decimal("65000"),
        estimated_fee_quote=Decimal("0.65"), signal_at=NOW - timedelta(seconds=1),
    )
    INTENT_ID = intent.intent_id
    state = LiveSpotState(
        account_ref=ACCOUNT_REF, symbol="BTCUSDT", reconciled=True, stopped=False,
        account_verified=True, instrument_verified=True, observed_at=NOW, quote_at=NOW,
        best_bid=Decimal("64999"), best_ask=Decimal("65000"), free_base=Decimal("0"),
        free_quote=Decimal("1000"), pending_base=Decimal(0), pending_quote=Decimal(0),
        daily_turnover_quote=Decimal(0), pending_turnover_quote=Decimal(0),
        exposure_quote=Decimal(0), daily_realized_pnl_quote=Decimal(0),
    )
    register_spot_policy(vault, policy, now=NOW)
    bind_uid_for_spot_submission(vault, ACCOUNT_REF, "123456789", now=NOW)
    assert reserve_spot_intent(vault, intent, state, now=NOW).status == "RESERVED"
    begin_spot_submission(vault, intent, state, now=NOW)
    archive_observed_spot_order(vault, intent, ObservedMexcOrder(
        intent.intent_id, ORDER_ID, "FILLED", Decimal("0.01"),
        Decimal("650"), (FILL,), (("USDT", Decimal("0.05")),), True,
    ), now=NOW)
    vault.lock()


seed()
app = FastAPI()
app.include_router(create_real_router(PATH, mexc_factory=FakeMexc))
register_ui(app)
