"""Private review, activation and encrypted readback for GRVT LIMIT."""
import json
import secrets
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from sqlalchemy import func, select

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.perp_observed_archive import ObservedArchive
from app.derivatives.perp_observed_archive_format import OriginSnapshot
from app.lab.access import lab_access
from app.lab.store import LabStore
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_executions import executions_page
from app.live_execution.grvt_mainnet_journal import require_new_mainnet_bracket
from app.live_execution.grvt_perp_limit_executions import limit_executions_view
from app.live_execution.grvt_perp_limit_journal import PARTIAL_SCHEMA, abandon_unsent_limit, read_limit_attempt, verified_limit_attempt
from app.live_execution.grvt_perp_limit_origin import verified_limit_paper_origin, verified_partial_paper_origin
from app.live_execution.grvt_perp_limit_plan import PerpPartialLimitEntryTerms, read_limit_terms
from app.live_execution.grvt_perp_limit_worker import (
    resume_limit_worker, start_limit_worker, validate_limit_timing, validate_resume_reductions, verify_limit_resume,
)
from app.live_execution.grvt_perp_plan import PerpRealBudget
from app.live_execution.grvt_perp_preflight import collect_perp_market, verify_perp_market
from app.portfolio.grvt_bracket_api import Amount, _body, _wire
from app.portfolio.grvt_mainnet_api import Action, GrvtFillExport, Send, Sub, export_fill_archive
from app.portfolio.models import GrvtMainnetLimitAttempt


class LimitPreview(Action):
    session_id: Annotated[StrictStr,Field(pattern=r'^[0-9a-f-]{36}$')]
    collateral_limit: Amount
    entry_cash_limit: Amount
    entry_notional_limit: Amount
    exit_notional_limit: Amount
    fee_rate_ceiling: Amount
    exit_seconds: Annotated[StrictInt,Field(ge=120,le=780)] = 120
    max_spread_bps: Annotated[StrictInt,Field(ge=0,le=100)] = 25
    max_slippage_bps: Annotated[StrictInt,Field(ge=0,le=100)] = 25
    wait_seconds: Annotated[StrictInt,Field(ge=30,le=600)] = 60
    max_pending_seconds: Annotated[StrictInt,Field(ge=5,le=120)] = 30
    authorization_seconds: Annotated[StrictInt,Field(ge=185,le=840)] = 300
    max_reductions: Annotated[StrictInt,Field(ge=1,le=20)] = 2
    reserved_close_attempts: Annotated[StrictInt,Field(ge=1,le=19)] | None = None
    dedicated_account_confirmed: StrictBool


class LimitStart(Send):
    confirmation: Literal['ACTIVAR_BOT_LIMIT_GRVT_MAINNET','ACTIVAR_BOT_LIMIT_PARCIALES_GRVT_MAINNET']


class LimitResumePreview(Action):
    client_order_id: Annotated[StrictStr,Field(pattern=r'^[0-9]{19,20}$')]
    authorization_seconds: Annotated[StrictInt,Field(ge=60,le=600)] = 180
    max_reductions: Annotated[StrictInt,Field(ge=1,le=20)] = 3


class LimitResumeStart(Send):
    confirmation: Literal['RETOMAR_BOT_LIMIT_GRVT_MAINNET','RETOMAR_BOT_LIMIT_PARCIALES_GRVT_MAINNET']


