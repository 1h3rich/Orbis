"""Reuse authenticated records only within an explicitly scoped SQL snapshot.

The original validators still run after any ORM, SQL, schema or transaction
change. Payloads are copied so a consumer cannot edit a later reader's proof.
Nothing survives the outer read scope, including successful absence checks.
"""
from contextlib import contextmanager
from copy import deepcopy

from app.portfolio.dex_verified_read import snapshot_pin

KEY = 'orbis_economic_verified_read'


@contextmanager
def scope(db):
    owns = KEY not in db.info
    if owns:
        db.info[KEY] = {'pin': None, 'records': {}}
    try:
        yield
    finally:
        if owns:
            db.info.pop(KEY, None)


def _pin(db):
    version = db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()
    return snapshot_pin(db, version)


def record(db, key, load):
    current = db.info.get(KEY)
    if current is None:
        return load()
    pin = _pin(db)
    if pin is None or pin != current['pin']:
        # Raw SQL need not refresh the ORM identity map. Never discard pending
        # ORM edits when invalidating; their ordinary validation must still run.
        if current['pin'] is not None and not (db.new or db.dirty or db.deleted):
            db.expire_all()
        current['records'].clear()
        current['pin'] = pin
    if pin is not None and key in current['records']:
        row, payload = current['records'][key]
        return row, deepcopy(payload)
    value = load()
    if value is not None and pin is not None and pin == _pin(db):
        row, payload = value
        current['records'][key] = (row, deepcopy(payload))
    return value
