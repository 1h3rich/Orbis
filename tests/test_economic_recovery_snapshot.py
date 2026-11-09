"""Complete encrypted recovery must avoid reauthenticating unchanged originals."""
from collections import Counter

import pytest
from sqlalchemy import create_engine, event, update

from app.portfolio import economic_acceptance as archive
from app.portfolio import economic_external_acceptance as single
from app.portfolio import economic_external_group_acceptance as joint
from app.portfolio import economic_external_booking as bookings
from app.portfolio import economic_external_group as groups
from app.portfolio import models as m
from app.portfolio.economic_verified_read import KEY, scope
from tests.test_economic_external_group_acceptance import prepared_case


@pytest.mark.parametrize('port', ['GROUP', 'RECEIPT'])
def test_recovery_authenticates_each_header_once_per_read_transaction(tmp_path, monkeypatch, port):
    with prepared_case(tmp_path) as case:
        saved = case.confirm()
        name = '_read_header' if hasattr(archive, '_read_header') else '_header'
        original = getattr(archive, name)
        loads = Counter()
        queries = []

        def header(db, identifier):
            loads[identifier] += 1
            return original(db, identifier)

        def query(*_):
            queries.append(1)

        monkeypatch.setattr(archive, name, header)
        event.listen(case.vault._engine, 'before_cursor_execute', query)
        try:
            if port == 'GROUP':
                recovered = joint.recover_group(case.vault, saved['group_id'], root_account_id=case.account)
            else:
                recovered = single.recover_external_acceptance(case.vault,
                    case.entries[0].observation_id, context_account_id=case.account)
        finally:
            event.remove(case.vault._engine, 'before_cursor_execute', query)
        assert recovered['id'] == saved['id'] and recovered['is_current']
        assert not recovered['created'] and not recovered['trading_authority']
        assert loads == {saved['id']: 2}  # Capture and response use separate SQL snapshots.
        assert len(queries) < 1000


def selected_record(saved, kind):
    if kind == 'HEADER':
        return archive._header, m.RealEconomicAcceptance, saved['id']
    if kind == 'BOOKING':
        return bookings.booking_record, m.RealDexExternalBooking, saved['booking_ids'][0]
    return groups.group_record, m.RealDexExternalAcceptanceGroup, saved['group_id']


@pytest.mark.parametrize('kind', ['HEADER', 'BOOKING', 'GROUP'])
@pytest.mark.parametrize('mutation', ['ORM', 'RAW_SQL', 'COMMIT'])
def test_modified_record_cannot_reuse_its_authenticated_payload(tmp_path, kind, mutation):
    with prepared_case(tmp_path) as case:
        saved = case.confirm()
        load, model, identifier = selected_record(saved, kind)
        with case.vault.session() as db:
            archive._snapshot(db)
            with scope(db):
                first = load(db, identifier)
                assert first is not None
                if mutation == 'ORM':
                    first[0].payload_digest = '0' * 64
                else:
                    db.connection().exec_driver_sql(
                        f'UPDATE {model.__tablename__} SET payload_digest=? WHERE id=?',
                        ('0' * 64, identifier))
                    if mutation == 'COMMIT':
                        db.commit()
                        archive._snapshot(db)
                with pytest.raises(ValueError):
                    load(db, identifier)
            assert KEY not in db.info


@pytest.mark.parametrize('kind', ['HEADER', 'BOOKING', 'GROUP'])
@pytest.mark.parametrize('journal_mode', ['DELETE', 'WAL'])
def test_raw_restart_cannot_hide_other_connections_modified_record(tmp_path, kind, journal_mode):
    with prepared_case(tmp_path) as case:
        saved = case.confirm()
        load, model, identifier = selected_record(saved, kind)
        with case.vault.session() as db:
            assert db.connection().exec_driver_sql('PRAGMA journal_mode=' + journal_mode).scalar_one() == journal_mode.lower()
            archive._snapshot(db)
            with scope(db):
                first = load(db, identifier)  # Keep the ORM object alive across raw SQL.
                connection = db.connection()
                driver = connection.connection.driver_connection
                transaction, changes = db.get_transaction(), driver.total_changes
                connection.exec_driver_sql('ROLLBACK')
                other = create_engine(db.get_bind().url, connect_args={'timeout': 5})
                try:
                    with other.begin() as writer:
                        writer.execute(update(model).where(model.id == identifier).values(payload_digest='0' * 64))
                finally:
                    other.dispose()
                connection.exec_driver_sql('BEGIN')
                assert db.get_transaction() is transaction and driver.total_changes == changes
                with pytest.raises(ValueError):
                    load(db, identifier)
                assert first[0] is db.get(model, identifier)


