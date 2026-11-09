"""API local de bots: opt-in separado de órdenes manuales y PAPER."""

from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.automation.spot_strategy_paper import SpotPaperLocked
from app.lab.engine import LabError
from app.live_execution.mexc_execution import MexcExecutionError
from app.live_execution.mexc_execution_state import MexcExecutionStateError
from app.live_execution.mexc_live_order import MexcOrderSendError
from app.live_execution.mexc_order_observer import MexcOrderObservationError
from app.live_execution.mexc_settlement import MexcSettlementError
from app.live_execution.spot_bot_source import SpotBotSourceError
from app.live_execution.spot_bot_store import SpotBotStoreError
from app.portfolio.vault import VaultError
from app.portfolio.models import RealAccount


class MexcBotActivate(BaseModel):
    request_id: UUID
    draft_id: UUID
    anchor_id: UUID
    paper_session_id: UUID
    entry_budget_quote: str = Field(min_length=1, max_length=80)
    confirmation: Literal["ACTIVAR_BOT_REAL","ACTIVAR_BOT_OPERATIVO","ACTIVAR_BOT_CONTINUO"]
    execution_mode:Literal["SESSION","OPERATIONAL","CONTINUOUS"] = "SESSION"
    continuous_until:datetime|None = None
    source_kind: Literal["STRATEGY","DCA"] = "STRATEGY"
    research_validation_id: UUID | None = None  # Omission only permits an old, inert replay.
    research_review_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dca_budget_total_quote: str | None = Field(default=None,min_length=1,max_length=80)
    dca_monthly_limit_quote: str | None = Field(default=None,min_length=1,max_length=80)


class MexcBotResearchExport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["EXPORTAR_JSONL_MEXC_REAL"]


class MexcBotStop(BaseModel):
    confirmation: Literal["DETENER_BOT"]


