"""A verified fact may be reused only inside its unchanged SQL read snapshot."""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine,delete,update

from app.portfolio import dex_external_outcomes as outcomes,economic_acceptance as archive
from app.portfolio.dex_verified_read import scope
from app.portfolio.models import RealDexExternalAcceptanceLink
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from tests.test_economic_external_acceptance import prepared_case


def counter(monkeypatch):
    calls=[];original=archive.capture_acceptance
    def capture(*args,**kwargs):
        calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(archive,'capture_acceptance',capture)
    return calls


def test_one_snapshot_reuses_full_authentication_without_caching_a_historical_absence(tmp_path,monkeypatch):
    with prepared_case(tmp_path)as c:
        c.confirm();calls=counter(monkeypatch)
        with c.vault.session()as db:
            with scope(db):
                assert outcomes.external_outcome(db,'WALLET_SWAP',c.source.id,as_of=c.stamp-timedelta(seconds=1))is None
                fact=outcomes.external_outcome(db,'WALLET_SWAP',c.source.id)
                assert fact is not None
                assert outcomes.external_outcome(db,'WALLET_SWAP',c.source.id) == fact
                assert len(calls)==1
                with scope(db,force_full=True):
                    assert outcomes.external_outcome(db,'WALLET_SWAP',c.source.id)==fact
                assert len(calls)==2
            assert outcomes.external_outcome(db,'WALLET_SWAP',c.source.id)==fact
            assert len(calls)==3  # Nothing survives the read scope.


@pytest.mark.parametrize('mutation',['ORM_DELETE','RAW_UPDATE','COMMIT_REOPEN'])
def test_changed_acceptance_never_reuses_authority_or_releases_a_nonce(tmp_path,monkeypatch,mutation):
    with prepared_case(tmp_path)as c:
        saved=c.confirm()
        with c.vault.session()as db:
            with scope(db):
                assert outcomes.external_outcome(db,'WALLET_SWAP',c.source.id)is not None
                if mutation=='ORM_DELETE':db.delete(db.get(RealDexExternalAcceptanceLink,saved['booking_id']))
                elif mutation=='RAW_UPDATE':db.execute(update(RealDexExternalAcceptanceLink).where(
                    RealDexExternalAcceptanceLink.booking_id==saved['booking_id']).values(payload_digest='0'*64))
                else:
                    db.rollback()
                    db.execute(delete(RealDexExternalAcceptanceLink).where(RealDexExternalAcceptanceLink.booking_id==saved['booking_id']))
                    db.commit()
                with pytest.raises(ValueError):outcomes.external_outcome(db,'WALLET_SWAP',c.source.id)
                with pytest.raises(ValueError):assert_wallet_nonce_available(db,c.meta['owner'],str(c.source.nonce+1))


def test_sql_schema_change_requires_a_new_authentication_even_without_changed_rows(tmp_path,monkeypatch):
    with prepared_case(tmp_path)as c:
        c.confirm();calls=counter(monkeypatch)
        with c.vault.session()as db:
            with scope(db):
                first=outcomes.external_outcome(db,'WALLET_SWAP',c.source.id)
                version=db.connection().exec_driver_sql('PRAGMA schema_version').scalar_one()
                db.connection().exec_driver_sql('PRAGMA schema_version='+str(version+1))
                assert outcomes.external_outcome(db,'WALLET_SWAP',c.source.id)==first
                assert len(calls)==2
                assert outcomes.external_outcome(db,'WALLET_SWAP',c.source.id)==first
                assert len(calls)==2


@pytest.mark.parametrize('journal_mode',['DELETE','WAL'])
def test_raw_sql_transaction_restart_cannot_hide_another_connections_corruption(tmp_path,journal_mode):
    with prepared_case(tmp_path)as c:
        saved=c.confirm()
        with c.vault.session()as db:
            assert db.connection().exec_driver_sql('PRAGMA journal_mode='+journal_mode).scalar_one().upper()==journal_mode
            with scope(db):
                assert outcomes.external_outcome(db,'WALLET_SWAP',c.source.id)is not None
                connection=db.connection();driver=connection.connection.driver_connection
                transaction=db.get_transaction();changes=driver.total_changes
                # This deliberately bypasses the normal read-only scope and
                # vault writer. Only this test's disposable SQLCipher file.
                connection.exec_driver_sql('ROLLBACK')
                other=create_engine(db.get_bind().url,connect_args={'timeout':5})
                try:
                    with other.begin()as writer:
                        assert writer.connection.driver_connection is not driver
                        writer.execute(update(RealDexExternalAcceptanceLink).where(
                            RealDexExternalAcceptanceLink.booking_id==saved['booking_id']).values(payload_digest='0'*64))
                finally:other.dispose()
                connection.exec_driver_sql('BEGIN')
                assert transaction is db.get_transaction()and driver.total_changes==changes
                with pytest.raises(ValueError):outcomes.external_outcome(db,'WALLET_SWAP',c.source.id)
                with pytest.raises(ValueError):assert_wallet_nonce_available(db,c.meta['owner'],str(c.source.nonce+1))