@pytest.mark.parametrize('kind', ['HEADER', 'BOOKING', 'GROUP'])
def test_payload_edits_are_private_and_rollback_recovers_originals(tmp_path, kind):
    with prepared_case(tmp_path) as case:
        saved = case.confirm()
        load, model, identifier = selected_record(saved, kind)
        with case.vault.session() as db:
            archive._snapshot(db)
            with scope(db):
                first = load(db, identifier)
                original = first[1]['schema']
                first[1]['schema'] = 'consumer mutation'
                with scope(db):
                    assert load(db, identifier)[1]['schema'] == original
                db.connection().exec_driver_sql(
                    f'UPDATE {model.__tablename__} SET payload_digest=? WHERE id=?',
                    ('0' * 64, identifier))
                with pytest.raises(ValueError):
                    load(db, identifier)
                db.rollback()
                archive._snapshot(db)
                assert load(db, identifier)[1]['schema'] == original
            assert KEY not in db.info


@pytest.mark.parametrize('version', ['user_version', 'schema_version'])
def test_changed_schema_reauthenticates_even_when_rows_did_not_change(tmp_path, monkeypatch, version):
    with prepared_case(tmp_path) as case:
        saved = case.confirm()
        calls = []
        original = archive._read_header

        def header(*args):
            calls.append(1)
            return original(*args)

        monkeypatch.setattr(archive, '_read_header', header)
        with case.vault.session() as db:
            archive._snapshot(db)
            with scope(db):
                first = archive._header(db, saved['id'])
                old = db.connection().exec_driver_sql('PRAGMA ' + version).scalar_one()
                db.connection().exec_driver_sql(f'PRAGMA {version}={old + 1}')
                assert archive._header(db, saved['id'])[1] == first[1]
                assert len(calls) == 2
                archive._header(db, saved['id'])
                assert len(calls) == 2


def test_no_fact_survives_a_scope_or_unstarted_sql_transaction(tmp_path, monkeypatch):
    with prepared_case(tmp_path) as case:
        saved = case.confirm()
        calls = []
        original = archive._read_header

        def header(*args):
            calls.append(1)
            return original(*args)

        monkeypatch.setattr(archive, '_read_header', header)
        with case.vault.session() as db:
            with scope(db):
                archive._header(db, saved['id'])
                archive._header(db, saved['id'])
                assert len(calls) == 2
            archive._snapshot(db)
            with scope(db):
                archive._header(db, saved['id'])
                archive._header(db, saved['id'])
                assert len(calls) == 3
            assert KEY not in db.info
            archive._header(db, saved['id'])
            assert len(calls) == 4


@pytest.mark.parametrize('dependency', [
    'OBSERVATION', 'ORDER', 'SALT', 'EVENT', 'BOOKING_MEMBER', 'BOOKING_ACCOUNT', 'GROUP_LINK', 'ACCEPTANCE_MEMBER',
])
def test_changed_dependency_invalidates_the_entire_cached_group(tmp_path, dependency):
    with prepared_case(tmp_path) as case:
        saved = case.confirm()
        edits = {
            'OBSERVATION': (m.RealDexExternalObservation, 'payload_digest', '0' * 64),
            'ORDER': (m.RealDexExternalOrderProof, 'payload_digest', '0' * 64),
            'SALT': (m.RealWalletIdentitySalt, 'salt_hex', '01' * 32),
            'EVENT': (m.RealEvent, 'note', 'Original modificado'),
        }
        deletions = {
            'BOOKING_MEMBER': m.RealDexExternalBookingMember,
            'BOOKING_ACCOUNT': m.RealDexExternalBookingAccount,
            'GROUP_LINK': m.RealDexExternalGroupLink,
            'ACCEPTANCE_MEMBER': m.RealEconomicAcceptanceAccount,
        }
        with case.vault.session() as db:
            archive._snapshot(db)
            with scope(db):
                first = groups.group_record(db, saved['group_id'])
                assert first is not None
                if dependency in edits:
                    model, column, value = edits[dependency]
                    db.connection().exec_driver_sql(f'UPDATE {model.__tablename__} SET {column}=?', (value,))
                else:
                    db.connection().exec_driver_sql('DELETE FROM ' + deletions[dependency].__tablename__)
                with pytest.raises(ValueError):
                    groups.group_record(db, saved['group_id'])
