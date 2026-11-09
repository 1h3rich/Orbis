"""Loopback API must preserve consent, source identity and PAPER separation."""
import io
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.perp_gap_close_export import verify_gap_close_export
from app.derivatives.perp_observed_api import create_observed_paper_router
from app.derivatives.perp_observed_service import ObservedPaperService
from app.lab.store import LabStore
from tests.perp_gap_close_fixture import owned_gap


def test_http_close_export_reopen_and_no_implicit_authority(tmp_path, monkeypatch):
    paper, archive, store, id, clock, adapter = owned_gap(tmp_path, monkeypatch)
    service = ObservedPaperService(LabStore(tmp_path / 'lab.db'), archive, source_factory=store.source_factory)
    app = FastAPI(); app.include_router(create_observed_paper_router(service))
    root = '/paper/perp/observed/sessions/' + id
    before = archive.read(id)
    body = {'request_id': str(uuid4()), 'expected_head': before['session']['head_hash'], 'acknowledge_gap': True}
    with TestClient(app) as client:
        assert client.get(root + '/gap-close').status_code == 404
        for invalid in (False, 1, 'true'):
            assert client.post(root + '/gap-close', json={**body, 'acknowledge_gap': invalid}).status_code == 422
        assert client.post(root + '/gap-close', json={**body, 'account': {}}).status_code == 422
        assert client.post(root + '/gap-close', json=body, headers={'Origin': 'https://outside.invalid'}).status_code == 403
        assert not adapter.calls
        response = client.post(root + '/gap-close', json=body)
        assert response.status_code == 200, response.text
        value = response.json()
        assert value['quality'] == 'ESTIMATED_GAP_CLOSE' and value['execution_authority'] == 'NONE'
        assert response.headers['cache-control'] == 'no-store'
        assert client.get(root).json() == before
        page = client.get('/paper/perp/observed/sessions').json()
        assert page['items'][0]['gap_close']['verification'] == 'NOT_REPLAYED'
        repeated = client.post(root + '/gap-close', json=body)
        assert repeated.json()['duplicate'] and repeated.json()['hash'] == value['hash']
        exported = client.get(root + '/gap-close/export')
        assert exported.status_code == 200
        assert verify_gap_close_export(io.BytesIO(exported.content))['hash'] == value['hash']
        paper_perp_access.lock(paper.path)
        for action in ('', '/export'):
            assert client.get(root + '/gap-close' + action).status_code == 423
        assert client.post(root + '/gap-close', json=body).status_code == 423
        assert not (tmp_path / 'real.db').exists()
