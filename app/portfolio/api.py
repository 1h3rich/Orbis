"""API local REAL: libro, órdenes Spot confirmadas y vigilancia de parada."""

from __future__ import annotations

import secrets
import hashlib
import hmac
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from threading import RLock, Timer
from typing import Annotated, Callable, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.exact_json import exact_jsonable
from app.core.config import get_orbis_config
from app.integrations.mexc_linked_view import MexcLinkedView
from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError
from app.integrations.grvt_readonly import GrvtReadOnlyClient, GrvtReadOnlyError
from app.live_execution.reconciliation_view import PENDING_SPOT_STATUSES, list_spot_execution_intents
from app.live_execution.mexc_uid_guard import (
    MexcTransferRecoveryView, MexcUidGuardError, assert_known_mexc_uid, claim_mexc_uid_in_transaction,
    inspect_mexc_view_identity, save_mexc_view_binding, saved_mexc_view_scope,
)
from app.live_execution.mexc_trade_identity import (
    MexcTradeIdentityError, MexcTradeIdentityProbe, verify_same_mexc_account,
)
from app.live_execution.mexc_live_cancel import (
    MexcSelectedCancellation, cancel_selected_stopped_spot_order, observe_stopped_spot_limit,
)
from app.live_execution.mexc_order_observer import MexcOrderObservationError
from app.live_execution.grvt_testnet_recovery import (
    list_testnet_recovery_subaccounts, read_testnet_recovery,
)
from app.live_execution.grvt_testnet_reconciliation import (
    reconcile_testnet_order, GrvtTestnetReconciliationError)
from app.live_execution.grvt_testnet_journal import (
    GrvtTestnetJournalError, close_rejected_testnet_zero_fill,
    require_new_testnet_submission, stop_testnet_subaccount,
)
from app.live_execution.grvt_testnet_evidence import GrvtTestnetEvidenceError
from app.live_execution.grvt_testnet_preflight import (
    GrvtTestnetPreflightError, client_id_for_signal,
)
from app.live_execution.grvt_testnet_order import (
    GrvtTestnetLimits, GrvtTestnetOrderClient, GrvtTestnetOrderError, _validate_order,
)
from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskError
from app.live_execution.grvt_testnet_signal import (
    GrvtTestnetSignalError, submit_signal_testnet_once,
    sign_and_submit_signal_testnet_once, list_testnet_signal_candidates,
)
from app.live_execution.grvt_testnet_signer import GrvtTestnetSignError
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuoteError
from app.live_execution.grvt_testnet_submit import (
    audit_testnet_intent, audit_testnet_cancel, stop_and_cancel_testnet_once,
)
from app.derivatives.paper_store import PaperPerpStore, PaperPerpError
from app.live_execution.mexc_booking import book_observed_spot_order
from app.live_execution.mexc_settlement import MexcSettlementError, close_final_spot_reservation
from app.live_execution.mexc_order_lifecycle import MexcLifecycleError, synchronize_spot_order
from app.live_execution.mexc_execution import (
    MexcExecutionError, activate_spot_execution, review_anchored_spot_limit, submit_anchored_spot_limit,
)
from app.live_execution.mexc_execution_state import MexcExecutionStateError
from app.live_execution.mexc_execution_recovery import resume_spot_execution
from app.live_execution.mexc_trade_window_audit import observe_and_audit_spot_trade_window
from app.live_execution.mexc_transfer_window_audit import observe_and_audit_spot_transfers
from app.live_execution.journal import (
    LiveJournalError, _stop_spot_account_in_transaction, _stored_policy, recover_spot_intent,
    register_spot_policy, release_unsent_spot_intent, stop_spot_account, stop_spot_account_for_intent,
)
from app.live_execution.policy import MEXC_MANUAL_ORIGIN, LiveSpotIntent, LiveSpotPolicy
from app.live_execution.mexc_instrument import (
    MexcInstrumentError, fetch_spot_rules, preflight_limit_order,
)
from app.live_execution.mexc_live_observation import (
    MexcLiveObservationError, fetch_live_quote, fetch_live_evidence, value_spot_account,
)
from app.live_execution.mexc_test_order import MexcOrderTestError, MexcOrderTestResult, MexcSpotOrderTestClient
from app.live_execution.mexc_live_order import MexcOrderSendError, MexcSpotLiveClient
from app.live_execution.mexc_stop_supervisor import MexcStopSupervisor, MexcStopSupervisorError
from app.live_execution.mexc_bot_service import MexcBotService
from app.live_execution.mexc_bot_supervisor import MexcBotSupervisor
from app.database.database import get_db
from app.portfolio.domain import EventInput, PortfolioError, decimal_value, realized_pnl_by_currency
from app.portfolio.daily_book_risk import calculate_daily_book_risk
from app.portfolio.mexc_cost_provenance import audit_mexc_cost_provenance
from app.portfolio.csv_import import MAX_CSV_CHARS, parse_real_events_csv
from app.portfolio.models import RealAccount, RealEvent, RealMexcAccountLink, RealMexcParityObservation, RealMexcOpenOrdersObservation, RealMexcOrderTestAttempt, RealLiveSpotIntent, RealLiveSpotPolicy, RealLiveStop, GrvtTestnetOrderIntent, GrvtTestnetCancelAttempt
from app.portfolio.mexc_order_test_archive import (
    MexcOrderTestArchiveError, claim_attempt, complete_attempt,
    list_attempts as list_order_test_attempts, read_attempt,
)
from app.portfolio.mexc_observations import (
    archived_window_coverage, list_observations, read_observations, save_observation,
)
from app.portfolio.mexc_transfers import (
    compare_transfer_snapshots, list_transfer_snapshots, read_transfer_snapshot,
    save_transfer_snapshot, find_mexc_deposit_by_evm_tx_hash,
    find_mexc_withdrawal_by_evm_tx_hash,
)
from app.portfolio.mexc_transfer_comparison import compare_mexc_transfer_snapshot
from app.portfolio.mexc_transfer_audit import audit_mexc_transfer_archive
from app.portfolio.mexc_transfer_matches import confirm_mexc_transfer_match
from app.portfolio.mexc_external_deposits import external_deposit_review, external_deposit_selection
from app.live_execution.mexc_withdrawal_transport import MexcWithdrawalClient
from app.portfolio.mexc_opening import prepare_mexc_opening
from app.portfolio.grvt_evidence import save_evidence, list_evidence, read_evidence
from app.portfolio.repository import (
    RequestConflict,
    account_book_revision,
    account_snapshot,
    account_snapshot_with_events,
    append_event,
    create_account,
    import_events,
    list_events,
    transfer_asset_between_accounts,
    transfer_cash_between_accounts,
    verify_account_event_digests,
)
from app.portfolio.vault import RealVault, VaultError
from app.live_execution.dex_operator import DexOperatorSession
from app.live_execution.dex_operator_drafts import DexOperatorDrafts
from app.live_execution.dex_operator_connection import DexOperatorConnection
from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.portfolio.reconciliation import compare_mexc_spot_inventory
from app.portfolio.mexc_parity import assess_mexc_spot_parity
from app.portfolio.mexc_open_orders_survey import summarize_open_orders
from app.portfolio.mexc_open_orders_archive import (
    MexcOpenOrdersArchiveError, add_observation as add_open_orders_observation,
    compare_observations as compare_open_orders_observations,
    list_observations as list_open_orders_observations,
    read_observation as read_open_orders_observation,
)
from app.portfolio.mexc_parity_archive import (
    MexcParityArchiveError, add_observation as add_parity_observation,
    list_observations as list_parity_observations, read_observation as read_parity_observation,
)
from app.portfolio.valuation import ReferencePrice, value_snapshot
from app.portfolio.valuation_history import MAX_VALUATION_DAYS, value_history
from app.portfolio.wallet_proof import (
    WalletChallenge, WalletProofError, canonical_chain, canonical_wallet_address,
    challenge_message, verify_eoa_signature,
)
from app.portfolio.wallet_links import (
    inspect_wallet_link, list_wallet_links, save_wallet_link, remove_wallet_link,
)
from app.pricing.providers import get_configured_price_with_meta
from app.pricing.mexc_preview import MexcPreviewError, preview_spot_valuation
from app.lab.store import LabStore
from app.lab.engine import LabError, validate_strategy


LOCAL_HOSTS = frozenset(("127.0.0.1", "::1", "localhost", "testserver", "testclient"))
IDLE_SECONDS = 900
MANAGED_BACKUP_NAME = re.compile(
    r"(?:real|pre-restore|pre-migrate-v[1-9][0-9]*)-[A-Za-z0-9-]+\.db"
)


class VaultPassphrase(BaseModel):
    passphrase: str = Field(min_length=12)


class MexcConnect(BaseModel):
    api_key: SecretStr
    secret_key: SecretStr


class MexcWindow(BaseModel):
    days: Literal[1, 7, 30] = 7


class MexcTransferWindow(BaseModel):
    days: Literal[1, 7, 30, 90] = 7


class MexcTransferMatchRequest(BaseModel):
    snapshot_id: UUID
    direction: Literal["deposit", "withdrawal"]
    source_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_id: UUID
    confirmation: str


class MexcExternalDepositSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: UUID
    source_refs: list[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] = Field(min_length=1, max_length=20)


class MexcExternalDepositCommit(MexcExternalDepositSelection):
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["REGISTRAR INGRESOS EXTERNOS"]


class MexcSpotLimits(BaseModel):
    request_id: UUID
    symbol: str = Field(min_length=3, max_length=30)
    base_asset: str = Field(min_length=2, max_length=16)
    quote_asset: str = Field(min_length=2, max_length=16)
    max_order_quote: str
    max_daily_turnover_quote: str
    max_exposure_quote: str
    max_daily_loss_quote: str
    max_fee_bps: str
    max_slippage_bps: str
    max_data_age_seconds: int = Field(ge=1, le=300)
    expires_at: datetime


class MexcPolicyDraft(MexcSpotLimits):
    strategy_version_id: str = Field(min_length=1, max_length=128)


class MexcManualLimits(MexcSpotLimits):
    model_config = ConfigDict(extra="forbid")


class MexcDraftOrderTest(BaseModel):
    request_id: UUID
    quantity_base: str = Field(min_length=1, max_length=80)
    limit_price_quote: str = Field(min_length=1, max_length=80)
    confirmation: Literal["PROBAR_SIN_EJECUTAR"]


class MexcActivateExecution(BaseModel):
    request_id: UUID
    anchor_id: UUID
    confirmation: Literal["ACTIVAR_ORDENES_REALES"]


class MexcResumeExecution(MexcActivateExecution):
    confirmation: Literal["REABRIR_OPERATIVA_REAL"]


class MexcReleaseUnsent(BaseModel):
    account_id: UUID
    confirmation: Literal["LIBERAR_RESERVA_NO_ENVIADA"]


class MexcLimitTerms(BaseModel):
    side: Literal["BUY", "SELL"]
    quantity_base: str = Field(min_length=1, max_length=80)
    limit_price_quote: str = Field(min_length=1, max_length=80)


class MexcOrderReview(MexcLimitTerms):
    model_config = ConfigDict(extra="forbid")


class MexcOrderBudgetReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["BUY", "SELL"]
    budget_quote: str = Field(min_length=1, max_length=80)


class MexcOrderMaximumSellReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["SELL"]
    size_mode: Literal["MAX_AVAILABLE"]


class MexcLiveLimitOrder(MexcLimitTerms):
    request_id: UUID
    max_spend_quote: str | None = Field(default=None, min_length=1, max_length=80)
    confirmation: Literal["ENVIAR_ORDEN_REAL"]


class MexcAccountLinkConfirm(BaseModel):
    confirmation: Literal["VINCULAR"]


class MexcRenewViewConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["RENOVAR_VIEW_Y_DETENER"]


class MexcParitySave(BaseModel):
    request_id: UUID


class MexcOpenOrdersSave(BaseModel):
    request_id: UUID


class MexcOpeningRequest(BaseModel):
    quote_currency: str = Field(min_length=2, max_length=12)
    costs: dict[str, str] = Field(default_factory=dict, max_length=200)
    costs_confirmed: Literal[True]


class MexcOpeningCommit(MexcOpeningRequest):
    observed_at: datetime
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["IMPORTAR_SALDO_INICIAL"]


class GrvtConnect(BaseModel):
    api_key: SecretStr


class GrvtPerpReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    session_id: str = Field(min_length=36, max_length=36)
    sub_account_id: str = Field(min_length=1, max_length=20, pattern=r'^[1-9][0-9]*$')


class GrvtOrderRef(BaseModel):
    sub_account_id: str = Field(min_length=1, max_length=30, pattern=r"^[0-9]+$")
    client_order_id: str = Field(min_length=1, max_length=20, pattern=r"^[0-9]+$")


