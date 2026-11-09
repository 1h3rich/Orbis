"""DCA's attached legacy tables coexist with LAB's separate perp data archive."""
import pytest

from app.automation.validation import VALIDATION_ENGINE_VERSION,chronological_dca_holdout
from tests.test_dca_validation import bars
from app.lab.engine import LabError
from app.lab.schema import CURRENT_SCHEMA_VERSION
from app.lab.perp_store import PerpDatasetStore
from app.lab.store import content_hash
from tests.perp_dataset_fixture import prepared as perp_inputs
from tests.test_dca_validation_api import prepared,create  # fixture


def test_encrypted_dca_reserve_coexists_with_perp_archive(prepared,monkeypatch):
    client,store,lab,dataset,version=prepared
    archive=PerpDatasetStore(lab)
    saved=archive.save(perp_inputs(monkeypatch))
    response=create(prepared)
    assert response.status_code==200,response.text
    result=response.json()
    assert result['dataset_hash']==dataset['hash']
    assert result['version_id']==version['version_id']
    check=client.get('/automation/dca/validations/'+result['id']+'/verify')
    assert check.status_code==200 and check.json()['verified']
    assert archive.read(saved['id'])[0]['hash']==saved['hash']
    assert lab.datasets()[0]['hash']==dataset['hash'] and len(lab.datasets())==1


@pytest.mark.parametrize('schema',[5,CURRENT_SCHEMA_VERSION+1])
def test_direct_attached_dca_save_still_rejects_unrecognized_schema(prepared,schema):
    _,store,lab,dataset,version=prepared
    key=lab.current_passphrase()
    result=chronological_dca_holdout(bars(),version['definition'],timeframe='1d')
    with lab.connect() as db:db.execute(f'PRAGMA user_version={schema}')
    with pytest.raises(LabError,match='LAB adjunto no compatible'):
        store.save_validation(version['version_id'],dataset['id'],dataset['hash'],result,
            VALIDATION_ENGINE_VERSION,lab_path=lab.path,
            expected_definition_hash=content_hash(version['definition']),lab_passphrase=key)
    assert store.validations()==[]
