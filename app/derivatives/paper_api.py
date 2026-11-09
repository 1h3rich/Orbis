"""API local de PAPER manual GRVT; jamás envía órdenes al exchange."""

from __future__ import annotations

from pathlib import Path
from app.security.local_session import LocalStoreUnlock
import time
from typing import Callable
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.derivatives.paper_math import InstrumentSpec, MarginRules, PaperPerpError, PerpQuote, decimal_string
from app.derivatives.paper_store import PaperPerpStore, PaperRequestConflict
from app.derivatives.paper_access import paper_perp_access, paper_perp_format
from app.derivatives.funding import FundingHistoryError, FundingRecord, estimate_funding
from app.marketdata.adapters.grvt import GrvtAdapter, GrvtHistoryError


LOCAL_HOSTS = frozenset(("127.0.0.1", "::1", "localhost", "testserver", "testclient"))


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("initial_balance", "max_position_notional", "max_total_notional",
                     "max_leverage", "fee_rate", "slippage_bps", "size", "leverage",
                     check_fields=False)
    @classmethod
    def decimal_text(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("Los importes se envían como cadenas decimales")
        decimal_string(value, "importe")
        return value


class AccountCreate(_ExactModel):
    name: str = Field(min_length=1, max_length=100)
    initial_balance: str
    max_position_notional: str
    max_total_notional: str
    max_leverage: str
    max_positions: int = Field(ge=1, le=100)
    fee_rate: str
    slippage_bps: str


class OpenRequest(_ExactModel):
    request_id: UUID
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,12}/USDT:PERP$")
    side: Literal["LONG", "SHORT"]
    size: str
    leverage: str


class CloseRequest(_ExactModel):
    request_id: UUID


class PaperPerpUnlock(LocalStoreUnlock):
    pass


