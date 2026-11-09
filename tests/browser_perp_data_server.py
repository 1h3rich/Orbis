"""Full application, own initially-empty LAB, actual public-wire adapter parsing."""
import json
import os
from pathlib import Path

import pytest

from app.lab.access import lab_access
from app.lab.store import LabStore
from tests import perp_dataset_fixture
from tests.perp_dataset_fixture import NOW, source

patch=pytest.MonkeyPatch()
adapter,calls=source(patch)
patch.setattr('app.marketdata.adapters.grvt.GrvtAdapter',lambda:adapter)
from app.main import app


@app.get('/__perp_fixture/state')
def fixture_state():
    path=Path(os.environ['ORBIS_LAB_PATH'])
    return {'phase':os.environ.get('ORBIS_BASIC_PHASE'),'calls':len(calls),
        'locked':lab_access.get(path) is None,'real_exists':Path(os.environ['ORBIS_REAL_VAULT_PATH']).exists()}


@app.post('/__perp_fixture/rules-next')
def next_rules(tight: bool = False):
    path=Path(os.environ['ORBIS_LAB_PATH']);directory=Path(os.environ['ORBIS_BASIC_DIRECTORY'])
    assert path.resolve().parent==directory.resolve() and lab_access.get(path) is not None
    original=perp_dataset_fixture.rules
    def changed():
        current=original()
        current['risk_brackets'][0]['maintenance_margin_rate']='0.012'
        return current
    patch.setattr(perp_dataset_fixture,'rules',changed)
    if tight:
        original_spec=perp_dataset_fixture.specification
        patch.setattr(perp_dataset_fixture,'specification',lambda:{**original_spec(),'min_notional':'100000'})
    patch.setattr('app.marketdata.adapters.grvt.time.time_ns',lambda:NOW+2_000_000_000)
    return {'changed_owned_fixture':True}


@app.post('/__perp_fixture/archive-rules/{dataset_id}')
def archive_rules(dataset_id: str):
    from app.lab.perp_rules import RuleObservation,RuleObservationStore
    from app.lab.perp_store import PerpDatasetStore
    path=Path(os.environ['ORBIS_LAB_PATH']);directory=Path(os.environ['ORBIS_BASIC_DIRECTORY'])
    assert path.resolve().parent==directory.resolve() and lab_access.get(path) is not None
    lab=LabStore(path)
    _,dataset=PerpDatasetStore(lab).read(dataset_id)
    material=RuleObservation.from_dataset(dataset).payload()
    at=str(dataset.trade.bars[1].t*10**9)
    material['rules']['fetched_at_ns']=at
    material['specification'].update(fetched_at_ns=at,min_notional='100000')
    return RuleObservationStore(lab).save(RuleObservation.model_validate(material))


@app.post('/__perp_fixture/corrupt')
def corrupt_owned_archive():
    path=Path(os.environ['ORBIS_LAB_PATH']);directory=Path(os.environ['ORBIS_BASIC_DIRECTORY'])
    assert path.resolve().parent==directory.resolve() and lab_access.get(path) is not None
    with LabStore(path).connect() as db:
        row=db.execute('SELECT id,payload_json FROM perp_datasets LIMIT 1').fetchone()
        payload=json.loads(row[1]);payload['mark']['price_kind']='TRADE'
        db.execute('UPDATE perp_datasets SET payload_json=? WHERE id=?',(json.dumps(payload),row[0]))
    return {'changed_owned_fixture':True}


@app.post('/__perp_fixture/corrupt_run')
def corrupt_owned_run():
    from app.lab.store import content_hash
    path=Path(os.environ['ORBIS_LAB_PATH']);directory=Path(os.environ['ORBIS_BASIC_DIRECTORY'])
    assert path.resolve().parent==directory.resolve() and lab_access.get(path) is not None
    with LabStore(path).connect() as db:
        row=db.execute('SELECT id,payload_json FROM perp_runs LIMIT 1').fetchone()
        payload=json.loads(row[1]);payload['result']['metrics']['cash']='999999'
        db.execute('UPDATE perp_runs SET payload_json=?,hash=? WHERE id=?',
            (json.dumps(payload),content_hash(payload),row[0]))
    return {'changed_owned_fixture':True}
