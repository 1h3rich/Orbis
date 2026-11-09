"""Explicit protected activation of bounded LONG and DCA DEX workers.

The owner has already funded gas and approved the executor. This router never
funds accounts, accepts keys or signs arbitrary client transactions.
"""
from app.live_execution.dex_versions import SUCCESSOR_BINDINGS
from typing import Literal
from uuid import UUID
import hashlib,hmac

from fastapi import APIRouter,Depends,Header,HTTPException,Query,Response
from pydantic import BaseModel,ConfigDict,Field

from app.lab.store import content_hash
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_market import market_overlap,warmup_market
from app.live_execution.dex_bot_worker import DexExecutionOptions
from app.live_execution.dex_permission import MAX_PERMISSION_SECONDS,LEGACY_PERMISSION_SECONDS
from app.live_execution.dex_gas_funding import FundingState, operator_gas_coverage, read_funding_state
from app.live_execution.dex_operator_transport import BnbOperatorReadClient,collect_operator_receipt
from app.live_execution.evm_transaction import word
from app.portfolio import dex_bot_bindings as bindings,dex_bot_journal as bots,dex_operator_journal as operator
from app.portfolio.dex_gas_funding import _live as live_funding
from app.portfolio.dex_deployments import deployment_record
from app.portfolio.models import RealAccount,RealDexBotRun
from app.portfolio.wallet_funding import exact_units
from app.portfolio.wallet_proof import canonical_wallet_address


