"""Activación MAINNET desde una revisión exacta y una conexión local ya validada."""
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated,Literal
from uuid import uuid4
from fastapi import APIRouter,Body,Depends,Header,HTTPException,Query,Response
from pydantic import Field,StrictInt,StrictStr,StrictBool
from app.portfolio.grvt_mainnet_api import Action,Send,Sub
from app.portfolio.grvt_bracket_api import Amount,_body,_wire
from app.live_execution.grvt_bot_source import bot_source
from app.live_execution.grvt_mainnet_bot_entry import MainnetBotPolicy,_policy,_entry_draft
from app.live_execution.grvt_mainnet_bot_service import GrvtMainnetBotService
from app.live_execution.grvt_mainnet_transport import fetch_mainnet_instrument,TICKER_URL,_result
from app.live_execution.grvt_mainnet_bracket import unsigned_mainnet_bracket
from app.live_execution.grvt_mainnet_journal import encode
from app.derivatives.paper_store import PaperPerpStore
from app.lab.store import LabStore
from app.research.grvt_binding import check_grvt_research,grvt_research_options

class BotPreview(Action):
    session_id:Annotated[StrictStr,Field(pattern=r'^[0-9a-f-]{36}$')]
    research_validation_id:Annotated[StrictStr,Field(pattern=r'^[0-9a-f-]{36}$')]
    entry_cap:Amount
    exit_cap:Amount
    total_entry_cap:Amount
    loss_cap:Amount
    max_entries:Annotated[StrictInt,Field(ge=1,le=20)]=1
    wait_seconds:Annotated[StrictInt,Field(ge=30,le=86400)]=3600
    exit_seconds:Annotated[StrictInt,Field(ge=120,le=86400)]=3600
    recovery_seconds:Annotated[StrictInt,Field(ge=30,le=300)]=180
    max_attempts:Annotated[StrictInt,Field(ge=1,le=3)]=3
    max_spread_bps:Annotated[StrictInt,Field(ge=0,le=100)]=25
    max_slippage_bps:Annotated[StrictInt,Field(ge=0,le=100)]=25
    keep_operating:StrictBool=False

class BotStart(Send):
    confirmation:Literal['ACTIVAR_BOT_GRVT_MAINNET']

class BotStop(Action):
    confirmation:Literal['DETENER_BOT_GRVT_MAINNET']