def create_paper_perp_router(path: Path, *, adapter_factory=GrvtAdapter,
                             clock_ns=time.time_ns,
                             on_access_change: Callable[[], None] | None = None) -> APIRouter:
    store = PaperPerpStore(path, allow_create=False)
    router = APIRouter(prefix="/paper/perp", tags=["paper-perpetuals"])

    def local_only(request: Request) -> None:
        client_host = request.client.host if request.client else None
        if client_host not in LOCAL_HOSTS or request.url.hostname not in LOCAL_HOSTS:
            raise HTTPException(status_code=403, detail="PAPER derivados sólo local")
        origin = request.headers.get("origin")
        if origin is not None and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Origen externo rechazado")

    def available(request: Request) -> None:
        local_only(request)
        try:
            kind = paper_perp_format(store.path)
        except PaperPerpError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if kind == "encrypted_or_invalid" and paper_perp_access.get(store.path) is None:
            raise HTTPException(status_code=423, detail="GRVT PAPER cifrado bloqueado; desbloquéalo en Ajustes")
        if kind in ("missing", "empty") and not (
            request.method == "GET" and request.url.path == "/paper/perp/accounts"
        ):
            raise HTTPException(status_code=423, detail="Crea primero la base GRVT PAPER cifrada en Ajustes")

    @router.get("/status", dependencies=[Depends(local_only)])
    def paper_status(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return paper_perp_access.status(store.path)
        except PaperPerpError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/unlock", dependencies=[Depends(local_only)])
    def unlock_paper(body: PaperPerpUnlock, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            result = paper_perp_access.unlock(store.path, body.passphrase.get_secret_value(),
                                              session_seconds=body.session_seconds)
            if on_access_change is not None:
                on_access_change()
            return result
        except PaperPerpError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/create", dependencies=[Depends(local_only)])
    def create_encrypted_paper(body: PaperPerpUnlock, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            result = paper_perp_access.create(store.path, body.passphrase.get_secret_value(),
                                              session_seconds=body.session_seconds)
            if on_access_change is not None:
                on_access_change()
            return result
        except PaperPerpError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/lock", dependencies=[Depends(local_only)])
    def lock_paper(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            result = paper_perp_access.lock(store.path)
            if on_access_change is not None:
                on_access_change()
            return result
        except PaperPerpError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def domain_error(exc: PaperPerpError) -> HTTPException:
        if isinstance(exc, PaperRequestConflict):
            return HTTPException(status_code=409, detail=str(exc))
        if "cifrado bloqueado" in str(exc):
            return HTTPException(status_code=423, detail=str(exc))
        status = 404 if "no encontrada" in str(exc) or "no encontrado" in str(exc) else 422
        return HTTPException(status_code=status, detail=str(exc))

    def quote_for(adapter: GrvtAdapter, symbol: str) -> PerpQuote:
        if symbol not in adapter.known_pairs:
            raise HTTPException(status_code=422, detail="Instrumento GRVT no admitido")
        native = adapter._instrument(symbol)
        try:
            return PerpQuote.from_ticker(adapter.get_ticker(symbol), native, clock_ns())
        except PaperPerpError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def spec_for(adapter: GrvtAdapter, symbol: str, instrument: str) -> InstrumentSpec:
        try:
            return InstrumentSpec.from_payload(adapter.get_instrument_spec(symbol), instrument, clock_ns())
        except PaperPerpError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/instrument", dependencies=[Depends(local_only)])
    def instrument_spec(symbol: str = Query(pattern=r"^[A-Z0-9]{2,12}/USDT:PERP$")) -> dict:
        adapter = adapter_factory()
        if symbol not in adapter.known_pairs:
            raise HTTPException(status_code=422, detail="Instrumento GRVT no admitido")
        return spec_for(adapter, symbol, adapter._instrument(symbol)).json()

    @router.post("/accounts", dependencies=[Depends(available)], status_code=201)
    def create_account(body: AccountCreate) -> dict:
        try:
            return store.create_account(**body.model_dump())
        except PaperPerpError as exc:
            raise domain_error(exc) from exc

    @router.get("/accounts", dependencies=[Depends(available)])
    def list_accounts() -> list[dict]:
        try:
            return store.accounts()
        except PaperPerpError as exc:
            raise domain_error(exc) from exc

    @router.get("/accounts/{account_id}", dependencies=[Depends(available)])
    def get_account(account_id: str) -> dict:
        try:
            return store.account(account_id)
        except PaperPerpError as exc:
            raise domain_error(exc) from exc

    @router.post("/accounts/{account_id}/stop", dependencies=[Depends(available)])
    def stop_account(account_id: str) -> dict:
        try:
            return store.stop_account(account_id)
        except PaperPerpError as exc:
            raise domain_error(exc) from exc

    @router.get("/accounts/{account_id}/positions", dependencies=[Depends(available)])
    def list_positions(account_id: str) -> list[dict]:
        try:
            return store.positions(account_id)
        except PaperPerpError as exc:
            raise domain_error(exc) from exc

    @router.get("/accounts/{account_id}/events", dependencies=[Depends(available)])
    def list_events(account_id: str) -> list[dict]:
        try:
            return store.events(account_id)
        except PaperPerpError as exc:
            raise domain_error(exc) from exc

    @router.get("/accounts/{account_id}/valuations", dependencies=[Depends(available)])
    def current_valuations(account_id: str, response: Response) -> dict:
        """Sólo expone importes abiertos con un mark todavía vigente."""
        response.headers["Cache-Control"] = "no-store"
        try:
            return store.valuation_snapshots(account_id, now_ns=clock_ns())
        except PaperPerpError as exc:
            raise domain_error(exc) from exc

    @router.post("/accounts/{account_id}/positions", dependencies=[Depends(available)], status_code=201)
    def open_position(account_id: str, body: OpenRequest) -> dict:
        payload = body.model_dump(mode="json") | {"account_id": account_id, "action": "OPEN"}
        digest = store.digest(payload)
        try:
            replay = store.replay(str(body.request_id), digest)
            if replay is not None:
                return replay
            size = decimal_string(body.size, "size", positive=True)
            leverage = decimal_string(body.leverage, "leverage", positive=True)
            adapter = adapter_factory()
            quote = quote_for(adapter, body.symbol)
            try:
                rules = MarginRules.from_payload(adapter.get_margin_rules(body.symbol), quote.instrument, clock_ns())
            except PaperPerpError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            spec = spec_for(adapter, body.symbol, quote.instrument)
            if clock_ns() - quote.event_time_ns > 15_000_000_000:
                raise HTTPException(status_code=503, detail="Cotización GRVT envejeció durante la validación")
            return store.open_position(account_id, str(body.request_id), digest, body.symbol,
                                       body.side, size, leverage, quote, rules, spec)
        except PaperPerpError as exc:
            raise domain_error(exc) from exc

    @router.post("/accounts/{account_id}/positions/{position_id}/close", dependencies=[Depends(available)])
    def close_position(account_id: str, position_id: str, body: CloseRequest) -> dict:
        digest = store.digest({"action": "CLOSE", "account_id": account_id,
                               "position_id": position_id, "request_id": str(body.request_id)})
        try:
            replay = store.replay(str(body.request_id), digest)
            if replay is not None:
                return replay
            position = store.position(account_id, position_id)
            adapter = adapter_factory()
            quote = quote_for(adapter, position["symbol"])
            return store.close_position(account_id, position_id, str(body.request_id), digest, quote)
        except PaperPerpError as exc:
            raise domain_error(exc) from exc

    @router.post("/accounts/{account_id}/positions/{position_id}/observe", dependencies=[Depends(available)])
    def observe_position(account_id: str, position_id: str) -> dict:
        try:
            position = store.position(account_id, position_id)
            adapter = adapter_factory()
            quote = quote_for(adapter, position["symbol"])
            try:
                rules = MarginRules.from_payload(adapter.get_margin_rules(position["symbol"]),
                                                 quote.instrument, clock_ns())
            except PaperPerpError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if clock_ns() - quote.event_time_ns > 15_000_000_000:
                raise HTTPException(status_code=503, detail="Cotización GRVT envejeció durante la validación")
            return store.observe(account_id, position_id, quote, rules)
        except PaperPerpError as exc:
            raise domain_error(exc) from exc

    @router.get("/accounts/{account_id}/positions/{position_id}/funding-estimate",
                dependencies=[Depends(available)])
    def funding_estimate(account_id: str, position_id: str) -> dict:
        """Cálculo de tasas públicas; no asienta cargos en PAPER."""
        try:
            position = store.position(account_id, position_id)
            through_ns = position["closed_at_ns"] if position["closed_at_ns"] is not None else clock_ns()
            raw = adapter_factory().get_funding_history(position["symbol"],
                                                        position["opened_at_ns"], through_ns)
            records = [FundingRecord.from_payload(row, position["instrument"]) for row in raw]
            result = estimate_funding(position, records, through_ns)
            result["paper_booked_funding_paid"] = position["funding_paid"]
            result["paper_coverage"] = position["funding_coverage"]
            if position["funding_coverage"] != "UNASSESSED":
                result["status"] = "HISTORICAL_ESTIMATE_WITH_PAPER_BOOKINGS"
            return result
        except PaperPerpError as exc:
            raise domain_error(exc) from exc
        except (FundingHistoryError, GrvtHistoryError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/accounts/{account_id}/positions/{position_id}/funding-settle",
                 dependencies=[Depends(available)])
    def funding_settle(account_id: str, position_id: str, body: CloseRequest) -> dict:
        """Liquidación ficticia explícita desde histórico público, nunca GRVT real."""
        request_id = str(body.request_id)
        digest = store.digest({"action": "FUNDING_PAPER", "account_id": account_id,
                               "position_id": position_id, "request_id": request_id})
        try:
            replay = store.replay(request_id, digest)
            if replay is not None:
                return replay
            position = store.position(account_id, position_id)
            adapter = adapter_factory()
            through_ns = (position["closed_at_ns"] if position["closed_at_ns"] is not None
                          else quote_for(adapter, position["symbol"]).event_time_ns)
            raw = adapter.get_funding_history(position["symbol"], position["opened_at_ns"], through_ns)
            records = [FundingRecord.from_payload(row, position["instrument"]) for row in raw]
            return store.settle_funding(account_id, position_id, request_id, digest, records, through_ns)
        except PaperPerpError as exc:
            raise domain_error(exc) from exc
        except (FundingHistoryError, GrvtHistoryError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return router
