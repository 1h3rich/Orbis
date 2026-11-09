"""Same-origin loopback API for the explicitly activated observed PAPER book."""
from __future__ import annotations

from fastapi import APIRouter,Depends,HTTPException,Query,Request,Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel,ConfigDict,Field,field_validator

from app.derivatives.paper_access import paper_perp_access,paper_perp_format
from app.derivatives.perp_observed_archive import identifier
from app.derivatives.perp_observed_comparison import compare_observed_reserves
from app.derivatives.perp_observed_service import ObservedPaperService
from app.derivatives.perp_observed_view import ObservedReplayView
from app.derivatives.perp_gap_close_store import GapCloseStore
from app.derivatives.perp_gap_close_export import export_gap_close
from app.lab.access import lab_access


CanonicalID=str


class PrepareRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    validation_id: CanonicalID=Field(min_length=36,max_length=36)
    preparation_id: CanonicalID=Field(min_length=36,max_length=36)


class ActivateRequest(PrepareRequest):
    request_id: CanonicalID=Field(min_length=36,max_length=36)


class ControlRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    request_id: CanonicalID=Field(min_length=36,max_length=36)


class GapCloseRequest(ControlRequest):
    expected_head: str=Field(pattern=r'^[0-9a-f]{64}$')
    acknowledge_gap: bool=Field(strict=True)

    @field_validator('acknowledge_gap')
    @classmethod
    def accepted(cls,value: bool) -> bool:
        if value is not True:raise ValueError('Confirma el intervalo sin observación')
        return value


