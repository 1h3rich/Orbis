"""API local de retirada puntual con credencial de un solo intento, no de bot."""

from datetime import datetime,timezone
import hashlib
import hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field

from app.integrations.mexc_readonly import MexcReadOnlyError
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_funding import collect_funding_receipt
from app.live_execution.dex_operator_transport import BnbOperatorReadClient
from app.live_execution.mexc_uid_guard import assert_known_mexc_uid
from app.live_execution.mexc_withdrawal import cached_permission,fetch_withdrawal_inputs,submit_withdrawal_once
from app.live_execution.mexc_withdrawal_transport import MexcWithdrawalClient,MexcWithdrawalError
from app.portfolio.domain import PortfolioError
from app.portfolio.models import RealMexcWithdrawal
from app.portfolio.mexc_withdrawal_journal import (
    WithdrawalError,cancel_prepared,check_current_plan,estimate_max_withdrawal,prepare_withdrawal,prepared_review_terms,
    public_record,recipient_matches,withdrawal_record,
)
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.vault import VaultError
from app.live_execution.mexc_withdrawal_reconciliation import observe_withdrawal,current_balance
from app.portfolio.mexc_withdrawal_receipt import confirm_receipt,receipt_record
from app.portfolio.mexc_withdrawal_booking import book_withdrawal,booking_options,resolve_rejected,booking_record,settlement_status


class WithdrawalRoute(BaseModel):
    address: str = Field(min_length=42,max_length=42)
    token_contract: str = Field(min_length=42,max_length=42)
    decimals: int = Field(strict=True,ge=0,le=36)


class WithdrawalEstimate(WithdrawalRoute):
    model_config = ConfigDict(extra="forbid")
    max_fee: str | None = Field(default=None,min_length=1,max_length=100)


class WithdrawalPrepare(WithdrawalRoute):
    request_id: UUID
    amount: str = Field(min_length=1,max_length=100)
    max_fee: str = Field(min_length=1,max_length=100)


class WithdrawalPlanReview(WithdrawalRoute):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class WithdrawalSubmit(BaseModel):
    address: str = Field(min_length=42,max_length=42)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    api_key: str = Field(min_length=8,max_length=128)
    secret_key: str = Field(min_length=8,max_length=256)
    confirmation: Literal["SOLICITAR_RETIRADA_REAL"]
    fee_quote_only: Literal[True]


class WithdrawalCancel(BaseModel):
    confirmation: Literal["CANCELAR_PLAN_SIN_ENVIO"]


class WithdrawalWallet(BaseModel):
    address: str = Field(min_length=42,max_length=42)


class WithdrawalReceipt(WithdrawalWallet):
    evidence: dict


class WithdrawalBnbReceipt(WithdrawalWallet):
    model_config = ConfigDict(extra="forbid")


class WithdrawalBook(WithdrawalWallet):
    mode: Literal["NEW_EXTERNAL","NEW_INTERNAL","EXISTING_EVENT"]
    target_account_id: UUID | None = None
    event_id: UUID | None = None
    accept_fee_override: bool = Field(default=False,strict=True)
    confirmation: Literal["CONTABILIZAR_RETIRADA_RECIBIDA"]


class WithdrawalRejected(BaseModel):
    confirmation: Literal["CONFIRMAR_RECHAZO_SIN_CARGO"]


