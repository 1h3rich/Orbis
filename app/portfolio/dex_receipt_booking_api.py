"""Explicit local accounting of verified DEX receipts; never an order route."""
import hashlib,hmac
from typing import Annotated,Literal
from uuid import UUID
from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import BaseModel,ConfigDict,Field
from app.lab.store import content_hash
from app.portfolio.domain import PortfolioError
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio import dex_receipt_booking as booking

Kind=Literal['DEPLOYMENT','CONTROL','GAS_FUNDING','OPERATOR','GAS_RETURN','DEPLOYMENT_RESOLUTION','NATIVE_RESOLUTION','CALL_RESOLUTION']


class Wallet(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address:str=Field(min_length=42,max_length=42)


class Query(Wallet):
    limit:int=Field(default=100,ge=1,le=200)
    offset:int=Field(default=0,ge=0,le=100000)
    revision:str|None=Field(default=None,pattern=r'^[0-9a-f]{64}$')


class Selection(Wallet):
    trade_event_id:UUID|None=Field(default=None,strict=False)
    gas_event_ids:list[Annotated[UUID,Field(strict=False)]]=Field(default_factory=list,max_length=100,strict=False)


class Confirm(Selection):
    expected_revision:str=Field(pattern=r'^[0-9a-f]{64}$')
    preview_digest:str=Field(pattern=r'^[0-9a-f]{64}$')
    confirmation:Literal['CONTABILIZAR_RECIBO_DEX']


def create_dex_receipt_booking_router(access,*,authorized,authorized_passive):
    router=APIRouter(prefix='/accounts/{account_id}/dex-receipts',tags=['dex-receipt-accounting'])
    def run(account,body,token,response,action,*,passive=False):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                access.require(token,touch=not passive)
                wallet=canonical_wallet_address(body.address)
                proof=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
                if (str(account),'0x38',proof) not in access._wallet_verified:raise booking.DexBookingError('Verifica el control de esta wallet en BNB Chain')
                with access.vault.session() as db:return action(db,wallet)
        except (PortfolioError,ValueError) as exc:raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'}) from None

    @router.post('/query',dependencies=[Depends(authorized_passive)])
    def history(account_id:UUID,body:Query,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        # Keep the transient wallet out of URLs and access logs.
        def read(db,wallet):
            values=booking.list_receipts(db,account_id=str(account_id),wallet_address=wallet);revision=content_hash(values)
            if body.offset>len(values) or body.offset>0 and body.revision is None or body.revision is not None and body.revision!=revision:
                raise booking.DexBookingError('Los recibos cambiaron; actualiza la lista')
            end=min(body.offset+body.limit,len(values))
            return {'items':values[body.offset:end],'total':len(values),'revision':revision,'offset':body.offset,'next_offset':end if end<len(values) else None}
        return run(account_id,body,token,response,read,passive=True)

    def selection(body):return {'trade_event_id':str(body.trade_event_id) if body.trade_event_id else None,'gas_event_ids':[str(value) for value in body.gas_event_ids]}

    @router.post('/{kind}/{identifier}/booking-preview',dependencies=[Depends(authorized)])
    def preview(account_id:UUID,kind:Kind,identifier:UUID,body:Selection,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,body,token,response,lambda db,wallet:booking.preview_booking(db,kind,str(identifier),context_account_id=str(account_id),wallet_address=wallet,**selection(body)))

    @router.post('/{kind}/{identifier}/book',dependencies=[Depends(authorized)])
    def book(account_id:UUID,kind:Kind,identifier:UUID,body:Confirm,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return run(account_id,body,token,response,lambda db,wallet:booking.book_receipt(db,kind,str(identifier),context_account_id=str(account_id),wallet_address=wallet,
            expected_revision=body.expected_revision,preview_digest=body.preview_digest,now=access._dex_bots.now(),**selection(body)))
    return router