def create_mainnet_bot_router(access,session,*,authorized,authorized_passive,lab_path,paper_path,poll_seconds=2):
    router=APIRouter(prefix='/bots',tags=['grvt-mainnet-bot'])
    bots=GrvtMainnetBotService(access.vault,client_factory=session.factory,public_post=session.public,clock_ns=session.clock,
        waiter=session.waiter,poll_seconds=poll_seconds,on_inactive=access._schedule_operational_cleanup)
    access._grvt_mainnet_bots=bots;session.external_status=bots.sub_status
    def stores():
        if lab_path is None or paper_path is None:raise HTTPException(status_code=503,detail='LAB y GRVT PAPER no están configurados')
        return PaperPerpStore(paper_path,allow_create=False),LabStore(lab_path,allow_create=False)
    def failed():return HTTPException(status_code=409,detail='Bot MAINNET sin confirmar; consulta su registro y revisa la autorización',headers={'Cache-Control':'no-store'})
    def generation(token):access.require(token,touch=False);return access._vault_generation

    @router.get('/capabilities',dependencies=[Depends(authorized_passive)])
    def capabilities(response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';generation(token)
        return {'network':'MAINNET','supervised_perpetual':True,'execution_authority':'NONE'}

    @router.get('/sources',dependencies=[Depends(authorized_passive)])
    def sources(response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';generation(token);paper,lab=stores()
        try:
            with paper.connect() as db:ids=[row[0] for row in db.execute("SELECT id FROM paper_signal_sessions WHERE status='ACTIVE' AND execution_mode='AUTO_PAPER' ORDER BY started_at_ns DESC LIMIT 100")]
            items=[]
            for identifier in ids:
                try:source=bot_source(paper,lab,identifier)
                except Exception:continue
                options,paper_ready=grvt_research_options(paper,lab,source)
                items.append({key:source[key] for key in ('session_id','symbol','timeframe','version_id')}|
                    {'direction':source['definition']['direction'],'paper_ready':paper_ready,'research_options':options,
                     'ready':paper_ready and bool(options)})
            generation(token);return {'network':'MAINNET','authorization':False,'items':items}
        except HTTPException:raise
        except Exception:raise failed() from None

    @router.get('',dependencies=[Depends(authorized_passive)])
    def history(response:Response,sub_account_id:Sub=Query(),offset:int=Query(default=0,ge=0,le=1_000_000),
            token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';current=generation(token)
        try:return _wire(bots.history(sub_account_id,generation=current,offset=offset))
        except Exception:raise failed() from None

    @router.post('/preview',dependencies=[Depends(authorized)])
    def preview(response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(BotPreview,body);current=generation(token);paper,lab=stores()
        try:
            if bots.active_sub(data.sub_account_id):raise ValueError('bot active')
            _,checked,_=session.capture(token,data.sub_account_id,tuple(data.permission_revision),current,sign=True)
            from app.live_execution.grvt_mainnet_journal import require_new_mainnet_bracket
            with access.vault.guard_session_generation(current),access.vault.session() as db:
                require_new_mainnet_bracket(db,data.sub_account_id,str(2**63+557))
            raw=data.model_dump(exclude={'permission_revision','session_id','keep_operating','research_validation_id'})
            for key in ('entry_cap','exit_cap','total_entry_cap','loss_cap'):raw[key]=Decimal(raw[key])
            policy=MainnetBotPolicy(**raw);source=bot_source(paper,lab,data.session_id)
            research=check_grvt_research(paper,lab,source,data.research_validation_id,now_ns=session.clock())
            instrument=fetch_mainnet_instrument(source['instrument'],http_post=session.public);checked()
            quote=_result(session.public(TICKER_URL,{'instrument':source['instrument']}))['result'];checked()
            draft=_entry_draft(policy,source,instrument,quote,session.clock())
            unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=str(2**63+556),nonces=(1,2,3),issued_at_ns=session.clock())
            research.assert_current(session.clock());checked()
            proposal={'run_id':str(uuid4()),'session_id':data.session_id,'source_digest':encode(source)[1],
                'source':{key:source[key] for key in ('symbol','instrument','timeframe','version_id','dataset_hash')}|
                    {'direction':source['definition']['direction']},'policy':asdict(policy),'keep_operating':data.keep_operating,
                'bot_control_revision':list(bots.ticket(data.sub_account_id)),
                'research_binding':research.binding.exported()}
            return _wire(session.seal_researched_bot_review(token,data.sub_account_id,tuple(data.permission_revision),current,proposal))
        except Exception:raise failed() from None

    @router.post('/start',dependencies=[Depends(authorized)],status_code=202)
    def start(response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(BotStart,body);current=generation(token);paper,lab=stores()
        try:
            credentials,checked=session.consume_researched_bot_review(token,data.review,data.review_mac,tuple(data.permission_revision),current)
            review=data.review;source=bot_source(paper,lab,review['session_id']);checked()
            if encode(source)[1]!=review['source_digest']:raise ValueError('source changed')
            research=check_grvt_research(paper,lab,source,review['research_binding']['research']['artifact_id'],
                now_ns=session.clock(),expected_binding=review['research_binding']);checked()
            policy=_policy(review['policy']);key,private,signer=credentials
            result=bots.start(paper,lab,review['session_id'],policy,api_key=key,private_key=private,expected_signer=signer,
                expected_ticket=tuple(review['bot_control_revision']),keep_operating=review['keep_operating'],expected_generation=current,session_checked=checked,requested_run_id=review['run_id'],
                research_check=research)
            return _wire(result)
        except Exception:raise failed() from None

    @router.post('/stop',dependencies=[Depends(authorized)])
    def stop(response:Response,body:object=Body(),token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(BotStop,body);generation(token)
        bots.stop_subaccount(data.sub_account_id)
        try:
            session.stop(data.sub_account_id);bots.retire_interrupted(data.sub_account_id)
            return {'network':'MAINNET','stop_requested':True,'control_revision':list(bots.ticket(data.sub_account_id))}
        except Exception:raise failed() from None

    from app.portfolio.grvt_perp_bot_api import create_perp_bot_router
    router.include_router(create_perp_bot_router(access,session,bots,authorized=authorized,
        authorized_passive=authorized_passive,lab_path=lab_path,paper_path=paper_path))
    from app.portfolio.grvt_perp_limit_api import create_limit_router
    router.include_router(create_limit_router(access,session,bots,authorized=authorized,
        authorized_passive=authorized_passive,lab_path=lab_path,paper_path=paper_path))

    @router.get('/{run_id}',dependencies=[Depends(authorized_passive)])
    def status(run_id:str,response:Response,token:str|None=Header(default=None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';current=generation(token)
        try:
            with access.vault.try_guard_session_generation(current) as available:
                if not available:return _wire({'network':'MAINNET','pending':True,'item':bots.cached_snapshot(run_id)})
                return _wire({'network':'MAINNET','pending':False,'item':bots.snapshot(run_id)})
        except Exception:raise failed() from None
    return router