class ActivationReview(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    binding_id:UUID=Field(strict=False)
    draft_id:UUID=Field(strict=False)
    address:str=Field(min_length=42,max_length=42)
    gas:int=Field(default=400000,ge=21000,le=2000000)
    gas_price:str=Field(pattern=r'^[1-9][0-9]{0,12}$')
    max_gas_bnb:str=Field(min_length=1,max_length=100)
    slippage_bps:int=Field(default=25,ge=1,le=500)

    def execution_options(self) -> DexExecutionOptions:
        options = DexExecutionOptions(gas=self.gas, gas_price=int(self.gas_price),
                                      max_gas_wei=exact_units(self.max_gas_bnb,18), slippage_bps=self.slippage_bps)
        options.validate()
        return options


class Activate(ActivationReview):
    request_id:UUID=Field(strict=False)
    research_validation_id:UUID|None=Field(default=None,strict=False)
    ttl_seconds:int=Field(default=900,ge=1,le=MAX_PERMISSION_SECONDS)
    permission_mode:Literal['SESSION','OPERATIONAL']=Field(default='SESSION')
    dca_activation_window_seconds:int|None=Field(default=None,ge=60,le=86400)
    confirmation:Literal['ACTIVAR_BOT_DEX']


class ResearchExport(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    revision:str=Field(pattern=r'^[0-9a-f]{64}$')
    confirmation:Literal['EXPORTAR_JSONL_UNISWAP_REAL']


class Stop(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    confirmation:Literal['DETENER_BOT_DEX']


class Rearm(Activate):
    permission_mode:Literal['SESSION','OPERATIONAL']=Field(...)
    confirmation:Literal['REARMAR_BOT_DEX']


class Observe(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    address:str=Field(min_length=42,max_length=42)
    tx_hash:str=Field(pattern=r'^0x[0-9a-fA-F]{64}$')


def require_policy_gas_price(snapshot: FundingState, options: DexExecutionOptions) -> None:
    policy = snapshot.executor.policy_limits
    if policy is None or options.gas_price > policy.max_gas_price:
        raise DexExecutorError('El precio de gas supera el máximo de la política del ejecutor')


def create_dex_bot_run_router(access,*,authorized,authorized_passive,rpc_factory=None,sources=None)->APIRouter:
    router=APIRouter(prefix='/accounts/{account_id}/dex-bots/runs',tags=['dex-bot-execution'])
    factory=rpc_factory or BnbOperatorReadClient
    supervisor=access._dex_bots

    def verified(token,account_id,wallet=None,*,passive=False):
        access.require(token,touch=not passive)
        with access.vault.session() as db:
            if db.get(RealAccount,str(account_id)) is None:raise DexExecutorError('Cuenta REAL no encontrada')
        if wallet is not None:
            digest=hmac.new(access._wallet_proof_key,wallet.encode('ascii'),hashlib.sha256).hexdigest()
            if (str(account_id),'0x38',digest) not in access._wallet_verified:
                raise DexExecutorError('Verifica esta wallet en BNB Chain y esta sesión REAL')

    def failure(exc):return HTTPException(status_code=409,detail=str(exc),headers={'Cache-Control':'no-store'})

    def local(account_id,token,response,action,*,passive=False):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                verified(token,account_id,passive=passive)
                return action()
        except ValueError as exc:raise failure(exc) from None

    @router.get('',dependencies=[Depends(authorized_passive)])
    def history(account_id:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return local(account_id,token,response,lambda:supervisor.list_runs(account_id=str(account_id)),passive=True)

    @router.post('/activation-review',dependencies=[Depends(authorized_passive)])
    def activation_review(account_id:UUID,body:ActivationReview,response:Response,
                          token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')) -> dict:
        response.headers['Cache-Control']='no-store'
        try:
            wallet, account = canonical_wallet_address(body.address), str(account_id)
            binding_id, draft_id = str(body.binding_id), str(body.draft_id)
            options = body.execution_options()
            with access._lock:
                verified(token,account,wallet,passive=True)
                generation = access.vault.session_generation
                access._dex_operator_drafts.require_verified(identifier=draft_id,account_id=account,
                    wallet_address=wallet,now=supervisor.now())
                with access.vault.session() as db:
                    parent,binding,_ = bindings.binding_record(db,binding_id)
                    review = binding['origin']['review']
                    if (parent.account_id!=account or binding['wallet_digest']!=operator._identity(db,'wallet',wallet)
                            or parent.status!='READY' or review['source_kind'] not in ('STRATEGY','DCA')):
                        raise DexExecutorError('El vínculo no corresponde a esta cuenta y wallet')
                    deployment = deployment_record(db,parent.deployment_id)[1]
            with factory() as rpc:
                snapshot = read_funding_state(rpc,wallet,int(deployment['nonce']),deployment['fee'],now=supervisor.now())
            with access._lock:
                verified(token,account,wallet,passive=True)
                if access.vault.session_generation!=generation:
                    raise DexExecutorError('La sesión REAL cambió durante la lectura BNB')
                with access.vault.session() as db:
                    db.connection().exec_driver_sql('BEGIN')
                    _,_,budget,_ = live_funding(db,binding_id=binding_id,account_id=account,wallet=wallet,
                        drafts=access._dex_operator_drafts,draft_id=draft_id,snapshot=snapshot,now=supervisor.now())
            require_policy_gas_price(snapshot,options)
            coverage = operator_gas_coverage(snapshot,budget,options.gas*options.gas_price)
            state = snapshot.executor
            return {'binding_id':binding_id,'draft_id':draft_id,'block_number':str(state.block_number),
                'gas':{'order_wei':str(coverage.order_wei),'stop_reserve_wei':str(coverage.stop_reserve_wei),
                       'available_wei':str(coverage.available_wei),'required_wei':str(coverage.required_wei),
                       'shortfall_wei':str(max(0,coverage.required_wei-coverage.available_wei))},
                'wallet':{'available_usdt_units':str(min(state.balance_usdt,state.allowance_usdt)),
                          'reference_entry_units':review['amounts']['entry_units'],
                          'available_wbnb_units':str(min(state.balance_wbnb,state.allowance_wbnb)),
                          'bot_position_wbnb_units':str(state.owned_wbnb)},
                'automatic_execution':False}
        except ValueError as exc:raise failure(exc) from None

    @router.get('/{identifier}',dependencies=[Depends(authorized_passive)])
    def detail(account_id:UUID,identifier:UUID,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return local(account_id,token,response,lambda:supervisor.view(str(identifier),account_id=str(account_id)),passive=True)

    @router.get('/{identifier}/executions-page',dependencies=[Depends(authorized_passive)])
    def executions(account_id:UUID,identifier:UUID,response:Response,limit:int=Query(default=500,ge=1,le=500),
                   offset:int=Query(default=0,ge=0,le=100000),revision:str|None=Query(default=None,pattern=r'^[0-9a-f]{64}$'),
                   token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.dex_executions import read_executions,executions_page
        def read():
            with access.vault.session() as db:
                # One read transaction for all pages' revision witnesses. The
                # passive route never renews a trading permission or signs.
                db.connection().exec_driver_sql('BEGIN')
                return executions_page(read_executions(db,account_id=str(account_id),run_id=str(identifier)),
                                       limit=limit,offset=offset,revision=revision)
        return local(account_id,token,response,read,passive=True)

    @router.post('/{identifier}/research-export',dependencies=[Depends(authorized_passive)])
    def research_export(account_id:UUID,identifier:UUID,body:ResearchExport,response:Response,
                        token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        from app.live_execution.dex_research_export import export_dex_bot_swaps

        def read():
            with access.vault.session() as db:
                db.connection().exec_driver_sql('BEGIN')
                result=export_dex_bot_swaps(db,account_id=str(account_id),run_id=str(identifier),
                                            revision=body.revision)
            access.require(token,touch=False)
            return result
        return local(account_id,token,response,read,passive=True)

    @router.post('/{identifier}/stop',dependencies=[Depends(authorized)])
    def stop(account_id:UUID,identifier:UUID,body:Stop,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            with access._lock:
                access.require(token)
                # RAM ownership is checked by the supervisor before touching
                # SQL. A damaged archive cannot prevent key revocation.
                return supervisor.stop(str(identifier),account_id=str(account_id))
        except ValueError as exc:raise failure(exc) from None

    @router.post('/{identifier}/intents/{intent_id}/observe',dependencies=[Depends(authorized)])
    def observe(account_id:UUID,identifier:UUID,intent_id:UUID,body:Observe,response:Response,
                token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store'
        try:
            wallet=canonical_wallet_address(body.address);tx_hash=word(body.tx_hash)
            with access._lock:
                verified(token,account_id,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:
                    run,terms,_,_=bots.run_record(db,str(identifier));item,recipe,state=operator.intent_record(db,str(intent_id))
                    if (run.account_id!=str(account_id) or item.account_id!=str(account_id) or item.grant_id!=run.grant_id
                            or terms['wallet_digest']!=operator._identity(db,'wallet',wallet)
                            or state['tx_ref']!=operator._identity(db,'tx',tx_hash)):
                        raise DexExecutorError('El recibo no corresponde al envío original de este bot')
                    if item.status in {'FINALIZED','REVERTED'}:return supervisor.view(str(identifier),account_id=str(account_id))
                    nonce=int(terms['creation_nonce'])
            with factory() as rpc:proof=collect_operator_receipt(rpc,wallet,nonce,tx_hash)
            if proof is None:raise DexExecutorError('El envío original todavía no tiene recibo')
            with access._lock:
                verified(token,account_id,wallet)
                if access.vault.session_generation!=generation:raise DexExecutorError('La sesión REAL cambió durante la consulta')
                with access.vault.session() as db:
                    operator.check_intent(db,str(intent_id),account_id=str(account_id),evidence=proof,now=supervisor.now())
                return supervisor.view(str(identifier),account_id=str(account_id))
        except ValueError as exc:raise failure(exc) from None

    @router.post('',dependencies=[Depends(authorized)])
    def activate(account_id:UUID,body:Activate,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return execute(account_id,body,response,token)

    @router.post('/{previous_id}/rearm',dependencies=[Depends(authorized)])
    def rearm(account_id:UUID,previous_id:UUID,body:Rearm,response:Response,
              token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        return execute(account_id,body,response,token,previous_run_id=str(previous_id))

    def execute(account_id,body,response,token,previous_run_id=None):
        response.headers['Cache-Control']='no-store';revision=None;created=False;successful=False
        identifier,account=str(body.request_id),str(account_id)
        try:
            wallet=canonical_wallet_address(body.address)
            options=body.execution_options()
            explicit_mode='permission_mode' in body.model_fields_set
            if not explicit_mode and body.ttl_seconds>LEGACY_PERMISSION_SECONDS:
                raise DexExecutorError('Elige expresamente el modo de permiso para superar 15 minutos')
            excluded={'address','request_id','confirmation'}
            if not explicit_mode:excluded.add('permission_mode')
            if 'dca_activation_window_seconds' not in body.model_fields_set:excluded.add('dca_activation_window_seconds')
            if 'research_validation_id' not in body.model_fields_set:excluded.add('research_validation_id')
            parameters=body.model_dump(mode='json',exclude=excluded)
            if previous_run_id is not None:parameters['resume_from']=previous_run_id
            digest=content_hash(parameters)
            with access._lock:
                verified(token,account,wallet);generation=access.vault.session_generation
                with access.vault.session() as db:
                    if db.get(RealDexBotRun,identifier) is not None:
                        row,terms,_,_=bots.run_record(db,identifier)
                        if (row.account_id!=account or row.binding_id!=str(body.binding_id)
                                or terms['wallet_digest']!=operator._identity(db,'wallet',wallet) or terms.get('request_digest')!=digest
                                or (terms.get('resume') or {}).get('previous_run_id')!=previous_run_id):
                            raise DexExecutorError('La solicitud de activación ya tiene otros datos')
                        return supervisor.view(identifier,account_id=account)
            with supervisor.preparing(identifier,account_id=account) as cancelled:
                def active():
                    if cancelled.is_set():raise DexExecutorError('Activación cancelada')
                    if access.vault.session_generation!=generation:raise DexExecutorError('La sesión REAL cambió durante la activación')
                with access._lock:
                    verified(token,account,wallet);active()
                    access._dex_operator_drafts.require_verified(identifier=str(body.draft_id),account_id=account,wallet_address=wallet,now=supervisor.now())
                    with access.vault.session() as db:
                        parent,binding,_=bindings.binding_record(db,str(body.binding_id))
                        if (parent.account_id!=account or binding['wallet_digest']!=operator._identity(db,'wallet',wallet)
                                or parent.status!='READY' or binding['origin']['review']['source_kind'] not in ('STRATEGY','DCA')):
                            raise DexExecutorError('El vínculo no está preparado para activar')
                        dca=binding['origin']['review']['source_kind']=='DCA'
                        if dca and not explicit_mode:raise DexExecutorError('Elige expresamente el modo de permiso DCA')
                        if not dca and body.dca_activation_window_seconds is not None:
                            raise DexExecutorError('La ventana de activación corresponde sólo a DCA')
                        deployment=deployment_record(db,parent.deployment_id)[1]
                        seed=binding['origin']['seed'];timeframe=binding['origin']['review']['source']['timeframe']
                        from app.portfolio.dex_research import binding_qualification,authorization_qualification
                        qualification=binding_qualification(db,parent.id)
                        validation_id=str(body.research_validation_id) if body.research_validation_id else (
                            qualification['evidence']['research']['artifact_id'] if qualification else None)
                        if previous_run_id is not None:
                            previous=bots.run_record(db,previous_run_id)
                            cross_policy=False
                            if binding['schema'] in SUCCESSOR_BINDINGS:
                                from app.portfolio.dex_controls import control_record
                                renewal=control_record(db,binding['rollover_id'])[1]['bot_rollover']
                                cross_policy=(previous[0].id==renewal['previous_run_id'] and previous[0].binding_id==renewal['previous_binding_id'])
                            if ((previous[0].binding_id!=parent.id and not cross_policy)
                                    or previous[0].account_id!=account or previous[0].status!='STOPPED'):
                                raise DexExecutorError('Detén el último permiso del mismo bot antes de rearmar')
                            if body.research_validation_id is None:
                                previous_research=authorization_qualification(db,previous_run_id)
                                if previous_research is not None:
                                    validation_id=previous_research['evidence']['research']['artifact_id']
                            if dca and previous[1]['dca']['limits']['activation_window_seconds']!=(body.dca_activation_window_seconds if body.dca_activation_window_seconds is not None else 900):
                                raise DexExecutorError('El rearme DCA conserva su ventana de activación original')
                            bots._settled(db,parent.grant_id,include_resolutions=True,as_of=supervisor.now(),
                                include_external=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=78)
                            seed=[bar.json() for bar in previous[3]['bars']]
                        if validation_id is None:raise DexExecutorError('Selecciona una reserva fuera de muestra para activar o rearmar')
                    supervisor.require_idle()
                # Public market and chain reads hold neither REAL nor vault locks.
                if sources is None:raise DexExecutorError('El almacén de investigación no está configurado')
                review=binding['origin']['review']
                checked=sources.check_research(review['source_kind'],review['source'],review['candle_t'],validation_id)
                active()
                seed=warmup_market(seed,supervisor.market_factory(),timeframe=timeframe,clock=supervisor.now,
                    require_active=active,overlap_count=market_overlap(review['definition']))
                active()
                with factory() as rpc:
                    snapshot=read_funding_state(rpc,wallet,int(deployment['nonce']),deployment['fee'],now=supervisor.now())
                with access._lock:
                    verified(token,account,wallet);active()
                    supervisor.require_idle()
                    checked.assert_current()
                    require_policy_gas_price(snapshot,options)
                    access._dex_connection.connect(request_id=identifier,account_id=account,binding_id=str(body.binding_id),
                        draft_id=str(body.draft_id),wallet_address=wallet,snapshot=snapshot,ttl_seconds=body.ttl_seconds,
                        now=supervisor.now(),order_gas_wei=options.gas*options.gas_price)
                    revision=access._dex_operator.permission_revision;active()
                    with access.vault.session() as db:
                        _,claim=bots.start_run(db,identifier=identifier,account_id=account,binding_id=str(body.binding_id),wallet_address=wallet,
                            connection=access._dex_connection,snapshot=snapshot.executor,seed=seed,now=supervisor.now(),request_digest=digest,
                            permission_mode=body.permission_mode if explicit_mode else None,requested_seconds=body.ttl_seconds if explicit_mode else None,
                            previous_run_id=previous_run_id,
                            activation_window_seconds=body.dca_activation_window_seconds if body.dca_activation_window_seconds is not None else 900,
                            research_check=checked)
                        from app.portfolio.dex_research import authorization_digest
                        research_digest=authorization_digest(db,identifier)
                        created=True
                    active()
                    if claim is None:raise DexExecutorError('La solicitud guardada no devuelve permiso para reiniciar')
                    result=supervisor.launch(identifier,account_id=account,claim=claim,wallet_address=wallet,options=options,cancelled=cancelled,
                        research_digest=research_digest)
                    successful=True;return result
        except ValueError as exc:raise failure(exc) from None
        finally:
            if not successful:
                if revision is not None:access._dex_connection.revoke_revision(revision)
                if created:
                    try:supervisor.stop(identifier,account_id=account)
                    except Exception:pass

    return router