class MexcLegacyReviewConfirmation(BaseModel):
    confirmation: Literal["ARCHIVAR_CANCELACION_SIN_FILLS", "CONCILIAR_EJECUCIONES_ANTIGUAS"]
    resolution_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class MexcBotRearm(BaseModel):
    request_id: UUID
    draft_id: UUID
    anchor_id: UUID
    paper_session_id: UUID
    entry_budget_quote: str = Field(min_length=1, max_length=80)
    confirmation: Literal["REANUDAR_BOT_REAL","REANUDAR_BOT_OPERATIVO","REANUDAR_BOT_CONTINUO"]
    execution_mode:Literal["SESSION","OPERATIONAL","CONTINUOUS"] = "SESSION"
    continuous_until:datetime|None = None
    research_validation_id: UUID | None = None
    allocation_review_digest: str | None = Field(default=None,pattern=r"^[0-9a-f]{64}$")
    research_review_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def create_mexc_bot_router(access, *, authorized, authorized_passive) -> APIRouter:
    router = APIRouter(prefix="/accounts/{account_id}/mexc-bots", tags=["real-spot-bots"])

    def call(response, action):
        response.headers["Cache-Control"] = "no-store"
        try:
            return action()
        except InvalidOperation:
            raise HTTPException(status_code=422, detail="Presupuesto no decimal") from None
        except (SpotBotStoreError, SpotBotSourceError, SpotPaperLocked, LabError, VaultError,
                MexcExecutionError, MexcExecutionStateError, MexcOrderSendError, MexcOrderObservationError,
                MexcSettlementError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    def require_account(account_id):
        with access.vault.session() as db:
            if db.get(RealAccount, str(account_id)) is None:
                raise HTTPException(status_code=404, detail="Cuenta REAL no encontrada")

    def read(token, account_id, response, action):
        with access._lock:
            access.require(token, touch=False)
            # Se fija al desbloquear bajo el mismo cerrojo del token. Leer la
            # propiedad de la bóveda aquí esperaría al ciclo mientras mantiene
            # RealAccess bloqueado e impediría recibir una parada concurrente.
            generation = access._vault_generation
        def guarded():
            with access.vault.guard_session_generation(generation):
                require_account(account_id)
                return action()
        return call(response, guarded)

    @router.get("/candidates", dependencies=[Depends(authorized_passive)])
    def candidates(account_id: UUID, response: Response,
                   token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        return read(token, account_id, response, access._mexc_bots.candidates)

    @router.get("/research-review", dependencies=[Depends(authorized_passive)])
    def research_review(account_id: UUID, response: Response, paper_session_id: UUID,
                        research_validation_id: UUID, source_kind: Literal["STRATEGY", "DCA"] = "STRATEGY",
                        bot_id: UUID | None = None,
                        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        def action():
            with access._lock:
                access.require(token, touch=False)
                generation = access._vault_generation
            proof = access._mexc_bots.research_review(
                str(account_id), str(paper_session_id), str(research_validation_id), kind=source_kind,
                bot_id=str(bot_id) if bot_id is not None else None, generation=generation)
            with access._lock:
                access.require(token, touch=False)
                if access._vault_generation != generation:
                    raise HTTPException(status_code=401, detail="La sesión de la bóveda REAL cambió",
                                        headers={"Cache-Control": "no-store"})
            return proof
        return call(response, action)

    @router.get("/dca-versions",dependencies=[Depends(authorized_passive)])
    def dca_versions(account_id:UUID,response:Response,
                     token:str|None=Header(default=None,alias="X-Orbis-Vault-Token")):
        return read(token,account_id,response,access._mexc_bots.dca_versions)

    @router.get("", dependencies=[Depends(authorized_passive)])
    def bots(account_id: UUID, response: Response,
             token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        return read(token, account_id, response, lambda: access._mexc_bots.list(str(account_id)))

    @router.post("", dependencies=[Depends(authorized)])
    def activate(account_id: UUID, body: MexcBotActivate, response: Response,
                 token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        def action():
            if body.confirmation != {"SESSION":"ACTIVAR_BOT_REAL","OPERATIONAL":"ACTIVAR_BOT_OPERATIVO","CONTINUOUS":"ACTIVAR_BOT_CONTINUO"}[body.execution_mode]:
                raise SpotBotStoreError("Confirma expresamente el modo de permiso elegido")
            limits = None
            if body.source_kind == "DCA":
                from app.live_execution.dca_bot_plan import dca_real_limits
                if body.dca_budget_total_quote is None or body.dca_monthly_limit_quote is None:
                    raise SpotBotStoreError("Define asignación y límite mensual DCA REAL")
                limits = dca_real_limits(body.dca_budget_total_quote,body.dca_monthly_limit_quote)
            elif body.dca_budget_total_quote is not None or body.dca_monthly_limit_quote is not None:
                raise SpotBotStoreError("Límites DCA en una estrategia no DCA")
            with access._lock:
                _, view, trade = access._mexc_execution_clients(token, str(account_id), str(body.draft_id))
                return access._mexc_bots.activate(str(account_id), view, trade,
                    bot_id=str(body.request_id), draft_id=str(body.draft_id), anchor_id=str(body.anchor_id),
                    paper_session_id=str(body.paper_session_id), entry_budget_quote=Decimal(body.entry_budget_quote),
                    deadline=access._deadline,source_kind=body.source_kind,dca_limits=limits,execution_mode=body.execution_mode,
                    continuous_until=body.continuous_until,
                    research_validation_id=str(body.research_validation_id) if body.research_validation_id else None,
                    research_review_digest=body.research_review_digest)
        return call(response, action)

    @router.post("/{bot_id}/stop", dependencies=[Depends(authorized)])
    def stop(account_id: UUID, bot_id: UUID, body: MexcBotStop, response: Response,
             token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        with access._lock:
            access.require(token, touch=False)
            return call(response, lambda: access._mexc_bots.stop(str(account_id), str(bot_id)))

    @router.post("/{bot_id}/rearm", dependencies=[Depends(authorized)])
    def rearm(account_id: UUID, bot_id: UUID, body: MexcBotRearm, response: Response,
              token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        def action():
            if body.confirmation != {"SESSION":"REANUDAR_BOT_REAL","OPERATIONAL":"REANUDAR_BOT_OPERATIVO","CONTINUOUS":"REANUDAR_BOT_CONTINUO"}[body.execution_mode]:
                raise SpotBotStoreError("Confirma expresamente el modo de permiso elegido")
            with access._lock:
                _, view, trade = access._mexc_execution_clients(token, str(account_id), str(body.draft_id))
                deadline, generation = access._deadline, access._vault_generation
            # No retener RealAccess mientras se consulta MEXC: permite revocar
            # la revisión desde STOP/bloqueo/cambio de claves concurrente.
            return access._mexc_bots.rearm(str(account_id), str(bot_id), view, trade,
                request_id=str(body.request_id), draft_id=str(body.draft_id), anchor_id=str(body.anchor_id),
                paper_session_id=str(body.paper_session_id), entry_budget_quote=Decimal(body.entry_budget_quote),
                deadline=deadline, generation=generation,execution_mode=body.execution_mode,continuous_until=body.continuous_until,
                research_validation_id=str(body.research_validation_id) if body.research_validation_id else None,
                allocation_review_digest=body.allocation_review_digest,
                research_review_digest=body.research_review_digest)
        return call(response, action)

    @router.get("/{bot_id}/allocation-review", dependencies=[Depends(authorized_passive)])
    def allocation_review(account_id: UUID, bot_id: UUID, response: Response,
                          token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        from app.live_execution.spot_bot_allocation import spot_bot_allocation_review
        return read(token,account_id,response,lambda:spot_bot_allocation_review(
            access.vault,str(account_id),str(bot_id)))

    @router.get("/{bot_id}/decisions", dependencies=[Depends(authorized_passive)])
    def decisions(account_id: UUID, bot_id: UUID, response: Response,
                  token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        return read(token, account_id, response, lambda: access._mexc_bots.decisions(str(account_id),str(bot_id)))

    def legacy_review(account_id: UUID, bot_id: UUID, decision_id: UUID, response: Response,
                      token: str | None, confirmation: MexcLegacyReviewConfirmation | None = None) -> dict:
        from app.live_execution.spot_bot_legacy_fills import (
            archive_legacy_fills, fetch_legacy_recovery_balances, legacy_fills_replay, preview_legacy_fill_recovery,
        )
        from app.live_execution.spot_bot_legacy_review import (
            archive_legacy_cancel, legacy_cancel_replay, observe_legacy_spot_order, read_legacy_spot_decision,
        )

        with access._lock:
            access.require(token, touch=False)
            view, generation = access._mexc, access._vault_generation
        identifiers = str(account_id), str(bot_id), str(decision_id)
        clock = access._mexc_bots.clock
        confirm_fills = confirmation is not None and confirmation.confirmation == "CONCILIAR_EJECUCIONES_ANTIGUAS"
        digest = confirmation.resolution_digest if confirmation is not None else None

        def snapshot():
            with access.vault.guard_session_generation(generation), access.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN")
                replay = legacy_fills_replay(db, *identifiers, digest) if confirmation is None or confirm_fills else None
                return (None, replay) if replay is not None else (read_legacy_spot_decision(db, *identifiers), None)

        def action():
            original, result = snapshot()
            if result is None and not confirm_fills:
                result = legacy_cancel_replay(original, digest)
            proposal = balances = None
            if result is None:
                result, observed = observe_legacy_spot_order(access.vault, original, view, now=clock())
                observed_at = clock()
                if (confirmation is None or confirm_fills) and observed is not None and observed.final and observed.fills:
                    with access.vault.guard_session_generation(generation), access.vault.session() as db:
                        db.connection().exec_driver_sql("BEGIN")
                        proposal = preview_legacy_fill_recovery(db, *identifiers[:2], original, observed, now=observed_at)
                    if proposal is not None:
                        result["fill_recovery_digest"] = proposal.digest
                        result.update(base_asset=proposal.base_asset, quote_asset=proposal.quote_asset)
                        result["fees_by_asset"] = [[asset, format(amount,"f")] for asset, amount in observed.fees_by_asset]
                if confirm_fills:
                    if proposal is None or proposal.digest != digest or view is None:
                        raise SpotBotStoreError("La orden o la cartera cambiaron. Consulta de nuevo.")
                    balances = fetch_legacy_recovery_balances(access.vault, view, proposal)
                elif digest is not None and result.get("resolution_digest") != digest:
                    raise SpotBotStoreError("MEXC no confirma la misma cancelación. Consulta de nuevo.")
            if confirmation is None and original is not None and snapshot() != (original, None):
                raise SpotBotStoreError("La decisión cambió durante la consulta")
            with access._lock:
                access.require(token, touch=False)
                if access._mexc is not view or access._vault_generation != generation:
                    raise SpotBotStoreError("La conexión MEXC cambió durante la consulta")
                if confirmation is None or result.get("replayed"):
                    return result
                with access.vault.try_guard_session_generation(generation) as available:
                    if not available:
                        raise SpotBotStoreError("La bóveda está ocupada. Repite la confirmación.")
                    with access.vault.session() as db:
                        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                        if confirm_fills:
                            archived = legacy_fills_replay(db, *identifiers, digest)
                            if archived is None:
                                archived = archive_legacy_fills(db, *identifiers[:2], proposal, balances,
                                                               observed_at=observed_at, now=clock())
                        else:
                            archived = archive_legacy_cancel(db, *identifiers[:2], original, result, digest)
                        db.commit()
                        return archived

        return call(response, action)

    @router.get("/{bot_id}/decisions/{decision_id}/mexc-review", dependencies=[Depends(authorized_passive)])
    def review_legacy_decision(account_id: UUID, bot_id: UUID, decision_id: UUID, response: Response,
                               token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        return legacy_review(account_id, bot_id, decision_id, response, token)

    @router.post("/{bot_id}/decisions/{decision_id}/mexc-review/confirm", dependencies=[Depends(authorized)])
    def confirm_legacy_review(account_id: UUID, bot_id: UUID, decision_id: UUID,
                              body: MexcLegacyReviewConfirmation, response: Response,
                              token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        return legacy_review(account_id, bot_id, decision_id, response, token, body)

    @router.get("/{bot_id}/executions-page",dependencies=[Depends(authorized_passive)])
    def executions(account_id: UUID,bot_id: UUID,response: Response,
                   limit: int = Query(default=500,ge=1,le=500),offset: int = Query(default=0,ge=0),
                   revision: str | None = Query(default=None,pattern=r"^[0-9a-f]{64}$"),
                   token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        from app.live_execution.spot_bot_executions import spot_bot_executions_page
        return read(token,account_id,response,lambda:spot_bot_executions_page(access.vault,str(account_id),str(bot_id),
            limit=limit,offset=offset,revision=revision))

    @router.post("/{bot_id}/research-export", dependencies=[Depends(authorized_passive)])
    def research_export(account_id: UUID, bot_id: UUID, body: MexcBotResearchExport, response: Response,
                        token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        from app.live_execution.spot_bot_executions import export_spot_bot_fills
        result = read(token, account_id, response, lambda: export_spot_bot_fills(
            access.vault, str(account_id), str(bot_id), revision=body.revision))
        access.require(token, touch=False)
        return result

    return router
