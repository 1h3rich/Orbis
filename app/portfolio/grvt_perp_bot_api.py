"""Separate, one-use activation of a supervised perpetual MAINNET bot."""
from dataclasses import asdict
from decimal import Decimal
import json
from typing import Annotated,Literal
from uuid import UUID,uuid4

from fastapi import APIRouter,Body,Depends,Header,HTTPException,Query,Response
from pydantic import Field,StrictBool,StrictInt,StrictStr,model_validator
from sqlalchemy import select

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.perp_observed_archive import ObservedArchive
from app.derivatives.perp_observed_archive_format import OriginSnapshot
from app.lab.access import lab_access
from app.lab.store import LabStore
from app.live_execution.grvt_mainnet_bot_entry import MainnetBotPolicy,_policy
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_journal import require_new_mainnet_bracket
from app.live_execution.grvt_perp_bot_origin import verified_perp_bot_origin
from app.live_execution.grvt_perp_plan import PerpEntryTerms,PerpRealBudget
from app.live_execution.grvt_perp_preflight import collect_perp_preflight
from app.live_execution.grvt_perp_supervision import SUPERVISION_ENGINE
from app.portfolio.grvt_mainnet_api import Action,Send
from app.portfolio.grvt_bracket_api import Amount,_body,_wire
from app.portfolio.models import GrvtMainnetBotRun


class PerpBotPreview(Action):
    session_id: Annotated[StrictStr,Field(pattern=r'^[0-9a-f-]{36}$')]
    collateral_limit: Amount
    entry_cash_limit: Amount
    entry_notional_limit: Amount
    exit_notional_limit: Amount
    fee_rate_ceiling: Amount
    total_entry_cap: Amount
    loss_cap: Amount
    max_entries: Annotated[StrictInt,Field(ge=1,le=20)]=1
    wait_seconds: Annotated[StrictInt,Field(ge=30,le=86400)]=3600
    exit_seconds: Annotated[StrictInt,Field(ge=120,le=86400)]=3600
    recovery_seconds: Annotated[StrictInt,Field(ge=30,le=300)]=180
    max_attempts: Annotated[StrictInt,Field(ge=1,le=3)]=3
    max_spread_bps: Annotated[StrictInt,Field(ge=0,le=100)]=25
    max_slippage_bps: Annotated[StrictInt,Field(ge=0,le=100)]=25
    keep_operating: StrictBool=False
    dedicated_account_confirmed: StrictBool

    @model_validator(mode='after')
    def dedicated(self):
        if self.dedicated_account_confirmed is not True:
            raise ValueError('Confirma la subcuenta dedicada y todo su colateral')
        return self


class PerpBotStart(Send):
    confirmation: Literal['ACTIVAR_BOT_PERPETUO_GRVT_MAINNET']