def create_observed_paper_router(service: ObservedPaperService) -> APIRouter:
    archive=service.archive
    gap_closes=GapCloseStore(archive,source_factory=lambda:service.source_factory())
    hosts=frozenset(('127.0.0.1','::1','localhost','testserver','testclient'))

    def local_only(request: Request,response: Response) -> None:
        response.headers['Cache-Control']='no-store'
        if not request.client or request.client.host not in hosts or request.url.hostname not in hosts:
            raise HTTPException(403,'PAPER observado sólo local')
        origin=request.headers.get('origin')
        if origin is not None and origin!=str(request.base_url).rstrip('/'):
            raise HTTPException(403,'Origen externo rechazado')

    def available() -> None:
        try:
            if paper_perp_format(archive.paper.path)!='encrypted_or_invalid':
                raise HTTPException(423,'Protege o crea GRVT PAPER en Ajustes')
            if paper_perp_access.get(archive.paper.path) is None:
                raise HTTPException(423,'GRVT PAPER bloqueado · desbloquea en Ajustes')
        except (ValueError,OSError) as exc:raise HTTPException(422,str(exc)) from exc

    def call(action,*args,**kwargs):
        try:return action(*args,**kwargs)
        except (ValueError,OSError) as exc:
            text=str(exc)
            status=404 if 'no encontrada' in text or 'no encontrado' in text else 422
            raise HTTPException(status,text,headers={'Cache-Control':'no-store'}) from exc

    router=APIRouter(prefix='/paper/perp/observed',tags=['paper-observed'],dependencies=[Depends(local_only)])

    @router.get('/status')
    def status() -> dict:
        return {**service.snapshot(),'storage':paper_perp_access.status(archive.paper.path)}

    @router.post('/prepare',dependencies=[Depends(available)])
    def prepare(body: PrepareRequest) -> dict:
        return call(service.prepare,body.validation_id,preparation_id=body.preparation_id)

    @router.post('/activate',dependencies=[Depends(available)])
    def activate(body: ActivateRequest,response: Response) -> dict:
        result=call(service.activate,body.validation_id,preparation_id=body.preparation_id,request_id=body.request_id)
        if result['in_progress']:response.status_code=202
        return result

    @router.get('/requests/{request_id}',dependencies=[Depends(available)])
    def request_status(request_id: str) -> dict:return call(service.request_status,request_id)

    @router.post('/requests/{request_id}/cancel')
    def cancel(request_id: str) -> dict:
        # Works while locked or while another HTTP request is still collecting.
        # Do not reveal account/worker details through this emergency control.
        result=call(service.cancel,request_id)
        return {key:result[key] for key in ('request_id','phase','cancel_requested','in_progress')}

    @router.get('/sessions',dependencies=[Depends(available)])
    def sessions(offset: int=Query(0,ge=0,le=1_000_000_000),limit: int=Query(25,ge=1,le=100)) -> dict:
        page=call(service.sessions,offset=offset,limit=limit)
        pointers=call(gap_closes.pointers,[row['id'] for row in page['items']])
        for row in page['items']:row['gap_close']=pointers.get(row['id'])
        return {**page, "lab_unlocked": lab_access.get(service.lab.path) is not None}

    @router.get('/sessions/{session_id}',dependencies=[Depends(available)])
    def detail(session_id: str) -> dict:return call(archive.read,session_id)

    @router.get('/sessions/{session_id}/view',dependencies=[Depends(available)])
    def view(session_id: str,before_t: int|None=Query(None,ge=1),limit: int=Query(1000,ge=1,le=2000),
             trade_offset: int=Query(0,ge=0,le=1_000_000_000),
             marker_offset: int=Query(0,ge=0,le=1_000_000_000),snapshot_sequence: int|None=Query(None,ge=0),
             snapshot_hash: str|None=Query(None,pattern=r'^[0-9a-f]{64}$')) -> dict:
        return call(archive.read,session_id,view=ObservedReplayView(before_t=before_t,candle_limit=limit,
            trade_offset=trade_offset,marker_offset=marker_offset),snapshot_sequence=snapshot_sequence,snapshot_hash=snapshot_hash)

    @router.get('/sessions/{session_id}/reserve-comparison', dependencies=[Depends(available)])
    def reserve_comparison(session_id: str, snapshot_sequence: int=Query(...,ge=0),
                           snapshot_hash: str=Query(...,pattern=r'^[0-9a-f]{64}$')) -> dict:
        if lab_access.get(service.lab.path) is None:
            raise HTTPException(423,'Desbloquea LAB en Ajustes para comparar la reserva',
                                headers={'Cache-Control':'no-store'})
        return call(compare_observed_reserves,service.lab,archive,session_id,
                    snapshot_sequence=snapshot_sequence,snapshot_hash=snapshot_hash)

    @router.get('/sessions/{session_id}/events',dependencies=[Depends(available)])
    def events(session_id: str,offset: int=Query(0,ge=0,le=1_000_000_000),limit: int=Query(100,ge=1,le=200)) -> dict:
        return call(archive.events_page,session_id,offset=offset,limit=limit)

    @router.post('/sessions/{session_id}/stop',dependencies=[Depends(available)])
    def stop(session_id: str,body: ControlRequest) -> dict:
        return call(archive.control,session_id,'STOP',request_id=body.request_id)

    @router.post('/sessions/{session_id}/close',dependencies=[Depends(available)])
    def close(session_id: str,body: ControlRequest) -> dict:
        return call(archive.control,session_id,'CLOSE_REQUEST',request_id=body.request_id)

    @router.get('/sessions/{session_id}/gap-close',dependencies=[Depends(available)])
    def gap_close_read(session_id: str) -> dict:
        return call(gap_closes.read,session_id)

    @router.post('/sessions/{session_id}/gap-close',dependencies=[Depends(available)])
    def gap_close(session_id: str,body: GapCloseRequest) -> dict:
        return call(gap_closes.close,session_id,request_id=body.request_id,expected_head=body.expected_head)

    @router.get('/sessions/{session_id}/gap-close/export',dependencies=[Depends(available)])
    def gap_close_export(session_id: str):
        session_id=call(identifier,session_id)
        lines=export_gap_close(gap_closes,session_id)
        first=call(next,lines)
        def chunks():
            try:
                yield first
                yield from lines
            finally:lines.close()
        return StreamingResponse(chunks(),media_type='application/x-ndjson',headers={
            'Cache-Control':'no-store','Content-Disposition':'attachment; filename="orbis-paper-gap-close-'+session_id+'.jsonl"'})

    @router.get('/sessions/{session_id}/export',dependencies=[Depends(available)])
    def export(session_id: str):
        session_id=call(identifier,session_id)
        lines=archive.export_lines(session_id)
        first=call(next,lines)  # Replay before offering HTTP success or bytes.
        def chunks():
            try:
                yield first
                yield from lines
            finally:lines.close()
        return StreamingResponse(chunks(),media_type='application/x-ndjson',headers={
            'Cache-Control':'no-store','Content-Disposition':'attachment; filename="orbis-paper-perp-'+session_id+'.jsonl"'})

    return router