def create_mexc_withdrawal_router(access,*,authorized,authorized_passive,
                                  client_factory=MexcWithdrawalClient, rpc_factory=None) -> APIRouter:
    read_bnb = rpc_factory or BnbOperatorReadClient
    router = APIRouter(prefix="/accounts/{account_id}/mexc-withdrawals",tags=["mexc-withdrawals"])

    def call(response,action):
        response.headers["Cache-Control"] = "no-store"
        try:
            return action()
        except (PortfolioError,MexcWithdrawalError,ValueError,VaultError) as exc:
            raise HTTPException(status_code=409,detail=str(exc),headers={"Cache-Control":"no-store"}) from None
        except MexcReadOnlyError:
            raise HTTPException(status_code=503,detail="No se pudo verificar MEXC View para la retirada",
                                headers={"Cache-Control":"no-store"}) from None

    def require_wallet(account_id: str, address: str) -> None:
        digest = hmac.new(access._wallet_proof_key,address.encode("ascii"),hashlib.sha256).hexdigest()
        if (account_id,"0x38",digest) not in access._wallet_verified:
            raise WithdrawalError("Verifica el control de la wallet de destino en BNB Chain y esta sesión REAL")

    def context(token,address,account_id):
        with access._lock:
            access.require(token)
            address = canonical_wallet_address(address)
            require_wallet(account_id,address)
            if access._mexc is None or access._mexc_account_ref is None:
                raise WithdrawalError("Conecta MEXC View")
            view,generation,ref = access._mexc,access._vault_generation,access._mexc_account_ref
            return address,view,generation,ref,cached_permission(access,token,view,generation)

    def observed_inputs(token: str | None, body: WithdrawalRoute, account_id: str):
        address,view,generation,ref,active = context(token,body.address,account_id)
        uid,source = fetch_withdrawal_inputs(view,address)
        if canonical_wallet_address(body.token_contract) != source["route"]["token_contract"]:
            raise WithdrawalError("El token consultado en la wallet no coincide con USDT/BSC de MEXC")
        active()
        if not assert_known_mexc_uid(access.vault,ref,uid):
            raise WithdrawalError("Identidad MEXC View no vinculada")
        return address,generation,ref,active,source

    @router.get("",dependencies=[Depends(authorized_passive)])
    def history(account_id: UUID,response: Response,focus_id: UUID | None = None,
                token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        with access._lock:
            access.require(token,touch=False)
            generation = access._vault_generation
        def action():
            with access.vault.guard_session_generation(generation),access.vault.session() as db:
                query = db.query(RealMexcWithdrawal).filter_by(account_id=str(account_id))
                if focus_id is not None:
                    query = query.order_by((RealMexcWithdrawal.id == str(focus_id)).desc())
                rows = query.order_by(RealMexcWithdrawal.created_at.desc(), RealMexcWithdrawal.id.desc()).limit(100).all()
                return [public_record(db,row.id,datetime.now(timezone.utc)) for row in rows]
        return call(response,action)

    @router.post("",dependencies=[Depends(authorized)])
    def prepare(account_id: UUID,body: WithdrawalPrepare,response: Response,
                token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            address,generation,ref,active,source = observed_inputs(token,body,str(account_id))
            with access.vault.guard_session_generation(generation),access.vault.session() as db:
                active()
                return prepare_withdrawal(db,identifier=str(body.request_id),account_id=str(account_id),
                    account_ref=ref,address=address,decimals=body.decimals,amount=body.amount,max_fee=body.max_fee,
                    source=source,now=datetime.now(timezone.utc))
        return call(response,action)

    @router.post("/estimate-max",dependencies=[Depends(authorized)])
    def estimate(account_id: UUID,body: WithdrawalEstimate,response: Response,
                 token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            address,generation,ref,active,source = observed_inputs(token,body,str(account_id))
            with access.vault.guard_session_generation(generation),access.vault.session() as db:
                active()
                db.connection().exec_driver_sql("BEGIN")
                return estimate_max_withdrawal(db,account_id=str(account_id),account_ref=ref,address=address,
                    decimals=body.decimals,max_fee=body.max_fee,source=source,now=datetime.now(timezone.utc))
        return call(response,action)

    @router.post("/{identifier}/review", dependencies=[Depends(authorized)])
    def review(account_id: UUID, identifier: UUID, body: WithdrawalPlanReview, response: Response,
               token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        def action():
            account, withdrawal = str(account_id), str(identifier)
            address, view, generation, ref, active = context(token, body.address, account)
            selection = {"account_id": account, "account_ref": ref, "address": address,
                         "contract": canonical_wallet_address(body.token_contract),
                         "decimals": body.decimals, "plan_digest": body.plan_digest}
            with access.vault.guard_session_generation(generation), access.vault.session() as db:
                active()
                prepared_review_terms(db, withdrawal, **selection, now=datetime.now(timezone.utc))
            uid, source = fetch_withdrawal_inputs(view, address)
            active()
            if not assert_known_mexc_uid(access.vault, ref, uid):
                raise WithdrawalError("Identidad MEXC View no vinculada")
            with access.vault.guard_session_generation(generation), access.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN")
                now = datetime.now(timezone.utc)
                terms = prepared_review_terms(db, withdrawal, **selection, now=now)
                check_current_plan(db, terms, source, now)
                result = public_record(db, withdrawal, now)
                active()
                return result
        return call(response, action)

    @router.post("/{identifier}/submit",dependencies=[Depends(authorized)])
    def submit(account_id: UUID,identifier: UUID,body: WithdrawalSubmit,response: Response,
               token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            address,view,generation,ref,active = context(token,body.address,str(account_id))
            # Credencial sólo en este objeto/request. No se conecta, comparte o guarda en RealAccess.
            client = client_factory(body.api_key,body.secret_key)
            return submit_withdrawal_once(access,identifier=str(identifier),account_id=str(account_id),account_ref=ref,
                address=address,plan_digest=body.plan_digest,view=view,generation=generation,assert_active=active,client=client)
        return call(response,action)

    @router.post("/{identifier}/cancel",dependencies=[Depends(authorized)])
    def cancel(account_id: UUID,identifier: UUID,body: WithdrawalCancel,response: Response,
               token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            with access._lock:
                access.require(token)
                with access.vault.session() as db:
                    return cancel_prepared(db,str(identifier),str(account_id),now=datetime.now(timezone.utc))
        return call(response,action)

    @router.post("/{identifier}/observe",dependencies=[Depends(authorized)])
    def observe(account_id: UUID,identifier: UUID,response: Response,
                token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        return call(response,lambda:observe_withdrawal(access,token,str(account_id),str(identifier))[0])

    @router.post("/{identifier}/receipt",dependencies=[Depends(authorized)])
    def receipt(account_id: UUID,identifier: UUID,body: WithdrawalReceipt,response: Response,
                token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            observation,context = observe_withdrawal(access,token,str(account_id),str(identifier))
            if observation["status"] != "MEXC_SUCCESS_PENDING_RECEIPT":
                raise WithdrawalError("MEXC aún no confirma esta retirada; no se acredita la wallet")
            _,_,generation,active,_ = context
            with access.vault.guard_session_generation(generation),access.vault.session() as db:
                active()
                return confirm_receipt(db,str(identifier),str(account_id),body.address,observation["observation_id"],body.evidence,
                                       now=datetime.now(timezone.utc))
        return call(response,action)

    @router.post("/{identifier}/receipt-bnb", dependencies=[Depends(authorized)])
    def receipt_bnb(account_id: UUID, identifier: UUID, body: WithdrawalBnbReceipt, response: Response,
                    token: str | None = Header(default=None, alias="X-Orbis-Vault-Token")):
        def action():
            account, withdrawal = str(account_id), str(identifier)
            address, _, generation, _, active = context(token, body.address, account)
            with access.vault.guard_session_generation(generation), access.vault.session() as db:
                plan, terms, _ = withdrawal_record(db, withdrawal)
                if (plan.account_id != account
                        or plan.status not in ("SUBMISSION_UNKNOWN", "ACKNOWLEDGED")
                        or not recipient_matches(db, terms, address)):
                    raise WithdrawalError("La retirada no corresponde a esta cuenta y wallet")
                if receipt_record(db, withdrawal):
                    active()
                    return settlement_status(db, withdrawal)

            observation, _ = observe_withdrawal(access, token, account, withdrawal)
            active()
            if observation["status"] != "MEXC_SUCCESS_PENDING_RECEIPT":
                return {"status": observation["status"], "wallet_received": False}

            try:
                with read_bnb() as rpc:
                    evidence = collect_funding_receipt(rpc, observation["tx_hash"])
            except DexExecutorError:
                raise WithdrawalError("El RPC de ORBIS no devolvió un recibo BNB verificable") from None

            with access._lock:
                access.require(token, touch=False)
                require_wallet(account, address)
                active()
                if evidence is None:
                    return {"status": "BNB_PENDING", "wallet_received": False}
                with access.vault.guard_session_generation(generation), access.vault.session() as db:
                    return confirm_receipt(
                        db, withdrawal, account, address, observation["observation_id"], evidence,
                        now=datetime.now(timezone.utc), chain_source="ORBIS_RPC_OBSERVED",
                    )
        return call(response, action)

    @router.post("/{identifier}/booking-options",dependencies=[Depends(authorized)])
    def options(account_id: UUID,identifier: UUID,body: WithdrawalWallet,response: Response,
                token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            with access._lock:
                access.require(token)
                generation = access._vault_generation
            with access.vault.guard_session_generation(generation),access.vault.session() as db:
                return booking_options(db,str(identifier),str(account_id),body.address)
        return call(response,action)

    @router.post("/{identifier}/book",dependencies=[Depends(authorized)])
    def book(account_id: UUID,identifier: UUID,body: WithdrawalBook,response: Response,
             token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            arguments = dict(address=body.address,mode=body.mode,target_account_id=str(body.target_account_id) if body.target_account_id else None,
                event_id=str(body.event_id) if body.event_id else None,accept_fee_override=body.accept_fee_override)
            with access._lock:
                access.require(token)
                generation = access._vault_generation
            with access.vault.guard_session_generation(generation),access.vault.session() as db:
                if booking_record(db,str(identifier)):
                    return book_withdrawal(db,str(identifier),str(account_id),**arguments,view={},uid="",observation_id="",now=datetime.now(timezone.utc))
            observation,context = observe_withdrawal(access,token,str(account_id),str(identifier))
            if observation["status"] != "MEXC_SUCCESS_PENDING_RECEIPT":
                raise WithdrawalError("MEXC no confirma una retirada correcta para contabilizar")
            view,recovery,generation,active,uid = context
            balance = current_balance(view,uid)
            with access.vault.guard_session_generation(generation),access.vault.session() as db:
                active()
                return book_withdrawal(db,str(identifier),str(account_id),**arguments,view=balance,uid=uid,
                    observation_id=observation["observation_id"],now=datetime.now(timezone.utc),recovery=recovery)
        return call(response,action)

    @router.post("/{identifier}/resolve-rejected",dependencies=[Depends(authorized)])
    def rejected(account_id: UUID,identifier: UUID,body: WithdrawalRejected,response: Response,
                 token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            observation,context = observe_withdrawal(access,token,str(account_id),str(identifier))
            if observation["status"] != "MEXC_REJECTED_NEEDS_REVIEW":
                raise WithdrawalError("MEXC no confirma el rechazo de esta solicitud")
            view,recovery,generation,active,uid = context
            balance = current_balance(view,uid)
            with access.vault.guard_session_generation(generation),access.vault.session() as db:
                active()
                return resolve_rejected(db,str(identifier),str(account_id),observation["observation_id"],balance,uid,
                                        now=datetime.now(timezone.utc),recovery=recovery)
        return call(response,action)

    return router
