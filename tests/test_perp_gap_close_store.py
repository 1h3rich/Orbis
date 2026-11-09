"""Complete stored closure/replay/epoch/idempotency/export on own SQLCipher."""
from decimal import Decimal as D
import io
from threading import Event, Thread
from uuid import uuid4

import pytest

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.paper_migration import verify_paper_perp_database, create_secure_paper_perp_backup
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.perp_gap_close_export import export_gap_close, verify_gap_close_export
from app.derivatives.perp_gap_close_store import GapCloseStore
from app.derivatives.perp_observed_archive import ObservedArchive
from app.derivatives.perp_observed_archive_format import pack, unpack
from app.derivatives.perp_observed_types import ObservedError
from app.lab.store import content_hash
from tests.perp_gap_close_fixture import owned_gap
from tests.perp_observed_archive_fixture import PAPER_KEY, NS


def uid(): return str(uuid4())


def test_close_then_cold_read_idempotency_export_and_original_exactly_unchanged(tmp_path, monkeypatch):
    paper, archive, store, id, clock, adapter = owned_gap(tmp_path, monkeypatch)
    before = archive.read(id)
    request = uid()
    result = store.close(id, request_id=request, expected_head=before['session']['head_hash'])
    assert result['quality'] == 'ESTIMATED_GAP_CLOSE' and result['execution_authority'] == 'NONE'
    assert D(result['result']['metrics']['cash']) == D('1074.997')
    assert archive.read(id) == before and not archive.active_ids()
    assert 'candles' not in adapter.calls and 'funding' not in adapter.calls  # No new settlement due yet.
    closed = GapCloseStore(ObservedArchive(PaperPerpStore(paper.path), clock_ns=lambda: clock['now']))
    assert closed.read(id) == result
    clock['now'] += 100 * NS
    calls = list(adapter.calls)
    repeated = store.close(id, request_id=request, expected_head=before['session']['head_hash'])
    assert repeated['duplicate'] and repeated['receipt'] == result['receipt']
    assert repeated['result'] == result['result'] and adapter.calls == calls
    assert store.pointers([id])[id]['verification'] == 'NOT_REPLAYED'
    raw = b''.join(export_gap_close(closed, id))
    verified = verify_gap_close_export(io.BytesIO(raw))
    assert verified['verified'] and verified['result'] == result['result'] and not verified['session_activated']
    for broken in (raw[:-1], b'\n'.join(raw.splitlines()[:-1]) + b'\n', raw + b'{}\n'):
        with pytest.raises(ObservedError): verify_gap_close_export(io.BytesIO(broken))
    with pytest.raises(ObservedError): store.close(id, request_id=uid(), expected_head=before['session']['head_hash'])


def test_active_and_flat_sources_rejected_before_public_collection(tmp_path, monkeypatch):
    paper, archive, store, id, clock, adapter = owned_gap(tmp_path, monkeypatch, interrupt=False)
    with pytest.raises(ObservedError, match='Detén'):
        store.close(id, request_id=uid(), expected_head=archive.read(id)['session']['head_hash'])
    assert not adapter.calls
    cold = GapCloseStore(ObservedArchive(PaperPerpStore(paper.path), clock_ns=lambda: clock['now']),
                        source_factory=store.source_factory)
    with pytest.raises(ObservedError, match='Detén'):
        cold.close(id, request_id=uid(), expected_head=archive.read(id)['session']['head_hash'])
    assert not adapter.calls
    with pytest.raises(ObservedError, match='prefijo'):
        archive.interrupt(id, 'OWNED_STOP')
        store.close(id, request_id=uid(), expected_head='0' * 64)
    assert not adapter.calls


def test_flat_inactive_source_cannot_be_closed(tmp_path, monkeypatch):
    from tests.perp_observed_archive_fixture import owned_archive
    lab, paper, archive, origin, seed, clock = owned_archive(tmp_path, monkeypatch)
    id = archive.create(origin, seed, request_id=uid())['id']
    archive.interrupt(id, 'OWNED_FLAT_STOP')
    store = GapCloseStore(archive)
    with pytest.raises(ObservedError, match='posición'):
        store.close(id, request_id=uid(), expected_head=archive.read(id)['session']['head_hash'])