class BackupRestore(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    confirmation: Literal["RESTAURAR"]


class MexcBookOrder(BaseModel):
    account_id: str
    confirmation: Literal["IMPORTAR"]


class MexcCloseReservation(BaseModel):
    confirmation: Literal["CERRAR"]


class MexcSynchronizeOrder(BaseModel):
    account_id: UUID


class MexcRecoverWithRenewedView(MexcSynchronizeOrder):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["RECUPERAR_CON_VIEW_RENOVADA"]


class MexcCancelSelectedOrder(MexcSynchronizeOrder):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["CANCELAR_ORDEN_MEXC"]


class StopAllOperational(BaseModel):
    confirmation:Literal["DETENER_Y_BLOQUEAR"]


class GrvtCloseRejectedZeroFill(BaseModel):
    sub_account_id: str = Field(min_length=1, max_length=30, pattern=r"^[0-9]+$")
    client_order_id: str = Field(min_length=19, max_length=20, pattern=r"^[0-9]+$")
    confirmation: Literal["CERRAR_RECHAZO_TESTNET"]


class GrvtTestnetSignalSubmit(BaseModel):
    api_key: SecretStr
    session_id: str = Field(min_length=1, max_length=64)
    action_request_id: str = Field(min_length=1, max_length=64)
    sub_account_id: str = Field(min_length=1, max_length=30, pattern=r"^[0-9]+$")
    instrument: str = Field(min_length=3, max_length=80)
    max_order_notional: str = Field(pattern=r"^[0-9]{1,6}(?:\.[0-9]{1,6})?$")
    order: dict
    confirmation: Literal["ENVIAR_GRVT_TESTNET"]

    model_config = {"extra": "forbid"}


class GrvtTestnetSignalSignAndSubmit(BaseModel):
    api_key: SecretStr
    dedicated_private_key: SecretStr
    expected_signer: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    builder: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    session_id: str = Field(min_length=1, max_length=64)
    action_request_id: str = Field(min_length=1, max_length=64)
    sub_account_id: str = Field(min_length=1, max_length=30, pattern=r"^[0-9]+$")
    instrument: str = Field(min_length=3, max_length=80)
    max_order_notional: str = Field(pattern=r"^[0-9]{1,6}(?:\.[0-9]{1,6})?$")
    side: Literal["BUY", "SELL"]
    size: str = Field(pattern=r"^[0-9]{1,30}(?:\.[0-9]{1,18})?$")
    limit_price: str = Field(pattern=r"^[0-9]{1,30}(?:\.[0-9]{1,18})?$")
    max_spread_bps: int = Field(strict=True, ge=0, le=100)
    max_slippage_bps: int = Field(strict=True, ge=0, le=100)
    reduce_only: bool
    confirmation: Literal["FIRMAR_Y_ENVIAR_GRVT_TESTNET"]

    model_config = {"extra": "forbid"}


class GrvtTestnetStopRequest(BaseModel):
    confirmation: Literal["DETENER_GRVT_TESTNET"]


class GrvtTestnetOrderAuditRequest(BaseModel):
    api_key: SecretStr
    sub_account_id: str = Field(min_length=1, max_length=30, pattern=r"^[0-9]+$")
    client_order_id: str = Field(min_length=19, max_length=20, pattern=r"^[0-9]+$")
    confirmation: Literal["AUDITAR_GRVT_TESTNET"]

    model_config = {"extra": "forbid"}


class GrvtTestnetStopCancelRequest(GrvtTestnetOrderAuditRequest):
    confirmation: Literal["DETENER_Y_CANCELAR_GRVT_TESTNET"]


class GrvtTestnetReconcileRequest(BaseModel):
    sub_account_id: str = Field(min_length=1, max_length=30, pattern=r"^[0-9]+$")
    client_order_id: str = Field(min_length=19, max_length=20, pattern=r"^[0-9]+$")
    confirmation: Literal["CONCILIAR_GRVT_TESTNET"]
    model_config = {"extra":"forbid"}


class MexcStopAccount(BaseModel):
    confirmation: Literal["DETENER"]


class MexcSuperviseStop(BaseModel):
    confirmation: Literal["VIGILAR_PARADA"]


class MexcStopSupervision(BaseModel):
    confirmation: Literal["DETENER_VIGILANCIA"]


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_currency: str = "EUR"


class WalletProofStart(BaseModel):
    address: str = Field(min_length=42, max_length=42)
    chain_id: str = Field(min_length=3, max_length=18)


class WalletProofConfirm(WalletProofStart):
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(min_length=132, max_length=132)


class WalletLinkCreate(WalletProofStart):
    alias: str = Field(min_length=1, max_length=80)
    confirmation: Literal["VINCULAR"]


class WalletLinkRemove(BaseModel):
    confirmation: Literal["DESVINCULAR"]


class EventCreate(BaseModel):
    request_id: str
    kind: Literal["CONTRIBUTION", "WITHDRAWAL", "BUY", "SELL", "TRANSFER", "ADJUSTMENT_IN", "ADJUSTMENT_OUT"]
    quote_currency: str
    occurred_at: datetime
    asset: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None
    fee: str = "0"
    fee_asset: str | None = None
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("quantity", "unit_price", "amount", "fee")
    @classmethod
    def exact_decimal_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 100:
            raise ValueError("Financial values must be decimal strings")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Invalid decimal value") from exc
        if not parsed.is_finite() or abs(parsed.as_tuple().exponent) > 100:
            raise ValueError("Financial values must be finite")
        return value


class AccountTransferCreate(BaseModel):
    request_id: str
    target_account_id: str
    asset: str
    quote_currency: str
    quantity: str
    fee: str = "0"
    occurred_at: datetime
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("quantity", "fee")
    @classmethod
    def exact_quantity(cls, value: str) -> str:
        if not isinstance(value, str) or len(value) > 100:
            raise ValueError("Financial values must be decimal strings")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Invalid decimal value") from exc
        if not parsed.is_finite() or abs(parsed.as_tuple().exponent) > 100:
            raise ValueError("Financial values must be finite")
        return value


class AccountTransferCommit(AccountTransferCreate):
    expected_cost_basis: str

    @field_validator("expected_cost_basis")
    @classmethod
    def exact_cost(cls, value: str) -> str:
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError("Transferred cost must be decimal text")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Transferred cost is invalid") from exc
        if (not parsed.is_finite() or parsed < 0
                or len(parsed.as_tuple().digits) > 100
                or abs(parsed.as_tuple().exponent) > 100):
            raise ValueError("Transferred cost is invalid")
        return value


class AccountCashTransferCreate(BaseModel):
    request_id: str
    target_account_id: str
    currency: str
    amount: str
    occurred_at: datetime
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("amount")
    @classmethod
    def exact_amount(cls, value: str) -> str:
        return AccountTransferCreate.exact_quantity(value)


class AccountCashTransferCommit(AccountCashTransferCreate):
    expected_source_balance: str

    @field_validator("expected_source_balance")
    @classmethod
    def exact_balance(cls, value: str) -> str:
        return AccountTransferCreate.exact_quantity(value)


class CsvImportPreview(BaseModel):
    csv_text: str = Field(min_length=1, max_length=MAX_CSV_CHARS)


class CsvImportCommit(CsvImportPreview):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["IMPORTAR"]


def _event_payload(event) -> dict:
    item = event.input
    result = {
        "id": event.id,
        "account_id": event.account_id,
        "request_id": event.request_id,
        "kind": item.kind,
        "asset": item.asset,
        "quote_currency": item.quote_currency,
        "quantity": str(item.quantity) if item.quantity is not None else None,
        "unit_price": str(item.unit_price) if item.unit_price is not None else None,
        "gross_amount": str(item.gross_amount),
        "fee": str(item.fee),
        "fee_asset": item.effective_fee_asset,
        "note": item.note,
        "occurred_at": item.occurred_at.isoformat(),
        "recorded_at": event.recorded_at.isoformat(),
    }
    if event.accounting is not None:
        context = event.accounting
        result['accounting'] = {
            'occurred_at': context.effective_input.occurred_at.isoformat(),
            'gross_amount': str(context.effective_input.gross_amount), 'ordinal': context.ordinal,
            'acceptance_ids': list(context.acceptance_ids), 'accepted_prefix': context.accepted_prefix,
            'source_order_record_id': context.source_order_record_id,
            'transfer_basis_derived': context.transfer_basis_derived,
        }
    return result


class RealAccess:
    def __init__(self, path: Path, *, lab_path: Path | None = None,
                 mexc_factory=MexcReadOnlyClient,
                 grvt_factory=GrvtReadOnlyClient, mexc_quote_get=None,
                 mexc_trade_factory=MexcTradeIdentityProbe,
                 mexc_order_test_factory=MexcSpotOrderTestClient,
                 mexc_rules_fetch=fetch_spot_rules,
                 mexc_live_factory=MexcSpotLiveClient,
                 mexc_stop_supervisor_factory=MexcStopSupervisor,
                 spot_paper=None, mexc_bot_supervisor_factory=MexcBotSupervisor, mexc_bot_clock=None,dca_paper=None,
                 dex_bot_supervisor_factory=DexBotSupervisor):
        self.vault = RealVault(path)
        self._dex_operator = DexOperatorSession(self.vault)
        self._dex_operator_drafts = DexOperatorDrafts(self.vault)
        self._dex_connection = DexOperatorConnection(self.vault,self._dex_operator_drafts,self._dex_operator)
        self._dex_bots = dex_bot_supervisor_factory(self.vault,self._dex_connection)
        self._lab = LabStore(lab_path, allow_create=False) if lab_path is not None else None
        self._mexc_factory = mexc_factory
        self._mexc_quote_get = mexc_quote_get
        self._mexc: MexcReadOnlyClient | MexcLinkedView | None = None
        self._mexc_account_ref: str | None = None
        self._mexc_uid: str | None = None
        self._mexc_trade_factory = mexc_trade_factory
        self._mexc_order_test_factory = mexc_order_test_factory
        self._mexc_rules_fetch = mexc_rules_fetch
        self._mexc_trade: MexcTradeIdentityProbe | None = None
        self._mexc_trade_credentials: tuple[str, str] | None = None
        self._mexc_live_factory = mexc_live_factory
        self._mexc_stop_supervisor_factory = mexc_stop_supervisor_factory
        self._mexc_stop_supervisor: MexcStopSupervisor | None = None
        self._mexc_stop_account_ref: str | None = None
        self._mexc_bots = MexcBotService(self.vault, spot_paper, quote_get=mexc_quote_get,
            rules_fetch=lambda symbol, now: self._mexc_rules_fetch(symbol, now=now),
            supervisor_factory=mexc_bot_supervisor_factory, clock=mexc_bot_clock,dca_paper=dca_paper)
        self._mexc_opening_key = secrets.token_bytes(32)
        self._mexc_opening_receipts: dict[tuple[str, str, str], tuple[MexcOpeningCommit, dict]] = {}
        self._wallet_proof_key = secrets.token_bytes(32)
        self._grvt_oco_preview_key = secrets.token_bytes(32)
        self._grvt_closes = None
        self._grvt_bots = None
        self._grvt_mainnet = None
        self._grvt_mainnet_bots = None
        self._wallet_challenges: dict[str, WalletChallenge] = {}
        self._wallet_verified: set[tuple[str, str, str]] = set()
        self._grvt_factory = grvt_factory
        self._grvt: GrvtReadOnlyClient | None = None
        self._token: str | None = None
        self._vault_generation: int | None = None
        self._deadline = 0.0
        self._timer: Timer | None = None
        self._lock = RLock()
        # Un cierre automático no consume el aviso antes del siguiente clic.
        # Es diagnóstico conservador del proceso, no una reparación del archivo.
        self._review_required = False
        self._mexc_bots.on_operational_idle = self._schedule_operational_cleanup
        self._dex_bots.on_operational_idle = self._schedule_operational_cleanup

    def _schedule_operational_cleanup(self):
        # El callback del trabajador nunca espera al cerrojo HTTP: STOP puede
        # estar esperando precisamente que esa hebra termine.
        timer = Timer(0,self._close_unused_operational_vault)
        timer.daemon = True
        timer.start()

    def _close_unused_operational_vault(self):
        with self._lock:
            if self._token is None and self.vault.unlocked and not self.operational_status()["active"]:
                self.lock()

    def operational_status(self):
        mexc=self._mexc_bots.operational_status()
        grvt=self._grvt_mainnet_bots.operational_status() if self._grvt_mainnet_bots is not None else {'active':False,'count':0,'until':None}
        dex=self._dex_bots.operational_status()
        return {'active':mexc['active'] or grvt['active'] or dex['active'],'count':mexc['count']+grvt['count']+dex['count'],
            'until':min((item['until'] for item in (mexc,grvt,dex) if item['active']),default=None)}

    def _protect_dex_session(self):
        self._dex_bots.stop_session_workers()
        if not self._dex_bots.operational_status()['active']:
            self._dex_connection.revoke()
            self._dex_operator.revoke()
            self._dex_operator_drafts.revoke()

    def _expire(self) -> None:
        with self._lock:
            if self._token is not None and time.monotonic() >= self._deadline:
                self.protect_view()

    def unlock(self, passphrase: str, *, create: bool = False) -> str:
        with self._lock:
            self._expire()
            self._protect_dex_session()
            if self._grvt_mainnet is not None: self._grvt_mainnet.revoke_all()
            if self.vault.unlocked and not create:
                if self.operational_status()["active"]:
                    # Reautenticar la vista no renueva la concesión ni sustituye
                    # la sesión criptográfica que usa el bot autorizado.
                    probe = RealVault(self.vault.path)
                    probe.unlock(passphrase)
                    probe.lock()
                    self._token = secrets.token_urlsafe(32)
                    self._vault_generation = self.vault.session_generation
                    self._touch()
                    return self._token
                # Una pestaña nueva puede reautenticarse sin conservar el
                # token anterior en localStorage. El token previo se revoca.
                self.lock()
            self.vault.last_migration_backup = None
            self.vault.unlock(passphrase, create=create)
            self._vault_generation = self.vault.session_generation
            self._token = secrets.token_urlsafe(32)
            self._touch()
            return self._token

    def _touch(self) -> None:
        self._deadline = time.monotonic() + IDLE_SECONDS
        self._mexc_bots.extend_deadline(self._deadline)
        if self._timer is not None:
            self._timer.cancel()
        self._timer = Timer(IDLE_SECONDS, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def require(self, token: str | None, *, touch: bool = True) -> None:
        with self._lock:
            self._expire()
            if self._token is None or token is None or not secrets.compare_digest(self._token, token):
                raise HTTPException(status_code=401, detail="Real vault is locked")
            if touch:
                self._touch()

    def protect_view(self) -> dict:
        with self._lock:
            self._protect_dex_session()
            if self._grvt_mainnet_bots is not None:self._grvt_mainnet_bots.stop_session_jobs()
            if self._grvt_mainnet is not None: self._grvt_mainnet.revoke_all()
            if self._grvt_bots is not None: self._grvt_bots.stop_all('VIEW_PROTECTED')
            if self._grvt_closes is not None: self._grvt_closes.stop_all('VIEW_PROTECTED')
            if not self.operational_status()["active"]:
                review = self.lock()
                return {"protected":True,"operating":False,**({"review_required":True} if review else {})}
            try:
                self._mexc_bots.stop_session_workers()
            except (LiveJournalError,VaultError):
                self.lock()
                return {"protected":True,"operating":False,"review_required":True}
            if self._timer is not None: self._timer.cancel(); self._timer = None
            if self._mexc_stop_supervisor is not None: self._mexc_stop_supervisor.request_stop()
            self._mexc_stop_supervisor = None; self._mexc_stop_account_ref = None
            self._token = None; self._vault_generation = None
            self._mexc = None; self._mexc_account_ref = self._mexc_uid = None
            self._mexc_trade = None; self._mexc_trade_credentials = None
            self._grvt = None
            self._mexc_opening_key = secrets.token_bytes(32)
            self._mexc_opening_receipts.clear()
            self._wallet_proof_key = secrets.token_bytes(32)
            self._grvt_oco_preview_key = secrets.token_bytes(32)
            self._wallet_challenges.clear(); self._wallet_verified.clear()
            return {"protected":True,"operating":True}

    def lock(self) -> bool:
        with self._lock:
            self._dex_bots.shutdown()
            self._dex_connection.revoke()
            self._dex_operator.revoke()
            self._dex_operator_drafts.revoke()
            if self._grvt_mainnet_bots is not None:self._grvt_mainnet_bots.stop_all()
            if self._grvt_mainnet is not None: self._grvt_mainnet.revoke_all()
            if self._grvt_bots is not None: self._grvt_bots.stop_all('VAULT_LOCKED')
            if self._grvt_closes is not None: self._grvt_closes.stop_all('VAULT_LOCKED')
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            worker = self._mexc_stop_supervisor
            if worker is not None:
                worker.request_stop()
            review_required = self._review_required
            try:
                self._mexc_bots.stop_all("VAULT_LOCKED")
            except (LiveJournalError,VaultError):
                # Los permisos ya se han revocado antes de intentar guardar
                # la pausa. Un archivo corrupto no puede impedir borrar claves.
                review_required = True
                self._review_required = True
            finally:
                self._token = None
                self._vault_generation = None
                self._mexc = None
                self._mexc_account_ref = None
                self._mexc_uid = None
                self._mexc_trade = None
                self._mexc_trade_credentials = None
                self._mexc_opening_key = secrets.token_bytes(32)
                self._mexc_opening_receipts.clear()
                self._wallet_proof_key = secrets.token_bytes(32)
                self._grvt_oco_preview_key = secrets.token_bytes(32)
                self._wallet_challenges.clear()
                self._wallet_verified.clear()
                self._grvt = None
                # Espera llamadas externas ya iniciadas. Después de lock no
                # puede comenzar otro ciclo de la sesión revocada.
                self.vault.lock()
                if worker is not None:
                    worker.join(1)
                self._mexc_stop_supervisor = None
                self._mexc_stop_account_ref = None
            return review_required

    def wallet_proof_start(self, token: str | None, account_id: str,
                           address: str, chain_id: str, origin: str) -> dict:
        with self._lock:
            self.require(token)
            address = canonical_wallet_address(address)
            chain_id = canonical_chain(chain_id)
            with self.vault.session() as db:
                if db.get(RealAccount, account_id) is None:
                    raise PortfolioError("Real account not found")
            now = datetime.now(timezone.utc)
            generation = self.vault.session_generation
            address_digest = hmac.new(self._wallet_proof_key, address.encode("ascii"),
                                      hashlib.sha256).hexdigest()
            self._wallet_challenges = {
                key: value for key, value in self._wallet_challenges.items()
                if (value.expires_at > now and value.session_generation == generation
                    and not (value.account_id == account_id and value.address_digest == address_digest))
            }
            if len(self._wallet_challenges) >= 8:
                raise WalletProofError("Hay demasiadas comprobaciones de wallet pendientes")
            nonce = secrets.token_hex(32)
            expires_at = (now + timedelta(minutes=2)).replace(microsecond=0)
            message = challenge_message(
                origin=origin, account_id=account_id, chain_id=chain_id,
                address=address, nonce=nonce, expires_at=expires_at,
            )
            self._wallet_challenges[nonce] = WalletChallenge(
                nonce, account_id, chain_id, address_digest, origin, expires_at, generation,
            )
            return {"nonce": nonce, "message": message,
                    "expires_at": expires_at.isoformat(), "purpose": "SESSION_EOA_CONTROL_ONLY"}

    def wallet_proof_confirm(self, token: str | None, account_id: str,
                             nonce: str, address: str, chain_id: str,
                             signature: str) -> dict:
        with self._lock:
            self.require(token)
            challenge = self._wallet_challenges.pop(nonce, None)
            if challenge is None:
                raise WalletProofError("Desafío de wallet inexistente o ya utilizado")
            address = canonical_wallet_address(address)
            digest = hmac.new(self._wallet_proof_key, address.encode("ascii"),
                              hashlib.sha256).hexdigest()
            if (challenge.account_id != account_id
                    or not hmac.compare_digest(challenge.address_digest, digest)
                    or challenge.chain_id != canonical_chain(chain_id)
                    or challenge.session_generation != self.vault.session_generation
                    or datetime.now(timezone.utc) >= challenge.expires_at):
                raise WalletProofError("La firma no corresponde al desafío de esta sesión")
            message = challenge_message(
                origin=challenge.origin, account_id=challenge.account_id,
                chain_id=challenge.chain_id, address=address,
                nonce=challenge.nonce, expires_at=challenge.expires_at,
            )
            if not verify_eoa_signature(message, address, signature):
                raise WalletProofError("La firma no corresponde al desafío de esta sesión")
            self._wallet_verified.add((account_id, challenge.chain_id, digest))
            return {"verified": True, "scope": "THIS_UNLOCKED_SESSION_ONLY",
                    "wallet_type": "EOA_EIP191", "spending_authorized": False,
                    "balances_reconciled": False}

    def wallet_link_save(self, token: str | None, account_id: str,
                         address: str, chain_id: str, alias: str) -> dict:
        with self._lock:
            self.require(token)
            address = canonical_wallet_address(address)
            chain_id = canonical_chain(chain_id)
            digest = hmac.new(self._wallet_proof_key, address.encode("ascii"), hashlib.sha256).hexdigest()
            if (account_id, chain_id, digest) not in self._wallet_verified:
                raise WalletProofError("Verifica de nuevo el control de esta wallet antes de vincularla")
            with self.vault.session() as db:
                saved = save_wallet_link(db, account_id, address, chain_id, alias)
                db.commit()
                return saved

    def wallet_link_inspect(self, token: str | None, account_id: str,
                            address: str, chain_id: str) -> dict:
        with self._lock:
            self.require(token)
            address = canonical_wallet_address(address)
            chain_id = canonical_chain(chain_id)
            digest = hmac.new(self._wallet_proof_key, address.encode("ascii"), hashlib.sha256).hexdigest()
            if (account_id, chain_id, digest) not in self._wallet_verified:
                raise WalletProofError("Verifica el control de esta wallet en la sesión actual")
            with self.vault.session() as db:
                return inspect_wallet_link(db, account_id, address, chain_id)

    def wallet_link_remove(self, token: str | None, account_id: str, link_id: str) -> None:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                remove_wallet_link(db, account_id, link_id)
                db.commit()
            self._wallet_verified = {item for item in self._wallet_verified if item[0] != account_id}

    def _halt_mexc_stop_supervisor(self, *, account_id: str | None = None,
                                   bot_reason: str = "CONNECTION_OR_MODE_CHANGED") -> None:
        self._mexc_bots.stop_all(bot_reason, account_id=account_id)
        worker = self._mexc_stop_supervisor
        if worker is None:
            return
        worker.request_stop()
        if not worker.join(5):
            raise HTTPException(status_code=409,
                                detail="Esperando a que termine la vigilancia MEXC")
        self._mexc_stop_supervisor = None
        self._mexc_stop_account_ref = None

    def mexc_status(self, token: str | None) -> dict:
        with self._lock:
            self.require(token)
            return {"connected": self._mexc is not None, "mode": "READ_ONLY",
                    "credential_storage": "MEMORY_ONLY"}

    def mexc_trade_status(self, token: str | None) -> dict:
        with self._lock:
            self.require(token)
            return {"verified": self._mexc_trade is not None,
                    "mode": "GET_ONLY_VERIFICATION", "execution_enabled": False,
                    "verification_scope": "AT_CONNECTION_ONLY",
                    "credential_storage": "MEMORY_ONLY"}

    @staticmethod
    def _mexc_policy_draft_payload(policy: LiveSpotPolicy) -> dict:
        return {"id": policy.policy_id,
                "strategy_version_id": None if policy.manual_only else policy.strategy_version_id,
                **({"manual_only": True} if policy.manual_only else {}),
                "symbol": policy.symbol,
                "base_asset": policy.base_asset, "quote_asset": policy.quote_asset,
                "max_order_quote": str(policy.max_order_quote),
                "max_daily_turnover_quote": str(policy.max_daily_turnover_quote),
                "max_exposure_quote": str(policy.max_exposure_quote),
                "max_daily_loss_quote": str(policy.max_daily_loss_quote),
                "max_fee_bps": str(policy.max_fee_bps),
                "max_slippage_bps": str(policy.max_slippage_bps),
                "max_data_age_seconds": policy.max_data_age_seconds,
                "expires_at": policy.expires_at.isoformat(),
                "enabled": False, "execution_enabled": False,
                "status": "DRAFT_NOT_ACTIVATED"}

    def _mexc_strategy_version(self, identifier: str, *, for_bot_draft: bool = False) -> dict:
        native = dca = None
        if self._lab is not None:
            try: native = self._lab.version(identifier)
            except LabError: pass
        if self._mexc_bots.dca_source is not None:
            try: dca = self._mexc_bots.dca_source.store.version(identifier)
            except LabError: pass
        if native is not None and dca is not None: raise LabError("Identificador de versión ambiguo")
        if dca is not None:
            from app.automation.dca import dca_signal_window_key, validate_dca
            rule = validate_dca(dca["definition"])
            if dca_signal_window_key(rule) and not for_bot_draft:
                raise HTTPException(status_code=422, detail="La condición DCA requiere activar un bot MEXC desde PAPER")
            return {**dca,"kind":"DCA"}
        if native is None: raise LabError("Versión LONG/DCA no disponible")
        validate_strategy(native["definition"])
        return native

    def create_mexc_policy_draft(self, token: str | None, account_id: str,
                                 body: MexcPolicyDraft | MexcManualLimits) -> dict:
        """Fija límites inactivos. El ámbito manual se asigna en el servidor."""
        with self._lock:
            self.require(token)
            if isinstance(body, MexcPolicyDraft) and self._lab is None:
                raise HTTPException(status_code=409, detail="LAB no está configurado")
            if self._mexc is None or self._mexc_account_ref is None:
                raise HTTPException(status_code=409, detail="Conecta primero MEXC View")
            try:
                account_ref = self._mexc.account_reference()
                uid = self._mexc.account_uid()
                if account_ref != self._mexc_account_ref or not assert_known_mexc_uid(
                        self.vault, account_ref, uid):
                    raise HTTPException(status_code=409,
                                        detail="La identidad MEXC View no coincide con la bóveda")
            except (MexcReadOnlyError, MexcUidGuardError):
                raise HTTPException(status_code=409,
                                    detail="No se pudo revalidar la identidad MEXC View") from None
            self.require(token)
            with self.vault.session() as db:
                linked = db.scalar(select(RealMexcAccountLink).where(
                    RealMexcAccountLink.account_ref == account_ref,
                    RealMexcAccountLink.account_id == account_id))
                if db.get(RealAccount, account_id) is None or linked is None:
                    raise HTTPException(status_code=409,
                                        detail="Esta cuenta REAL no está vinculada a MEXC View")
            version_id = MEXC_MANUAL_ORIGIN
            if isinstance(body, MexcPolicyDraft):
                if body.strategy_version_id == MEXC_MANUAL_ORIGIN:
                    raise HTTPException(status_code=422, detail="Elige una versión LONG o DCA")
                try:
                    version = self._mexc_strategy_version(body.strategy_version_id, for_bot_draft=True)
                except (LabError, KeyError, ValueError):
                    raise HTTPException(status_code=409,
                                        detail="Versión LAB no verificable") from None
                if version.get("kind") != "DCA" and validate_strategy(version["definition"])["direction"] != "LONG":
                    raise HTTPException(status_code=422,
                                        detail="El borrador MEXC Spot requiere estrategia LONG")
                version_id = version["id"]
            try:
                policy = LiveSpotPolicy(
                    policy_id=str(body.request_id), strategy_version_id=version_id,
                    account_ref=account_ref, symbol=body.symbol,
                    base_asset=body.base_asset, quote_asset=body.quote_asset,
                    max_order_quote=Decimal(body.max_order_quote),
                    max_daily_turnover_quote=Decimal(body.max_daily_turnover_quote),
                    max_exposure_quote=Decimal(body.max_exposure_quote),
                    max_daily_loss_quote=Decimal(body.max_daily_loss_quote),
                    max_fee_bps=Decimal(body.max_fee_bps),
                    max_slippage_bps=Decimal(body.max_slippage_bps),
                    max_data_age_seconds=body.max_data_age_seconds,
                    expires_at=body.expires_at, enabled=False,
                )
            except (ValueError, InvalidOperation):
                raise HTTPException(status_code=422,
                                    detail="Límites o ámbito de política inválidos") from None
            if policy.expires_at <= datetime.now(timezone.utc):
                raise HTTPException(status_code=422,
                                    detail="La caducidad debe ser futura")
            self.require(token)
            try:
                register_spot_policy(self.vault, policy, now=datetime.now(timezone.utc))
            except LiveJournalError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            return self._mexc_policy_draft_payload(policy)

    def list_mexc_policy_drafts(self, token: str | None, account_id: str, *,
                                manual_only: bool = False) -> list[dict]:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                refs = select(RealMexcAccountLink.account_ref).where(
                    RealMexcAccountLink.account_id == account_id)
                origin = RealLiveSpotPolicy.strategy_version_id
                scope = origin == MEXC_MANUAL_ORIGIN if manual_only else origin != MEXC_MANUAL_ORIGIN
                rows = db.scalars(select(RealLiveSpotPolicy).where(
                    RealLiveSpotPolicy.account_ref.in_(refs), scope).order_by(
                    RealLiveSpotPolicy.created_at.desc(), RealLiveSpotPolicy.id.desc()).limit(100)).all()
                try:
                    return [self._mexc_policy_draft_payload(policy)
                            for row in rows if not (policy := _stored_policy(db, row.id)).enabled]
                except LiveJournalError:
                    raise HTTPException(status_code=409,
                                        detail="Archivo de políticas REAL no verificable") from None

    def test_mexc_policy_draft_buy(self, token: str | None, account_id: str,
                                   policy_id: str, body: MexcDraftOrderTest) -> dict:
        """Ensayo MEXC sin reserva, señal LAB, asiento REAL ni orden ejecutada."""
        with self._lock:
            self.require(token)
            try:
                with self.vault.session() as db:
                    policy = _stored_policy(db, policy_id)
                    linked = db.scalar(select(RealMexcAccountLink).where(
                        RealMexcAccountLink.account_ref == policy.account_ref,
                        RealMexcAccountLink.account_id == account_id))
                    stop = db.get(RealLiveStop, policy.account_ref)
                    if linked is None or db.get(RealAccount, account_id) is None:
                        raise HTTPException(status_code=409, detail="Borrador ajeno a esta cuenta REAL")
                    if stop is not None and stop.stopped:
                        raise HTTPException(status_code=409, detail="Cuenta MEXC detenida")
                    previous = db.get(RealMexcOrderTestAttempt, str(body.request_id))
                    if previous is not None:
                        archived = read_attempt(previous)
                        try:
                            quantity = Decimal(body.quantity_base)
                            price = Decimal(body.limit_price_quote)
                        except InvalidOperation:
                            raise HTTPException(status_code=422, detail="Cantidad o precio inválido") from None
                        if (archived["account_id"] != account_id
                                or archived["policy_id"] != policy.policy_id
                                or not quantity.is_finite() or not price.is_finite()
                                or Decimal(archived["quantity_base"]) != quantity
                                or Decimal(archived["limit_price_quote"]) != price):
                            raise HTTPException(status_code=409,
                                                detail="ID de prueba reutilizado con otros parámetros")
                        return {**archived, "replayed_without_post": True}
            except LiveJournalError:
                raise HTTPException(status_code=409, detail="Borrador MEXC no verificable") from None
            except MexcOrderTestArchiveError:
                raise HTTPException(status_code=409, detail="Archivo de prueba MEXC no verificable") from None
            if self._mexc is None or self._mexc_trade is None or self._mexc_trade_credentials is None:
                raise HTTPException(status_code=409, detail="Conecta MEXC View y verifica Trade")
            now = datetime.now(timezone.utc)
            if policy.enabled or now >= policy.expires_at:
                raise HTTPException(status_code=409, detail="El borrador no está vigente y desactivado")
            if self._lab is None:
                raise HTTPException(status_code=409, detail="LAB no está configurado")
            try:
                version = self._mexc_strategy_version(policy.strategy_version_id)
                if version.get("kind") != "DCA" and validate_strategy(version["definition"])["direction"] != "LONG":
                    raise ValueError("direction")
            except (LabError, KeyError, ValueError):
                raise HTTPException(status_code=409, detail="Versión LAB no verificable") from None
            try:
                view_ref = self._mexc.account_reference()
                view_uid = self._mexc.account_uid()
                if view_ref != policy.account_ref or not assert_known_mexc_uid(
                        self.vault, view_ref, view_uid):
                    raise HTTPException(status_code=409, detail="MEXC View no coincide con el borrador")
                if not verify_same_mexc_account(view_uid, self._mexc_trade):
                    raise HTTPException(status_code=409, detail="MEXC Trade no coincide con View")
            except (MexcReadOnlyError, MexcUidGuardError, MexcTradeIdentityError):
                raise HTTPException(status_code=409, detail="No se pudo revalidar MEXC View/Trade") from None
            self.require(token)
            try:
                rules = self._mexc_rules_fetch(policy.symbol, now=now)
                options = {"http_get": self._mexc_quote_get} if self._mexc_quote_get is not None else {}
                _, ask, last, price_at = fetch_live_quote(
                    policy.symbol, now=now, max_age_seconds=policy.max_data_age_seconds, **options)
                quantity = Decimal(body.quantity_base)
                price = Decimal(body.limit_price_quote)
                with localcontext() as context:
                    context.prec = 300
                    estimated_fee = quantity * price * rules.fee_rate_upper
                intent = LiveSpotIntent(
                    intent_id=str(body.request_id), policy_id=policy.policy_id,
                    strategy_version_id=policy.strategy_version_id,
                    account_ref=policy.account_ref, symbol=policy.symbol, side="BUY",
                    quantity_base=quantity, limit_price_quote=price,
                    estimated_fee_quote=estimated_fee, signal_at=now)
                dispatch_at = datetime.now(timezone.utc)
                if dispatch_at >= policy.expires_at:
                    raise HTTPException(status_code=409, detail="El borrador caducó durante la prueba")
                preflight = preflight_limit_order(policy, intent, rules, last_price=last,
                                                 last_price_at=price_at, now=dispatch_at)
                if not preflight.allowed:
                    raise HTTPException(status_code=422, detail="Preflight MEXC: " + preflight.reason)
                if price > ask * (Decimal(1) + policy.max_slippage_bps / Decimal(10000)):
                    raise HTTPException(status_code=422, detail="Precio fuera del límite de deslizamiento")
            except (InvalidOperation, ValueError, MexcInstrumentError,
                    MexcLiveObservationError, MexcOrderTestError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            self.require(token)
            try:
                with self.vault.session() as db:
                    archived, created = claim_attempt(db, account_id, policy, intent,
                                                      now=datetime.now(timezone.utc))
            except MexcOrderTestArchiveError:
                raise HTTPException(status_code=409, detail="Ensayo MEXC no archivable") from None
            if not created:
                return {**archived, "replayed_without_post": True}
            try:
                tester = self._mexc_order_test_factory(*self._mexc_trade_credentials)
                tested = tester.test_draft_buy(policy, intent, preflight)
                if (not isinstance(tested, MexcOrderTestResult)
                        or tested.intent_id != intent.intent_id
                        or tested.client_order_id != UUID(intent.intent_id).hex
                        or tested.accepted_by_test_endpoint is not True
                        or tested.executed is not False):
                    raise MexcOrderTestError("MEXC no confirmó la prueba de orden")
            except MexcOrderTestError as exc:
                with self.vault.session() as db:
                    complete_attempt(db, intent.intent_id, "NOT_CONFIRMED",
                                     now=datetime.now(timezone.utc))
                raise HTTPException(status_code=422, detail=str(exc)) from None
            except Exception:
                with self.vault.session() as db:
                    complete_attempt(db, intent.intent_id, "NOT_CONFIRMED",
                                     now=datetime.now(timezone.utc))
                raise HTTPException(status_code=422,
                                    detail="No se pudo confirmar la prueba MEXC") from None
            with self.vault.session() as db:
                result = complete_attempt(db, intent.intent_id, "ACCEPTED",
                                          now=datetime.now(timezone.utc))
            self.require(token)
            return {**result, "replayed_without_post": False}

    @staticmethod
    def _mexc_active_policy_payload(policy: LiveSpotPolicy, *, stopped: bool = False,
                                     reactivation_required: bool = False) -> dict:
        expired = datetime.now(timezone.utc) >= policy.expires_at
        return {**RealAccess._mexc_policy_draft_payload(policy),
                "enabled": True, "execution_enabled": not (expired or stopped or reactivation_required),
                "status": "STOPPED" if stopped else "REACTIVATION_REQUIRED" if reactivation_required else
                          "EXPIRED" if expired else "MANUAL_REAL_ACTIVE",
                "anchor_id": policy.reconciliation_anchor_id,
                "account_stopped": stopped,
                "mode": "MANUAL", "bot_active": False,
                "allow_risk_reducing_exits": policy.allow_risk_reducing_exits}

    def _mexc_execution_clients(self, token: str | None, account_id: str, policy_id: str):
        self.require(token)
        if (self._mexc is None or self._mexc_trade is None
                or self._mexc_trade_credentials is None):
            raise HTTPException(status_code=409, detail="Conecta MEXC View y verifica Trade")
        with self.vault.session() as db:
            policy = _stored_policy(db, policy_id)
            link = db.get(RealMexcAccountLink, policy.account_ref)
            if (link is None or link.account_id != account_id
                    or self._mexc_account_ref != policy.account_ref):
                raise HTTPException(status_code=409, detail="La política no corresponde a esta cuenta MEXC")
        return policy, self._mexc, self._mexc_live_factory(*self._mexc_trade_credentials)

    def _require_mexc_execution_origin(self, policy: LiveSpotPolicy) -> None:
        if policy.manual_only:
            return
        if self._lab is None:
            raise HTTPException(status_code=409, detail="LAB no está configurado")
        try:
            version = self._mexc_strategy_version(policy.strategy_version_id)
            if version.get("kind") != "DCA" and validate_strategy(version["definition"])["direction"] != "LONG":
                raise LabError("LONG required")
        except (LabError, KeyError, ValueError):
            raise HTTPException(status_code=409, detail="La versión LAB LONG no es verificable") from None

    def activate_mexc_execution(self, token: str | None, account_id: str,
                                 policy_id: str, body: MexcActivateExecution) -> dict:
        with self._lock:
            draft, view, trade = self._mexc_execution_clients(token, account_id, policy_id)
            with self.vault.session() as db:
                anchor = db.get(RealMexcParityObservation, str(body.anchor_id))
                if anchor is None or anchor.account_id != account_id:
                    raise HTTPException(status_code=409, detail="La apertura no pertenece a esta cuenta REAL")
            self._require_mexc_execution_origin(draft)
            active = activate_spot_execution(
                self.vault, view, trade, policy_id, str(body.anchor_id), str(body.request_id),
                quote_get=self._mexc_quote_get,
                rules_fetch=lambda symbol, now: self._mexc_rules_fetch(symbol, now=now))
            self.require(token)
            return self._mexc_active_policy_payload(active)

    def list_mexc_execution_policies(self, token: str | None, account_id: str) -> list[dict]:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                refs = select(RealMexcAccountLink.account_ref).where(RealMexcAccountLink.account_id == account_id)
                rows = db.scalars(select(RealLiveSpotPolicy).where(
                    RealLiveSpotPolicy.account_ref.in_(refs)).order_by(
                        RealLiveSpotPolicy.created_at.desc()).limit(100)).all()
                result = []
                from app.portfolio.models import RealSpotBot
                bots = db.scalars(select(RealSpotBot).where(RealSpotBot.account_ref.in_(refs))).all()
                from app.live_execution.spot_bot_store import bot_authorizations, bot_record
                bound_policies = {item["policy_id"] for bot in bots
                                  for item in bot_authorizations(bot_record(db, bot.id)[1])}
                bot_accounts = {bot.account_ref for bot in bots if bot.status == "ACTIVE"}
                for row in rows:
                    policy = _stored_policy(db, row.id)
                    if not policy.enabled or policy.reconciliation_anchor_id is None:
                        continue
                    stop = db.get(RealLiveStop, policy.account_ref)
                    payload = self._mexc_active_policy_payload(
                        policy, stopped=bool(stop and stop.stopped),
                        reactivation_required=bool(stop and not stop.stopped and row.created_at < stop.updated_at))
                    if policy.policy_id in bound_policies or policy.account_ref in bot_accounts:
                        payload.update(execution_enabled=False, bot_active=policy.account_ref in bot_accounts,
                                       mode="BOT" if policy.policy_id in bound_policies else "MANUAL",
                                       status="BOT_POLICY" if policy.policy_id in bound_policies else "BOT_ACCOUNT_RESERVED")
                    result.append(payload)
                return result

    @staticmethod
    def _mexc_execution_state(db: Session, ref: str) -> dict:
        stop = db.get(RealLiveStop, ref)
        pending = db.scalar(select(func.count()).select_from(RealLiveSpotIntent).where(
            RealLiveSpotIntent.account_ref == ref,
            RealLiveSpotIntent.status.in_(PENDING_SPOT_STATUSES)))
        stopped = bool(stop and stop.stopped)
        from app.portfolio.models import RealSpotBot
        bot_active = db.scalar(select(RealSpotBot.id).where(
            RealSpotBot.account_ref == ref, RealSpotBot.status == "ACTIVE").limit(1)) is not None
        from app.portfolio.mexc_withdrawal_journal import pending_withdrawals, WithdrawalError
        try:
            withdrawal_pending = pending_withdrawals(db, ref, datetime.now(timezone.utc))
        except WithdrawalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"linked": True, "stopped": stopped, "pending_orders": pending,
                "withdrawal_pending": withdrawal_pending, "bot_active": bot_active,
                "can_review_resume": stopped and pending == 0 and not withdrawal_pending,
                "exchange_orders_cancelled": False}

    def mexc_execution_status(self, token: str | None, account_id: str) -> dict:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                links = db.scalars(select(RealMexcAccountLink).where(
                    RealMexcAccountLink.account_id == account_id)).all()
                if len(links) != 1:
                    return {"linked": False, "stopped": False, "pending_orders": 0, "can_review_resume": False}
                return self._mexc_execution_state(db, links[0].account_ref)

    def mexc_workflow(self, token: str | None, account_id: str) -> dict:
        """Passive local guidance for the selected book. Never contacts the venue."""
        from app.portfolio.mexc_workflow import workflow_actions

        with self._lock:
            self.require(token, touch=False)
            with self.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN")
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                links = db.scalars(select(RealMexcAccountLink).where(
                    RealMexcAccountLink.account_id == account_id).limit(2)).all()
                actions = {}
                if len(links) == 1:
                    try:
                        actions = workflow_actions(db, account_id, links[0].account_ref, now=datetime.now(timezone.utc))
                    except PortfolioError as exc:
                        raise HTTPException(status_code=409, detail=str(exc), headers={"Cache-Control": "no-store"}) from None
                ref = self._mexc_account_ref
                if self._mexc is None or ref is None:
                    return {"view": "DISCONNECTED", **actions}
                link = db.get(RealMexcAccountLink, ref)
                if link is not None and link.account_id != account_id:
                    return {"view": "OTHER_ACCOUNT", **actions}
                if any(item.account_ref != ref for item in links):
                    return {"view": "DIFFERENT_CONNECTION", **actions}
                book_empty = db.scalar(select(RealEvent.id).where(
                    RealEvent.account_id == account_id).limit(1)) is None
                if link is None:
                    return {"view": "UNLINKED", "book_empty": book_empty}
                state = self._mexc_execution_state(db, ref)
                return {"view": "LINKED", "book_empty": book_empty,
                        **({"view_renewed": True} if isinstance(self._mexc, MexcLinkedView) else {}),
                        "trade_verified": self._mexc_trade is not None,
                        "stopped": state["stopped"], "pending_orders": bool(state["pending_orders"]),
                        "withdrawal_pending": state["withdrawal_pending"], "bot_active": state["bot_active"], **actions}

    def mexc_pending_orders(self, token: str | None, account_id: str) -> dict:
        with self._lock:
            self.require(token, touch=False)
            with self.vault.session() as db:
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                refs = db.scalars(select(RealMexcAccountLink.account_ref).where(
                    RealMexcAccountLink.account_id == account_id).limit(2)).all()
            if len(refs) != 1:
                return {"view_ready": False, "trade_ready": False, "orders": []}
            ref = refs[0]
            view_ready = self._mexc is not None and self._mexc_account_ref == ref
            return {"view_ready": view_ready, "trade_ready": view_ready and self._mexc_trade is not None,
                    "orders": list_spot_execution_intents(self.vault, pending_link=(account_id, ref))}

    def stop_mexc_execution(self, token: str | None, account_id: str) -> dict:
        with self._lock:
            self.require(token)
            self._mexc_bots.stop_all("ACCOUNT_STOP", account_id=account_id)
            with self.vault.session() as db:
                refs = db.scalars(select(RealMexcAccountLink.account_ref).where(
                    RealMexcAccountLink.account_id == account_id)).all()
                if len(refs) != 1:
                    raise HTTPException(status_code=409, detail="La cuenta requiere un vínculo MEXC único")
            released = stop_spot_account(self.vault, refs[0], now=datetime.now(timezone.utc))
            return {**self.mexc_execution_status(token, account_id), "released_unsent": released}

    def release_mexc_unsent(self, token: str | None, account_id: str, intent_id: str) -> dict:
        with self._lock:
            self.require(token)
            return release_unsent_spot_intent(self.vault, intent_id,
                                               now=datetime.now(timezone.utc), account_id=account_id)

    def resume_mexc_execution(self, token: str | None, account_id: str,
                               policy_id: str, body: MexcResumeExecution) -> dict:
        with self._lock:
            draft, view, trade = self._mexc_execution_clients(token, account_id, policy_id)
            with self.vault.session() as db:
                anchor = db.get(RealMexcParityObservation, str(body.anchor_id))
                if anchor is None or anchor.account_id != account_id:
                    raise HTTPException(status_code=409, detail="La apertura no pertenece a esta cuenta REAL")
            self._require_mexc_execution_origin(draft)
            self._halt_mexc_stop_supervisor()
            policy = resume_spot_execution(
                self.vault, view, trade, policy_id, str(body.anchor_id), str(body.request_id),
                quote_get=self._mexc_quote_get,
                rules_fetch=lambda symbol, now: self._mexc_rules_fetch(symbol, now=now))
            self.require(token)
            return self._mexc_active_policy_payload(policy)

    def review_mexc_execution_order(self, token: str | None, account_id: str,
                                    policy_id: str, body: MexcOrderReview | MexcOrderBudgetReview | MexcOrderMaximumSellReview) -> dict:
        max_available_sell = isinstance(body, MexcOrderMaximumSellReview)
        try:
            budget = Decimal(body.budget_quote) if isinstance(body, MexcOrderBudgetReview) else None
            quantity = Decimal(body.quantity_base) if isinstance(body, MexcOrderReview) else None
            price = Decimal(body.limit_price_quote) if isinstance(body, MexcOrderReview) else None
        except InvalidOperation:
            raise HTTPException(status_code=422, detail="Importe, cantidad o precio no decimal") from None
        with self._lock:
            policy, view, trade = self._mexc_execution_clients(token, account_id, policy_id)
            trade_probe = self._mexc_trade

        def current():
            with self._lock:
                self.require(token, touch=False)
                if (self._mexc is not view or self._mexc_account_ref != policy.account_ref
                        or self._mexc_trade is not trade_probe):
                    raise MexcExecutionError("La conexión MEXC cambió durante la revisión")

        return review_anchored_spot_limit(
            self.vault, view, trade, policy, side=body.side,
            quantity_base=quantity, limit_price_quote=price, budget_quote=budget,
            max_available_sell=max_available_sell, quote_get=self._mexc_quote_get,
            rules_fetch=lambda symbol, now: self._mexc_rules_fetch(symbol, now=now),
            assert_active=current)

    def submit_mexc_execution_order(self, token: str | None, account_id: str,
                                    policy_id: str, body: MexcLiveLimitOrder) -> dict:
        with self._lock:
            _policy, view, trade = self._mexc_execution_clients(token, account_id, policy_id)
            try:
                quantity, price = Decimal(body.quantity_base), Decimal(body.limit_price_quote)
                cap = Decimal(body.max_spend_quote) if body.max_spend_quote is not None else None
            except InvalidOperation:
                raise HTTPException(status_code=422, detail="Importe, cantidad o precio no decimal") from None
            return submit_anchored_spot_limit(
                self.vault, view, trade, policy_id, str(body.request_id), side=body.side,
                quantity_base=quantity, limit_price_quote=price, max_spend_quote=cap,
                quote_get=self._mexc_quote_get,
                rules_fetch=lambda symbol, now: self._mexc_rules_fetch(symbol, now=now),
                assert_active=lambda: self.require(token))

    def list_mexc_policy_order_tests(self, token: str | None, account_id: str,
                                     policy_id: str) -> list[dict]:
        with self._lock:
            self.require(token)
            try:
                with self.vault.session() as db:
                    policy = _stored_policy(db, policy_id)
                    linked = db.scalar(select(RealMexcAccountLink).where(
                        RealMexcAccountLink.account_ref == policy.account_ref,
                        RealMexcAccountLink.account_id == account_id))
                    if linked is None or db.get(RealAccount, account_id) is None:
                        raise HTTPException(status_code=409, detail="Borrador ajeno a esta cuenta REAL")
                    return list_order_test_attempts(db, account_id, policy_id=policy_id)
            except (LiveJournalError, MexcOrderTestArchiveError):
                raise HTTPException(status_code=409, detail="Archivo de pruebas MEXC no verificable") from None

    def mexc_policy_daily_book_risk(self, token: str | None, account_id: str,
                                    policy_id: str) -> dict:
        """Parte contable del riesgo; nunca estima fills externos ausentes."""
        with self._lock:
            self.require(token)
            try:
                with self.vault.session() as db:
                    # SQLite en modo legacy no abre necesariamente una
                    # transacción DBAPI por cada SELECT. Fijar aquí el mismo
                    # snapshot para integridad, eventos, revisión y origen.
                    db.connection().exec_driver_sql("BEGIN")
                    policy = _stored_policy(db, policy_id)
                    now = datetime.now(timezone.utc)
                    linked = db.scalar(select(RealMexcAccountLink).where(
                        RealMexcAccountLink.account_ref == policy.account_ref,
                        RealMexcAccountLink.account_id == account_id))
                    if linked is None or db.get(RealAccount, account_id) is None:
                        raise HTTPException(status_code=409, detail="Borrador ajeno a esta cuenta REAL")
                    verify_account_event_digests(db, account_id)
                    events = list_events(db, account_id)
                    book_revision = self._book_revision(db, account_id)
                    provenance = audit_mexc_cost_provenance(
                        db, account_id, policy.account_ref, policy.base_asset,
                        policy.quote_asset, now=now,
                    )
            except (LiveJournalError, PortfolioError):
                raise HTTPException(status_code=409, detail="Borrador MEXC no verificable") from None
            try:
                risk = calculate_daily_book_risk(account_id, events, policy.quote_asset,
                                                 now=now, base_asset=policy.base_asset)
            except PortfolioError:
                raise HTTPException(status_code=409, detail="Libro REAL no verificable") from None
            with localcontext() as context:
                context.prec = 300
                remaining_book_turnover = max(
                    Decimal(0), policy.max_daily_turnover_quote - risk.turnover_quote,
                )
            return {"account_id": account_id, "policy_id": policy.policy_id,
                    "book_revision": book_revision,
                    "quote_asset": risk.quote_currency,
                    "day_start": risk.day_start.isoformat(),
                    "calculated_at": risk.calculated_at.isoformat(),
                    "trade_count": risk.trade_count,
                    "turnover_quote": format(risk.turnover_quote, "f"),
                    "realized_pnl_quote": format(risk.realized_pnl_quote, "f"),
                    "base_asset": risk.base_asset,
                    "position_quantity_base": format(risk.position_quantity_base, "f"),
                    "position_cost_basis_quote": format(risk.position_cost_basis_quote, "f"),
                    "average_cost_quote_per_base": (format(risk.average_cost_quote_per_base, "f")
                                                    if risk.average_cost_quote_per_base is not None else None),
                    "cost_provenance": provenance.status,
                    "verified_fill_count": provenance.verified_fill_count,
                    "max_daily_turnover_quote": format(policy.max_daily_turnover_quote, "f"),
                    "max_daily_loss_quote": format(policy.max_daily_loss_quote, "f"),
                    "remaining_book_turnover_quote": format(remaining_book_turnover, "f"),
                    "book_turnover_limit_reached": risk.turnover_quote >= policy.max_daily_turnover_quote,
                    "book_realized_loss_limit_reached": risk.realized_pnl_quote <= -policy.max_daily_loss_quote,
                    "source": risk.source,
                    "reconciled_with_venue": False, "execution_ready": False,
                    "missing": ["VENUE_FILL_COVERAGE", "UNREALIZED_PNL", "OTHER_PAIR_RISK"]}

    def mexc_policy_spot_exposure(self, token: str | None, account_id: str,
                                  policy_id: str) -> dict:
        """Valora todos los saldos Spot observados; no certifica el libro ni habilita Trade."""
        with self._lock:
            self.require(token)
            view, account_ref = self._mexc, self._mexc_account_ref
            if view is None or account_ref is None:
                raise HTTPException(status_code=409, detail="Conecta MEXC View primero")
            try:
                with self.vault.session() as db:
                    db.connection().exec_driver_sql("BEGIN")
                    policy = _stored_policy(db, policy_id)
                    link = db.get(RealMexcAccountLink, policy.account_ref)
                    if policy.account_ref != account_ref or link is None or link.account_id != account_id:
                        raise HTTPException(status_code=409, detail="Borrador ajeno a esta cuenta MEXC")
            except LiveJournalError:
                raise HTTPException(status_code=409, detail="Borrador MEXC no verificable") from None
        quote_options = {"quote_get": self._mexc_quote_get} if self._mexc_quote_get is not None else {}
        rules_options = ({"rules_fetch": lambda symbol, now: self._mexc_rules_fetch(symbol, now=now)}
                         if self._mexc_rules_fetch is not None else {})
        try:
            evidence = fetch_live_evidence(view, policy, **quote_options, **rules_options)
            valuation = value_spot_account(evidence, policy, **quote_options)
        except MexcLiveObservationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        with self._lock:
            self.require(token)
            if self._mexc is not view or self._mexc_account_ref != account_ref:
                raise HTTPException(status_code=409, detail="La conexión MEXC cambió")
            try:
                if (view.account_reference() != account_ref
                        or not assert_known_mexc_uid(self.vault, account_ref, evidence.view_uid)):
                    raise HTTPException(status_code=409, detail="Identidad MEXC View no confirmada")
            except (MexcUidGuardError, MexcReadOnlyError):
                raise HTTPException(status_code=409, detail="Identidad MEXC View no confirmada") from None
            try:
                with self.vault.session() as db:
                    db.connection().exec_driver_sql("BEGIN")
                    current = _stored_policy(db, policy_id)
                    link = db.get(RealMexcAccountLink, account_ref)
                    if current != policy or link is None or link.account_id != account_id:
                        raise HTTPException(status_code=409, detail="Borrador o vínculo MEXC cambió")
            except LiveJournalError:
                raise HTTPException(status_code=409, detail="Borrador MEXC no verificable") from None
        base = next((row.value_quote for row in valuation.balances
                     if row.asset == policy.base_asset), Decimal(0))
        with localcontext() as context:
            context.prec = 300
            other = valuation.non_quote_exposure_quote - base
            remaining = max(Decimal(0), policy.max_exposure_quote - base)
        return {"account_id": account_id, "policy_id": policy_id,
                "quote_asset": policy.quote_asset, "base_asset": policy.base_asset,
                "observed_at": valuation.observed_at.isoformat(),
                "valued_at": valuation.valued_at.isoformat(),
                "total_spot_quote": format(valuation.total_quote, "f"),
                "non_quote_exposure_quote": format(valuation.non_quote_exposure_quote, "f"),
                "base_exposure_quote": format(base, "f"),
                "other_assets_exposure_quote": format(other, "f"),
                "max_base_exposure_quote": format(policy.max_exposure_quote, "f"),
                "remaining_base_headroom_quote": format(remaining, "f"),
                "base_limit_reached": base >= policy.max_exposure_quote,
                "balances": [{"asset": row.asset, "quantity": format(row.total, "f"),
                              "bid_quote": format(row.bid_quote, "f"),
                              "value_quote": format(row.value_quote, "f")}
                             for row in valuation.balances],
                "source": "MEXC_VIEW_PUBLIC_QUOTES_READ_ONLY",
                "reconciled_with_book": False, "execution_ready": False,
                "missing": ["BOOK_PARITY", "FILL_COVERAGE", "ORDER_COVERAGE", "COST_AND_PNL"]}

    def mexc_account_link_status(self, token: str | None, account_id: str) -> dict:
        """Identidad local de la conexión View, no conciliación de patrimonio."""
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
            if self._mexc is None or self._mexc_account_ref is None:
                return {"connected": False, "linked": False, "assigned_elsewhere": False,
                        "account_has_other_link": False,
                        "reconciled": False, "orders_enabled": False}
            try:
                uid = self._mexc.account_uid()
                identity = inspect_mexc_view_identity(self.vault, self._mexc_account_ref, uid)
            except (MexcReadOnlyError, MexcUidGuardError):
                raise HTTPException(status_code=503,
                                    detail="No se pudo comprobar la identidad MEXC View") from None
            self.require(token)
            with self.vault.session() as db:
                link = db.get(RealMexcAccountLink, self._mexc_account_ref)
                others = db.scalars(select(RealMexcAccountLink).where(
                    RealMexcAccountLink.account_id == account_id,
                    RealMexcAccountLink.account_ref != self._mexc_account_ref).limit(2)).all()
                can_renew = False
                if link is None and len(others) == 1 and identity["local_identity_status"] == "ROTATED_KEY_MATCH":
                    try:
                        MexcTransferRecoveryView(account_id, others[0].account_ref, self._mexc_account_ref).verify(db, uid)
                    except MexcUidGuardError:
                        pass
                    else:
                        can_renew = True
                return {"connected": True,
                        "linked": bool(link and link.account_id == account_id),
                        "assigned_elsewhere": bool(link and link.account_id != account_id),
                        "account_has_other_link": bool(others),
                        "identity_status": identity["local_identity_status"],
                        **({"can_renew_view": True} if can_renew else {}),
                        **({"session_renewed": True} if isinstance(self._mexc, MexcLinkedView)
                           and self._mexc.account_id == account_id else {}),
                        "reconciled": False, "orders_enabled": False}

    def renew_mexc_view(self, token: str | None, account_id: str) -> dict:
        """Keep the original reference and save the verified replacement fingerprint."""
        with self._lock:
            self.require(token)
            view, credential_ref = self._mexc, self._mexc_account_ref
            if view is None or credential_ref is None:
                raise HTTPException(status_code=409, detail="Conecta primero la clave View renovada")
            uid = view.account_uid()
            if view.account_reference() != credential_ref:
                raise MexcUidGuardError("La referencia de la clave View cambió")
            with self.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN")
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                refs = db.scalars(select(RealMexcAccountLink.account_ref).where(
                    RealMexcAccountLink.account_id == account_id).limit(2)).all()
                if len(refs) != 1:
                    raise MexcUidGuardError("La cuenta requiere un vínculo MEXC original único")
                archive_ref = refs[0]
                if isinstance(view, MexcLinkedView) and view.account_id == account_id and archive_ref == credential_ref:
                    stop = db.get(RealLiveStop, archive_ref)
                    return {"renewed": True, "new_binding": False, "stopped": bool(stop and stop.stopped)}
                if archive_ref == credential_ref or isinstance(view, MexcLinkedView):
                    raise MexcUidGuardError("Conecta una clave View distinta para renovar el vínculo")
                scope = MexcTransferRecoveryView(account_id, archive_ref, credential_ref)
                scope.verify(db, uid)
            self._halt_mexc_stop_supervisor(account_id=account_id, bot_reason="VIEW_RENEWED")
            self._mexc_trade = None
            self._mexc_trade_credentials = None
            self._mexc_opening_receipts.clear()
            self.require(token, touch=False)
            if (self._mexc is not view or self._mexc_account_ref != credential_ref
                    or view.account_reference() != credential_ref or view.account_uid() != uid):
                raise MexcUidGuardError("La identidad View cambió durante la renovación")
            self.require(token, touch=False)
            with self.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                refs = db.scalars(select(RealMexcAccountLink.account_ref).where(
                    RealMexcAccountLink.account_id == account_id).limit(2)).all()
                if refs != [archive_ref]:
                    raise MexcUidGuardError("El vínculo MEXC original cambió durante la renovación")
                now = datetime.now(timezone.utc)
                existing_stop = db.get(RealLiveStop, archive_ref)
                if existing_stop is not None and now < existing_stop.updated_at:
                    raise MexcUidGuardError("El reloj local retrocedió antes de renovar View")
                save_mexc_view_binding(db, scope, uid, now=now)
                _stop_spot_account_in_transaction(db, archive_ref, now)
                db.commit()
            self._mexc = MexcLinkedView(view, account_id, archive_ref, credential_ref, uid)
            self._mexc_account_ref = archive_ref
            self._mexc_uid = uid
            return {"renewed": True, "new_binding": True, "stopped": True}

    def link_mexc_account(self, token: str | None, account_id: str) -> dict:
        """Asigna View a un libro existente, sin importar ni comparar saldos."""
        with self._lock:
            self.require(token)
            view, account_ref = self._mexc, self._mexc_account_ref
            if view is None or account_ref is None:
                raise HTTPException(status_code=409, detail="Conecta MEXC View primero")
            try:
                uid_before = view.account_uid()
                view.balances()
                uid_after = view.account_uid()
            except MexcReadOnlyError:
                raise HTTPException(status_code=503,
                                    detail="No se pudo comprobar la identidad MEXC View") from None
            if uid_before != uid_after:
                raise HTTPException(status_code=409,
                                    detail="La identidad MEXC cambió durante el vínculo")
            self.require(token)
            if self._mexc is not view or self._mexc_account_ref != account_ref:
                raise HTTPException(status_code=409, detail="La conexión MEXC cambió")
            with self.vault.session() as db:
                try:
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    if db.get(RealAccount, account_id) is None:
                        raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                    link = db.get(RealMexcAccountLink, account_ref)
                    if link is not None and link.account_id != account_id:
                        raise HTTPException(status_code=409,
                                            detail="Esta identidad MEXC ya pertenece a otra cuenta REAL")
                    another = db.scalars(select(RealMexcAccountLink).where(
                        RealMexcAccountLink.account_id == account_id,
                        RealMexcAccountLink.account_ref != account_ref).limit(1)).first()
                    if another is not None:
                        raise HTTPException(status_code=409,
                                            detail="Esta cuenta REAL ya tiene otra identidad MEXC")
                    if link is None and db.scalars(select(RealLiveSpotIntent).where(
                            RealLiveSpotIntent.account_ref == account_ref).limit(1)).first() is not None:
                        raise HTTPException(status_code=409,
                                            detail="Hay intenciones MEXC previas; revisar antes de vincular")
                    claim_mexc_uid_in_transaction(db, account_ref, uid_after)
                    if link is None:
                        db.add(RealMexcAccountLink(account_ref=account_ref,
                                                   account_id=account_id,
                                                   created_at=datetime.now(timezone.utc)))
                    db.commit()
                except MexcUidGuardError as exc:
                    db.rollback()
                    raise HTTPException(status_code=409, detail=str(exc)) from None
                except Exception:
                    db.rollback()
                    raise
            return {"connected": True, "linked": True, "already_linked": link is not None,
                    "reconciled": False, "orders_enabled": False}

    def mexc_stop_supervisor_status(self, token: str | None) -> dict:
        with self._lock:
            self.require(token, touch=False)
            worker = self._mexc_stop_supervisor
            state = worker.state() if worker is not None else None
            running = bool(state and state.running)
            # Un hilo todavía puede figurar vivo mientras publica su estado
            # terminal y libera referencias. No anunciar vigilancia sana en
            # esa ventana; la cuenta sigue necesitando recuperación.
            monitoring = bool(running and not state.stop_requested and not state.stalled
                              and state.status in {
                                  "RUNNING", "NO_ACTIONABLE_OWN_ORDER",
                                  "FINAL_ARCHIVED_NEEDS_SETTLEMENT",
                                  "CANCEL_ACKNOWLEDGED_REOBSERVE",
                                  "STILL_OPEN_RECHECK", "OPEN_ARCHIVED_NEEDS_RECHECK",
                              })
            with self.vault.session() as db:
                pending = db.execute(select(
                    RealLiveSpotIntent.account_ref, func.count(RealLiveSpotIntent.id),
                ).join(RealLiveStop, RealLiveStop.account_ref == RealLiveSpotIntent.account_ref)
                  .where(RealLiveStop.stopped.is_(True),
                         RealLiveSpotIntent.status == "SUBMISSION_UNKNOWN")
                  .group_by(RealLiveSpotIntent.account_ref)).all()
            unmonitored = [(ref, count) for ref, count in pending
                           if not monitoring or ref != self._mexc_stop_account_ref]
            return {"running": state.running if state else False,
                    "status": state.status if state else "NOT_STARTED",
                    "stop_requested": state.stop_requested if state else False,
                    "stalled": state.stalled if state else False,
                    "cycles": state.cycles if state else 0,
                    "intent_id": state.intent_id if state else None,
                    "unresolved_intents": state.unresolved_intents if state else None,
                    "new_orders_enabled": False,
                    "scope": "LOCAL_PROCESS_ONLY",
                    "monitoring_active": monitoring,
                    "may_cancel_own_open_order": running,
                    "stopped_accounts_with_unresolved": len(pending),
                    "unresolved_stopped_intents": sum(count for _, count in pending),
                    "unmonitored_stopped_accounts": len(unmonitored),
                    "unmonitored_stopped_intents": sum(count for _, count in unmonitored),
                    "recovery_required": bool(unmonitored)}

    def start_mexc_stop_supervisor(self, token: str | None, intent_id: str) -> dict:
        """Arranque explícito: sólo cuenta detenida con envío incierto propio."""
        with self._lock:
            self.require(token)
            if self._mexc_stop_supervisor is not None and self._mexc_stop_supervisor.state().running:
                raise HTTPException(status_code=409, detail="Ya hay vigilancia MEXC activa")
            if self._mexc is None or self._mexc_trade_credentials is None:
                raise HTTPException(status_code=409, detail="Se requieren claves MEXC View y Trade")
            intent = recover_spot_intent(self.vault, intent_id)
            with self.vault.session() as db:
                row = db.get(RealLiveSpotIntent, intent.intent_id)
                stop = db.get(RealLiveStop, intent.account_ref)
                if (row is None or row.status != "SUBMISSION_UNKNOWN"
                        or stop is None or not stop.stopped):
                    raise HTTPException(status_code=409,
                                        detail="La intención MEXC no está enviada e incierta en una cuenta detenida")
            if self._mexc.account_reference() != intent.account_ref:
                raise HTTPException(status_code=409, detail="MEXC View no corresponde a la cuenta detenida")
            try:
                view_uid = self._mexc.account_uid()
                if not assert_known_mexc_uid(self.vault, intent.account_ref, view_uid):
                    raise HTTPException(status_code=409,
                                        detail="Falta identidad archivada para vigilancia automática")
                trade = self._mexc_live_factory(*self._mexc_trade_credentials)
                trade.pair_view_account(view_uid)
            except MexcReadOnlyError:
                raise HTTPException(status_code=422, detail="No se pudo verificar MEXC View") from None
            worker = self._mexc_stop_supervisor_factory(
                self.vault, trade, self._mexc, intent.account_ref)
            worker.start()
            self._mexc_stop_supervisor = worker
            self._mexc_stop_account_ref = intent.account_ref
            return self.mexc_stop_supervisor_status(token)

    def stop_mexc_stop_supervisor(self, token: str | None) -> dict:
        with self._lock:
            self.require(token)
            self._halt_mexc_stop_supervisor()
            return self.mexc_stop_supervisor_status(token)

    def connect_mexc_trade(self, token: str | None, api_key: str, secret_key: str) -> dict:
        """Verifica permisos/UID y conserva la clave sólo hasta bloqueo o desconexión.

        Esta sesión no tiene transporte POST/DELETE ni autoriza ejecución REAL.
        """
        with self._lock:
            self.require(token)
            self._halt_mexc_stop_supervisor()
            self._mexc_trade = None
            self._mexc_trade_credentials = None
            if self._mexc is None:
                raise HTTPException(status_code=409, detail="Conecta primero MEXC View")
            try:
                view_uid = self._mexc.account_uid()
            except MexcReadOnlyError:
                self._mexc_uid = None
                raise HTTPException(status_code=422, detail="No se pudo verificar MEXC View") from None
            self.require(token)
            self._mexc_uid = view_uid
            candidate = self._mexc_trade_factory(api_key, secret_key)
            if not verify_same_mexc_account(view_uid, candidate):
                raise MexcTradeIdentityError("Las claves MEXC View y Trade pertenecen a cuentas distintas")
            self.require(token)
            self._mexc_trade = candidate
            self._mexc_trade_credentials = (api_key, secret_key)
            return self.mexc_trade_status(token)

    def disconnect_mexc_trade(self, token: str | None) -> dict:
        with self._lock:
            self.require(token)
            self._halt_mexc_stop_supervisor()
            self._mexc_trade = None
            self._mexc_trade_credentials = None
            return self.mexc_trade_status(token)

    def connect_mexc(self, token: str | None, api_key: str, secret_key: str) -> dict:
        with self._lock:
            self.require(token)
            self._halt_mexc_stop_supervisor()
            self._mexc = None
            self._mexc_account_ref = None
            self._mexc_opening_receipts.clear()
            self._mexc_uid = None
            self._mexc_trade = None
            self._mexc_trade_credentials = None
            candidate = self._mexc_factory(api_key, secret_key)
            credential_ref = hmac.new(secret_key.encode("utf-8"), api_key.encode("utf-8"),
                                      hashlib.sha256).hexdigest()
            with self.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN")
                scope = saved_mexc_view_scope(db, credential_ref)
            uid = None
            if scope is not None:
                uid = candidate.account_uid()
                if candidate.account_reference() != credential_ref:
                    raise MexcUidGuardError("La referencia de la clave View cambió")
                with self.vault.session() as db:
                    db.connection().exec_driver_sql("BEGIN")
                    scope.verify(db, uid)
            observed = candidate.balances()
            if uid is not None and (candidate.account_reference() != credential_ref or candidate.account_uid() != uid):
                raise MexcUidGuardError("La identidad View cambió al recuperar el vínculo guardado")
            self.require(token)
            with self.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN")
                if saved_mexc_view_scope(db, credential_ref) != scope:
                    raise MexcUidGuardError("El vínculo View guardado cambió durante la conexión")
                if scope is not None and uid is not None:
                    scope.verify(db, uid)
            if scope is not None and uid is not None:
                self._mexc = MexcLinkedView(candidate, scope.account_id, scope.archive_ref, credential_ref, uid)
                self._mexc_account_ref = scope.archive_ref
                self._mexc_uid = uid
            else:
                self._mexc = candidate
                self._mexc_account_ref = credential_ref
            return observed

    def mexc_identity(self, token: str | None) -> dict:
        """Comprueba UID View y busca una huella previa sin devolverlo."""
        with self._lock:
            self.require(token)
            if self._mexc is None:
                raise HTTPException(status_code=409, detail="MEXC no está conectada")
            # Una comprobación fallida no debe dejar una identidad antigua válida.
            previous_uid = self._mexc_uid
            self._mexc_uid = None
            try:
                uid = self._mexc.account_uid()
            except MexcReadOnlyError:
                self._halt_mexc_stop_supervisor()
                self._mexc_trade = None
                self._mexc_trade_credentials = None
                raise
            self.require(token)
            if previous_uid is not None and not secrets.compare_digest(previous_uid, uid):
                self._halt_mexc_stop_supervisor()
                self._mexc_trade = None
                self._mexc_trade_credentials = None
            if self._mexc_account_ref is None:
                self._halt_mexc_stop_supervisor()
                self._mexc_trade = None
                self._mexc_trade_credentials = None
                raise HTTPException(status_code=409, detail="Falta referencia MEXC")
            diagnostic = inspect_mexc_view_identity(self.vault, self._mexc_account_ref, uid)
            self._mexc_uid = uid
            return {"view_uid_observed": True, "storage": "MEMORY_ONLY", **diagnostic}

    def mexc_balances(self, token: str | None) -> dict:
        with self._lock:
            self.require(token)
            if self._mexc is None:
                raise HTTPException(status_code=409, detail="MEXC no está conectada")
            observed = self._mexc.balances()
            self.require(token)
            return observed

    def mexc_erc20_deposit_destination(self, token: str | None,
                                       account_id: str, asset: str, chain_id: str = "0x1") -> dict:
        """Destino efímero, sólo de cuenta View vinculada; no crea traslados."""
        with self._lock:
            self.require(token)
            view, account_ref = self._mexc, self._mexc_account_ref
            if view is None or account_ref is None:
                raise HTTPException(status_code=409, detail="Conecta MEXC View primero")
            with self.vault.session() as db:
                link = db.get(RealMexcAccountLink, account_ref)
                if link is None or link.account_id != account_id:
                    raise HTTPException(status_code=409,
                                        detail="Vincula la conexión MEXC con esta cuenta REAL")
            try:
                uid_before = view.account_uid()
                if not assert_known_mexc_uid(self.vault, account_ref, uid_before):
                    raise HTTPException(status_code=409,
                                        detail="La identidad MEXC View aún no está fijada")
                if chain_id == "0x1":
                    destination = view.ethereum_erc20_deposit_destination(asset)
                elif chain_id == "0x38":
                    destination = view.evm_token_deposit_destination(asset, chain_id=chain_id)
                else:
                    raise MexcReadOnlyError("Red de depósito no admitida")
                uid_after = view.account_uid()
            except (MexcReadOnlyError, MexcUidGuardError):
                raise HTTPException(status_code=503,
                                    detail="No se pudo verificar el destino o la identidad MEXC") from None
            self.require(token)
            if (self._mexc is not view or self._mexc_account_ref != account_ref
                    or uid_before != uid_after or view.account_reference() != account_ref):
                raise HTTPException(status_code=409,
                                    detail="La conexión MEXC cambió durante la consulta")
            return destination

    def mexc_compare_account(self, token: str | None, account_id: str) -> dict:
        """Lectura nueva y libro cifrado; no conserva la observación ni asienta."""
        with self._lock:
            self.require(token)
            if self._mexc is None:
                raise HTTPException(status_code=409, detail="MEXC no está conectada")
            view = self._mexc
        try:
            uid_before = view.account_uid()
            observed = view.balances()
            uid_after = view.account_uid()
        except Exception:
            raise HTTPException(status_code=503, detail="No se pudo leer identidad y saldo MEXC") from None
        if uid_before != uid_after:
            raise HTTPException(status_code=409, detail="La identidad MEXC cambió durante la lectura")
        with self._lock:
            self.require(token)
            if self._mexc is not view:
                raise HTTPException(status_code=409, detail="La conexión MEXC cambió durante la consulta")
            account_ref = self._mexc_account_ref
            if account_ref is None:
                raise HTTPException(status_code=409, detail="Falta la referencia de conexión MEXC")
            try:
                identity = inspect_mexc_view_identity(self.vault, account_ref, uid_after)
            except MexcUidGuardError:
                raise HTTPException(status_code=409, detail="La identidad MEXC no es verificable") from None
            if identity["local_identity_status"] in (
                    "KEY_UID_CHANGED", "ROTATED_KEY_MATCH", "AMBIGUOUS_LOCAL_IDENTITY"):
                raise HTTPException(status_code=409, detail="La identidad MEXC difiere del vínculo local")
            with self.vault.session() as db:
                link = db.get(RealMexcAccountLink, account_ref)
                if link is not None and link.account_id != account_id:
                    raise HTTPException(status_code=409, detail="Esta conexión MEXC está asignada a otra cuenta REAL")
                book = account_snapshot(db, account_id)
            result = compare_mexc_spot_inventory(book, observed)
            result["local_account_link"] = (
                "LOCAL_ACCOUNT_LINK" if link and identity["local_identity_status"] == "SAME_KEY"
                else "LOCAL_ACCOUNT_LINK_UID_UNVERIFIED" if link else "NONE")
            result["local_identity_status"] = identity["local_identity_status"]
            return result

    @staticmethod
    def _book_revision(db: Session, account_id: str) -> str:
        return account_book_revision(db, account_id)

    def mexc_spot_parity(self, token: str | None, account_id: str) -> dict:
        """Doble lectura ligada al mismo libro e identidad; no declara conciliación."""
        with self._lock:
            self.require(token)
            view, account_ref = self._mexc, self._mexc_account_ref
            if view is None or account_ref is None:
                raise HTTPException(status_code=409, detail="Conecta MEXC View primero")
            with self.vault.session() as db:
                # El snapshot del libro y su revisión deben proceder de la
                # misma transacción DBAPI, no de SELECTs independientes.
                db.connection().exec_driver_sql("BEGIN")
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                link = db.get(RealMexcAccountLink, account_ref)
                if link is None or link.account_id != account_id:
                    raise HTTPException(status_code=409,
                                        detail="Vincula MEXC View con esta cuenta REAL")
                book = account_snapshot(db, account_id)
                before_revision = self._book_revision(db, account_id)
        try:
            uid_first = view.account_uid()
            first = view.balances()
            uid_middle = view.account_uid()
            second = view.balances()
            uid_last = view.account_uid()
        except MexcReadOnlyError:
            raise HTTPException(status_code=503,
                                detail="No se pudo completar la doble lectura MEXC") from None
        if uid_first != uid_middle or uid_middle != uid_last:
            raise HTTPException(status_code=409,
                                detail="La identidad MEXC cambió durante la comprobación")
        with self._lock:
            self.require(token)
            if self._mexc is not view or self._mexc_account_ref != account_ref:
                raise HTTPException(status_code=409, detail="La conexión MEXC cambió")
            try:
                if not assert_known_mexc_uid(self.vault, account_ref, uid_last):
                    raise HTTPException(status_code=409,
                                        detail="La identidad MEXC View no está confirmada")
            except MexcUidGuardError:
                raise HTTPException(status_code=409,
                                    detail="La identidad MEXC View no coincide") from None
            with self.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN")
                link = db.get(RealMexcAccountLink, account_ref)
                if link is None or link.account_id != account_id:
                    raise HTTPException(status_code=409, detail="El vínculo MEXC cambió")
                after_revision = self._book_revision(db, account_id)
            try:
                return assess_mexc_spot_parity(
                    book, first, second,
                    book_revision_before=before_revision,
                    book_revision_after=after_revision,
                )
            except PortfolioError:
                raise HTTPException(status_code=409,
                                    detail="Lecturas MEXC no comparables") from None

    def _mexc_open_orders_capture(self, token: str | None,
                                  account_id: str) -> tuple[dict, dict, object, str]:
        """Detecta órdenes visibles en otros pares; no acredita ausencia global."""
        with self._lock:
            self.require(token)
            view, account_ref = self._mexc, self._mexc_account_ref
            if view is None or account_ref is None:
                raise HTTPException(status_code=409, detail="Conecta MEXC View primero")
            with self.vault.session() as db:
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                link = db.get(RealMexcAccountLink, account_ref)
                if link is None or link.account_id != account_id:
                    raise HTTPException(status_code=409,
                                        detail="Vincula MEXC View con esta cuenta REAL")
        try:
            uid_before = view.account_uid()
            snapshot = view.open_orders_unfiltered()
            uid_after = view.account_uid()
        except MexcReadOnlyError:
            raise HTTPException(status_code=503,
                                detail="No se pudo comprobar órdenes abiertas MEXC") from None
        if uid_before != uid_after:
            raise HTTPException(status_code=409,
                                detail="La identidad MEXC cambió durante la consulta")
        with self._lock:
            self.require(token)
            if self._mexc is not view or self._mexc_account_ref != account_ref:
                raise HTTPException(status_code=409, detail="La conexión MEXC cambió")
            try:
                if not assert_known_mexc_uid(self.vault, account_ref, uid_after):
                    raise HTTPException(status_code=409,
                                        detail="La identidad MEXC View no está confirmada")
            except MexcUidGuardError:
                raise HTTPException(status_code=409,
                                    detail="La identidad MEXC View no coincide") from None
            with self.vault.session() as db:
                link = db.get(RealMexcAccountLink, account_ref)
                if link is None or link.account_id != account_id:
                    raise HTTPException(status_code=409, detail="El vínculo MEXC cambió")
            return summarize_open_orders(snapshot, account_id=account_id), snapshot, view, account_ref

    def mexc_open_orders_survey(self, token: str | None, account_id: str) -> dict:
        return self._mexc_open_orders_capture(token, account_id)[0]

    def save_mexc_open_orders_survey(self, token: str | None, account_id: str,
                                     request_id: UUID) -> dict:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                prior = db.get(RealMexcOpenOrdersObservation, str(request_id))
                if prior is not None:
                    if prior.account_id != account_id:
                        raise HTTPException(status_code=409,
                                            detail="Identificador de revisión usado por otra cuenta")
                    try:
                        report = read_open_orders_observation(prior)
                    except MexcOpenOrdersArchiveError:
                        raise HTTPException(status_code=409,
                                            detail="Evidencia de órdenes no verificable") from None
                    link = db.scalar(select(RealMexcAccountLink).where(
                        RealMexcAccountLink.account_id == account_id))
                    return {**report, "identity_current": link is not None and
                            link.account_ref == prior.account_ref,
                            "recent": 0 <= (datetime.now(timezone.utc) - prior.observed_at
                                           ).total_seconds() <= 60}
        _, snapshot, view, account_ref = self._mexc_open_orders_capture(token, account_id)
        with self._lock:
            self.require(token)
            if self._mexc is not view or self._mexc_account_ref != account_ref:
                raise HTTPException(status_code=409, detail="La conexión MEXC cambió")
            with self.vault.session() as db:
                try:
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    prior = db.get(RealMexcOpenOrdersObservation, str(request_id))
                    if prior is not None:
                        if prior.account_id != account_id:
                            raise HTTPException(status_code=409,
                                                detail="Identificador de revisión usado por otra cuenta")
                        return {**read_open_orders_observation(prior),
                                "identity_current": prior.account_ref == account_ref,
                                "recent": 0 <= (datetime.now(timezone.utc) - prior.observed_at
                                               ).total_seconds() <= 60}
                    link = db.get(RealMexcAccountLink, account_ref)
                    if link is None or link.account_id != account_id:
                        raise HTTPException(status_code=409, detail="El vínculo MEXC cambió")
                    report = add_open_orders_observation(db, str(request_id), account_id,
                                                         account_ref, snapshot)
                    db.commit()
                    return {**report, "identity_current": True, "recent": True}
                except Exception:
                    db.rollback()
                    raise

    def list_mexc_open_orders_survey(self, token: str | None,
                                     account_id: str) -> list[dict]:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                link = db.scalar(select(RealMexcAccountLink).where(
                    RealMexcAccountLink.account_id == account_id))
                try:
                    reports = list_open_orders_observations(db, account_id)
                except MexcOpenOrdersArchiveError:
                    raise HTTPException(status_code=409,
                                        detail="Evidencia de órdenes no verificable") from None
                rows = db.scalars(select(RealMexcOpenOrdersObservation).where(
                    RealMexcOpenOrdersObservation.account_id == account_id)).all()
                refs = {row.id: row.account_ref for row in rows}
            current = datetime.now(timezone.utc)
            return [{**report, "identity_current": link is not None and
                     refs[report["id"]] == link.account_ref,
                     "recent": 0 <= (current - datetime.fromisoformat(
                         report["observed_at"])).total_seconds() <= 60}
                    for report in reports]

    def compare_mexc_open_orders_survey(self, token: str | None, account_id: str,
                                        before_id: UUID, after_id: UUID) -> dict:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                try:
                    return compare_open_orders_observations(
                        db, account_id, str(before_id), str(after_id))
                except MexcOpenOrdersArchiveError:
                    raise HTTPException(status_code=409,
                                        detail="No se pudieron comparar las revisiones de órdenes") from None

    def save_mexc_spot_parity(self, token: str | None, account_id: str,
                              request_id: UUID) -> dict:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                prior = db.get(RealMexcParityObservation, str(request_id))
                if prior is not None:
                    if prior.account_id != account_id:
                        raise HTTPException(status_code=409,
                                            detail="Identificador de comprobación usado por otra cuenta")
                    try:
                        report = read_parity_observation(prior)
                        current_revision = self._book_revision(db, account_id)
                        return {**report, "book_current": report["book_revision"] == current_revision,
                                "recent": 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(
                                    report["checked_at"])).total_seconds() <= 60}
                    except MexcParityArchiveError:
                        raise HTTPException(status_code=409,
                                            detail="Evidencia de paridad no verificable") from None
            view, account_ref = self._mexc, self._mexc_account_ref
        report = self.mexc_spot_parity(token, account_id)
        if report["blockers"]:
            return {**report, "saved": False}
        with self._lock:
            self.require(token)
            if self._mexc is not view or self._mexc_account_ref != account_ref or view is None:
                raise HTTPException(status_code=409, detail="La conexión MEXC cambió")
            try:
                if not assert_known_mexc_uid(self.vault, account_ref, view.account_uid()):
                    raise HTTPException(status_code=409,
                                        detail="La identidad MEXC no está confirmada")
            except MexcReadOnlyError:
                raise HTTPException(status_code=503,
                                    detail="No se pudo revalidar la identidad MEXC") from None
            except MexcUidGuardError:
                raise HTTPException(status_code=409,
                                    detail="La identidad MEXC cambió") from None
            with self.vault.session() as db:
                try:
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    prior = db.get(RealMexcParityObservation, str(request_id))
                    if prior is not None:
                        if prior.account_id != account_id:
                            raise HTTPException(status_code=409,
                                                detail="Identificador de comprobación usado por otra cuenta")
                        report = read_parity_observation(prior)
                        return {**report, "book_current": report["book_revision"] ==
                                self._book_revision(db, account_id),
                                "recent": 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(
                                    report["checked_at"])).total_seconds() <= 60}
                    link = db.get(RealMexcAccountLink, account_ref)
                    if link is None or link.account_id != account_id:
                        raise HTTPException(status_code=409, detail="El vínculo MEXC cambió")
                    if self._book_revision(db, account_id) != report["book_revision"]:
                        raise HTTPException(status_code=409,
                                            detail="El libro REAL cambió antes de guardar la comprobación")
                    checked_at = datetime.fromisoformat(report["checked_at"])
                    if not 0 <= (datetime.now(timezone.utc) - checked_at).total_seconds() <= 30:
                        raise HTTPException(status_code=409,
                                            detail="La comprobación de paridad ya no es reciente")
                    result = add_parity_observation(db, str(request_id), account_id,
                                                    account_ref, report)
                    db.commit()
                    return {**result, "book_current": True, "recent": True}
                except Exception:
                    db.rollback()
                    raise

    def list_mexc_spot_parity(self, token: str | None, account_id: str) -> list[dict]:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                revision = self._book_revision(db, account_id)
                try:
                    reports = list_parity_observations(db, account_id)
                except MexcParityArchiveError:
                    raise HTTPException(status_code=409,
                                        detail="Evidencia de paridad no verificable") from None
            current = datetime.now(timezone.utc)
            return [{**report, "book_current": report["book_revision"] == revision,
                     "recent": 0 <= (current - datetime.fromisoformat(
                         report["checked_at"])).total_seconds() <= 60}
                    for report in reports]

    def _replay_mexc_opening(self, key: tuple[str, str, str], commit: MexcOpeningCommit) -> dict | None:
        receipt = self._mexc_opening_receipts.get(key)
        if receipt is None:
            return None
        if commit != receipt[0]:
            raise RequestConflict("El registro de inventario no coincide con el original")
        response = receipt[1]
        total = response["counts"]["total"]
        return response | {"counts": {"total": total, "new": 0, "already_present": total}}

    def mexc_opening(self, token: str | None, account_id: str,
                     body: MexcOpeningRequest, *, commit: bool = False) -> dict:
        """Sólo libro manual: fotografía nueva, coste declarado y commit atómico."""
        command = body if commit and isinstance(body, MexcOpeningCommit) else None
        with self._lock:
            self.require(token)
            view, account_ref = self._mexc, self._mexc_account_ref
            if view is None or account_ref is None:
                raise HTTPException(status_code=409, detail="Conecta MEXC View primero")
            receipt_key = (account_id, account_ref, command.preview_hash) if command else None
            if receipt_key is not None and command is not None:
                if (replay := self._replay_mexc_opening(receipt_key, command)) is not None:
                    return replay
        try:
            uid_before = view.account_uid()
            observed = view.balances()
            uid_after = view.account_uid()
        except Exception:
            raise HTTPException(status_code=503, detail="No se pudo leer identidad y saldo MEXC") from None
        if uid_before != uid_after:
            raise HTTPException(status_code=409, detail="La identidad MEXC cambió durante la lectura")
        with self._lock:
            self.require(token)
            if self._mexc is not view or self._mexc_account_ref != account_ref:
                raise HTTPException(status_code=409, detail="La conexión MEXC cambió durante la lectura")
            if receipt_key is not None and command is not None:
                if (replay := self._replay_mexc_opening(receipt_key, command)) is not None:
                    return replay
            as_of = body.observed_at if isinstance(body, MexcOpeningCommit) else None
            prepared = prepare_mexc_opening(account_id, observed,
                                            quote_currency=body.quote_currency,
                                            costs=body.costs, occurred_at=as_of)
            payload = {"account_id": account_id, "account_ref": account_ref,
                       "view_uid": uid_after, "quote_currency": prepared["quote_currency"],
                       "occurred_at": prepared["occurred_at"], "lines": prepared["lines"]}
            digest = hmac.new(self._mexc_opening_key,
                              json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                              hashlib.sha256).hexdigest()
            with self.vault.session() as db:
                if not commit:
                    counts = import_events(db, account_id, prepared["entries"], dry_run=True,
                                           opening_account_ref=account_ref, opening_uid=uid_after)
                else:
                    if not isinstance(body, MexcOpeningCommit) or not hmac.compare_digest(
                            digest, body.preview_hash):
                        raise RequestConflict("El inventario MEXC cambió desde la vista previa")
                    counts = import_events(db, account_id, prepared["entries"], dry_run=False,
                                           opening_account_ref=account_ref, opening_uid=uid_after)
            response = {key: value for key, value in prepared.items() if key != "entries"} | {
                "preview_hash": digest, "status": "RECORDED" if commit else "PREVIEW",
                "counts": counts, "reconciled": False, "orders_enabled": False,
            }
            # Una respuesta perdida se confirma con el recibo de esta sesión, aunque el saldo ya cambie.
            if receipt_key is not None and command is not None:
                if len(self._mexc_opening_receipts) >= 64:
                    self._mexc_opening_receipts.pop(next(iter(self._mexc_opening_receipts)))
                self._mexc_opening_receipts[receipt_key] = (command, response)
            return response

    def mexc_valuation(self, token: str | None, quote_asset: str) -> dict:
        """Vista puntual y no contable; la petición puede consultar varios tickers."""
        with self._lock:
            self.require(token)
            view = self._mexc
            if view is None:
                raise HTTPException(status_code=409, detail="MEXC no está conectada")
        observed = view.balances()
        options = {"quote_get": self._mexc_quote_get} if self._mexc_quote_get is not None else {}
        result = preview_spot_valuation(observed, quote_asset, **options)
        with self._lock:
            self.require(token)
            if self._mexc is not view:
                raise HTTPException(status_code=409, detail="La conexión MEXC cambió durante la consulta")
        return result

    def close_mexc_reservation(self, token: str | None, intent_id: str) -> dict:
        with self._lock:
            self.require(token)
            if self._mexc is None or self._mexc_account_ref is None:
                raise HTTPException(status_code=409, detail="Conecta la clave MEXC View de esta cuenta")
            result = close_final_spot_reservation(
                self.vault, self._mexc, self._mexc_account_ref, intent_id,
            )
            return result

    def synchronize_mexc_order(self, token: str | None, intent_id: str,
                               account_id: str, *, renewed_view: bool = False) -> dict:
        with self._lock:
            self.require(token)
            if self._mexc is None or self._mexc_account_ref is None:
                raise HTTPException(status_code=409, detail="Conecta la clave MEXC View de esta cuenta")
            return synchronize_spot_order(
                self.vault, self._mexc, intent_id, account_id,
                assert_active=lambda: self.require(token),
                renewed_view_ref=self._mexc_account_ref if renewed_view else None,
            )

    def observe_stopped_mexc_intent(self, token: str | None, intent_id: str) -> dict:
        """Sólo View y archivo cifrado; jamás cancela ni cierra una reserva."""
        with self._lock:
            self.require(token)
            if self._mexc is None:
                raise HTTPException(status_code=409, detail="Conecta la clave MEXC View de esta cuenta")
            step = observe_stopped_spot_limit(self.vault, self._mexc, intent_id)
            return {"status": step.status, "intent_id": step.intent_id,
                    "unresolved_intents": step.unresolved_intents,
                    "external_status": step.external_status,
                    "new_snapshot": step.new_snapshot,
                    "exchange_orders_cancelled": False,
                    "reservation_closed": False}

    def cancel_selected_mexc_order(self, token: str | None, intent_id: str,
                                   account_id: str) -> MexcSelectedCancellation:
        with self._lock:
            self.require(token)
            if self._mexc is None or self._mexc_trade is None or self._mexc_trade_credentials is None:
                raise HTTPException(status_code=409, detail="Conecta MEXC View y verifica Trade")
            return cancel_selected_stopped_spot_order(
                self.vault, self._mexc, self._mexc_live_factory(*self._mexc_trade_credentials),
                intent_id, account_id, assert_active=lambda: self.require(token),
            )

    def mexc_transfers_recent(self, token: str | None, days: int, *,
                              recovery_scope: tuple[str, str] | None = None) -> dict:
        with self._lock:
            self.require(token)
            if self._mexc is None or self._mexc_account_ref is None:
                raise HTTPException(status_code=409, detail="MEXC no está conectada")
            try:
                reference_before = self._mexc.account_reference()
                uid_before = self._mexc.account_uid()
            except Exception:
                raise HTTPException(status_code=503, detail="Identidad MEXC View no verificable") from None
            if reference_before != self._mexc_account_ref:
                raise HTTPException(status_code=409, detail="La referencia MEXC cambió")
            recovery = MexcTransferRecoveryView(*recovery_scope, reference_before) if recovery_scope else None
            if recovery is not None:
                try:
                    with self.vault.session() as db:
                        db.connection().exec_driver_sql("BEGIN")
                        recovery.verify(db, uid_before)
                except MexcUidGuardError as exc:
                    raise HTTPException(status_code=409, detail=str(exc),
                                        headers={"Cache-Control": "no-store"}) from None
            observed = self._mexc.transfers_recent(days=days, include_source_ids=True)
            deposits = observed.get("deposits") if isinstance(observed, dict) else None
            withdrawals = observed.get("withdrawals") if isinstance(observed, dict) else None
            rows_by_direction = {"deposit": deposits, "withdrawal": withdrawals}
            if any(isinstance(rows, list) and any(
                    isinstance(row, dict) and row.get("network") in ("ETH", "ERC20")
                    for row in rows) for rows in rows_by_direction.values()):
                dual_reader = getattr(self._mexc, "ethereum_token_contracts_by_direction", None)
                if callable(dual_reader):
                    try:
                        contracts_by_direction = dual_reader()
                    except MexcReadOnlyError:
                        contracts_by_direction = {}
                else:
                    legacy_reader = getattr(self._mexc, "ethereum_token_contracts", None)
                    try:
                        contracts_by_direction = {"deposit": legacy_reader()} if callable(legacy_reader) else {}
                    except MexcReadOnlyError:
                        contracts_by_direction = {}
                if isinstance(contracts_by_direction, dict):
                    for direction, rows in rows_by_direction.items():
                        contracts = contracts_by_direction.get(direction)
                        if not isinstance(rows, list) or not isinstance(contracts, dict):
                            continue
                        for row in rows:
                            if (isinstance(row, dict) and row.get("network") in ("ETH", "ERC20")
                                    and row.get("asset") in contracts):
                                row["token_contract"] = contracts[row["asset"]]
            if any(isinstance(rows,list) and any(isinstance(row,dict) and row.get("network")
                    in ("BSC","BEP20","BEP20(BSC)") for row in rows) for rows in rows_by_direction.values()):
                reader = getattr(self._mexc,"evm_token_contracts_by_direction",None)
                try:
                    bsc_contracts = reader(chain_id="0x38") if callable(reader) else {}
                except MexcReadOnlyError:
                    bsc_contracts = {}
                if isinstance(bsc_contracts,dict):
                    for direction,rows in rows_by_direction.items():
                        contracts = bsc_contracts.get(direction)
                        if isinstance(rows,list) and isinstance(contracts,dict):
                            for row in rows:
                                if (isinstance(row,dict) and row.get("network") in ("BSC","BEP20","BEP20(BSC)")
                                        and row.get("asset") in contracts):
                                    row["token_contract"] = contracts[row["asset"]]
            try:
                uid_after = self._mexc.account_uid()
                reference_after = self._mexc.account_reference()
            except Exception:
                raise HTTPException(status_code=503, detail="Identidad MEXC View no verificable") from None
            if uid_before != uid_after or reference_after != reference_before or reference_after != self._mexc_account_ref:
                raise HTTPException(status_code=409,
                                    detail="La identidad MEXC cambió durante la consulta")
            self.require(token)
            with self.vault.session() as db:
                try:
                    archive = save_transfer_snapshot(
                        db, recovery.archive_ref if recovery else reference_before, observed,
                        view_uid=uid_after, recovery=recovery,
                    )
                except (PortfolioError, MexcUidGuardError):
                    raise HTTPException(status_code=409,
                                        detail="Identidad o captura MEXC no verificable") from None
                stored = read_transfer_snapshot(db, archive["id"])
            return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                    "accounting": stored["accounting"], "coverage": stored["coverage"],
                    "identity_verified": stored["identity_verified"],
                    "start_ms": stored["start_ms"], "end_ms": stored["end_ms"],
                    "observed_at": stored["observed_at"], "requests": stored["requests"],
                    "deposits": stored["deposits"], "withdrawals": stored["withdrawals"],
                    "archive": archive}

    def mexc_transfer_snapshots(self, token: str | None) -> list[dict]:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                return list_transfer_snapshots(db)

    def mexc_saved_transfer_snapshot(self, token: str | None, identifier: str) -> dict:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                return read_transfer_snapshot(db, identifier)

    def mexc_compare_transfer_snapshots(self, token: str | None,
                                        before_id: str, after_id: str) -> dict:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                return compare_transfer_snapshots(db, before_id, after_id)

    def confirm_mexc_transfer_match(self, token: str | None, account_id: str,
                                    body: MexcTransferMatchRequest) -> dict:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                return confirm_mexc_transfer_match(
                    db, account_id, str(body.snapshot_id), body.direction,
                    body.source_ref, str(body.event_id),
                    confirmation=body.confirmation,
                )

    def mexc_external_deposits(self, token: str | None, account_id: str,
                              body: MexcExternalDepositSelection, *, expected_hash: str | None = None) -> dict:
        snapshot_id = str(body.snapshot_id)
        with self._lock:
            self.require(token)
            view = self._mexc
            now = datetime.now(timezone.utc)
            with self.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN")
                account_ref, sources, booked = external_deposit_selection(
                    db, account_id, snapshot_id, body.source_refs, now=now)
            if booked:
                return {"status": "ALREADY_RECORDED", "event_ids": booked}
            if view is None or self._mexc_account_ref != account_ref:
                raise PortfolioError("Conecta MEXC View de esta cuenta para comprobar el saldo")
        refreshed = None
        if expected_hash is not None:
            oldest = datetime.fromtimestamp(min(row["time_ms"] for row in sources) / 1000, timezone.utc)
            days = next((days for days in (1, 7, 30, 90) if now - oldest < timedelta(days=days)), None)
            if days is None:
                raise PortfolioError("El ingreso queda fuera del historial actual de MEXC")
            refreshed = self.mexc_transfers_recent(token, days)["archive"]["id"]
        parity = self.mexc_spot_parity(token, account_id)
        with self._lock:
            self.require(token)
            if self._mexc is not view or self._mexc_account_ref != account_ref:
                raise PortfolioError("La conexión MEXC cambió durante la revisión")
            with self.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE" if expected_hash is not None else "BEGIN")
                result = external_deposit_review(db, account_id, snapshot_id, body.source_refs, parity,
                    now=datetime.now(timezone.utc), expected_hash=expected_hash, current_snapshot_id=refreshed)
                if expected_hash is not None:
                    db.commit()
                return result

    def mexc_trades(self, token: str | None, symbol: str) -> dict:
        with self._lock:
            self.require(token)
            if self._mexc is None:
                raise HTTPException(status_code=409, detail="MEXC no está conectada")
            observed = self._mexc.trades(symbol)
            self.require(token)
            if self._mexc_account_ref is None:
                raise HTTPException(status_code=409, detail="Referencia de cuenta MEXC no disponible")
            with self.vault.session() as db:
                archive = save_observation(db, self._mexc_account_ref, observed)
            return {**observed, "archive": archive}

    def mexc_trades_window(self, token: str | None, symbol: str, days: int) -> dict:
        with self._lock:
            self.require(token)
            if self._mexc is None:
                raise HTTPException(status_code=409, detail="MEXC no está conectada")
            observed = self._mexc.trades_window(symbol, days=days)
            self.require(token)
            if self._mexc_account_ref is None:
                raise HTTPException(status_code=409, detail="Referencia de cuenta MEXC no disponible")
            with self.vault.session() as db:
                archive = save_observation(db, self._mexc_account_ref, observed)
            return {key: value for key, value in observed.items() if key != "trades"} | {"archive": archive}

    def mexc_trade_window_audit(self, token: str | None, policy_id: str, days: int) -> dict:
        """Diagnóstico de un par/periodo; nunca entrega permiso de ejecución."""
        with self._lock:
            self.require(token)
            if self._mexc is None or self._mexc_account_ref is None:
                raise HTTPException(status_code=409, detail="MEXC View no está conectada")
            with self.vault.session() as db:
                try:
                    policy = _stored_policy(db, policy_id)
                except LiveJournalError:
                    raise HTTPException(status_code=404, detail="Política MEXC no disponible") from None
            if policy.account_ref != self._mexc_account_ref:
                raise HTTPException(status_code=409, detail="La política pertenece a otra conexión View")
            result = observe_and_audit_spot_trade_window(
                self.vault, self._mexc, policy, days=days,
            )
            self.require(token)
            return {"consistent_for_window": result.consistent_for_window,
                    "reason": result.reason, "start_ms": result.start_ms,
                    "end_ms": result.end_ms, "fill_count": result.fill_count,
                    "scope": "ONE_SYMBOL_ONE_WINDOW_DIAGNOSTIC",
                    "can_submit_real_order": False}

    def mexc_transfer_window_audit(self, token: str | None, policy_id: str, days: int) -> dict:
        """Diagnóstico de traslados de la cuenta; no permite enviar órdenes."""
        with self._lock:
            self.require(token)
            if self._mexc is None or self._mexc_account_ref is None:
                raise HTTPException(status_code=409, detail="MEXC View no está conectada")
            with self.vault.session() as db:
                try:
                    policy = _stored_policy(db, policy_id)
                except LiveJournalError:
                    raise HTTPException(status_code=404, detail="Política MEXC no disponible") from None
            if policy.account_ref != self._mexc_account_ref:
                raise HTTPException(status_code=409, detail="La política pertenece a otra conexión View")
            result = observe_and_audit_spot_transfers(
                self.vault, self._mexc, policy, days=days,
            )
            self.require(token)
            return {"clear_for_window": result.clear_for_window,
                    "reason": result.reason, "start_ms": result.start_ms,
                    "end_ms": result.end_ms,
                    "deposit_count": result.deposit_count,
                    "withdrawal_count": result.withdrawal_count,
                    "scope": "ACCOUNT_TRANSFER_WINDOW_DIAGNOSTIC",
                    "can_submit_real_order": False}

    def mexc_observations(self, token: str | None) -> list[dict]:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                return list_observations(db)

    def mexc_saved_trades(self, token: str | None, account_ref: str, symbol: str) -> dict:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                return read_observations(db, account_ref, symbol)

    def mexc_archived_trade_coverage(self, token: str | None, account_ref: str,
                                     symbol: str, start_ms: int, end_ms: int) -> dict:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                return archived_window_coverage(db, account_ref, symbol, start_ms, end_ms)

    def disconnect_mexc(self, token: str | None) -> dict:
        with self._lock:
            self.require(token)
            self._halt_mexc_stop_supervisor()
            self._mexc = None
            self._mexc_account_ref = None
            self._mexc_opening_receipts.clear()
            self._mexc_uid = None
            self._mexc_trade = None
            self._mexc_trade_credentials = None
            return {"connected": False}

    def grvt_status(self, token: str | None) -> dict:
        with self._lock:
            self.require(token)
            return {"connected": self._grvt is not None, "mode": "READ_ONLY_CLIENT",
                    "permission_verified": False, "credential_storage": "MEMORY_ONLY"}

    def connect_grvt(self, token: str | None, api_key: str) -> dict:
        with self._lock:
            self.require(token)
            self._grvt = None
            candidate = self._grvt_factory(api_key)
            observed = candidate.snapshot()
            self.require(token)
            self._grvt = candidate
            return observed

    def grvt_snapshot(self, token: str | None) -> dict:
        with self._lock:
            self.require(token)
            if self._grvt is None:
                raise HTTPException(status_code=409, detail="GRVT no está conectada")
            observed = self._grvt.snapshot()
            self.require(token)
            return observed

    def grvt_perp_review(self, token: str | None, paper_path: Path, session_id: str, sub_account_id: str) -> dict:
        from app.derivatives.paper_store import PaperPerpStore
        from app.derivatives.perp_observed_archive import ObservedArchive
        from app.research.perp_live_review import review_perp_live
        with self._lock:
            self.require(token)
            if self._grvt is None:
                raise HTTPException(status_code=409, detail='GRVT no está conectada')
            if self._lab is None:
                raise HTTPException(status_code=409, detail='LAB no está disponible')
            client = self._grvt
            generation = self.vault.session_generation

        def checked():
            with self._lock:
                self.require(token, touch=False)
                if self._grvt is not client or self.vault.session_generation != generation:
                    raise HTTPException(status_code=409, detail='Conexión GRVT cambiada durante la revisión')

        # No REAL mutex, vault transaction, password or key is carried through
        # the private HTTP calls. Lock/disconnect can revoke the result promptly.
        result = review_perp_live(self._lab, ObservedArchive(PaperPerpStore(paper_path, allow_create=False)),
            session_id, sub_account_id, client, clock_ns=time.time_ns, require_permission=checked)
        checked()
        return result.model_dump(mode='json', by_alias=True)

    def grvt_open_orders(self, token: str | None) -> dict:
        with self._lock:
            self.require(token)
            if self._grvt is None:
                raise HTTPException(status_code=409, detail="GRVT no está conectada")
            observed = self._grvt.open_orders()
            self.require(token)
            return observed

    def grvt_order(self, token: str | None, sub_account_id: str,
                   client_order_id: str) -> dict:
        with self._lock:
            self.require(token)
            if self._grvt is None:
                raise HTTPException(status_code=409, detail="GRVT no está conectada")
            observed = self._grvt.order(sub_account_id, client_order_id)
            self.require(token)
            return observed

    def grvt_order_fills(self, token: str | None, sub_account_id: str,
                         client_order_id: str) -> dict:
        with self._lock:
            self.require(token)
            if self._grvt is None:
                raise HTTPException(status_code=409, detail="GRVT no está conectada")
            observed = self._grvt.order_fills(sub_account_id, client_order_id)
            self.require(token)
            return observed

    def archive_grvt_order_fills(self, token: str | None, sub_account_id: str,
                                 client_order_id: str) -> dict:
        with self._lock:
            self.require(token)
            if self._grvt is None:
                raise HTTPException(status_code=409, detail="GRVT no está conectada")
            observed = self._grvt.order_fills(sub_account_id, client_order_id)
            self.require(token)
            with self.vault.session() as db:
                archived = save_evidence(db, observed)
            return {**observed, "archive": archived}

    def grvt_evidence(self, token: str | None, identifier: str | None = None) -> dict | list[dict]:
        with self._lock:
            self.require(token)
            with self.vault.session() as db:
                return read_evidence(db, identifier) if identifier is not None else list_evidence(db)

    def disconnect_grvt(self, token: str | None) -> dict:
        with self._lock:
            self.require(token)
            self._grvt = None
            return {"connected": False}


