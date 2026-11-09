"""API local para planes de ingreso firmados por una wallet EOA, no por ORBIS."""

from datetime import datetime, timezone
import hashlib
import hmac
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from app.integrations.mexc_readonly import MexcReadOnlyError
from app.live_execution.dex_gas_funding import collect_funding_receipt
from app.live_execution.dex_nonce_search import find_finalized_nonce, pinned_block
from app.live_execution.dex_operator_transport import BnbNonceReadClient, BnbOperatorReadClient
from app.live_execution.evm_transaction import quantity, word
from app.live_execution.mexc_uid_guard import MexcTransferRecoveryView
from app.portfolio.domain import PortfolioError
from app.portfolio.vault import VaultError
from app.portfolio.wallet_funding import (
    WalletFundingError, _identity, _public, claim_funding, funding_record, list_funding, prepare_funding, report_funding,
)
from app.portfolio.wallet_proof import canonical_wallet_address, WalletProofError
from app.portfolio.wallet_funding_receipt import FundingReceiptSource, receipt_observation
from app.portfolio.wallet_funding_resolution import settle_discovered_funding
from app.portfolio.wallet_funding_credit import book_credit, booking_candidates, booking_options, confirm_credit, settlement_status


class WalletFundingPrepare(BaseModel):
    request_id: UUID
    address: str = Field(min_length=42,max_length=42)
    chain_id: Literal["0x38"]
    asset: Literal["USDT"]
    amount: str = Field(min_length=1,max_length=100)
    minimum_deposit_usdt: str = Field(max_length=100,pattern=r"^[0-9]+(\.[0-9]+)?$")
    decimals: int = Field(strict=True,ge=0,le=36)
    nonce: str = Field(min_length=1,max_length=20)
    gas: str = Field(min_length=1,max_length=7)
    gas_price: str = Field(min_length=1,max_length=19)
    max_gas_bnb: str = Field(min_length=1,max_length=100)


class WalletFundingClaim(BaseModel):
    address: str = Field(min_length=42,max_length=42)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["SOLICITAR_ENVIO_WALLET"]


class WalletFundingReport(BaseModel):
    tx_hash: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")


class WalletFundingDiscover(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)
    address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")


class WalletFundingBnbCheck(WalletFundingDiscover):
    tx_hash: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")


class WalletFundingRejection(BaseModel):
    confirmation: Literal["WALLET_RECHAZO_4001"]


class WalletFundingCheck(BaseModel):
    evidence: dict


