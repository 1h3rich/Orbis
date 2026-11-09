"""Concesiones de cierre sucesivas: el permiso anterior permanece inmutable."""
import re
from app.live_execution.grvt_mainnet_journal import encode
from app.live_execution.grvt_mainnet_finance import stamp
from app.live_execution.grvt_mainnet_preflight import number,canonical
from app.live_execution.grvt_mainnet_bracket import MAX_EXIT_CAP


def reviewed_exit_cap(previous:dict,requested:str|None)->str:
    if requested is None:return previous['exit_cap']
    cap=number(requested)
    if not 0<cap<=MAX_EXIT_CAP:raise ValueError('reviewed exit cap outside supported bounds')
    return previous['exit_cap'] if cap==number(previous['exit_cap']) else canonical(cap)


def effective_exit_draft(claim:dict,request:dict):
    from dataclasses import replace
    from app.live_execution.grvt_mainnet_journal import _draft
    draft=_draft(claim['draft'])
    return replace(draft,exit_cap=number(request['exit_cap'])) if request['schema']=='orbis.grvt.mainnet.recovery.grant.v2' else draft


def exhausted(request:dict,reductions:list[dict],at_ns:int)->bool:
    return at_ns>=request['deadline_ns'] or len(reductions)>=request['max_attempts']


def verified_grant(previous:dict,value:dict,*,sequence:int,at_ns:int,reductions:list[dict])->dict:
    adjusted=value.get('schema')=='orbis.grvt.mainnet.recovery.grant.v2'
    fields=set(previous)-{'previous_request_digest','first_attempt','grant_sequence','review_id','previous_exit_cap','reason'}
    fields|={'previous_request_digest','first_attempt','grant_sequence','review_id'}
    if adjusted:fields|={'previous_exit_cap','reason'}
    spent=exhausted(previous,reductions,at_ns)
    if (set(value)!=fields or value['schema'] not in ('orbis.grvt.mainnet.recovery.grant.v1','orbis.grvt.mainnet.recovery.grant.v2')
            or value['network']!='MAINNET' or value['authorization'] is not False
            or any(value[key]!=previous[key] for key in ('parent_client_order_id','claim_digest','max_spread_bps','max_slippage_bps'))
            or not adjusted and (value['exit_cap']!=previous['exit_cap'] or previous['schema']=='orbis.grvt.mainnet.recovery.grant.v2')
            or value['previous_request_digest']!=encode(previous)[1] or value['grant_sequence']!=sequence
            or type(value['grant_sequence']) is not int or type(value['first_attempt']) is not int
            or value['first_attempt']!=len(reductions)+1 or type(value['max_attempts']) is not int
            or not value['first_attempt']<=value['max_attempts']<=value['first_attempt']+2
            or stamp(value['started_at_ns'])!=at_ns
            or not 30_000_000_000<=stamp(value['deadline_ns'])-at_ns<=300_000_000_000
            or not isinstance(value['review_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',value['review_id'])
            or not adjusted and not spent
            or any(item['state'] is None or item['state']['status'] not in ('FILLED','CANCELLED','REJECTED') for item in reductions)):
        raise ValueError('renewed recovery terms or unresolved earlier IOC')
    if adjusted:
        changed=number(value['exit_cap'])!=number(previous['exit_cap'])
        if (value['previous_exit_cap']!=previous['exit_cap'] or value['exit_cap']!=reviewed_exit_cap(previous,value['exit_cap'])
                or not spent and not changed or value['reason']!=('EXHAUSTED' if spent else 'EXIT_LIMIT_CHANGE')):
            raise ValueError('unproven explicit exit cap change')
    return value


def renew_mainnet_recovery(vault,parent:str,*,expected_request_digest:str,expected_event_count:int,
        ttl_seconds:int,max_attempts:int,review_id:str,require_permission,now_ns:int,exit_cap:str|None=None)->dict:
    from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery,_event
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    from app.portfolio.models import GrvtMainnetRecovery,GrvtMainnetBracket
    from app.live_execution.grvt_mainnet_journal import verified_mainnet_bracket
    generation=vault.session_generation;require_permission()
    if (type(ttl_seconds) is not int or not 30<=ttl_seconds<=300 or type(max_attempts) is not int
            or not 1<=max_attempts<=3 or type(expected_event_count) is not int):raise GrvtMainnetError('Límites de renovación no válidos')
    with vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<55:raise ValueError('old grant schema')
            group=db.get(GrvtMainnetBracket,parent);saved=verified_mainnet_bracket(db,group);view=saved['recovery']
            if saved['reconciliation'] is not None or view is None:raise ValueError('no pending recovery')
            previous=view['request'];header=db.get(GrvtMainnetRecovery,parent)
            if encode(previous)[1]!=expected_request_digest or header.event_count!=expected_event_count:raise ValueError('review changed')
            value={**previous,'schema':'orbis.grvt.mainnet.recovery.grant.v1','started_at_ns':stamp(now_ns),
                'deadline_ns':now_ns+ttl_seconds*1_000_000_000,'max_attempts':len(view['reductions'])+max_attempts,
                'first_attempt':len(view['reductions'])+1,'grant_sequence':header.event_count+1,'review_id':review_id,
                'previous_request_digest':expected_request_digest}
            if exit_cap is not None or previous['schema']=='orbis.grvt.mainnet.recovery.grant.v2':
                if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<57:raise ValueError('exit cap review schema unavailable')
                value.update(schema='orbis.grvt.mainnet.recovery.grant.v2',exit_cap=reviewed_exit_cap(previous,exit_cap),
                    previous_exit_cap=previous['exit_cap'],reason='EXHAUSTED' if exhausted(previous,view['reductions'],now_ns) else 'EXIT_LIMIT_CHANGE')
            verified_grant(previous,value,sequence=header.event_count+1,at_ns=now_ns,reductions=view['reductions'])
            require_permission();_event(db,header,'RECOVERY_GRANT',value,now_ns)
            db.flush();verified_mainnet_bracket(db,group);db.commit()
        except Exception:
            db.rollback();raise GrvtMainnetError('No se pudo renovar el cierre MAINNET; revisa intentos y autorización') from None
    return read_mainnet_recovery(vault,parent)