def create_real_router(path: Path, *, lab_path: Path | None = None,
                       paper_perp_path: Path | None = None,
                       spot_paper=None, mexc_bot_supervisor_factory=MexcBotSupervisor, mexc_bot_clock=None,dca_paper=None,
                       mexc_factory=MexcReadOnlyClient,
                       grvt_factory=GrvtReadOnlyClient, mexc_quote_get=None,
                       mexc_trade_factory=MexcTradeIdentityProbe,
                       mexc_order_test_factory=MexcSpotOrderTestClient,
                       mexc_rules_fetch=fetch_spot_rules,
                       mexc_live_factory=MexcSpotLiveClient,
                       mexc_withdrawal_factory=MexcWithdrawalClient,
                       dex_rpc_factory=None,dex_bot_supervisor_factory=DexBotSupervisor,dex_return_sender_factory=None,dex_cancel_sender_factory=None,
                       mexc_stop_supervisor_factory=MexcStopSupervisor,
                       grvt_testnet_factory=GrvtTestnetOrderClient,
                       grvt_testnet_oco_public_post=None,
                       grvt_testnet_oco_clock_ns=time.time_ns,
                       grvt_testnet_bracket_public_post=None,
                       grvt_testnet_bracket_clock_ns=time.time_ns,
                       grvt_testnet_bracket_close_waiter=None,
                       grvt_testnet_exit_stream_factory=None,
                       grvt_testnet_signal_submitter=submit_signal_testnet_once,
                       grvt_testnet_signal_signed_submitter=sign_and_submit_signal_testnet_once,
                       grvt_testnet_auditor=audit_testnet_intent,
                       grvt_testnet_cancel_auditor=audit_testnet_cancel,
                       grvt_testnet_canceller=stop_and_cancel_testnet_once,
                       grvt_mainnet_factory=None,grvt_mainnet_public_post=None,grvt_mainnet_clock_ns=None,
                       grvt_mainnet_waiter=None,grvt_mainnet_bot_poll_seconds=2,
                       register_storage_idle: Callable[[Callable[[], bool]], None] | None=None) -> APIRouter:
    access = RealAccess(path, lab_path=lab_path,
                        mexc_factory=mexc_factory, grvt_factory=grvt_factory,
                        mexc_quote_get=mexc_quote_get,
                        mexc_trade_factory=mexc_trade_factory,
                        mexc_order_test_factory=mexc_order_test_factory,
                        mexc_rules_fetch=mexc_rules_fetch,
                        mexc_live_factory=mexc_live_factory,
                        mexc_stop_supervisor_factory=mexc_stop_supervisor_factory,
                        spot_paper=spot_paper, mexc_bot_supervisor_factory=mexc_bot_supervisor_factory,
                        mexc_bot_clock=mexc_bot_clock,dca_paper=dca_paper,dex_bot_supervisor_factory=dex_bot_supervisor_factory)

    if register_storage_idle is not None:
        def storage_idle() -> bool:
            with access._lock:
                return not access.vault.unlocked and not access.operational_status()['active']
        register_storage_idle(storage_idle)

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            access.lock()

    router = APIRouter(prefix="/real", tags=["real-portfolio"], lifespan=lifespan)

    def local_only(request: Request) -> None:
        host = request.client.host if request.client else None
        server = request.url.hostname
        if host not in LOCAL_HOSTS or server not in LOCAL_HOSTS:
            raise HTTPException(status_code=403, detail="Real portfolio is local-only")
        origin = request.headers.get("origin")
        if origin is not None and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Cross-origin access denied")

    def authorized(
        request: Request,
        _: None = Depends(local_only),
        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token"),
        passive: str | None = Header(default=None, alias="X-Orbis-Passive"),
    ) -> None:
        # Sólo las lecturas explícitamente pasivas (p. ej. resumen de Inicio)
        # consultan sin prolongar el desbloqueo por inactividad.
        access.require(token, touch=not (request.method == "GET" and passive == "1"))

    def authorized_passive(
        _: None = Depends(local_only),
        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token"),
    ) -> None:
        # El sondeo de seguridad nunca prolonga el desbloqueo por inactividad.
        access.require(token, touch=False)

    @router.get("/vault/status", dependencies=[Depends(local_only)])
    def status(response:Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        access._expire()
        operating = access.operational_status()
        return {"exists": access.vault.path.exists(), "unlocked": access.vault.unlocked,
                "capabilities":{"dex_receipt_order_proof":True,"dex_gas_order_proof":True,"economic_review":True,"economic_review_gas":True,"economic_review_order":True,"economic_review_wallets":True,"external_recovery":True},
                **({"review_required":True} if access._review_required else {}),
                **({"view_protected":access._token is None,"operating":operating} if operating["active"] else {})}

    @router.post("/vault/protect-view",dependencies=[Depends(authorized)])
    def protect_view():
        return access.protect_view()

    @router.post("/operations/stop",dependencies=[Depends(local_only)])
    def stop_all_operational(body:StopAllOperational):
        # Sólo parada local: nunca habilita, firma, compra, vende o retira.
        review = access.lock()
        return {"locked":True,**({"review_required":True} if review else {})}

    @router.post("/vault/create", dependencies=[Depends(local_only)])
    def create(body: VaultPassphrase) -> dict:
        try:
            return {"token": access.unlock(body.passphrase, create=True), "migrated_backup": None}
        except VaultError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/vault/unlock", dependencies=[Depends(local_only)])
    def unlock(body: VaultPassphrase) -> dict:
        try:
            token = access.unlock(body.passphrase)
            backup = access.vault.last_migration_backup
            return {"token": token, "migrated_backup": backup.name if backup else None}
        except VaultError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @router.post("/vault/lock", dependencies=[Depends(authorized)])
    def lock() -> dict:
        review_required = access.lock()
        return {"locked":True,**({"review_required":True} if review_required else {})}

    @router.get("/exchanges/mexc/status", dependencies=[Depends(authorized)])
    def mexc_status(response: Response,
                    token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_status(token)

    @router.get("/exchanges/mexc/trade/status", dependencies=[Depends(authorized)])
    def mexc_trade_status(response: Response,
                          token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_trade_status(token)

    @router.post("/accounts/{account_id}/mexc-policy-drafts", dependencies=[Depends(authorized)])
    def create_mexc_policy_draft(account_id: str, body: MexcPolicyDraft, response: Response,
                                 token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.create_mexc_policy_draft(token, account_id, body)

    @router.get("/accounts/{account_id}/mexc-policy-drafts", dependencies=[Depends(authorized)])
    def list_mexc_policy_drafts(account_id: str, response: Response,
                                token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        return access.list_mexc_policy_drafts(token, account_id)

    @router.post("/accounts/{account_id}/mexc-manual-limits", dependencies=[Depends(authorized)])
    def create_mexc_manual_limits(account_id: str, body: MexcManualLimits, response: Response,
                                  token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.create_mexc_policy_draft(token, account_id, body)

    @router.get("/accounts/{account_id}/mexc-manual-limits", dependencies=[Depends(authorized)])
    def list_mexc_manual_limits(account_id: str, response: Response,
                                token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        return access.list_mexc_policy_drafts(token, account_id, manual_only=True)

    @router.post("/accounts/{account_id}/mexc-policy-drafts/{policy_id}/activate",
                 dependencies=[Depends(authorized)])
    def activate_mexc_execution(account_id: str, policy_id: UUID,
                                body: MexcActivateExecution, response: Response,
                                token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.activate_mexc_execution(token, account_id, str(policy_id), body)
        except (MexcExecutionError, MexcExecutionStateError, MexcOrderSendError,
                LiveJournalError, MexcParityArchiveError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("/accounts/{account_id}/mexc-trading-sessions", dependencies=[Depends(authorized)])
    def list_mexc_trading_sessions(account_id: str, response: Response,
                                   token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.list_mexc_execution_policies(token, account_id)
        except LiveJournalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("/accounts/{account_id}/mexc-execution-status", dependencies=[Depends(authorized)])
    def mexc_execution_status(account_id: str, response: Response,
                               token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_execution_status(token, account_id)

    @router.get("/accounts/{account_id}/mexc-workflow", dependencies=[Depends(authorized_passive)])
    def mexc_workflow(account_id: str, response: Response,
                      token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_workflow(token, account_id)

    @router.get("/accounts/{account_id}/mexc-pending-orders", dependencies=[Depends(authorized_passive)])
    def mexc_pending_orders(account_id: UUID, response: Response,
                            token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_pending_orders(token, str(account_id))

    @router.post("/accounts/{account_id}/mexc-stop", dependencies=[Depends(authorized)])
    def mexc_stop_execution(account_id: str, body: MexcStopAccount, response: Response,
                             token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.stop_mexc_execution(token, account_id)

    @router.post("/accounts/{account_id}/mexc-policy-drafts/{policy_id}/resume",
                 dependencies=[Depends(authorized)])
    def resume_mexc_execution(account_id: str, policy_id: UUID,
                              body: MexcResumeExecution, response: Response,
                              token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.resume_mexc_execution(token, account_id, str(policy_id), body)
        except (MexcExecutionError, MexcExecutionStateError, MexcOrderSendError,
                LiveJournalError, MexcParityArchiveError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/mexc-trading-sessions/{policy_id}/orders/review",
                 dependencies=[Depends(authorized)])
    def review_mexc_live_order(account_id: str, policy_id: UUID, body: MexcOrderReview | MexcOrderBudgetReview | MexcOrderMaximumSellReview,
                               response: Response,
                               token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.review_mexc_execution_order(token, account_id, str(policy_id), body)
        except (MexcExecutionError, MexcExecutionStateError, MexcOrderSendError, LiveJournalError) as exc:
            raise HTTPException(status_code=409, detail=str(exc), headers={"Cache-Control": "no-store"}) from None
        except VaultError:
            raise HTTPException(status_code=401, detail="Real vault is locked", headers={"Cache-Control": "no-store"}) from None

    @router.post("/accounts/{account_id}/mexc-trading-sessions/{policy_id}/orders",
                 dependencies=[Depends(authorized)])
    def submit_mexc_live_order(account_id: str, policy_id: UUID, body: MexcLiveLimitOrder,
                               response: Response,
                               token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.submit_mexc_execution_order(token, account_id, str(policy_id), body)
        except (MexcExecutionError, MexcExecutionStateError, MexcOrderSendError, LiveJournalError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/mexc-policy-drafts/{policy_id}/order-test",
                 dependencies=[Depends(authorized)])
    def test_mexc_policy_draft_buy(account_id: str, policy_id: UUID,
                                   body: MexcDraftOrderTest, response: Response,
                                   token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.test_mexc_policy_draft_buy(token, account_id, str(policy_id), body)

    @router.get("/accounts/{account_id}/mexc-policy-drafts/{policy_id}/order-tests",
                dependencies=[Depends(authorized)])
    def list_mexc_policy_order_tests(account_id: str, policy_id: UUID, response: Response,
                                     token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        return access.list_mexc_policy_order_tests(token, account_id, str(policy_id))

    @router.get("/accounts/{account_id}/mexc-policy-drafts/{policy_id}/daily-book-risk",
                dependencies=[Depends(authorized)])
    def mexc_policy_daily_book_risk(account_id: str, policy_id: UUID, response: Response,
                                    token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_policy_daily_book_risk(token, account_id, str(policy_id))

    @router.get("/accounts/{account_id}/mexc-policy-drafts/{policy_id}/spot-exposure",
                dependencies=[Depends(authorized)])
    def mexc_policy_spot_exposure(account_id: str, policy_id: UUID, response: Response,
                                  token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_policy_spot_exposure(token, account_id, str(policy_id))

    @router.get("/accounts/{account_id}/mexc-link", dependencies=[Depends(authorized)])
    def mexc_account_link_status(account_id: str, response: Response,
                                 token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_account_link_status(token, account_id)

    @router.post("/accounts/{account_id}/mexc-link", dependencies=[Depends(authorized)])
    def link_mexc_account(account_id: str, body: MexcAccountLinkConfirm, response: Response,
                          token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.link_mexc_account(token, account_id)

    @router.post("/accounts/{account_id}/mexc-link/renew-view", dependencies=[Depends(authorized)])
    def renew_mexc_view(account_id: UUID, body: MexcRenewViewConfirm, response: Response,
                        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.renew_mexc_view(token, str(account_id))
        except MexcUidGuardError as exc:
            raise HTTPException(status_code=409, detail=str(exc), headers={"Cache-Control": "no-store"}) from None
        except MexcReadOnlyError:
            raise HTTPException(status_code=503, detail="No se pudo verificar la clave View renovada",
                                headers={"Cache-Control": "no-store"}) from None

    @router.post("/exchanges/mexc/trade/connect", dependencies=[Depends(authorized)])
    def mexc_trade_connect(body: MexcConnect, response: Response,
                           token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.connect_mexc_trade(token, body.api_key.get_secret_value(),
                                             body.secret_key.get_secret_value())
        except MexcTradeIdentityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/exchanges/mexc/trade/disconnect", dependencies=[Depends(authorized)])
    def mexc_trade_disconnect(response: Response,
                              token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.disconnect_mexc_trade(token)

    @router.post("/exchanges/mexc/connect", dependencies=[Depends(authorized)])
    def mexc_connect(body: MexcConnect, response: Response,
                     token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.connect_mexc(token, body.api_key.get_secret_value(),
                                       body.secret_key.get_secret_value())
        except MexcUidGuardError as exc:
            raise HTTPException(status_code=409, detail=str(exc), headers={"Cache-Control": "no-store"}) from None
        except MexcReadOnlyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/exchanges/mexc/balances", dependencies=[Depends(authorized)])
    def mexc_balances(response: Response,
                      token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_balances(token)
        except MexcReadOnlyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @router.get("/accounts/{account_id}/mexc-erc20-deposit-destination",
                dependencies=[Depends(authorized)])
    def mexc_erc20_deposit_destination(account_id: str, asset: str, response: Response, chain_id: str = "0x1",
                                      token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_erc20_deposit_destination(token, account_id, asset, chain_id)

    @router.get("/accounts/{account_id}/mexc-spot-comparison", dependencies=[Depends(authorized)])
    def mexc_spot_comparison(account_id: str, response: Response,
                             token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_compare_account(token, account_id)
        except MexcReadOnlyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except PortfolioError as exc:
            raise HTTPException(status_code=404 if str(exc) == "Real account not found" else 422,
                                detail=str(exc)) from None

    @router.get("/accounts/{account_id}/mexc-spot-parity", dependencies=[Depends(authorized)])
    def mexc_spot_parity(account_id: str, response: Response,
                         token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_spot_parity(token, account_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/accounts/{account_id}/mexc-open-orders-survey", dependencies=[Depends(authorized)])
    def mexc_open_orders_survey(account_id: str, response: Response,
                                token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_open_orders_survey(token, account_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/mexc-open-orders-survey/observations",
                 dependencies=[Depends(authorized)])
    def save_mexc_open_orders_survey(account_id: str, body: MexcOpenOrdersSave, response: Response,
                                     token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.save_mexc_open_orders_survey(token, account_id, body.request_id)
        except MexcOpenOrdersArchiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("/accounts/{account_id}/mexc-open-orders-survey/observations",
                dependencies=[Depends(authorized)])
    def list_mexc_open_orders_survey(account_id: str, response: Response,
                                     token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        return access.list_mexc_open_orders_survey(token, account_id)

    @router.get("/accounts/{account_id}/mexc-open-orders-survey/observations/{before_id}/compare/{after_id}",
                dependencies=[Depends(authorized)])
    def compare_mexc_open_orders_survey(account_id: str, before_id: UUID, after_id: UUID,
                                        response: Response,
                                        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.compare_mexc_open_orders_survey(token, account_id, before_id, after_id)

    @router.post("/accounts/{account_id}/mexc-spot-parity/observations", dependencies=[Depends(authorized)])
    def save_mexc_spot_parity(account_id: str, body: MexcParitySave, response: Response,
                              token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.save_mexc_spot_parity(token, account_id, body.request_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except MexcParityArchiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("/accounts/{account_id}/mexc-spot-parity/observations", dependencies=[Depends(authorized)])
    def list_mexc_spot_parity(account_id: str, response: Response,
                              token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        return access.list_mexc_spot_parity(token, account_id)

    @router.post("/accounts/{account_id}/mexc-opening/preview", dependencies=[Depends(authorized)])
    def mexc_opening_preview(account_id: str, body: MexcOpeningRequest, response: Response,
                             token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_opening(token, account_id, body)
        except RequestConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/mexc-opening/commit", dependencies=[Depends(authorized)])
    def mexc_opening_commit(account_id: str, body: MexcOpeningCommit, response: Response,
                            token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_opening(token, account_id, body, commit=True)
        except RequestConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/exchanges/mexc/valuation", dependencies=[Depends(authorized)])
    def mexc_valuation(response: Response,
                       quote: Literal["USDT", "EUR"] = Query(default="USDT"),
                       token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_valuation(token, quote)
        except MexcReadOnlyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except MexcPreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/exchanges/mexc/identity", dependencies=[Depends(authorized)])
    def mexc_identity(response: Response,
                      token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_identity(token)
        except MexcReadOnlyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except MexcUidGuardError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("/exchanges/mexc/transfers", dependencies=[Depends(authorized)])
    def mexc_transfers(response: Response, days: Literal["1", "7", "30", "90"] = Query(default="7"),
                       token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_transfers_recent(token, int(days))
        except MexcReadOnlyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PortfolioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("/exchanges/mexc/transfers/archive", dependencies=[Depends(authorized)])
    def mexc_transfer_archive(response: Response,
                              token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_transfer_snapshots(token)

    @router.get("/exchanges/mexc/transfers/archive/{identifier}", dependencies=[Depends(authorized)])
    def mexc_transfer_archive_detail(identifier: str, response: Response,
                                     token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_saved_transfer_snapshot(token, identifier)
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/exchanges/mexc/transfers/archive/{before_id}/compare/{after_id}",
                dependencies=[Depends(authorized)])
    def mexc_transfer_archive_compare(before_id: str, after_id: str, response: Response,
                                      token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_compare_transfer_snapshots(token, before_id, after_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/accounts/{account_id}/mexc-transfer-candidates/{identifier}", dependencies=[Depends(authorized)])
    def mexc_transfer_candidates(account_id: str, identifier: str, response: Response,
                                 token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Pistas de una fotografía guardada; no importa ni concilia movimientos."""
        response.headers["Cache-Control"] = "no-store"
        try:
            access.require(token)
            with access.vault.session() as db:
                return compare_mexc_transfer_snapshot(db, account_id, identifier)
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/accounts/{account_id}/mexc-transfer-audit", dependencies=[Depends(authorized)])
    def mexc_transfer_audit(account_id: str, response: Response,
                            token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Cobertura y pendientes de la conexión local; nunca estado conciliado."""
        response.headers["Cache-Control"] = "no-store"
        try:
            access.require(token)
            with access.vault.session() as db:
                return audit_mexc_transfer_archive(db, account_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/mexc-deposit-tx-lookup", dependencies=[Depends(authorized)])
    async def mexc_deposit_tx_lookup(account_id: str, request: Request, response: Response,
                                     token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Busca sólo en capturas cifradas; el tx hash no se persiste ni sale en la respuesta."""
        response.headers["Cache-Control"] = "no-store"
        # No delegar la deserialización sensible a FastAPI/Pydantic: sus errores
        # pueden repetir `input` completo, incluyendo hash y direcciones.
        payload_bytes = bytearray()
        async for chunk in request.stream():
            payload_bytes.extend(chunk)
            if len(payload_bytes) > 4096:
                raise HTTPException(status_code=413, detail="Consulta demasiado grande")
        try:
            body = json.loads(payload_bytes)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(status_code=422, detail="Consulta EVM inválida") from None
        if not isinstance(body, dict) or set(body) not in ({"tx_hash"}, {"tx_hash", "destinations"}):
            raise HTTPException(status_code=422, detail="Consulta EVM inválida")
        try:
            access.require(token)
            with access.vault.session() as db:
                return find_mexc_deposit_by_evm_tx_hash(db, account_id,
                                                        body["tx_hash"],
                                                        body.get("destinations"))
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/mexc-withdrawal-tx-lookup", dependencies=[Depends(authorized)])
    async def mexc_withdrawal_tx_lookup(account_id: str, request: Request, response: Response,
                                        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Hash y receptor transitorios; sin reflejar datos sensibles en errores."""
        response.headers["Cache-Control"] = "no-store"
        payload_bytes = bytearray()
        async for chunk in request.stream():
            payload_bytes.extend(chunk)
            if len(payload_bytes) > 4096:
                raise HTTPException(status_code=413, detail="Consulta demasiado grande")
        try:
            body = json.loads(payload_bytes)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(status_code=422, detail="Consulta EVM inválida") from None
        if not isinstance(body, dict) or set(body) not in ({"tx_hash"}, {"tx_hash", "recipient"}):
            raise HTTPException(status_code=422, detail="Consulta EVM inválida")
        try:
            access.require(token)
            with access.vault.session() as db:
                return find_mexc_withdrawal_by_evm_tx_hash(
                    db, account_id, body["tx_hash"], body.get("recipient"))
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/mexc-transfer-matches", dependencies=[Depends(authorized)])
    def mexc_transfer_match(account_id: str, body: MexcTransferMatchRequest,
                            response: Response,
                            token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Sólo fija una correspondencia revisada; no importa ni transfiere."""
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.confirm_mexc_transfer_match(token, account_id, body)
        except PortfolioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    def read_mexc_direct_journeys(token: str | None, response: Response, operation) -> dict:
        response.headers["Cache-Control"] = "no-store"
        with access._lock:
            access.require(token, touch=False)
            generation = access._vault_generation
        try:
            with access.vault.guard_session_generation(generation), access.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN")
                return operation(db)
        except (PortfolioError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc), headers={"Cache-Control":"no-store"}) from None
        except VaultError:
            raise HTTPException(status_code=401, detail="La sesión de la bóveda REAL cambió",
                                headers={"Cache-Control":"no-store"}) from None

    @router.get("/accounts/{account_id}/mexc-external-deposits/journeys", dependencies=[Depends(authorized_passive)])
    def mexc_direct_journeys(account_id: UUID, response: Response,
                             offset: int = Query(0, ge=0, le=1_000_000), limit: int = Query(20, ge=1, le=50),
                             token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        from app.portfolio.mexc_roundtrip_audit import list_mexc_direct_journeys

        return read_mexc_direct_journeys(token, response, lambda db: list_mexc_direct_journeys(
            db, str(account_id), offset, limit))

    @router.get("/accounts/{account_id}/mexc-external-deposits/journeys/{event_id}",
                dependencies=[Depends(authorized_passive)])
    def mexc_direct_journey(account_id: UUID, event_id: UUID, response: Response,
                            token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        from app.portfolio.mexc_roundtrip_audit import review_mexc_direct_journey

        return read_mexc_direct_journeys(token, response, lambda db: review_mexc_direct_journey(
            db, str(account_id), str(event_id)))

    @router.post("/accounts/{account_id}/mexc-external-deposits/preview", dependencies=[Depends(authorized)])
    def preview_mexc_external_deposits(account_id: str, body: MexcExternalDepositSelection, response: Response,
                                       token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_external_deposits(token, account_id, body)
        except PortfolioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/mexc-external-deposits/commit", dependencies=[Depends(authorized)])
    def commit_mexc_external_deposits(account_id: str, body: MexcExternalDepositCommit, response: Response,
                                      token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_external_deposits(token, account_id, body, expected_hash=body.preview_hash)
        except MexcReadOnlyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except PortfolioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("/exchanges/mexc/trades/{symbol}", dependencies=[Depends(authorized)])
    def mexc_trades(symbol: str, response: Response,
                    token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_trades(token, symbol)
        except MexcReadOnlyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PortfolioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/exchanges/mexc/trades/{symbol}/window", dependencies=[Depends(authorized)])
    def mexc_trades_window(symbol: str, body: MexcWindow, response: Response,
                           token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_trades_window(token, symbol, body.days)
        except MexcReadOnlyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PortfolioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("/exchanges/mexc/observations", dependencies=[Depends(authorized)])
    def mexc_observations(response: Response,
                          token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_observations(token)

    @router.get("/exchanges/mexc/observations/{account_ref}/{symbol}", dependencies=[Depends(authorized)])
    def mexc_saved_trades(account_ref: str, symbol: str, response: Response,
                          token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_saved_trades(token, account_ref, symbol)
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/exchanges/mexc/observations/{account_ref}/{symbol}/coverage", dependencies=[Depends(authorized)])
    def mexc_archived_trade_coverage(account_ref: str, symbol: str, response: Response,
                                     start_ms: int = Query(...), end_ms: int = Query(...),
                                     token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.mexc_archived_trade_coverage(token, account_ref, symbol, start_ms, end_ms)
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/exchanges/mexc/disconnect", dependencies=[Depends(authorized)])
    def mexc_disconnect(response: Response,
                        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.disconnect_mexc(token)

    @router.get("/execution/mexc/intents", dependencies=[Depends(authorized)])
    def mexc_execution_intents(response: Response) -> list[dict]:
        """Estado local de intenciones y órdenes observadas, sin consulta a MEXC."""
        response.headers["Cache-Control"] = "no-store"
        return list_spot_execution_intents(access.vault)

    @router.post("/execution/mexc/policies/{policy_id}/trade-window-audit",
                 dependencies=[Depends(authorized)])
    def mexc_trade_window_audit(policy_id: str, body: MexcWindow, response: Response,
                                token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_trade_window_audit(token, policy_id, body.days)

    @router.post("/execution/mexc/policies/{policy_id}/transfer-window-audit",
                 dependencies=[Depends(authorized)])
    def mexc_transfer_window_audit(policy_id: str, body: MexcTransferWindow,
                                   response: Response,
                                   token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_transfer_window_audit(token, policy_id, body.days)

    @router.post("/execution/mexc/intents/{intent_id}/stop-account", dependencies=[Depends(authorized)])
    def mexc_stop_account(intent_id: str, body: MexcStopAccount, response: Response) -> dict:
        """Sólo parada local; no cancela ni consulta órdenes en MEXC."""
        response.headers["Cache-Control"] = "no-store"
        try:
            return stop_spot_account_for_intent(access.vault, intent_id,
                                                now=datetime.now(timezone.utc))
        except LiveJournalError as exc:
            status = 404 if "no encontrada" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from None

    @router.get("/execution/mexc/stop-supervisor/status", dependencies=[Depends(authorized_passive)])
    def mexc_stop_supervisor_status(response: Response,
                                    token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.mexc_stop_supervisor_status(token)

    @router.post("/execution/mexc/intents/{intent_id}/supervise-stop", dependencies=[Depends(authorized)])
    def mexc_supervise_stop(intent_id: str, body: MexcSuperviseStop, response: Response,
                            token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Puede cancelar UNA orden propia abierta; nunca envía órdenes nuevas."""
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.start_mexc_stop_supervisor(token, intent_id)
        except (LiveJournalError, MexcUidGuardError, MexcOrderSendError,
                MexcStopSupervisorError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/execution/mexc/stop-supervisor/stop", dependencies=[Depends(authorized)])
    def mexc_stop_supervision(body: MexcStopSupervision, response: Response,
                              token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.stop_mexc_stop_supervisor(token)

    @router.post("/execution/mexc/intents/{intent_id}/observe-stopped", dependencies=[Depends(authorized)])
    def mexc_observe_stopped(intent_id: str, response: Response,
                            token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Consulta orden y fills con View; no emite DELETE ni libera capital."""
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.observe_stopped_mexc_intent(token, intent_id)
        except (MexcOrderObservationError, LiveJournalError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/execution/mexc/intents/{intent_id}/synchronize", dependencies=[Depends(authorized)])
    def mexc_synchronize_order(intent_id: str, body: MexcSynchronizeOrder, response: Response,
                               token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Consulta una orden propia y registra sus hechos; no envía órdenes ni retiros."""
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.synchronize_mexc_order(token, intent_id, str(body.account_id))
        except (MexcLifecycleError, MexcOrderObservationError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/execution/mexc/intents/{intent_id}/synchronize-renewed-view", dependencies=[Depends(authorized)])
    def mexc_synchronize_renewed_view(intent_id: UUID, body: MexcRecoverWithRenewedView, response: Response,
                                      token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.synchronize_mexc_order(token, str(intent_id), str(body.account_id), renewed_view=True)
        except (MexcLifecycleError, MexcOrderObservationError) as exc:
            raise HTTPException(status_code=409, detail=str(exc), headers={"Cache-Control": "no-store"}) from None

    @router.post("/execution/mexc/intents/{intent_id}/cancel", dependencies=[Depends(authorized)])
    def mexc_cancel_selected_order(intent_id: UUID, body: MexcCancelSelectedOrder, response: Response,
                                   token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> MexcSelectedCancellation:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.cancel_selected_mexc_order(token, str(intent_id), str(body.account_id))
        except (MexcOrderSendError, MexcOrderObservationError, MexcLifecycleError, LiveJournalError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/execution/mexc/intents/{intent_id}/release-unsent", dependencies=[Depends(authorized)])
    def mexc_release_unsent(intent_id: UUID, body: MexcReleaseUnsent, response: Response,
                            token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.release_mexc_unsent(token, str(body.account_id), str(intent_id))
        except LiveJournalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/execution/mexc/intents/{intent_id}/book", dependencies=[Depends(authorized)])
    def mexc_book_intent(intent_id: str, body: MexcBookOrder, response: Response) -> dict:
        """Asienta fills observados tras confirmación humana; no envía órdenes."""
        response.headers["Cache-Control"] = "no-store"
        try:
            return book_observed_spot_order(access.vault, intent_id, body.account_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/execution/mexc/intents/{intent_id}/close-reservation", dependencies=[Depends(authorized)])
    def mexc_close_reservation(intent_id: str, body: MexcCloseReservation, response: Response,
                               token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Reconsulta MEXC con la clave View conectada antes de cerrar una reserva."""
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.close_mexc_reservation(token, intent_id)
        except MexcSettlementError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("/exchanges/grvt/status", dependencies=[Depends(authorized)])
    def grvt_status(response: Response,
                    token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.grvt_status(token)

    @router.post("/exchanges/grvt/testnet/signals/submit",
                 dependencies=[Depends(authorized)])
    def grvt_testnet_signal_submit(body: GrvtTestnetSignalSubmit, response: Response,
                                   token: str | None = Header(
                                       default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Un único intento IOC firmado por señal PAPER; nunca usa mainnet."""
        response.headers["Cache-Control"] = "no-store"
        if lab_path is None or paper_perp_path is None:
            raise HTTPException(status_code=503, detail="GRVT testnet no está configurado")
        try:
            cap = Decimal(body.max_order_notional)
            if not cap.is_finite() or cap <= 0 or cap > Decimal("100"):
                raise ValueError
            limits = GrvtTestnetLimits(body.sub_account_id, body.instrument, cap)
            client_order_id = _validate_order(body.order, limits, time.time_ns())
            if client_order_id != client_id_for_signal(body.session_id, body.action_request_id):
                raise ValueError
        except (InvalidOperation, ValueError, GrvtTestnetOrderError,
                GrvtTestnetPreflightError):
            raise HTTPException(status_code=422,
                                detail="Orden GRVT testnet o límite inválido") from None
        with access._lock:
            access.require(token)
            try:
                paper = PaperPerpStore(paper_perp_path, allow_create=False)
                lab = LabStore(lab_path, allow_create=False)
                # Un identificador inventado no debe provocar siquiera un login
                # externo. La verificación completa se repite bajo transacción.
                with paper.connect() as db:
                    action = db.execute(
                        "SELECT 1 FROM paper_signal_actions WHERE session_id=? AND request_id=?",
                        (body.session_id, body.action_request_id),
                    ).fetchone()
                if action is None:
                    raise GrvtTestnetSignalError("Acción GRVT PAPER no encontrada")
                require_new_testnet_submission(access.vault, body.sub_account_id,
                                               client_order_id)
                client = grvt_testnet_factory(body.api_key.get_secret_value())
                binding, result = grvt_testnet_signal_submitter(
                    paper, lab, access.vault, client, body.order, limits,
                    session_id=body.session_id,
                    action_request_id=body.action_request_id,
                )
            except GrvtTestnetOrderError:
                raise HTTPException(status_code=422,
                                    detail="Clave u orden GRVT testnet rechazada; comprueba el registro por ID") from None
            except (GrvtTestnetSignalError, GrvtTestnetRiskError,
                    GrvtTestnetJournalError, GrvtTestnetPreflightError,
                    PaperPerpError, LabError, VaultError):
                raise HTTPException(status_code=409,
                                    detail="Envío GRVT testnet bloqueado o incierto; consulta el registro por ID") from None
            except Exception:
                raise HTTPException(status_code=503,
                                    detail="GRVT testnet no verificable; consulta el registro antes de reintentar") from None
        return {"network": "TESTNET", "client_order_id": client_order_id,
                "session_id": binding.session_id, "action_request_id": binding.action_request_id,
                "journal_status": result.status, "external_status": result.external_status,
                "execution_unverified": True, "mainnet_order_sent": False}

    @router.get("/exchanges/grvt/testnet/signals/candidates",
                dependencies=[Depends(authorized_passive)])
    def grvt_testnet_signal_candidates(response: Response,
                                      token: str | None = Header(
                                          default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        if paper_perp_path is None or lab_path is None:
            raise HTTPException(status_code=503, detail="GRVT testnet no está configurado")
        with access._lock:
            access.require(token, touch=False)
            try:
                items = list_testnet_signal_candidates(
                    PaperPerpStore(paper_perp_path, allow_create=False), now_ns=time.time_ns())
            except (PaperPerpError, GrvtTestnetSignalError):
                raise HTTPException(status_code=409,
                                    detail="Desbloquea GRVT PAPER para consultar sus señales") from None
        return {"network": "TESTNET", "authorization": False, "items": items}

    @router.post("/exchanges/grvt/testnet/signals/sign-and-submit",
                 dependencies=[Depends(authorized)])
    def grvt_testnet_signal_sign_and_submit(body: GrvtTestnetSignalSignAndSubmit,
                                            response: Response,
                                            token: str | None = Header(
                                                default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Firma con clave Trade efímera y delega el único envío testnet al puente PAPER."""
        response.headers["Cache-Control"] = "no-store"
        if lab_path is None or paper_perp_path is None:
            raise HTTPException(status_code=503, detail="GRVT testnet no está configurado")
        try:
            cap = Decimal(body.max_order_notional)
            if not cap.is_finite() or cap <= 0 or cap > Decimal("100"):
                raise ValueError
            limits = GrvtTestnetLimits(body.sub_account_id, body.instrument, cap)
            size = Decimal(body.size)
            price = Decimal(body.limit_price)
            with localcontext() as context:
                context.prec = 80
                if size <= 0 or price <= 0 or size * price > cap:
                    raise ValueError
            client_order_id = client_id_for_signal(body.session_id, body.action_request_id)
        except (InvalidOperation, ValueError, GrvtTestnetOrderError,
                GrvtTestnetPreflightError):
            raise HTTPException(status_code=422,
                                detail="Importe o límite GRVT testnet inválido") from None
        with access._lock:
            access.require(token)
            try:
                paper = PaperPerpStore(paper_perp_path, allow_create=False)
                lab = LabStore(lab_path, allow_create=False)
                # Nunca consultar metadatos ni autenticar con una acción inventada.
                with paper.connect() as db:
                    action = db.execute(
                        "SELECT 1 FROM paper_signal_actions WHERE session_id=? AND request_id=?",
                        (body.session_id, body.action_request_id),
                    ).fetchone()
                if action is None:
                    raise GrvtTestnetSignalError("Acción GRVT PAPER no encontrada")
                require_new_testnet_submission(access.vault, body.sub_account_id,
                                               client_order_id)
                binding, result = grvt_testnet_signal_signed_submitter(
                    paper, lab, access.vault, grvt_testnet_factory,
                    body.api_key.get_secret_value(), limits,
                    session_id=body.session_id,
                    action_request_id=body.action_request_id,
                    dedicated_key_hex=body.dedicated_private_key.get_secret_value(),
                    expected_signer=body.expected_signer, builder=body.builder,
                    side=body.side, size=size, limit_price=price,
                    reduce_only=body.reduce_only,
                    max_spread_bps=body.max_spread_bps,
                    max_slippage_bps=body.max_slippage_bps,
                )
            except GrvtTestnetSignError:
                raise HTTPException(status_code=422,
                                    detail="Firma u orden GRVT testnet no verificable") from None
            except GrvtTestnetOrderError:
                raise HTTPException(status_code=422,
                                    detail="Clave u orden GRVT testnet rechazada; audita el ID antes de reintentar") from None
            except (GrvtTestnetSignalError, GrvtTestnetQuoteError, GrvtTestnetRiskError,
                    GrvtTestnetJournalError, GrvtTestnetPreflightError,
                    PaperPerpError, LabError, VaultError):
                raise HTTPException(status_code=409,
                                    detail="Envío GRVT testnet bloqueado o incierto; consulta el registro por ID") from None
            except Exception:
                raise HTTPException(status_code=503,
                                    detail="GRVT testnet no verificable; consulta el registro antes de reintentar") from None
        return {"network": "TESTNET", "client_order_id": binding.client_order_id,
                "session_id": binding.session_id, "action_request_id": binding.action_request_id,
                "journal_status": result.status, "external_status": result.external_status,
                "signed_locally": True, "execution_unverified": True,
                "mainnet_order_sent": False}

    @router.post("/exchanges/grvt/testnet/subaccounts/{sub_account_id}/stop",
                 dependencies=[Depends(authorized)])
    def grvt_testnet_stop(sub_account_id: str, body: GrvtTestnetStopRequest,
                          response: Response,
                          token: str | None = Header(
                              default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Parada local duradera; no presume cancelar órdenes en GRVT."""
        response.headers["Cache-Control"] = "no-store"
        if not re.fullmatch(r"[0-9]{1,30}", sub_account_id):
            raise HTTPException(status_code=422, detail="Subcuenta testnet inválida")
        with access._lock:
            access.require(token)
            try:
                if access._grvt_bots is not None:
                    access._grvt_bots.stop_subaccount(sub_account_id)
                stop_testnet_subaccount(access.vault, sub_account_id,
                                        now=datetime.now(timezone.utc))
            except GrvtTestnetJournalError:
                raise HTTPException(status_code=409,
                                    detail="No se pudo detener la subcuenta testnet") from None
        return {"network": "TESTNET", "sub_account_id": sub_account_id,
                "stopped": True, "external_orders_cancelled": False,
                "mainnet_order_sent": False}

    def testnet_intent_for_review(sub_account_id: str,
                                  client_order_id: str) -> bool:
        """Comprueba procedencia local antes de autenticar contra testnet."""
        if not 2**63 <= int(client_order_id) <= 2**64 - 1:
            raise HTTPException(status_code=422, detail="ID testnet inválido")
        with access.vault.session() as db:
            intent = db.get(GrvtTestnetOrderIntent, client_order_id)
            if (intent is None or intent.sub_account_id != sub_account_id
                    or intent.status not in ("POST_CLAIMED", "ACKNOWLEDGED", "OBSERVED")):
                raise HTTPException(status_code=409,
                                    detail="Intento testnet no encontrado o sin POST")
            return db.get(GrvtTestnetCancelAttempt, client_order_id) is not None

    @router.post("/exchanges/grvt/testnet/orders/audit",
                 dependencies=[Depends(authorized)])
    def grvt_testnet_audit_order(body: GrvtTestnetOrderAuditRequest,
                                 response: Response,
                                 token: str | None = Header(
                                     default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Reconsulta orden, fills y cuenta; nunca libera capital por sí sola."""
        response.headers["Cache-Control"] = "no-store"
        with access._lock:
            access.require(token)
            cancelled = testnet_intent_for_review(body.sub_account_id,
                                                   body.client_order_id)
            try:
                client = grvt_testnet_factory(body.api_key.get_secret_value())
                if cancelled:
                    result = grvt_testnet_cancel_auditor(
                        access.vault, client, body.sub_account_id,
                        body.client_order_id,
                    )
                    audit = result.audit
                    cancel_status = result.cancellation.status
                else:
                    audit = grvt_testnet_auditor(
                        access.vault, client, body.sub_account_id,
                        body.client_order_id,
                    )
                    cancel_status = None
            except (GrvtTestnetOrderError, GrvtTestnetJournalError,
                    GrvtTestnetEvidenceError, GrvtTestnetPreflightError):
                raise HTTPException(status_code=409,
                                    detail="Auditoría testnet no verificable; consulta la recuperación local") from None
            except Exception:
                raise HTTPException(status_code=503,
                                    detail="GRVT testnet no disponible; conserva el intento pendiente") from None
        return {"network": "TESTNET", "sub_account_id": body.sub_account_id,
                "client_order_id": body.client_order_id,
                "journal_status": audit.record.status,
                "external_status": audit.record.external_status,
                "traded_size": (str(audit.record.traded_size)
                                if audit.record.traded_size is not None else None),
                "audit_id": audit.evidence.id,
                "cancel_status": cancel_status,
                "execution_unverified": True, "mainnet_order_sent": False}

    @router.post("/exchanges/grvt/testnet/orders/stop-cancel",
                 dependencies=[Depends(authorized)])
    def grvt_testnet_stop_cancel(body: GrvtTestnetStopCancelRequest,
                                  response: Response,
                                  token: str | None = Header(
                                      default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Parada duradera y una sola cancelación testnet por ID cliente."""
        response.headers["Cache-Control"] = "no-store"
        with access._lock:
            access.require(token)
            cancelled = testnet_intent_for_review(body.sub_account_id,
                                                   body.client_order_id)
            if cancelled:
                raise HTTPException(status_code=409,
                                    detail="Cancelación testnet ya reclamada; audita el ID")
            # Si la autenticación externa falla, la parada local sigue activa.
            stop_testnet_subaccount(access.vault, body.sub_account_id,
                                    now=datetime.now(timezone.utc))
            try:
                client = grvt_testnet_factory(body.api_key.get_secret_value())
                result = grvt_testnet_canceller(
                    access.vault, client, body.sub_account_id,
                    body.client_order_id,
                )
            except (GrvtTestnetOrderError, GrvtTestnetJournalError):
                try:
                    with access.vault.session() as db:
                        claimed = db.get(GrvtTestnetCancelAttempt,
                                         body.client_order_id) is not None
                except Exception:
                    claimed = True
                raise HTTPException(status_code=409,
                                    detail=("Cancelación testnet incierta; no reintentes, audita el ID"
                                            if claimed else
                                            "No se envió cancelación; parada local activa. Revisa la clave y el ID")) from None
            except Exception:
                raise HTTPException(status_code=503,
                                    detail="GRVT testnet no disponible; parada local activa, audita el ID") from None
        return {"network": "TESTNET", "sub_account_id": body.sub_account_id,
                "client_order_id": body.client_order_id,
                "stopped": True, "cancel_status": result.status,
                "external_status": result.external_status,
                "execution_unverified": True, "mainnet_order_sent": False}

    @router.get("/exchanges/grvt/testnet/recovery", dependencies=[Depends(authorized)])
    def grvt_testnet_recovery(response: Response,
                              sub_account_id: str = Query(min_length=1, max_length=30,
                                                          pattern=r"^[0-9]+$")) -> dict:
        """Sólo lee la bóveda local; no requiere ni usa credenciales GRVT."""
        response.headers["Cache-Control"] = "no-store"
        try:
            report = read_testnet_recovery(access.vault, sub_account_id)
        except GrvtTestnetJournalError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except GrvtTestnetEvidenceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except GrvtTestnetPreflightError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {
            "sub_account_id": report.sub_account_id,
            "stopped": report.stopped,
            "can_submit": False,
            "items": [
                {"client_order_id": item.client_order_id,
                 "instrument": item.instrument, "side": item.side,
                 "intended_size": str(item.intended_size),
                 "journal_status": item.journal_status,
                 "external_status": item.external_status,
                 "external_traded_size": (str(item.external_traded_size)
                                          if item.external_traded_size is not None else None),
                 "cancel_status": item.cancel_status,
                 "cancel_external_status": item.cancel_external_status,
                 "verified_audit_count": item.verified_audit_count,
                 "current_state_audited": item.current_state_audited,
                 "current_audit_findings": list(item.current_audit_findings),
                 "next_check": item.next_check,
                 "signal_source": item.signal_source,
                 "price_guard": item.price_guard,
                 "history_continuity": item.history_continuity,
                 "fill_summary": item.fill_summary,
                 "funding_summary": item.funding_summary,
                 "reconciliation": item.reconciliation,
                 "can_reconcile": item.can_reconcile}
                for item in report.items
            ],
        }

    @router.post("/exchanges/grvt/testnet/orders/reconcile", dependencies=[Depends(authorized)])
    def grvt_testnet_reconcile(body: GrvtTestnetReconcileRequest, response: Response,
                               token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        with access._lock:
            access.require(token)
            try:
                payload = reconcile_testnet_order(access.vault,body.sub_account_id,body.client_order_id,
                                                 now=datetime.now(timezone.utc))
            except (GrvtTestnetReconciliationError, GrvtTestnetEvidenceError, GrvtTestnetPreflightError):
                raise HTTPException(status_code=409,detail="Conciliación testnet bloqueada: audita dos veces, separadas 5 s; revisa saldo, posición y movimientos") from None
        return {"network":"TESTNET","client_order_id":body.client_order_id,"journal_status":"RECONCILED",
                "reconciliation":payload,"can_submit":False,"new_order_sent":False,"mainnet_order_sent":False}

    @router.get("/exchanges/grvt/testnet/subaccounts", dependencies=[Depends(authorized)])
    def grvt_testnet_subaccounts(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            known = list_testnet_recovery_subaccounts(access.vault)
        except GrvtTestnetJournalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"network": "TESTNET", "can_submit": False,
                "sub_account_ids": list(known)}

    @router.post("/exchanges/grvt/testnet/rejected-zero-fill/close",
                 dependencies=[Depends(authorized)])
    def grvt_testnet_close_rejected_zero_fill(body: GrvtCloseRejectedZeroFill,
                                               response: Response,
                                               token: str | None = Header(
                                                   default=None, alias="X-Orbis-Vault-Token")) -> dict:
        """Cierra un rechazo testnet auditado; jamás llama a GRVT ni envía."""
        response.headers["Cache-Control"] = "no-store"
        with access._lock:
            access.require(token)
            try:
                closed = close_rejected_testnet_zero_fill(
                    access.vault, body.sub_account_id, body.client_order_id,
                    now=datetime.now(timezone.utc),
                )
            except (GrvtTestnetJournalError, GrvtTestnetEvidenceError,
                    GrvtTestnetPreflightError):
                raise HTTPException(status_code=409,
                                    detail="Rechazo GRVT testnet no conciliado") from None
        return {"network": "TESTNET", "client_order_id": closed.client_order_id,
                "journal_status": closed.status, "can_submit": False,
                "new_order_sent": False}

    @router.post("/exchanges/grvt/connect", dependencies=[Depends(authorized)])
    def grvt_connect(body: GrvtConnect, response: Response,
                     token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.connect_grvt(token, body.api_key.get_secret_value())
        except GrvtReadOnlyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/exchanges/grvt/snapshot", dependencies=[Depends(authorized)])
    def grvt_snapshot(response: Response,
                      token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.grvt_snapshot(token)
        except GrvtReadOnlyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @router.get("/exchanges/grvt/open-orders", dependencies=[Depends(authorized)])
    def grvt_open_orders(response: Response,
                         token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.grvt_open_orders(token)
        except GrvtReadOnlyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @router.post('/exchanges/grvt/perp-review', dependencies=[Depends(authorized)])
    def grvt_perp_review(body: GrvtPerpReviewRequest, response: Response,
                         token: str | None = Header(default=None, alias='X-Orbis-Vault-Token')) -> dict:
        from app.derivatives.paper_access import paper_perp_access, paper_perp_format
        from app.lab.access import lab_access
        response.headers['Cache-Control'] = 'no-store'
        if lab_path is None or paper_perp_path is None:
            raise HTTPException(409, 'Origen LAB/PAPER no configurado')
        if (lab_access.get(lab_path) is None or paper_perp_access.get(paper_perp_path) is None
                or paper_perp_format(paper_perp_path) != 'encrypted_or_invalid'):
            raise HTTPException(423, 'Desbloquea LAB y GRVT PAPER en Ajustes')
        try:
            return access.grvt_perp_review(token, paper_perp_path, body.session_id, body.sub_account_id)
        except GrvtReadOnlyError as exc:
            raise HTTPException(503, str(exc)) from None
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/exchanges/grvt/order", dependencies=[Depends(authorized)])
    def grvt_order(response: Response,
                   sub_account_id: str = Query(min_length=1, max_length=30, pattern=r"^[0-9]+$"),
                   client_order_id: str = Query(min_length=1, max_length=20, pattern=r"^[0-9]+$"),
                   token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.grvt_order(token, sub_account_id, client_order_id)
        except GrvtReadOnlyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @router.get("/exchanges/grvt/order-fills", dependencies=[Depends(authorized)])
    def grvt_order_fills(response: Response,
                         sub_account_id: str = Query(min_length=1, max_length=30, pattern=r"^[0-9]+$"),
                         client_order_id: str = Query(min_length=1, max_length=20, pattern=r"^[0-9]+$"),
                         token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.grvt_order_fills(token, sub_account_id, client_order_id)
        except GrvtReadOnlyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @router.post("/exchanges/grvt/order-fills/archive", dependencies=[Depends(authorized)])
    def grvt_archive_order_fills(body: GrvtOrderRef, response: Response,
                                 token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.archive_grvt_order_fills(token, body.sub_account_id, body.client_order_id)
        except GrvtReadOnlyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except PortfolioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.get("/exchanges/grvt/evidence", dependencies=[Depends(authorized)])
    def grvt_evidence(response: Response,
                      token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        return access.grvt_evidence(token)

    @router.get("/exchanges/grvt/evidence/{identifier}", dependencies=[Depends(authorized)])
    def grvt_evidence_detail(identifier: str, response: Response,
                             token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.grvt_evidence(token, identifier)
        except PortfolioError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post("/exchanges/grvt/disconnect", dependencies=[Depends(authorized)])
    def grvt_disconnect(response: Response,
                        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return access.disconnect_grvt(token)

    @router.post("/vault/backup", dependencies=[Depends(authorized)])
    def backup() -> dict:
        directory = access.vault.path.parent / "backups"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = directory / f"real-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}.db"
        try:
            return {"path": str(access.vault.backup(destination)), "encrypted": True}
        except VaultError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/vault/backups", dependencies=[Depends(authorized)])
    def backups() -> list[dict]:
        directory = access.vault.path.parent / "backups"
        if not directory.is_dir():
            return []
        rows = []
        for candidate in directory.iterdir():
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if not MANAGED_BACKUP_NAME.fullmatch(candidate.name):
                continue
            info = candidate.stat()
            rows.append({"name": candidate.name, "size_bytes": info.st_size, "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()})
        return sorted(rows, key=lambda row: row["name"], reverse=True)

    @router.get("/vault/backups/{name}/verify", dependencies=[Depends(authorized)])
    def verify_backup(name: str, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        if not MANAGED_BACKUP_NAME.fullmatch(name):
            raise HTTPException(status_code=422, detail="Invalid managed backup name")
        try:
            return access.vault.verify_managed_backup(access.vault.path.parent / "backups" / name)
        except VaultError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/vault/restore", dependencies=[Depends(authorized)])
    def restore(body: BackupRestore) -> dict:
        if not MANAGED_BACKUP_NAME.fullmatch(body.name):
            raise HTTPException(status_code=422, detail="Invalid managed backup name")
        source = access.vault.path.parent / "backups" / body.name
        try:
            recovery = access.vault.restore_backup(source)
            return {"restored": True, "locked": True, "previous_vault_backup": recovery.name}
        except (VaultError, PortfolioError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            access.lock()

    @router.get("/accounts", dependencies=[Depends(authorized)])
    def accounts() -> list[dict]:
        with access.vault.session() as db:
            rows = db.scalars(select(RealAccount).order_by(RealAccount.created_at, RealAccount.id)).all()
            return [{"id": row.id, "name": row.name, "base_currency": row.base_currency} for row in rows]

    @router.post("/accounts", dependencies=[Depends(authorized)])
    def add_account(body: AccountCreate) -> dict:
        try:
            with access.vault.session() as db:
                row = create_account(db, body.name, body.base_currency)
                return {"id": row.id, "name": row.name, "base_currency": row.base_currency}
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/accounts/{account_id}/wallet-proof/start", dependencies=[Depends(authorized)])
    def start_wallet_proof(account_id: str, body: WalletProofStart,
                           request: Request, response: Response,
                           token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.wallet_proof_start(token, account_id, body.address,
                                             body.chain_id, str(request.base_url).rstrip("/"))
        except WalletProofError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PortfolioError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/wallet-proof/confirm", dependencies=[Depends(authorized)])
    def confirm_wallet_proof(account_id: str, body: WalletProofConfirm,
                             response: Response,
                             token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.wallet_proof_confirm(token, account_id, body.nonce,
                                               body.address, body.chain_id,
                                               body.signature)
        except WalletProofError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get("/accounts/{account_id}/wallet-links", dependencies=[Depends(authorized)])
    def wallet_links(account_id: str, response: Response) -> list[dict]:
        response.headers["Cache-Control"] = "no-store"
        try:
            with access.vault.session() as db:
                return list_wallet_links(db, account_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/wallet-links/inspect", dependencies=[Depends(authorized)])
    def inspect_link(account_id: str, body: WalletProofStart, response: Response,
                     token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.wallet_link_inspect(token, account_id, body.address, body.chain_id)
        except WalletProofError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except PortfolioError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/wallet-links", dependencies=[Depends(authorized)])
    def add_wallet_link(account_id: str, body: WalletLinkCreate, response: Response,
                        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return access.wallet_link_save(token, account_id, body.address,
                                           body.chain_id, body.alias)
        except WalletProofError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except RequestConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post("/accounts/{account_id}/wallet-links/{link_id}/unlink", dependencies=[Depends(authorized)])
    def unlink_wallet(account_id: str, link_id: str, body: WalletLinkRemove,
                      response: Response,
                      token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            access.wallet_link_remove(token, account_id, link_id)
        except PortfolioError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {"unlinked": True, "spending_authorized": False}

    @router.get("/accounts/{account_id}/events", dependencies=[Depends(authorized)])
    def events(account_id: str) -> list[dict]:
        with access.vault.session() as db:
            if db.get(RealAccount, account_id) is None:
                raise HTTPException(status_code=404, detail="Real account not found")
            return [_event_payload(row) for row in list_events(db, account_id)]

    @router.post("/accounts/{account_id}/csv-preview", dependencies=[Depends(authorized)])
    def csv_preview(account_id: str, body: CsvImportPreview, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            source_account_id, entries = parse_real_events_csv(body.csv_text)
            with access.vault.session() as db:
                counts = import_events(db, account_id, entries, dry_run=True)
            return counts | {"source_account_id": source_account_id,
                             "sha256": hashlib.sha256(body.csv_text.encode("utf-8")).hexdigest()}
        except RequestConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/accounts/{account_id}/csv-import", dependencies=[Depends(authorized)])
    def csv_import(account_id: str, body: CsvImportCommit, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        digest = hashlib.sha256(body.csv_text.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, body.sha256):
            raise HTTPException(status_code=409, detail="El CSV cambió desde la vista previa")
        try:
            source_account_id, entries = parse_real_events_csv(body.csv_text)
            with access.vault.session() as db:
                counts = import_events(db, account_id, entries, dry_run=False)
            return counts | {"source_account_id": source_account_id}
        except RequestConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/accounts/{account_id}/events", dependencies=[Depends(authorized)])
    def add_event(account_id: str, body: EventCreate) -> dict:
        try:
            item = EventInput(
                kind=body.kind,
                quote_currency=body.quote_currency,
                occurred_at=body.occurred_at,
                asset=body.asset,
                quantity=Decimal(body.quantity) if body.quantity is not None else None,
                unit_price=Decimal(body.unit_price) if body.unit_price is not None else None,
                amount=Decimal(body.amount) if body.amount is not None else None,
                fee=Decimal(body.fee),
                fee_asset=body.fee_asset,
                note=body.note,
            )
            with access.vault.session() as db:
                return _event_payload(append_event(db, account_id, body.request_id, item))
        except RequestConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def paired_transfer(account_id: str, body: AccountTransferCreate,
                        response: Response, *, dry_run: bool) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            with access.vault.session() as db:
                return transfer_asset_between_accounts(
                    db, account_id, body.target_account_id, body.request_id,
                    asset=body.asset, quote_currency=body.quote_currency,
                    quantity=Decimal(body.quantity), occurred_at=body.occurred_at,
                    note=body.note, dry_run=dry_run, fee=Decimal(body.fee),
                    expected_cost_basis=(Decimal(body.expected_cost_basis)
                                         if isinstance(body, AccountTransferCommit) else None),
                ) | {"execution": "MANUAL_RECORD_ONLY_NO_WALLET_OR_EXCHANGE_TRANSFER"}
        except RequestConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/accounts/{account_id}/transfers/preview", dependencies=[Depends(authorized)])
    def preview_transfer(account_id: str, body: AccountTransferCreate,
                         response: Response) -> dict:
        """Misma validación contable que el commit; revierte la transacción."""
        return paired_transfer(account_id, body, response, dry_run=True)

    @router.post("/accounts/{account_id}/transfers", dependencies=[Depends(authorized)])
    def transfer_between_accounts(account_id: str, body: AccountTransferCommit,
                                  response: Response) -> dict:
        """Asiento local pareado; no ejecuta un traslado externo."""
        return paired_transfer(account_id, body, response, dry_run=False)

    def paired_cash_transfer(account_id: str, body: AccountCashTransferCreate,
                             response: Response, *, dry_run: bool) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            with access.vault.session() as db:
                return transfer_cash_between_accounts(
                    db, account_id, body.target_account_id, body.request_id,
                    currency=body.currency, amount=Decimal(body.amount),
                    occurred_at=body.occurred_at, note=body.note, dry_run=dry_run,
                    expected_source_balance=(Decimal(body.expected_source_balance)
                                             if isinstance(body, AccountCashTransferCommit) else None),
                ) | {"execution": "MANUAL_RECORD_ONLY_NO_WALLET_OR_EXCHANGE_TRANSFER"}
        except RequestConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/accounts/{account_id}/cash-transfers/preview", dependencies=[Depends(authorized)])
    def preview_cash_transfer(account_id: str, body: AccountCashTransferCreate,
                              response: Response) -> dict:
        return paired_cash_transfer(account_id, body, response, dry_run=True)

    @router.post("/accounts/{account_id}/cash-transfers", dependencies=[Depends(authorized)])
    def cash_transfer_between_accounts(account_id: str, body: AccountCashTransferCommit,
                                       response: Response) -> dict:
        return paired_cash_transfer(account_id, body, response, dry_run=False)

    @router.get("/accounts/{account_id}/snapshot", dependencies=[Depends(authorized)])
    def snapshot(account_id: str, response: Response, include_events: bool = False,
                 token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            with access.vault.session() as db:
                result, events = account_snapshot_with_events(db, account_id)
                payload = {
                    "account_id": account_id,
                    "cash": {key: str(value) for key, value in result.cash.items()},
                    "contributions": {key: str(value) for key, value in result.contributions.items()},
                    "withdrawals": {key: str(value) for key, value in result.withdrawals.items()},
                    "cash_fees": {key: str(value) for key,value in result.cash_fees.items()},
                    "realized_pnl": {key: str(value) for key, value in realized_pnl_by_currency(result).items()},
                    "positions": [
                        {
                            "asset": p.asset, "quote_currency": p.quote_currency,
                            "quantity": str(p.quantity), "cost_basis": str(p.cost_basis),
                            "average_cost": str(p.average_cost), "realized_pnl": str(p.realized_pnl),
                            "fees": str(p.fees),
                            "fees_by_asset": {asset: str(amount) for asset, amount in p.fees_by_asset.items()},
                        }
                        for p in result.positions.values()
                    ],
                }
                if include_events:
                    payload["events"] = [_event_payload(event) for event in events]
            access.require(token, touch=False)
            return payload
        except VaultError as exc:
            raise HTTPException(status_code=401, detail="Real vault is locked") from exc
        except PortfolioError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/accounts/{account_id}/valuation", dependencies=[Depends(authorized)])
    def valuation(account_id: str, response: Response, market_db: Session = Depends(get_db),
                  token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            with access.vault.session() as db:
                book = account_snapshot(db, account_id)
            quotes: dict[tuple[str, str], ReferencePrice] = {}
            for key, position in book.positions.items():
                if position.quantity == 0:
                    continue
                asset, currency = key
                try:
                    meta = get_configured_price_with_meta(
                        market_db, asset, currency,
                        provider_name=get_orbis_config().price_provider,
                    )
                    price = Decimal(str(meta["price"])) if meta.get("price") is not None else None
                    if price is not None:
                        try:
                            decimal_value(price, positive=True)
                        except PortfolioError:
                            price = None
                    quotes[key] = ReferencePrice(
                        price=price,
                        source=str(meta.get("source") or "unknown"),
                        as_of=str(meta["as_of"]) if meta.get("as_of") is not None else None,
                        stale=bool(meta.get("stale", True)),
                    )
                except Exception:
                    quotes[key] = ReferencePrice(None, "unavailable", None, True)
            result = value_snapshot(book, quotes)
            def serialized(value):
                if isinstance(value, dict):
                    return {key: serialized(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [serialized(item) for item in value]
                return str(value) if isinstance(value, Decimal) else value

            payload = serialized({"account_id": account_id, **result})
            access.require(token, touch=False)
            return payload
        except VaultError as exc:
            raise HTTPException(status_code=401, detail="Real vault is locked") from exc
        except PortfolioError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/accounts/{account_id}/valuation-history", dependencies=[Depends(authorized)])
    def valuation_history(
        account_id: str, response: Response,
        currency: str = Query(pattern=r"^[A-Z0-9]{2,12}$"),
        provider: Literal["kraken", "bitstamp", "binance", "mexc"] = "kraken",
        days: int = Query(default=30, ge=7, le=MAX_VALUATION_DAYS),
        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token"),
    ) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            with access.vault.session() as db:
                if db.get(RealAccount, account_id) is None:
                    raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")
                events = list_events(db, account_id)
            access.require(token, touch=False)
            result = exact_jsonable(value_history(
                account_id, events, currency=currency, provider=provider, days=days,
                now=datetime.now(timezone.utc),
            ))
            access.require(token, touch=False)
            return result
        except VaultError as exc:
            raise HTTPException(status_code=401, detail="Real vault is locked") from exc
        except PortfolioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    from app.portfolio.spot_bot_api import create_mexc_bot_router
    from app.portfolio.wallet_funding_api import create_wallet_funding_router
    from app.portfolio.wallet_swap_api import create_wallet_swap_router
    from app.portfolio.dex_deployment_api import create_dex_deployment_router
    from app.portfolio.dex_control_api import create_dex_control_router
    from app.portfolio.dex_operator_draft_api import create_dex_operator_draft_router
    from app.portfolio.dex_gas_funding_api import create_dex_gas_funding_router
    from app.portfolio.dex_gas_return_api import create_dex_gas_return_router
    from app.portfolio.dex_gas_cancellation_api import create_dex_gas_cancellation_router
    from app.portfolio.dex_wallet_cancellation_api import create_dex_wallet_cancellation_router
    from app.portfolio.dex_owner_observation_api import create_dex_owner_observation_router
    from app.portfolio.dex_deployment_resolution_api import create_deployment_resolution_router
    from app.portfolio.dex_native_resolution_api import create_native_resolution_router
    from app.portfolio.dex_call_resolution_api import create_call_resolution_router
    from app.portfolio.dex_bot_api import create_dex_bot_router
    from app.live_execution.dex_bot_source import DexBotSources
    from app.portfolio.dex_bot_binding_api import create_dex_bot_binding_router
    from app.portfolio.dex_bot_run_api import create_dex_bot_run_router
    from app.portfolio.dex_receipt_booking_api import create_dex_receipt_booking_router
    from app.portfolio.dex_receipt_order_api import create_receipt_order_router
    from app.portfolio.dex_gas_order_api import create_gas_order_router
    from app.portfolio.economic_review_api import create_economic_review_router
    from app.portfolio.dex_external_observation_api import create_external_observation_router
    from app.portfolio.dex_external_order_api import create_external_order_router
    from app.portfolio.economic_external_api import create_external_effects_router
    from app.portfolio.economic_external_group_api import create_external_group_router
    from app.portfolio.economic_external_workspace_api import create_external_workspace_router
    from app.portfolio.dex_bot_rollover_api import create_dex_bot_rollover_router
    dex_sources = DexBotSources(spot_paper,dca_paper)
    router.include_router(create_dex_bot_router(access, authorized=authorized,
        authorized_passive=authorized_passive, sources=dex_sources))
    router.include_router(create_dex_bot_binding_router(access, sources=dex_sources,
        authorized=authorized, authorized_passive=authorized_passive, rpc_factory=dex_rpc_factory))
    router.include_router(create_dex_receipt_booking_router(access,authorized=authorized,authorized_passive=authorized_passive))
    router.include_router(create_receipt_order_router(access,authorized=authorized,
        authorized_passive=authorized_passive,rpc_factory=dex_rpc_factory))
    router.include_router(create_gas_order_router(access,
        authorized_passive=authorized_passive,rpc_factory=dex_rpc_factory))
    router.include_router(create_economic_review_router(access,authorized=authorized,authorized_passive=authorized_passive,
        clock=access._dex_bots.now))
    router.include_router(create_dex_bot_run_router(access,authorized=authorized,
        authorized_passive=authorized_passive,rpc_factory=dex_rpc_factory,sources=dex_sources))
    router.include_router(create_dex_bot_rollover_router(access,authorized=authorized,rpc_factory=dex_rpc_factory))
    router.include_router(create_dex_control_router(access, authorized=authorized,
                                                   authorized_passive=authorized_passive, rpc_factory=dex_rpc_factory))
    router.include_router(create_dex_deployment_router(access, authorized=authorized,
                                                      authorized_passive=authorized_passive))
    router.include_router(create_deployment_resolution_router(access,authorized=authorized,rpc_factory=dex_rpc_factory))
    router.include_router(create_native_resolution_router(access,authorized=authorized,rpc_factory=dex_rpc_factory))
    router.include_router(create_call_resolution_router(access,authorized=authorized,authorized_passive=authorized_passive,rpc_factory=dex_rpc_factory))
    router.include_router(create_external_observation_router(access,
        authorized_passive=authorized_passive,rpc_factory=dex_rpc_factory))
    router.include_router(create_external_order_router(access,
        authorized_passive=authorized_passive,rpc_factory=dex_rpc_factory))
    router.include_router(create_external_effects_router(access,authorized=authorized))
    router.include_router(create_external_group_router(access,authorized=authorized))
    router.include_router(create_external_workspace_router(access,authorized=authorized))
    router.include_router(create_dex_owner_observation_router(access, authorized=authorized, rpc_factory=dex_rpc_factory))
    router.include_router(create_dex_operator_draft_router(access, authorized=authorized))
    router.include_router(create_dex_gas_funding_router(access,authorized=authorized,
        authorized_passive=authorized_passive,rpc_factory=dex_rpc_factory))
    router.include_router(create_dex_gas_return_router(access,authorized=authorized,
        authorized_passive=authorized_passive,rpc_factory=dex_rpc_factory,sender_factory=dex_return_sender_factory))
    router.include_router(create_dex_gas_cancellation_router(access,authorized=authorized,
        authorized_passive=authorized_passive,rpc_factory=dex_rpc_factory,sender_factory=dex_cancel_sender_factory))
    router.include_router(create_dex_wallet_cancellation_router(access,authorized=authorized,
        authorized_passive=authorized_passive,rpc_factory=dex_rpc_factory))
    router.include_router(create_wallet_swap_router(access, authorized=authorized,
                                                    authorized_passive=authorized_passive))
    router.include_router(create_wallet_funding_router(access, authorized=authorized,
        authorized_passive=authorized_passive,rpc_factory=dex_rpc_factory))
    from app.portfolio.mexc_withdrawal_api import create_mexc_withdrawal_router
    router.include_router(create_mexc_withdrawal_router(access,authorized=authorized,
        authorized_passive=authorized_passive,client_factory=mexc_withdrawal_factory,rpc_factory=dex_rpc_factory))
    router.include_router(create_mexc_bot_router(access, authorized=authorized,
                                                authorized_passive=authorized_passive))
    from app.portfolio.grvt_oco_api import create_grvt_oco_router
    router.include_router(create_grvt_oco_router(access,authorized=authorized,
        authorized_passive=authorized_passive,client_factory=grvt_testnet_factory,
        public_post=grvt_testnet_oco_public_post,clock_ns=grvt_testnet_oco_clock_ns))
    from app.portfolio.grvt_bracket_api import create_grvt_bracket_router
    router.include_router(create_grvt_bracket_router(access,authorized=authorized,
        authorized_passive=authorized_passive,client_factory=grvt_testnet_factory,
        lab_path=lab_path,paper_path=paper_perp_path,public_post=grvt_testnet_bracket_public_post,
        clock_ns=grvt_testnet_bracket_clock_ns,close_waiter=grvt_testnet_bracket_close_waiter,
        exit_stream_factory=grvt_testnet_exit_stream_factory))
    router.include_router(access._grvt_bot_router)
    from app.portfolio.grvt_mainnet_api import create_grvt_mainnet_router
    router.include_router(create_grvt_mainnet_router(access,authorized=authorized,authorized_passive=authorized_passive,
        client_factory=grvt_mainnet_factory,public_post=grvt_mainnet_public_post,clock_ns=grvt_mainnet_clock_ns,waiter=grvt_mainnet_waiter,
        lab_path=lab_path,paper_path=paper_perp_path,bot_poll_seconds=grvt_mainnet_bot_poll_seconds))
    return router