class LimitAbandon(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    sub_account_id: Sub
    client_order_id: Annotated[StrictStr,Field(pattern=r'^[0-9]{19,20}$')]
    confirmation: Literal['LIBERAR_RESERVA_LIMIT_SIN_ENVIO']


def _attempt(saved):
    claim,events = saved['claim'],saved['events']
    terms = claim['preflight']['plan']['source']['terms']
    receipt = events['receipt']
    audit = events['exit_audits'][-1] if events['exit_audits'] else events['exit_prefix']
    partial = {'partial_limits':{'max_attempts':terms['max_attempts'],
                                 'reserved_close_attempts':terms['reserved_close_attempts']}} if claim['schema']==PARTIAL_SCHEMA else {}
    return {**partial,'client_order_id':claim['client_order_id'],'claimed_at_ns':claim['claimed_at_ns'],
            'instrument':terms['research']['symbol'],'direction':terms['definition']['direction'],
            'paper_session_id':terms['paper']['session_id'],'reserved':saved['slot_reserved'],
            'resumable':saved['slot_reserved'] and events['send_started'],
            'abandonable':saved['slot_reserved'] and not events['send_started'],
            'entry':({key:receipt[key] for key in ('status','phase','filled_size','entry_notional','policy_breaches')}
                     if receipt else None),
            'decision':events['supervision'],'cancel_requested':events['cancel_claimed'],
            'reductions':len(events['reductions']),'financial':audit['result'] if audit else None,
            'status':'ABANDONED' if events['abandoned_unsent'] else 'INTERRUPTED' if saved['slot_reserved'] else 'COMPLETE',
            'authorization':False}


def create_limit_router(access,session,bots,*,authorized,authorized_passive,lab_path,paper_path):
    router = APIRouter(prefix='/perp/limit',tags=['grvt-limit-mainnet'])

    def generation(token,response):
        response.headers['Cache-Control'] = 'no-store'
        access.require(token,touch=False)
        return access._vault_generation

    def stores():
        if lab_path is None or paper_path is None:
            raise HTTPException(503,'LAB y PAPER LIMIT no están configurados')
        if lab_access.get(lab_path) is None or paper_perp_access.get(paper_path) is None:
            raise HTTPException(423,'Desbloquea LAB y GRVT PAPER en Ajustes')
        return LabStore(lab_path,allow_create=False),ObservedArchive(PaperPerpStore(paper_path,allow_create=False))

    def fail():
        return HTTPException(409,'Bot LIMIT sin confirmar. Revisa la sesión, los límites y el diario',
                             headers={'Cache-Control':'no-store'})

    def preview_origin(lab,archive,data,budget):
        with archive.paper.connect() as db:
            row = db.execute("SELECT json_extract(seed_json,'$.schema') FROM paper_observed_sessions WHERE id=?",
                             (data.session_id,)).fetchone()
        if row is not None and row[0]=='orbis.paper.perp.observed.seed.v3':
            if data.reserved_close_attempts is None:
                raise ValueError('Faltan los intentos reservados para el cierre')
            origin = verified_partial_paper_origin(lab,archive,data.session_id)
            terms = origin.limit_terms(budget,max_pending_seconds=data.max_pending_seconds,
                max_attempts=data.max_reductions,reserved_close_attempts=data.reserved_close_attempts)
        else:
            if data.max_reductions > 5 or data.reserved_close_attempts is not None:
                raise ValueError('Límite de reducciones incompatible con el perfil')
            origin = verified_limit_paper_origin(lab,archive,data.session_id)
            terms = origin.terms(budget,max_pending_seconds=data.max_pending_seconds)
        return origin,terms

    def require_valid_review(token,sub,current,review,checked):
        checked()
        if (not int(review['created_at_ns']) <= session.clock() < int(review['expires_at_ns'])
                or review['authorization_seconds'] > session.status(token,sub,current)['connection_remaining_seconds']):
            raise GrvtMainnetError('Revisión o sesión LIMIT vencida')

    @router.get('/sources',dependencies=[Depends(authorized_passive)])
    def sources(response: Response,offset: int=Query(0,ge=0,le=1_000_000),
                session_id: UUID|None=None,
                token: str|None=Header(None,alias='X-Orbis-Vault-Token')):
        generation(token,response)
        _,archive = stores()
        try:
            with archive.paper.connect() as db:
                session_filter = 'AND s.id=?' if session_id else ''
                parameters = (str(session_id),0) if session_id else (offset,)
                rows = db.execute(f'''SELECT id,created_at,origin_json,
                    json_extract(seed_json,'$.definition.direction') AS direction,
                    (SELECT count(*) FROM paper_observed_trades t WHERE t.session_id=s.id) AS closed_trades
                    FROM paper_observed_sessions s
                    WHERE status='STOPPED' AND (
                        json_extract(seed_json,'$.schema')='orbis.paper.perp.observed.seed.v2'
                        OR (json_extract(seed_json,'$.schema')='orbis.paper.perp.observed.seed.v3'
                            AND json_type(seed_json,'$.profile.entry_order')='object'
                            AND EXISTS (SELECT 1 FROM paper_observed_trades t,
                                json_each(t.payload_json,'$.partial_exits') p WHERE t.session_id=s.id
                                AND json_extract(p.value,'$.kind')='PARTIAL_TARGET_FILLED')))
                    AND NOT EXISTS (SELECT 1 FROM paper_observed_gap_closes g WHERE g.session_id=s.id)
                    {session_filter}
                    ORDER BY created_at DESC,id DESC LIMIT 26 OFFSET ?''',parameters).fetchall()
            items = []
            for row in rows[:25]:
                ref = OriginSnapshot.model_validate(json.loads(row['origin_json'])).reference
                if row['closed_trades'] and row['direction'] in ('LONG','SHORT'):
                    item = {'session_id':row['id'],'symbol':ref.symbol,'timeframe':ref.timeframe,
                                  'direction':row['direction'],'closed_trades':row['closed_trades'],
                                  'margin_mode':ref.profile.margin_mode,'leverage':ref.profile.leverage}
                    if ref.schema_name=='orbis.research.perp.reference.v3':
                        item['partial_targets'] = len(ref.profile.partial_targets)
                    items.append(item)
            generation(token,response)
            return {'items':items,'has_more':len(rows)>25,'execution_authority':'NONE'}
        except HTTPException:
            raise
        except Exception:
            raise fail() from None

    @router.post('/preview',dependencies=[Depends(authorized)])
    def preview(response: Response,body: object=Body(),token: str|None=Header(None,alias='X-Orbis-Vault-Token')):
        current = generation(token,response)
        data = _body(LimitPreview,body)
        lab,archive = stores()
        try:
            if not data.dedicated_account_confirmed:
                raise ValueError('dedicated account')
            validate_limit_timing(data.wait_seconds,data.max_pending_seconds,data.exit_seconds,data.authorization_seconds)
            sub = data.sub_account_id
            revision = tuple(data.permission_revision)
            if bots.active_sub(sub) or data.authorization_seconds > session.status(token,sub,current)['connection_remaining_seconds']:
                raise ValueError('active or connection expiring')
            ticket = bots.ticket(sub)
            _,checked,factory = session.capture(token,sub,revision,current,sign=True)
            cid = str(2**63|secrets.randbits(63))
            with access.vault.guard_session_generation(current),access.vault.session() as db:
                require_new_mainnet_bracket(db,sub,cid)
            budget = PerpRealBudget(schema_name='orbis.grvt.perp.real.budget.v1',sub_account_id=sub,
                **data.model_dump(include={'collateral_limit','entry_cash_limit','entry_notional_limit','exit_notional_limit',
                                          'fee_rate_ceiling','exit_seconds','max_spread_bps','max_slippage_bps'}),
                collateral_scope='WHOLE_DEDICATED_USDT_SUBACCOUNT')
            origin,terms = preview_origin(lab,archive,data,budget)
            profile = terms.research.profile
            proof = collect_perp_market(factory(),terms.instrument,sub,public_post=session.public,
                                        clock_ns=lambda:checked() or session.clock())
            market = verify_perp_market(proof,instrument=terms.instrument,margin_mode=profile.margin_mode,
                                         leverage_value=profile.leverage,budget=budget,now_ns=session.clock())
            with origin.hold_current():
                checked()
                bots._require_ticket(sub,ticket)
                proposal = {'client_order_id':cid,'terms':terms.payload(),'account_equity':market.account_equity,
                            'wait_seconds':data.wait_seconds,'authorization_seconds':data.authorization_seconds,
                            'max_reductions':data.max_reductions,'bot_control_revision':list(ticket),
                            'lab_origin':origin.origin.review_lab_origin(),
                            'execution_scope':('ONE_FUTURE_LIMIT_ENTRY_AND_BOUNDED_PARTIAL_EXITS'
                                if isinstance(terms,PerpPartialLimitEntryTerms) else 'ONE_FUTURE_LIMIT_ENTRY_AND_OWN_POSITION_CLOSE'),
                            'execution_authority':'NONE'}
                return _wire(session.seal_limit_review(token,sub,revision,current,proposal,origin))
        except HTTPException:
            raise
        except Exception:
            raise fail() from None

    @router.post('/start',dependencies=[Depends(authorized)],status_code=202)
    def start(response: Response,body: object=Body(),token: str|None=Header(None,alias='X-Orbis-Vault-Token')):
        current = generation(token,response)
        data = _body(LimitStart,body)
        stores()
        try:
            review = data.review
            sub,revision = review['sub_account_id'],tuple(data.permission_revision)
            partial = data.confirmation=='ACTIVAR_BOT_LIMIT_PARCIALES_GRVT_MAINNET'
            credentials,checked,origin = session.consume_limit_review(token,review,data.review_mac,revision,current,partial=partial)
            _,_,factory = session.capture(token,sub,revision,current,sign=True)

            def start_checked():
                require_valid_review(token,sub,current,review,checked)

            _,key,signer = credentials
            terms = read_limit_terms(review['terms'])
            if terms.budget.sub_account_id != sub or partial != isinstance(terms,PerpPartialLimitEntryTerms):
                raise ValueError('subaccount')
            result = start_limit_worker(bots,origin,terms,cid=review['client_order_id'],
                review_id=review['review_id'],private_key=key,
                expected_signer=signer,client_factory=factory,session_checked=checked,start_checked=start_checked,
                generation=current,ticket=tuple(review['bot_control_revision']),wait_seconds=review['wait_seconds'],
                authorization_seconds=review['authorization_seconds'],max_reductions=review['max_reductions'])
            return _wire(result)
        except HTTPException:
            raise
        except Exception:
            raise fail() from None

    @router.post('/resume/preview',dependencies=[Depends(authorized)])
    def resume_preview(response: Response,body: object=Body(),token: str|None=Header(None,alias='X-Orbis-Vault-Token')):
        current = generation(token,response)
        data = _body(LimitResumePreview,body)
        try:
            sub,cid = data.sub_account_id,data.client_order_id
            revision = tuple(data.permission_revision)
            if bots.active_sub(sub) or data.authorization_seconds > session.status(token,sub,current)['connection_remaining_seconds']:
                raise ValueError('active or connection expiring')
            ticket = bots.ticket(sub)
            connection,checked,factory = session.capture(token,sub,revision,current,sign=True)
            client = factory()
            with access.vault.guard_session_generation(current):
                saved = verify_limit_resume(access.vault,sub,cid,client=client,signer=connection.signer)
                partial = validate_resume_reductions(saved,data.max_reductions)
                checked()
            bots._require_ticket(sub,ticket)
            budget = saved['claim']['preflight']['plan']['source']['terms']['budget']
            proposal = {'attempt':_attempt(saved),'claim_digest':saved['claim_digest'],
                        'event_head':saved['events']['head'],'bot_control_revision':list(ticket),
                        'authorization_seconds':data.authorization_seconds,'max_reductions':data.max_reductions,
                        'exit_notional_limit':budget['exit_notional_limit'],
                        'execution_scope':'MANAGE_OWN_EXISTING_LIMIT_ONLY','execution_authority':'NONE'}
            return session.seal_limit_resume(token,sub,revision,current,_wire(proposal),partial=partial)
        except HTTPException:
            raise
        except Exception:
            raise fail() from None

    @router.post('/resume/start',dependencies=[Depends(authorized)],status_code=202)
    def resume_start(response: Response,body: object=Body(),token: str|None=Header(None,alias='X-Orbis-Vault-Token')):
        current = generation(token,response)
        data = _body(LimitResumeStart,body)
        try:
            review = data.review
            sub,revision = review['sub_account_id'],tuple(data.permission_revision)
            partial = data.confirmation=='RETOMAR_BOT_LIMIT_PARCIALES_GRVT_MAINNET'
            credentials,checked = session.consume_limit_resume(token,review,data.review_mac,revision,current,partial=partial)
            _,_,factory = session.capture(token,sub,revision,current,sign=True)
            _,key,signer = credentials
            result = resume_limit_worker(bots,sub,review['attempt']['client_order_id'],
                review_id=review['review_id'],claim_digest=review['claim_digest'],event_head=review['event_head'],
                private_key=key,expected_signer=signer,client_factory=factory,session_checked=checked,
                start_checked=lambda:require_valid_review(token,sub,current,review,checked),generation=current,
                ticket=tuple(review['bot_control_revision']),authorization_seconds=review['authorization_seconds'],
                max_reductions=review['max_reductions'])
            return _wire(result)
        except HTTPException:
            raise
        except Exception:
            raise fail() from None

    @router.get('/attempts',dependencies=[Depends(authorized_passive)])
    def attempts(response: Response,sub_account_id: Sub=Query(),offset: int=Query(0,ge=0,le=1_000_000),
                 token: str|None=Header(None,alias='X-Orbis-Vault-Token')):
        current = generation(token,response)
        job = bots.limit_status(sub_account_id)
        try:
            with access.vault.try_guard_session_generation(current) as available:
                if not available:
                    return _wire({'pending':True,'job':job,'items':[],'has_more':False})
                with access.vault.session() as db:
                    rows = list(db.scalars(select(GrvtMainnetLimitAttempt)
                        .where(GrvtMainnetLimitAttempt.sub_account_id==sub_account_id)
                        .order_by(func.json_extract(GrvtMainnetLimitAttempt.payload_json,'$.claimed_at_ns').desc(),
                                  GrvtMainnetLimitAttempt.client_order_id.desc()).offset(offset).limit(21)))
                    items = [_attempt(verified_limit_attempt(db,row)) for row in rows[:20]]
                return _wire({'pending':False,'job':job,'items':items,'has_more':len(rows)>20})
        except Exception:
            raise fail() from None

    @router.get('/attempts/{cid}/executions-page',dependencies=[Depends(authorized_passive)])
    def executions(cid: str,response: Response,limit: int=Query(500,ge=1,le=500),offset: int=Query(0,ge=0,le=10000),
                   revision: str|None=Query(None,pattern=r'^[a-f0-9]{64}$'),
                   token: str|None=Header(None,alias='X-Orbis-Vault-Token')):
        current = generation(token,response)
        try:
            with access.vault.try_guard_session_generation(current) as available:
                if not available:
                    raise ValueError('Historial LIMIT ocupado')
                value = limit_executions_view(read_limit_attempt(access.vault,cid))
                return _wire(executions_page(value,limit=limit,offset=offset,revision=revision))
        except Exception:
            raise fail() from None

    @router.post('/attempts/{cid}/research-export',dependencies=[Depends(authorized_passive)])
    def research_export(cid: str, body: GrvtFillExport, response: Response,
                        token: str | None=Header(None,alias='X-Orbis-Vault-Token')):
        return export_fill_archive(access,token,response,cid,body,limit=True)

    @router.post('/attempts/abandon',dependencies=[Depends(authorized)])
    def abandon(response: Response,body: object=Body(),token: str|None=Header(None,alias='X-Orbis-Vault-Token')):
        data = _body(LimitAbandon,body)
        try:
            with access._lock:
                current = generation(token,response)
                if bots.active_sub(data.sub_account_id):
                    raise ValueError('Subcuenta ocupada')
                saved = abandon_unsent_limit(access.vault,data.sub_account_id,data.client_order_id,
                                             generation=current,clock_ns=session.clock)
            return _wire(_attempt(saved))
        except HTTPException:
            raise
        except Exception:
            raise fail() from None

    return router