class WalletFundingBook(BaseModel):
    mode: Literal["NEW_CONTRIBUTION","NEW_INTERNAL","EXISTING_EVENT"]
    address: str | None = Field(default=None,min_length=42,max_length=42)
    event_id: UUID | None = None
    source_account_id: UUID | None = None
    source_revision: str | None = Field(default=None,pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["REGISTRAR_INGRESO_ACREDITADO"]


class WalletFundingBookingOptions(BaseModel):
    address: str | None = Field(default=None,min_length=42,max_length=42)


def create_wallet_funding_router(access, *, authorized, authorized_passive, rpc_factory=None) -> APIRouter:
    router = APIRouter(prefix="/accounts/{account_id}/wallet-funding",tags=["wallet-funding"])

    def call(response, action):
        response.headers["Cache-Control"] = "no-store"
        try:
            return action()
        except (PortfolioError,WalletProofError,ValueError) as exc:
            raise HTTPException(status_code=409,detail=str(exc),headers={"Cache-Control":"no-store"}) from None
        except MexcReadOnlyError:
            raise HTTPException(status_code=503,detail="No se pudo verificar MEXC View para el ingreso",
                                headers={"Cache-Control":"no-store"}) from None

    def verified(address, account_id):
        address = canonical_wallet_address(address)
        if int(address[2:],16) == 0:
            raise WalletFundingError("Wallet no válida")
        digest = hmac.new(access._wallet_proof_key,address.encode("ascii"),hashlib.sha256).hexdigest()
        if (account_id,"0x38",digest) not in access._wallet_verified:
            raise WalletFundingError("Verifica el control de esta wallet en BNB Chain y esta sesión REAL")

    @router.get("",dependencies=[Depends(authorized_passive)])
    def history(account_id: UUID, response: Response, focus_id: UUID | None = None,
                token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        with access._lock:
            access.require(token,touch=False)
            generation = access._vault_generation
        def action():
            with access.vault.guard_session_generation(generation), access.vault.session() as db:
                return list_funding(db,str(account_id),now=datetime.now(timezone.utc),
                                    focus_id=str(focus_id) if focus_id else None)
        return call(response,action)

    @router.post("",dependencies=[Depends(authorized)])
    def prepare(account_id: UUID, body: WalletFundingPrepare, response: Response,
                token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            with access._lock:
                access.require(token)
                verified(body.address,str(account_id))
                destination = access.mexc_erc20_deposit_destination(token,str(account_id),body.asset,body.chain_id)
                with access.vault.session() as db:
                    return prepare_funding(db,identifier=str(body.request_id),account_id=str(account_id),
                        account_ref=access._mexc_account_ref,wallet_address=body.address,destination=destination,
                        amount=body.amount,minimum_deposit_usdt=body.minimum_deposit_usdt,
                        decimals=body.decimals,nonce=body.nonce,gas=body.gas,
                        gas_price=body.gas_price,max_gas_bnb=body.max_gas_bnb,now=datetime.now(timezone.utc))
        return call(response,action)

    @router.post("/{identifier}/claim",dependencies=[Depends(authorized)])
    def claim(account_id: UUID, identifier: UUID, body: WalletFundingClaim, response: Response,
              token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            with access._lock:
                access.require(token)
                verified(body.address,str(account_id))
                # Nueva lectura del catálogo/destino y de la identidad MEXC.
                destination = access.mexc_erc20_deposit_destination(token,str(account_id),"USDT","0x38")
                with access.vault.session() as db:
                    return claim_funding(db,str(identifier),account_id=str(account_id),account_ref=access._mexc_account_ref,
                        wallet_address=body.address,destination=destination,plan_digest=body.plan_digest,
                        now=datetime.now(timezone.utc))
        return call(response,action)

    def report_action(token, account_id, identifier, tx_hash=None, rejected=False):
        with access._lock:
            access.require(token)
            with access.vault.session() as db:
                return report_funding(db,str(identifier),account_id=str(account_id),tx_hash=tx_hash,
                                       rejected=rejected,now=datetime.now(timezone.utc))

    @router.post("/{identifier}/hash",dependencies=[Depends(authorized)])
    def report_hash(account_id: UUID, identifier: UUID, body: WalletFundingReport, response: Response,
                    token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        return call(response,lambda:report_action(token,account_id,identifier,body.tx_hash))

    def locate_funding(wallet, nonce, assert_current):
        with (rpc_factory or BnbNonceReadClient)() as rpc:
            match = find_finalized_nonce(rpc,wallet,nonce)
        assert_current()
        if match is None:
            return None, None
        with (rpc_factory or BnbOperatorReadClient)() as rpc:
            try:
                if quantity(rpc("eth_chainId",[])) != 56:
                    raise ValueError("wrong chain")
                block = pinned_block(rpc,match.block_number,match.block_hash)
                tx = rpc("eth_getTransactionByHash",[match.tx_hash])
                receipt = rpc("eth_getTransactionReceipt",[match.tx_hash])
                finalized = rpc("eth_getBlockByNumber",["finalized",False])
                cancellation_code = None
                if isinstance(tx,dict) and tx.get("input") == "0x" and str(tx.get("to","")).lower() == wallet:
                    cancellation_code = rpc("eth_getCode",[wallet,hex(match.block_number)])
                pinned_block(rpc,quantity(finalized["number"]),word(finalized["hash"]))
                pinned_block(rpc,match.block_number,match.block_hash)
                evidence = {"chain_id":"0x38","tx_hash":match.tx_hash,"transaction":tx,
                            "receipt":receipt,"block":block,"finalized":finalized}
                match.check_receipt(evidence)
            except (KeyError,TypeError,ValueError,OverflowError):
                raise WalletFundingError("El RPC no devolvió el recibo BNB finalizado del envío") from None
        return evidence, cancellation_code

    def read_known_funding(tx_hash: str) -> dict | None:
        with (rpc_factory or BnbOperatorReadClient)() as rpc:
            evidence = collect_funding_receipt(rpc, tx_hash)
        if evidence is None:
            return None
        try:
            tx, block = evidence["transaction"], evidence["block"]
            index = quantity(tx["transactionIndex"])
            hashes = block["transactions"]
            if type(hashes) is not list or index >= len(hashes) or word(hashes[index]) != tx_hash:
                raise ValueError("block index")
        except (KeyError, ValueError, TypeError, OverflowError):
            raise WalletFundingError("El bloque BNB no contiene el ingreso en su índice") from None
        return evidence

    def confirm_funding_credit(token, account, identifier, evidence,
                               chain_source: FundingReceiptSource = "WALLET_RPC_OBSERVED"):
        with access._lock:
            access.require(token)
            with access.vault.session() as db:
                receipt_observation(db,identifier,account,evidence,now=datetime.now(timezone.utc))
                row,_,_ = funding_record(db,identifier)
                archive_ref = row.account_ref
                age = (datetime.now(timezone.utc)-row.created_at).total_seconds()
            # Captura fresca View y catálogo BSC; la wallet nunca declara un abono.
            observation = access.mexc_transfers_recent(
                token, 90 if age > 7*86400 else 7, recovery_scope=(account, archive_ref),
            )
            access.require(token)
            with access.vault.session() as db:
                return confirm_credit(db,identifier,account,evidence,observation["archive"]["id"],
                                      now=datetime.now(timezone.utc),chain_source=chain_source)

    def recover_funding(token, account, identifier, address, *, check_credit=False, tx_hash=None):
        wallet = canonical_wallet_address(address)
        with access._lock:
            access.require(token)
            verified(wallet,account)
            generation = access._vault_generation
            view, credential_ref = access._mexc, access._mexc_account_ref
            with access.vault.session() as db:
                row, terms, state = funding_record(db,identifier)
                if (row.account_id != account or row.status not in ("WALLET_REQUESTED","HASH_REPORTED","CHAIN_CLOSED")
                        or _identity(db,"wallet",wallet) != terms["wallet_digest"]):
                    raise WalletFundingError("El envío pendiente no corresponde a esta wallet")
                if row.status == "CHAIN_CLOSED":
                    return _public(db,row,terms,state,now=datetime.now(timezone.utc))
                if check_credit:
                    prior = settlement_status(db,identifier)
                    if prior["exchange_credited"]:
                        return prior
                    if view is None or credential_ref is None:
                        raise WalletFundingError("Conecta MEXC View de la cuenta del envío")
                if tx_hash is not None and _identity(db,"tx",tx_hash) != state["tx_digest"]:
                    raise WalletFundingError("El txId no coincide con el ingreso registrado")
                nonce, plan_digest, archive_ref = int(terms["nonce"]), row.payload_digest, row.account_ref
            if check_credit and credential_ref != archive_ref:
                # Reject a foreign View before querying BNB or recording a recovered hash.
                uid = view.account_uid()
                if view.account_reference() != credential_ref:
                    raise WalletFundingError("La referencia MEXC cambió")
                with access.vault.session() as db:
                    db.connection().exec_driver_sql("BEGIN")
                    MexcTransferRecoveryView(account, archive_ref, credential_ref).verify(db, uid)

        def assert_current():
            with access._lock:
                access.require(token,touch=False)
                if (access._vault_generation != generation or check_credit
                        and (access._mexc is not view or access._mexc_account_ref != credential_ref)):
                    raise WalletFundingError("La sesión cambió durante la búsqueda BNB")
                verified(wallet,account)

        if tx_hash is None:
            evidence, cancellation_code = locate_funding(wallet, nonce, assert_current)
        else:
            evidence, cancellation_code = read_known_funding(tx_hash), None
        with access._lock:
            assert_current()
            if evidence is None:
                if check_credit:
                    return {"status":"BNB_PENDING","exchange_credited":False,"booked":False}
                raise WalletFundingError("El nonce todavía no tiene una transacción finalizada")
            with access.vault.session() as db:
                result = settle_discovered_funding(db,identifier,account,wallet,plan_digest,evidence,
                    cancellation_code=cancellation_code,now=datetime.now(timezone.utc))
            if not check_credit or result["status"] == "CHAIN_CLOSED":
                return result
            return confirm_funding_credit(token, account, identifier, evidence, "ORBIS_RPC_OBSERVED")

    @router.post("/{identifier}/discover",dependencies=[Depends(authorized)])
    def discover(account_id: UUID, identifier: UUID, body: WalletFundingDiscover, response: Response,
                 token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        return call(response, lambda:recover_funding(token,str(account_id),str(identifier),body.address))

    @router.post("/{identifier}/check-bnb", dependencies=[Depends(authorized)])
    def check_bnb(account_id: UUID, identifier: UUID, body: WalletFundingBnbCheck, response: Response,
                  token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        return call(response, lambda:recover_funding(token,str(account_id),str(identifier),body.address,
                    check_credit=True,tx_hash=body.tx_hash.lower() if body.tx_hash else None))

    @router.post("/{identifier}/rejected",dependencies=[Depends(authorized)])
    def report_rejected(account_id: UUID, identifier: UUID, body: WalletFundingRejection, response: Response,
                        token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        return call(response,lambda:report_action(token,account_id,identifier,rejected=True))

    @router.post("/{identifier}/check",dependencies=[Depends(authorized)])
    def check(account_id: UUID, identifier: UUID, body: WalletFundingCheck, response: Response,
              token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        return call(response, lambda:confirm_funding_credit(token,str(account_id),str(identifier),body.evidence))

    @router.get("/{identifier}/roundtrip", dependencies=[Depends(authorized_passive)])
    def archived_roundtrip(account_id: UUID, identifier: UUID, response: Response,
                           token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        from app.portfolio.mexc_roundtrip_audit import review_mexc_roundtrip

        with access._lock:
            access.require(token,touch=False)
            generation = access._vault_generation

        def action():
            with access.vault.guard_session_generation(generation), access.vault.session() as db:
                db.connection().exec_driver_sql("BEGIN")
                return review_mexc_roundtrip(db, str(account_id), str(identifier))
        try:
            return call(response, action)
        except VaultError:
            raise HTTPException(status_code=401, detail="La sesión de la bóveda REAL cambió",
                                headers={"Cache-Control":"no-store"}) from None

    @router.post("/{identifier}/roundtrip", dependencies=[Depends(authorized_passive)])
    def roundtrip(account_id: UUID, identifier: UUID, body: WalletFundingDiscover, response: Response,
                  token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        from app.portfolio.mexc_roundtrip_audit import review_mexc_roundtrip

        def action():
            wallet = canonical_wallet_address(body.address)
            with access._lock:
                access.require(token,touch=False)
                verified(wallet,str(account_id))
                with access.vault.session() as db:
                    db.connection().exec_driver_sql("BEGIN")
                    return review_mexc_roundtrip(db,str(account_id),str(identifier),wallet)
        return call(response, action)

    @router.get("/{identifier}/booking-candidates",dependencies=[Depends(authorized_passive)])
    def candidates(account_id: UUID, identifier: UUID, response: Response,
                   token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        with access._lock:
            access.require(token,touch=False)
            generation = access._vault_generation
        def action():
            with access.vault.guard_session_generation(generation), access.vault.session() as db:
                return booking_candidates(db,str(identifier),str(account_id))
        return call(response,action)

    @router.post("/{identifier}/booking-options",dependencies=[Depends(authorized_passive)])
    def options(account_id: UUID,identifier: UUID,body: WalletFundingBookingOptions,response: Response,
                token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            with access._lock:
                access.require(token,touch=False)
                if body.address is not None:
                    verified(body.address,str(account_id))
                with access.vault.session() as db:
                    return booking_options(db,str(identifier),str(account_id),body.address)
        return call(response,action)

    @router.post("/{identifier}/book",dependencies=[Depends(authorized)])
    def book(account_id: UUID, identifier: UUID, body: WalletFundingBook, response: Response,
             token: str | None = Header(default=None,alias="X-Orbis-Vault-Token")):
        def action():
            with access._lock:
                access.require(token)
                if body.mode == "NEW_INTERNAL":
                    verified(body.address,str(account_id))
                with access.vault.session() as db:
                    return book_credit(db,str(identifier),str(account_id),mode=body.mode,
                        wallet_address=body.address,event_id=str(body.event_id) if body.event_id else None,
                        source_account_id=str(body.source_account_id) if body.source_account_id else None,
                        source_revision=body.source_revision,
                        now=datetime.now(timezone.utc))
        return call(response,action)

    return router
