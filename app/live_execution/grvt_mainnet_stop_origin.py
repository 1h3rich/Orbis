"""Procedencia del STOP MAINNET. La ausencia nunca permite un rearme automático."""
import json
from app.live_execution.grvt_mainnet_journal import encode,_control
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.portfolio.models import GrvtMainnetStopOrigin,GrvtMainnetBotRun,GrvtMainnetBracket


def available(db):
    names={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    exists='grvt_mainnet_stop_origins' in names
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=54 and not exists:
        raise GrvtMainnetError('Origen de STOP MAINNET truncado')
    return exists


def origin(db,sub):
    if not available(db):return None
    row=db.get(GrvtMainnetStopOrigin,sub)
    if row is None:return None
    value=json.loads(row.payload_json);control=_control(db,sub)
    if (set(value)!={'sub_account_id','kind','parent_client_order_id','stop_revision','released_revision'}
            or value['sub_account_id']!=sub or value['kind'] not in ('MANUAL','OBSERVATION','BOT_TERMINAL')
            or encode(value)!=(row.payload_json,row.payload_digest) or control is None
            or type(value['stop_revision']) is not int or not 0<value['stop_revision']<=control.revision
            or value['released_revision'] is not None and (type(value['released_revision']) is not int
                or not value['stop_revision']<value['released_revision']<=control.revision or control.stopped)
            or value['kind']=='OBSERVATION' and (not isinstance(value['parent_client_order_id'],str)
                or not value['parent_client_order_id'].isdigit())):raise GrvtMainnetError('Origen de STOP MAINNET no verificable')
    parent=value['parent_client_order_id']
    if value['kind']=='OBSERVATION':
        group=db.get(GrvtMainnetBracket,parent)
        if (not parent.isascii() or not 2**63<=int(parent)<2**64 or str(int(parent))!=parent
                or group is None or group.sub_account_id!=sub):raise GrvtMainnetError('Grupo del STOP MAINNET no verificable')
    elif parent is not None:raise GrvtMainnetError('STOP MAINNET sin grupo esperado')
    return value


def _store(db,value):
    row=db.get(GrvtMainnetStopOrigin,value['sub_account_id']);text,digest=encode(value)
    if row is None:db.add(GrvtMainnetStopOrigin(sub_account_id=value['sub_account_id'],payload_json=text,payload_digest=digest))
    else:row.payload_json=text;row.payload_digest=digest


def record_stop(db,control,kind,*,parent=None,was_stopped=False):
    if not available(db):return
    if kind in ('OBSERVATION','BOT_TERMINAL') and was_stopped:
        # Nunca sustituye un STOP manual, desconocido o ya registrado.
        return
    _store(db,{'sub_account_id':control.sub_account_id,'kind':kind,'parent_client_order_id':parent,
        'stop_revision':control.revision,'released_revision':None})


def record_release(db,control):
    if not available(db):return
    row=db.get(GrvtMainnetStopOrigin,control.sub_account_id)
    if row is None:return
    value=json.loads(row.payload_json)
    if encode(value)!=(row.payload_json,row.payload_digest):raise GrvtMainnetError('Origen de STOP modificado')
    _store(db,{**value,'released_revision':control.revision})


def release_bot_stop(vault,run_id,parent,*,expected_revision,require_permission,now_ns):
    from app.live_execution.grvt_mainnet_bot_entry import _read,_append
    generation=vault.session_generation;require_permission()
    with vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE');run=db.get(GrvtMainnetBotRun,run_id);grant=_read(db,run)
            if (grant['terminal'] is not None or not grant['selections'] or grant['selections'][-1]['parent_client_order_id']!=parent
                    or grant['selections'][-1]['settlement'] is None):raise ValueError('unsettled bot cycle')
            sub=grant['policy']['sub_account_id'];control=_control(db,sub);value=origin(db,sub)
            if (control is None or not control.stopped or control.revision!=expected_revision or control.pending_parent_id is not None
                    or value is None or value['kind']!='OBSERVATION' or value['parent_client_order_id']!=parent
                    or value['released_revision'] is not None):raise ValueError('manual or unknown STOP')
            require_permission();control.stopped=False;control.revision+=1;record_release(db,control)
            _append(db,run,'AUTO_REARM',{'parent_client_order_id':parent,'stop_revision':expected_revision,
                'released_revision':control.revision,'origin_digest':encode(value)[1]},now_ns)
            db.flush();_read(db,run);db.commit();return control.revision
        except Exception:
            db.rollback();raise GrvtMainnetError('STOP MAINNET requiere revisión manual') from None


def verify_all_stop_origins(db):
    if not available(db):return
    for row in db.query(GrvtMainnetStopOrigin):origin(db,row.sub_account_id)
