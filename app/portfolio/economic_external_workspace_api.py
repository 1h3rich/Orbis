"""Read-only inputs for the external-receipt editor; no inferred accounting."""
from datetime import datetime,timedelta
import hashlib,hmac,json
from uuid import UUID

from fastapi import APIRouter,Depends,Header,HTTPException,Response
from pydantic import Field
from sqlalchemy import select,or_

from app.portfolio import models as m,dex_external_observation as observations,dex_external_order as orders
from app.portfolio import economic_external_booking as bookings,economic_external_acceptance as accepted
from app.portfolio.economic_external_capture import capture_external_effects,WalletParticipant
from app.portfolio.economic_review_api import StrictModel,Address,WalletContext
from app.portfolio.economic_review_wallets import review_wallets
from app.portfolio.economic_projection import ProjectionError,ProjectionLimits
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.economic_acceptance import _snapshot
from app.portfolio.dex_deployments import _identity
from app.portfolio.domain import PortfolioError
from app.portfolio.vault import VaultError
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_funding_credit import _wallet_owner


class Contexts(StrictModel):
    address:Address
    wallets:list[WalletContext]=Field(default_factory=list,max_length=1023)


def create_external_workspace_router(access,*,authorized):
    router=APIRouter(prefix='/accounts/{account_id}/external-review',tags=['external-effects'])
    def capture(token,account,body):
        pairs=((str(account),canonical_wallet_address(body.address)),*((item.context_account_id,canonical_wallet_address(item.address))for item in body.wallets))
        access.require(token,touch=False)
        for context,wallet in pairs:
            digest=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
            if(context,'0x38',digest)not in access._wallet_verified:raise ProjectionError('Verifica cada wallet y contexto de la revisión')
        return pairs
    def fail(exc,token):
        access.require(token,touch=False)
        raise HTTPException(409,str(exc),headers={'Cache-Control':'no-store'})from None

    @router.get('/saved',dependencies=[Depends(authorized)])
    def saved(account_id:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                access.require(token,touch=False)
                with access.vault.session()as db:
                    _snapshot(db);limit=ProjectionLimits().max_claims
                    if db.get(m.RealAccount,str(account_id))is None:raise ProjectionError('No se encuentra la cartera')
                    linked=select(m.RealDexExternalBookingAccount.booking_id).where(m.RealDexExternalBookingAccount.account_id==str(account_id))
                    contexts=select(m.RealDexExternalObservation.id).where(m.RealDexExternalObservation.account_id==str(account_id))
                    query=select(m.RealDexExternalBooking).where(or_(m.RealDexExternalBooking.id.in_(linked),
                        m.RealDexExternalBooking.observation_id.in_(contexts))).order_by(
                        m.RealDexExternalBooking.created_at.desc(),m.RealDexExternalBooking.id).limit(limit+1)
                    result=[]
                    for raw in db.scalars(query):
                        row,book=bookings.booking_record(db,raw.id);link=accepted.accepted_link(db,row.id)
                        observed=observations.observation_record(db,row.observation_id)[1]
                        result.append({'source_kind':observed['source_kind'],'source_id':observed['source_id'],
                            'context_account_id':observed['account_id'],'observation_id':row.observation_id,
                            'accepted_id':link[0].acceptance_id,'accepted_at':link[0].created_at.isoformat(),'booked':True})
                    if len(result)>limit:raise ProjectionError('Demasiados recibos guardados para esta revisión')
                    return {'sources':result,'moves_funds':False,'trading_authority':False}
        except(PortfolioError,VaultError,ValueError)as exc:
            with access._lock:fail(exc,token)

    @router.post('/catalog',dependencies=[Depends(authorized)])
    def catalog(account_id:UUID,body:Contexts,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                pairs=capture(token,account_id,body)
                with access.vault.session()as db:
                    _snapshot(db);_existing_salt(db);limits=ProjectionLimits()
                    accounts=tuple(db.scalars(select(m.RealAccount.id).order_by(m.RealAccount.id).limit(limits.max_accounts+1)))
                    if len(accounts)>limits.max_accounts:raise ProjectionError('Demasiadas carteras para esta revisión')
                    targets,descriptors=review_wallets(db,accounts,tuple({wallet for _,wallet in pairs}),limits=limits)
                    owner=_wallet_owner(db,pairs[0][1])
                    if owner is None:raise ProjectionError('Vincula la wallet a una cartera propia')
                    addresses={_identity(db,'wallet',item.address):item for item in targets};sources=[]
                    query=select(m.RealDexExternalObservation).where(m.RealDexExternalObservation.wallet_digest.in_(addresses)).order_by(
                        m.RealDexExternalObservation.created_at,m.RealDexExternalObservation.id).limit(limits.max_claims+1)
                    for raw in db.scalars(query):
                        observed=observations.observation_record(db,raw.id);row,stored=observed;target=addresses[row.wallet_digest]
                        proof_digest=hmac.new(access._wallet_proof_key,target.address.encode('ascii'),hashlib.sha256).hexdigest()
                        book=bookings.booking_record(db,bookings.identifier(row.id));link=accepted.accepted_link(db,book[0].id)if book else None
                        sources.append({'source_kind':row.source_kind,'source_id':stored['source_id'],'context_account_id':row.account_id,
                            'context_name':db.get(m.RealAccount,row.account_id).name,'wallet_link_id':target.link_id,
                            'context_verified':(row.account_id,'0x38',proof_digest)in access._wallet_verified,
                            'observation_id':row.id,'order_checked':orders.order_record(db,orders.identifier(row.id))is not None,
                            'booked':book is not None,'accepted_id':link[0].acceptance_id if link else None,
                            'occurred_at':stored['proof']['receipt']['block_at']})
                    if len(sources)>limits.max_claims:raise ProjectionError('Demasiados recibos para esta revisión')
                    return {'root_account_id':owner.id,'wallets':descriptors,'sources':sources,'moves_funds':False,'trading_authority':False}
        except(PortfolioError,VaultError,ValueError)as exc:
            with access._lock:fail(exc,token)

    @router.post('/options/{kind}/{source_id}',dependencies=[Depends(authorized)])
    def options(account_id:UUID,kind:str,source_id:UUID,body:Contexts,response:Response,
            token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            if kind not in('WALLET_SWAP','OPERATOR'):raise ProjectionError('Fuente externa no compatible')
            with access._lock:
                capture(token,account_id,body)
                generation=access.vault.session_generation
                with access.vault.session()as db:
                    inputs=capture_external_effects(db,kind,str(source_id),context_account_id=str(account_id),owner_address=body.address,
                        wallets=tuple(WalletParticipant(item.context_account_id,item.address)for item in body.wallets))
            proof=json.loads(inputs.observation_json);at=datetime.fromisoformat(proof['receipt']['block_at'])
            events=[]
            for event in inputs.events:
                if event.id in inputs.unavailable_event_ids or abs(event.occurred_at-at)>timedelta(minutes=2):continue
                item=event.input
                events.append({'id':event.id,'account_id':event.account_id,'kind':item.kind,'quote_currency':item.quote_currency,
                    'asset':item.asset,'quantity':format(item.quantity,'f')if item.quantity is not None else None,
                    'amount':format(item.amount,'f')if item.amount is not None else None,'occurred_at':event.occurred_at.isoformat()})
            if len(events)>2000:raise ProjectionError('Demasiados asientos próximos al recibo; delimita el historial')
            # Recheck revocation after detaching; no address or identity salt is returned.
            with access._lock:
                capture(token,account_id,body)
                if generation!=access.vault.session_generation:raise VaultError('The real vault session changed')
            return {'observation_id':inputs.observation_id,'root_account_id':inputs.root_account_id,'state_digest':inputs.state_digest,
                'observation':proof,'accounts':[{'id':key,'name':name}for key,name in inputs.account_names],
                'bindings':[{'address_ref':item.address_ref,'account_id':item.account_id,'role':item.role}for item in inputs.bindings],
                'events':events,'moves_funds':False,'trading_authority':False}
        except(PortfolioError,VaultError,ValueError)as exc:
            with access._lock:fail(exc,token)
    return router