def create_perp_bot_router(access,session,bots,*,authorized,authorized_passive,lab_path,paper_path):
    router=APIRouter(prefix='/perp',tags=['grvt-perp-mainnet-bot'])
    def generation(token):
        access.require(token,touch=False)
        return access._vault_generation
    def stores():
        if lab_path is None or paper_path is None:
            raise HTTPException(503,'LAB y PAPER no están configurados')
        if lab_access.get(lab_path) is None or paper_perp_access.get(paper_path) is None:
            raise HTTPException(423,'Desbloquea LAB y GRVT PAPER en Ajustes')
        return LabStore(lab_path,allow_create=False),ObservedArchive(PaperPerpStore(paper_path,allow_create=False))
    def failed():
        return HTTPException(409,'Bot perpetuo sin confirmar; consulta su ID antes de otra activación',
            headers={'Cache-Control':'no-store'})

    @router.get('/sources',dependencies=[Depends(authorized_passive)])
    def sources(response: Response,offset: int=Query(0,ge=0,le=1_000_000),
                session_id: UUID|None=None,
                token: str|None=Header(None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';generation(token);_,archive=stores()
        try:
            with archive.paper.connect() as db:
                if session_id:
                    offset = 0
                    session_filter = """AND s.id=? AND s.status IN ('RUNNING','CLOSE_ONLY','STOPPED')
                        AND EXISTS (SELECT 1 FROM paper_observed_trades t WHERE t.session_id=s.id)
                        AND NOT EXISTS (SELECT 1 FROM paper_observed_gap_closes g WHERE g.session_id=s.id)"""
                    parameters = (str(session_id),offset)
                else:
                    session_filter,parameters = '',(offset,)
                rows=db.execute(f'''SELECT id,created_at,status,origin_json,
                    json_extract(seed_json,'$.definition.direction') AS direction,
                    (SELECT count(*) FROM paper_observed_trades t WHERE t.session_id=s.id) AS closed_trades
                    FROM paper_observed_sessions s
                    WHERE json_extract(seed_json,'$.schema')='orbis.paper.perp.observed.seed.v1'
                    {session_filter}
                    ORDER BY created_at DESC,id DESC LIMIT 26 OFFSET ?''',parameters).fetchall()
            items=[]
            for row in rows[:25]:
                source=OriginSnapshot.model_validate(json.loads(row['origin_json'])).reference
                if row['direction'] not in ('LONG','SHORT'):raise ValueError('direction')
                items.append({'session_id':row['id'],'symbol':source.symbol,'timeframe':source.timeframe,
                    'direction':row['direction'],'margin_mode':source.profile.margin_mode,'leverage':source.profile.leverage,
                    'closed_trades':row['closed_trades'],'status':row['status'],'created_at':row['created_at'],
                    'verification':'ON_PREVIEW_AND_START'})
            generation(token)
            return {'network':'MAINNET','execution_authority':'NONE','items':items,'has_more':len(rows)>25,'offset':offset}
        except HTTPException:raise
        except Exception:raise failed() from None

    @router.post('/preview',dependencies=[Depends(authorized)])
    def preview(response: Response,body: object=Body(),token: str|None=Header(None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(PerpBotPreview,body);current=generation(token)
        lab,archive=stores()
        try:
            sub=data.sub_account_id
            if bots.active_sub(sub):raise ValueError('bot active')
            bot_revision=bots.ticket(sub)
            _,checked,factory=session.capture(token,sub,tuple(data.permission_revision),current,sign=True)
            with access.vault.guard_session_generation(current),access.vault.session() as db:
                require_new_mainnet_bracket(db,sub,str(2**63+560))
                if db.scalar(select(GrvtMainnetBotRun.id).where(GrvtMainnetBotRun.active_slot==sub)) is not None:
                    raise ValueError('interrupted grant')
            origin=verified_perp_bot_origin(lab,archive,data.session_id,sub,factory(),
                clock_ns=session.clock,require_permission=checked)
            source=origin.source()
            budget=PerpRealBudget(schema_name='orbis.grvt.perp.real.budget.v1',sub_account_id=sub,
                **data.model_dump(include={'collateral_limit','entry_cash_limit','entry_notional_limit','exit_notional_limit',
                    'fee_rate_ceiling','exit_seconds','max_spread_bps','max_slippage_bps'}),
                collateral_scope='WHOLE_DEDICATED_USDT_SUBACCOUNT')
            terms=PerpEntryTerms(schema_name='orbis.grvt.perp.entry.terms.v1',instrument=source.instrument,
                timeframe=source.timeframe,profile=source.research.profile,definition=source.definition,budget=budget,
                execution_mapping='BOUNDED_FOK_WITH_NATIVE_LAST_BRACKETS',
                fee_assumption='USER_CEILING_NOT_VERIFIED_EXCHANGE_FEE',
                reserve_assumption='SHARED_BOOK_ESTIMATE_NOT_EXCHANGE_APPROVAL',
                execution_scope='PROTECTED_ENTRY_ONLY_NOT_SIGNAL_OR_EXIT_COORDINATOR')
            policy=MainnetBotPolicy(sub,Decimal(budget.entry_notional_limit),Decimal(budget.exit_notional_limit),
                Decimal(data.total_entry_cap),Decimal(data.loss_cap),**data.model_dump(include={
                    'max_entries','wait_seconds','exit_seconds','recovery_seconds','max_attempts',
                    'max_spread_bps','max_slippage_bps'}))
            proof=collect_perp_preflight(factory(),terms,public_post=session.public,
                clock_ns=lambda:(checked() or session.clock()))
            with origin.hold_current():
                checked();bots._require_ticket(sub,bot_revision)
                proposal={'run_id':str(uuid4()),'source':_wire(source.payload()),'terms':terms.payload(),
                    'policy':asdict(policy),'keep_operating':data.keep_operating,'bot_control_revision':list(bot_revision),
                    'lab_origin':origin.origin.review_lab_origin(),
                    'supervision_engine':SUPERVISION_ENGINE,'sample_plan':proof['plan'],
                    'execution_scope':'SUPERVISED_FUTURE_SIGNALS_AND_OWN_POSITION_CLOSE',
                    'dedicated_account_confirmed':True,'execution_authority':'NONE'}
                return _wire(session.seal_perp_bot_review(token,sub,tuple(data.permission_revision),current,proposal,origin))
        except HTTPException:raise
        except Exception:raise failed() from None

    @router.post('/start',dependencies=[Depends(authorized)],status_code=202)
    def start(response: Response,body: object=Body(),token: str|None=Header(None,alias='X-Orbis-Vault-Token')):
        response.headers['Cache-Control']='no-store';data=_body(PerpBotStart,body);current=generation(token)
        # Check storage availability before consuming a usable review. The
        # capability itself checks its original epochs and bytes afterwards.
        stores()
        try:
            credentials,checked,origin=session.consume_perp_bot_review(token,data.review,data.review_mac,
                tuple(data.permission_revision),current)
            review=data.review
            if _wire(origin.source().payload())!=review['source'] or review['supervision_engine']!=SUPERVISION_ENGINE:
                raise ValueError('origin changed')
            def start_checked():
                checked()
                if not int(review['created_at_ns'])<=session.clock()<int(review['expires_at_ns']):
                    raise GrvtMainnetError('Revisión de activación vencida')
            key,private,signer=credentials
            result=bots.start_perp(origin,PerpEntryTerms.model_validate(review['terms']),_policy(review['policy']),
                api_key=key,private_key=private,expected_signer=signer,
                expected_ticket=tuple(review['bot_control_revision']),keep_operating=review['keep_operating'],
                expected_generation=current,session_checked=checked,start_checked=start_checked,
                requested_run_id=review['run_id'],confirmation=data.confirmation)
            return _wire(result)
        except HTTPException:raise
        except Exception:raise failed() from None
    return router
