"""Empty owned LAB, real adapter/HTTP/UI, controlled 120 hourly public-wire bars."""
import json
import os
from pathlib import Path

import pytest

from app.lab.access import lab_access
from app.lab.store import LabStore, content_hash
from app.marketdata.adapters.grvt import GrvtAdapter
from tests.perp_dataset_fixture import START,NOW,INSTRUMENT,rules,specification


calls=[];patch=pytest.MonkeyPatch()
patch.setattr('app.marketdata.adapters.grvt.time.time_ns',lambda:NOW)


def public_post(url,body):
    calls.append((url,body))
    if url.endswith('/kline'):
        return {'result':[{'instrument':INSTRUMENT,'open_time':str((START+i*3600)*10**9),
            'close_time':str((START+(i+1)*3600)*10**9),'open':'100','high':'102' if body['type']=='TRADE' else '100',
            'low':'99' if body['type']=='TRADE' else '100','close':'101' if body['type']=='TRADE' else '100','volume_b':'10'} for i in reversed(range(120))],'next':''}
    if url.endswith('/funding'):
        return {'result':[{'instrument':INSTRUMENT,'funding_time':str((START+i*3600)*10**9),
            'funding_rate':'0','mark_price':'100','funding_interval_hours':1} for i in reversed(range(-1,120))],'next':''}
    if url.endswith('/margin_rules'):return rules()
    if url.endswith('/instrument'):return {'result':specification()}
    raise AssertionError(url)


adapter=GrvtAdapter(http_post=public_post)
patch.setattr('app.marketdata.adapters.grvt.GrvtAdapter',lambda:adapter)
from app.main import app


@app.get('/__perp_validation_fixture/state')
def state():
    path=Path(os.environ['ORBIS_LAB_PATH'])
    return {'locked':lab_access.get(path) is None,'calls':len(calls),
            'real_exists':Path(os.environ['ORBIS_REAL_VAULT_PATH']).exists()}


@app.post('/__perp_validation_fixture/corrupt')
def corrupt():
    path=Path(os.environ['ORBIS_LAB_PATH']);directory=Path(os.environ['ORBIS_BASIC_DIRECTORY'])
    assert path.resolve().parent==directory.resolve() and lab_access.get(path) is not None
    with LabStore(path).connect() as db:
        row=db.execute('SELECT id,payload_json FROM perp_validations ORDER BY created_at,id LIMIT 1').fetchone()
        raw=json.loads(row['payload_json']);raw['result']['windows'][0]['result_hash']='0'*64
        db.execute('UPDATE perp_validations SET payload_json=?,hash=? WHERE id=?',(json.dumps(raw),content_hash(raw),row['id']))
    return {'id':row['id'],'owned_fixture_changed':True}