@pytest.mark.parametrize('change', ['LOCK', 'SAME_KEY_REAUTH'])
def test_lock_during_public_http_cancels_publication_and_never_resurrects(tmp_path, monkeypatch, change):
    paper, archive, store, id, clock, adapter = owned_gap(tmp_path, monkeypatch)
    head = archive.read(id)['session']['head_hash']
    entered, release = Event(), Event()
    original = store.collect
    errors = []
    def collect(state):
        entered.set(); assert release.wait(15)
        return original(state)
    monkeypatch.setattr(store, 'collect', collect)
    def close():
        try: store.close(id, request_id=uid(), expected_head=head)
        except ValueError as exc: errors.append(str(exc))
    worker = Thread(target=close); worker.start()
    try:
        assert entered.wait(15)
        paper_perp_access.lock(paper.path)
        if change == 'SAME_KEY_REAUTH': paper_perp_access.unlock(paper.path, PAPER_KEY)
        release.set(); worker.join(15)
        assert not worker.is_alive() and len(errors) == 1
        if change == 'LOCK': paper_perp_access.unlock(paper.path, PAPER_KEY)
        assert not store.pointers([id]) and archive.read(id)['session']['checkpoint']['opened']
        monkeypatch.setattr(store, 'collect', original)
        assert store.close(id, request_id=uid(), expected_head=head)['result']['status'] == 'ESTIMATED_CLOSED'
    finally:
        release.set(); worker.join(15)


def test_inflight_same_request_is_exclusive_then_retry_recovers_without_another_quote(tmp_path, monkeypatch):
    paper, archive, store, id, clock, adapter = owned_gap(tmp_path, monkeypatch)
    head, request = archive.read(id)['session']['head_hash'], uid()
    entered, release = Event(), Event()
    original = store.collect
    results, errors = [], []
    def collect(state):
        entered.set(); assert release.wait(15)
        return original(state)
    monkeypatch.setattr(store, 'collect', collect)
    def close():
        try: results.append(store.close(id, request_id=request, expected_head=head))
        except Exception as exc: errors.append(type(exc).__name__)
    worker = Thread(target=close); worker.start()
    try:
        assert entered.wait(15)
        with pytest.raises(ObservedError, match='curso'):
            store.close(id, request_id=request, expected_head=head)
        assert not store.pointers([id])
        release.set(); worker.join(15)
        assert not worker.is_alive() and not errors and len(results) == 1
        calls = list(adapter.calls)
        repeated = store.close(id, request_id=request, expected_head=head)
        assert repeated['duplicate'] and repeated['hash'] == results[0]['hash'] and adapter.calls == calls
    finally:
        release.set(); worker.join(15)


def test_original_header_changed_during_http_blocks_publication(tmp_path, monkeypatch):
    paper, archive, store, id, clock, adapter = owned_gap(tmp_path, monkeypatch)
    head = archive.read(id)['session']['head_hash']
    original = store.collect
    def changed(state):
        point = original(state)
        with paper.connect() as db:
            db.execute('UPDATE paper_observed_sessions SET created_at=? WHERE id=?', ('2026-01-01T00:00:00+00:00', id))
        return point
    monkeypatch.setattr(store, 'collect', changed)
    with pytest.raises(ObservedError, match='prefijo cambió'):
        store.close(id, request_id=uid(), expected_head=head)
    assert not store.pointers([id])


def test_result_corruption_even_with_recomputed_hash_blocks_read_and_backup(tmp_path, monkeypatch):
    paper, archive, store, id, clock, adapter = owned_gap(tmp_path, monkeypatch)
    result = store.close(id, request_id=uid(), expected_head=archive.read(id)['session']['head_hash'])
    bad = {**result['result'], 'metrics': {**result['result']['metrics'], 'cash': '999999'}}
    digest = content_hash({'receipt': result['receipt'], 'result': bad})
    with paper.connect() as db:
        db.execute('UPDATE paper_observed_gap_closes SET result_blob=?,hash=? WHERE session_id=?', (pack(bad), digest, id))
    with pytest.raises(ObservedError, match='reproduce'): store.read(id)
    with pytest.raises(ValueError): verify_paper_perp_database(paper.path, PAPER_KEY)


def test_encrypted_backup_contains_replayable_closure_and_v13_migrates_additively(tmp_path, monkeypatch):
    paper, archive, store, id, clock, adapter = owned_gap(tmp_path, monkeypatch)
    with paper.connect() as db:
        db.execute('DROP TABLE paper_observed_gap_closes'); db.execute('PRAGMA user_version=13')
    before = verify_paper_perp_database(paper.path, PAPER_KEY)
    assert before['schema_version'] == 13
    with paper.connect(): pass
    after = verify_paper_perp_database(paper.path, PAPER_KEY)
    assert after['schema_version'] == 14
    assert all(after['table_digests'][key] == value for key, value in before['table_digests'].items())
    result = store.close(id, request_id=uid(), expected_head=archive.read(id)['session']['head_hash'])
    key = 'owned separate encrypted closure backup'
    backup = create_secure_paper_perp_backup(paper, key)
    # The helper returns the basename in the existing managed backup directory.
    path = paper.path.parent / 'paper-perp-backups' / backup['name']
    copied = GapCloseStore(ObservedArchive(PaperPerpStore(path, passphrase=key)))
    assert copied.read(id)['result'] == result['result']
