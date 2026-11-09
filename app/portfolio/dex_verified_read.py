"""One read-only verification scope; no trusted result survives its return.

The vault serializes sessions and owns the file lease. Within a pure read,
verify policy bindings root-to-leaf once, then reuse those checked ancestors.
Writes/commits happen after this scope has been discarded.
"""
from app.live_execution.dex_versions import SUCCESSOR_BINDINGS,ROLLOVER_CONTROLS
from contextlib import contextmanager
from uuid import UUID

from app.live_execution.dex_artifact import DexExecutorError
from app.portfolio.models import RealDexBotBinding,RealDexControlIntent
from app.portfolio.wallet_funding import _decoded

KEY='orbis_dex_verified_read'


def snapshot_pin(db, version: int) -> tuple | None:
    """Identify a clean SQLite read, including raw transaction restarts."""
    if db.new or db.dirty or db.deleted:
        return None
    connection = db.connection()
    if connection.dialect.name != 'sqlite':
        return None
    driver = connection.connection.driver_connection
    if not driver.in_transaction:
        return None
    return (db.get_transaction(), driver, driver.total_changes, version,
        connection.exec_driver_sql('PRAGMA schema_version').scalar_one(),
        connection.exec_driver_sql('PRAGMA data_version').scalar_one())


@contextmanager
def scope(db,*,force_full=False):
    current=db.info.get(KEY);owns=current is None
    if owns:
        # A caller's pending ORM updates must be visible before memoization.
        db.flush();current={'values':{},'active':set(),'warming':False,'force_full':force_full}
        db.info[KEY]=current
    elif force_full and not current['force_full']:
        current['values'].clear();current['force_full']=True
    try:yield current
    finally:
        if owns:db.info.pop(KEY,None)


def warm_bindings(db,identifier,current):
    """Walk raw references first; each row is verified before it is reused."""
    if current['warming']:return
    pending=[];seen=set();identifier=str(UUID(identifier))
    while ('binding',identifier) not in current['values']:
        if identifier in seen:raise DexExecutorError('El linaje de políticas contiene un ciclo')
        seen.add(identifier);row=db.get(RealDexBotBinding,identifier)
        if row is None:raise DexExecutorError('Falta un vínculo anterior del bot')
        terms=_decoded(row.payload_json,row.payload_digest);pending.append(identifier)
        if terms.get('schema') not in SUCCESSOR_BINDINGS:break
        control=db.get(RealDexControlIntent,str(UUID(terms['rollover_id'])))
        if control is None:raise DexExecutorError('Falta el control de renovación')
        payload=_decoded(control.payload_json,control.payload_digest)
        if payload.get('schema') not in ROLLOVER_CONTROLS:raise DexExecutorError('El sucesor no tiene una renovación versionada')
        identifier=str(UUID(payload['bot_rollover']['previous_binding_id']))
    from app.portfolio.dex_bot_bindings import binding_record
    current['warming']=True
    try:
        for previous in reversed(pending):binding_record(db,previous,replay=current['force_full'])
    finally:current['warming']=False


def read_record(db,kind,identifier,load,*,warm=None,force_full=False):
    identifier=str(UUID(identifier));key=(kind,identifier)
    try:
        with scope(db,force_full=force_full) as current:
            if key in current['values']:return current['values'][key]
            if key in current['active']:raise DexExecutorError('Una referencia de verificación vuelve sobre sí misma')
            if warm is not None and not current['warming']:warm_bindings(db,warm,current)
            if key in current['values']:return current['values'][key]
            current['active'].add(key)
            try:
                result=load(current);current['values'][key]=result
                return result
            finally:current['active'].remove(key)
    except DexExecutorError:raise
    except (ValueError,KeyError,TypeError,AttributeError,IndexError,OverflowError):
        raise DexExecutorError('El linaje verificado del bot DEX no es coherente') from None
