"""Entrada ejecutada conservada sin precios PAPER, firma ni permiso de envío."""
import json
from app.live_execution.grvt_mainnet_journal import encode,verified_mainnet_bracket
from app.live_execution.grvt_mainnet_bot_exit import entry_from_fills
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.portfolio.models import GrvtMainnetBracket,GrvtMainnetObservation,GrvtMainnetEntryFills


def verify_entry_fills(db,value:dict)->dict:
    if (set(value)!={'schema','network','parent_client_order_id','claim_digest','observation_sequence','observation_digest','cutoff_ns','fills','size','notional','last_fill_ns'}
            or value['schema']!='orbis.grvt.mainnet.entry.fills.v1' or value['network']!='MAINNET'
            or type(value['observation_sequence']) is not int or value['observation_sequence']<1
            or type(value['cutoff_ns']) is not int):raise ValueError('entry fill proof scope')
    parent=value['parent_client_order_id'];group=verified_mainnet_bracket(db,db.get(GrvtMainnetBracket,parent))
    row=db.get(GrvtMainnetObservation,(parent,value['observation_sequence']))
    if row is None or row.payload_digest!=value['observation_digest'] or encode(group['claim'])[1]!=value['claim_digest']:
        raise ValueError('entry fill observation link')
    observation=json.loads(row.payload_json)
    if not 0<=value['cutoff_ns']-observation['state']['observed_at_ns']<=120_000_000_000 or value['cutoff_ns']>=2**63:raise ValueError('entry fill chronology')
    size,notional,last=entry_from_fills(group['claim'],observation,value['fills'],cutoff_ns=value['cutoff_ns'])
    if (value['size'],value['notional'],value['last_fill_ns'])!=(format(size,'f'),format(notional,'f'),last):raise ValueError('entry fill quantities changed')
    return value


def entry_fills_from_exit_proof(proof:dict)->dict:
    return {'schema':'orbis.grvt.mainnet.entry.fills.v1','network':'MAINNET',
        **{key:proof[key] for key in ('parent_client_order_id','claim_digest','observation_sequence','observation_digest','cutoff_ns','fills')},
        'size':proof['decision']['size'],'notional':proof['decision']['entry_notional'],'last_fill_ns':proof['decision']['last_fill_at_ns']}


def save_entry_fills(vault,proof:dict,*,require_permission)->dict:
    return save_verified_entry_fills(vault,entry_fills_from_exit_proof(proof),require_permission=require_permission)


def save_verified_entry_fills(vault,value:dict,*,require_permission)->dict:
    """Conserve the same generic fill contract without requiring a MARK exit."""
    value=json.loads(json.dumps(value,allow_nan=False))
    generation=vault.session_generation;require_permission()
    with vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<56:raise ValueError('entry fill schema unavailable')
            verify_entry_fills(db,value);parent=value['parent_client_order_id'];row=db.get(GrvtMainnetEntryFills,parent)
            if row is None:
                text,digest=encode(value);db.add(GrvtMainnetEntryFills(parent_client_order_id=parent,payload_json=text,payload_digest=digest))
            else:
                old=json.loads(row.payload_json);verify_entry_fills(db,old)
                if encode(old)!=(row.payload_json,row.payload_digest):raise ValueError('entry fill hash changed')
                same=('claim_digest','size','notional','last_fill_ns')
                ordered=lambda snapshot:sorted(snapshot['fills']['rows'],key=lambda fill:(fill['event_time_ns'],fill['trade_id']))
                if any(old[key]!=value[key] for key in same) or ordered(old)!=ordered(value):raise ValueError('filled entry changed')
                value=old
            require_permission();db.flush();db.commit();return value
        except Exception:
            db.rollback();raise GrvtMainnetError('Entrada ejecutada MAINNET no verificable para conservarla') from None


def read_entry_fills(db,parent:str)->dict|None:
    version=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()
    if version<56:return None
    row=db.get(GrvtMainnetEntryFills,parent)
    if row is None:return None
    value=json.loads(row.payload_json)
    if value['parent_client_order_id']!=parent or encode(value)!=(row.payload_json,row.payload_digest):raise ValueError('entry fill journal changed')
    return verify_entry_fills(db,value)


def verify_all_entry_fills(db)->None:
    names={row[0] for row in db.connection().exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'grvt_mainnet_entry_fills' not in names:raise ValueError('entry fill journal truncated')
    for row in db.query(GrvtMainnetEntryFills):read_entry_fills(db,row.parent_client_order_id)
