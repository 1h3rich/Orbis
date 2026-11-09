"""Atomic joint recovery using encrypted storage and two signed EVM receipts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session

from app.portfolio import models as m,economic_acceptance as archive
from app.portfolio import economic_external_group_acceptance as acceptance,economic_external_group as groups
from app.portfolio import economic_external_acceptance as single,economic_external_booking as booking
from app.portfolio.economic_external_batch import ExternalDeclaration,prepare_external_batch
from app.portfolio.economic_projection import ProjectionError
from app.portfolio.repository import account_snapshot,append_event,create_account
from app.portfolio.domain import EventInput
from app.portfolio.vault import RealVault,VaultError,SCHEMA_VERSION
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from app.portfolio.wallet_funding import _encoded
from tests.external_batch_fixture import batch_case
from tests.test_wallet_swap import PASSWORD


def rows(vault):
    with vault.session()as db:
        return tuple(db.scalar(select(func.count()).select_from(cls))for cls in
            (m.RealEvent,m.RealDexExternalBooking,m.RealDexExternalBookingMember,m.RealEconomicAcceptance,
             m.RealDexExternalAcceptanceLink,m.RealDexExternalAcceptanceGroup,m.RealDexExternalGroupLink))


@contextmanager
def prepared_case(path,**options):
    with batch_case(path,**options)as c:
        plan=prepare_external_batch(tuple(ExternalDeclaration(**item)for item in c.declarations),
            batch_id=str(uuid4()),root_account_id=c.account,recorded_at=c.recorded_at)
        wallets=(c.meta['owner'],c.meta['operator']);before=rows(c.vault)
        prepared=acceptance.prepare_group(c.vault,c.inputs,plan,capture=c.recapture,
            generation=c.vault.session_generation,selection_digest=plan.digest,wallet_addresses=wallets)
        assert rows(c.vault)==before
        def confirm(*,vault=None,**changes):
            target=vault or c.vault
            values=dict(capture=c.recapture,preview_digest=prepared.digest,now=c.recorded_at+timedelta(seconds=1),
                generation=target.session_generation,wallet_addresses=wallets)
            values.update(changes)
            return acceptance.confirm_group(target,prepared,**values)
        yield SimpleNamespace(**vars(c),prepared=prepared,confirm=confirm,before=before,wallets=wallets)


@pytest.mark.parametrize('mode',['CASH','ASSET','MIXED','SHARED'])
def test_joint_commit_keeps_per_receipt_gas_and_cold_recovery_without_duplicate_originals(tmp_path,mode):
    with prepared_case(tmp_path,asset=mode!='CASH',later_sale=mode=='MIXED',shared_custody=mode=='SHARED')as c:
        result=c.confirm();count=2 if mode=='SHARED'else 6
        assert result['created']and result['joint_review']and result['events_created']==count
        assert not result['trading_authority']and not result['strategy_executed']and not result['resolves_intent']
        assert rows(c.vault)==(c.before[0]+count,2,count,1,0,1,2)
        with c.vault.session()as db:
            captured=archive.capture_acceptance(db,result['id'])
            assert json.loads(c.prepared.draft_json)['schema']==archive.EXTERNAL_GROUP_DRAFT
            left,right=account_snapshot(db,c.account),account_snapshot(db,c.peer)
            if mode=='CASH':assert left.cash['USDT']==D('999.997')and right.cash['USDT']==D('0.003')
            elif mode=='SHARED':assert left.positions['WBNB','USDT'].cost_basis==D(100)
            elif mode=='MIXED':
                position=right.positions['WBNB','USDT']
                assert(position.quantity,position.cost_basis,position.realized_pnl)==(D('0.006'),D('0.9'),D('0.3'))
            else:assert left.positions['WBNB','USDT'].cost_basis==D('99.7')and right.positions['WBNB','USDT'].cost_basis==D('0.3')
            for entry in c.entries:
                assert_wallet_nonce_available(db,entry.current.meta['owner'],str(entry.current.source.nonce+1))
                source=db.get(m.RealWalletSwapIntent,entry.current.source.id)
                assert source.status in('WALLET_REQUESTED','HASH_REPORTED')
        assert archive.replay_acceptance(captured).projection==c.prepared.plan.projection
        assert not c.confirm()['created']and rows(c.vault)==(c.before[0]+count,2,count,1,0,1,2)
        path=c.vault.backup(tmp_path/'group-cold.db');assert path.read_bytes()[:16]!=b'SQLite format 3\x00'
        cold=RealVault(path);cold.unlock(PASSWORD)
        try:
            restored=acceptance.recover_group(cold,result['group_id'],root_account_id=c.account,selection_digest=c.prepared.plan.digest)
            assert restored['id']==result['id']and not restored['created']
            for entry in c.entries:
                recovered=single.recover_external_acceptance(cold,entry.observation_id,context_account_id=entry.current.account)
                assert recovered['id']==result['id']and recovered['joint_review']and not recovered['created']
        finally:cold.lock()


@pytest.mark.parametrize('failure',['COMMIT','CRITERION','GROUP','MEMBER'])
def test_joint_failure_rolls_back_all_sources_originals_and_links(tmp_path,monkeypatch,failure):
    with prepared_case(tmp_path)as c:
        if failure=='COMMIT':monkeypatch.setattr(Session,'commit',lambda db:(_ for _ in ()).throw(OSError('owned failure')))
        elif failure=='CRITERION':monkeypatch.setattr(archive,'_store_reviewed_draft',lambda *a,**kw:(_ for _ in ()).throw(OSError('owned failure')))
        else:
            model=m.RealDexExternalAcceptanceGroup if failure=='GROUP'else m.RealDexExternalGroupLink
            original=Session.flush
            def flush(db,*args,**kw):
                if any(isinstance(item,model)for item in db.new):raise OSError('owned failure')
                return original(db,*args,**kw)
            monkeypatch.setattr(Session,'flush',flush)
        with pytest.raises(OSError):c.confirm()
        assert rows(c.vault)==c.before


def test_two_connections_accept_the_whole_group_exactly_once(tmp_path):
    with prepared_case(tmp_path)as c:
        second=RealVault(c.vault.path);second.unlock(PASSWORD)
        try:
            with ThreadPoolExecutor(max_workers=2)as pool:
                replies=[pool.submit(c.confirm),pool.submit(c.confirm,vault=second)]
                values=[item.result(timeout=30)for item in replies]
            assert values[0]['id']==values[1]['id']and sorted(item['created']for item in values)==[False,True]
        finally:second.lock()
        assert rows(c.vault)==(c.before[0]+6,2,6,1,0,1,2)


@pytest.mark.parametrize('failure',['STALE','DIGEST','EXPIRED'])
def test_changed_or_expired_preview_cannot_write_any_member(tmp_path,failure):
    with prepared_case(tmp_path)as c:
        changes={}
        if failure=='STALE':
            with c.vault.session()as db:append_event(db,c.account,str(uuid4()),EventInput('CONTRIBUTION','USDT',c.recorded_at,amount=D(1)))
        elif failure=='DIGEST':changes['preview_digest']='0'*64
        else:changes['now']=c.recorded_at+timedelta(minutes=16)
        before=rows(c.vault)
        with pytest.raises(ProjectionError):c.confirm(**changes)
        assert rows(c.vault)==before


@pytest.mark.parametrize('change',['GROUP','MEMBER','MISSING_GROUP'])
def test_changed_public_hash_or_removed_joint_links_cannot_pass_backup_verification(tmp_path,change):
    with prepared_case(tmp_path)as c:
        result=c.confirm()
        with c.vault.session()as db:
            if change=='MISSING_GROUP':
                db.query(m.RealDexExternalGroupLink).delete();db.query(m.RealDexExternalAcceptanceGroup).delete()
            else:
                row=db.get(m.RealDexExternalAcceptanceGroup,result['group_id'])if change=='GROUP'else db.get(m.RealDexExternalGroupLink,result['booking_ids'][0])
                body=json.loads(row.payload_json)
                if change=='GROUP':body['members'][0]['booking_digest']='0'*64
                else:body['ordinal']=999
                row.payload_json,row.payload_digest=_encoded(body)
            db.commit()
        with pytest.raises((VaultError,ProjectionError)):c.vault.backup(tmp_path/'rejected.db')


def test_v78_migration_preserves_encrypted_sources_and_can_accept_joint_receipts(tmp_path):
    with prepared_case(tmp_path)as c:
        with c.vault.session()as db:
            originals=tuple(db.execute(select(m.RealEvent.id,m.RealEvent.payload_digest)).all())
            observed=tuple(db.execute(select(m.RealDexExternalObservation.id,m.RealDexExternalObservation.payload_json)).all())
            for model in(m.RealDexExternalGroupLink,m.RealDexExternalAcceptanceGroup):
                assert db.scalar(select(func.count()).select_from(model))==0
                model.__table__.drop(db.connection())
            db.connection().exec_driver_sql('PRAGMA user_version=78');db.commit()
        c.vault.lock();c.vault.unlock(PASSWORD)
        preimage=RealVault(c.vault.last_migration_backup);preimage.unlock(PASSWORD,allow_legacy_for_verification=True)
        try:
            with preimage.session()as db:
                assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==78
                assert tuple(db.execute(select(m.RealDexExternalObservation.id,m.RealDexExternalObservation.payload_json)).all())==observed
        finally:preimage.lock()
        with c.vault.session()as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
            assert tuple(db.execute(select(m.RealEvent.id,m.RealEvent.payload_digest)).all())==originals
        # Unlock rotates the generation; the pure selection is unchanged.
        assert c.confirm()['created']


@pytest.mark.parametrize('matching',['CONTEXT','ALIAS'])
def test_another_pending_wallet_in_attested_custody_still_blocks_joint_acceptance(tmp_path,matching):
    from app.portfolio.economic_external_capture import capture_external_effects,WalletParticipant
    from app.portfolio.economic_operational_guard import AccountingOperationActive
    from app.portfolio.wallet_links import save_wallet_link
    from tests.test_wallet_swap import prepare,claim
    with batch_case(tmp_path)as c:
        wallet='0x'+'34'*20
        with c.vault.session()as db:
            custody=create_account(db,'Tercera custodia','USDT').id
            pending_context=create_account(db,'Contexto pendiente','USDT').id
            proof_context=pending_context if matching=='CONTEXT'else create_account(db,'Contexto de prueba','USDT').id
            save_wallet_link(db,custody,wallet,'0x38','Tercera');db.commit()
        pending=prepare(c.vault,pending_context,c.recorded_at,wallet_address=wallet,nonce='0')
        assert claim(c.vault,pending_context,c.recorded_at,pending,wallet_address=wallet)['claim_granted']
        def capture(db):
            return tuple(capture_external_effects(db,'WALLET_SWAP',entry.current.source.id,
                context_account_id=entry.current.account,owner_address=entry.current.meta['owner'],wallets=(
                    WalletParticipant(c.entries[1-index].current.account,c.entries[1-index].current.meta['owner']),
                    WalletParticipant(proof_context,wallet)))for index,entry in enumerate(c.entries))
        with c.vault.session()as db:inputs=capture(db)
        declarations=tuple(ExternalDeclaration(**{**item,'inputs':captured})for item,captured in zip(c.declarations,inputs,strict=True))
        plan=prepare_external_batch(declarations,batch_id=str(uuid4()),root_account_id=c.account,recorded_at=c.recorded_at)
        wallets=(c.meta['owner'],c.meta['operator'],wallet)
        prepared=acceptance.prepare_group(c.vault,inputs,plan,capture=capture,generation=c.vault.session_generation,
            selection_digest=plan.digest,wallet_addresses=wallets)
        operating=json.loads(prepared.staged.operating_json)
        assert not operating['can_accept']and {'code':'WALLET_PENDING','kind':'SWAP','id':pending['id']}in operating['blockers']
        before=rows(c.vault)
        with pytest.raises(AccountingOperationActive):acceptance.confirm_group(c.vault,prepared,capture=capture,
            preview_digest=prepared.digest,now=c.recorded_at+timedelta(seconds=1),generation=c.vault.session_generation,wallet_addresses=wallets)
        assert rows(c.vault)==before


def test_later_accounting_criterion_keeps_both_first_acceptances_and_causal_gas(tmp_path):
    from app.portfolio.dex_external_outcomes import external_outcome
    with prepared_case(tmp_path)as c:
        saved=c.confirm();accepted_at=c.recorded_at+timedelta(seconds=1)
        with c.vault.session()as db:
            witnesses=[booking.archived_witness(db,item.observation_id)for item in c.entries]
            for entry in c.entries:
                assert external_outcome(db,'WALLET_SWAP',entry.current.source.id,as_of=accepted_at-timedelta(microseconds=1))is None
                fact=external_outcome(db,'WALLET_SWAP',entry.current.source.id,as_of=accepted_at)
                assert fact.acceptance_id==saved['id']and fact.gas_fee_wei==int(entry.proof['receipt']['gas_fee_wei'])
        preview=archive.preview_acceptance(c.vault,c.account,witnesses,wallet_addresses=c.wallets)
        later=archive.confirm_acceptance(c.vault,c.account,witnesses,wallet_addresses=c.wallets,
            preview_digest=preview['preview_digest'],now=c.recorded_at+timedelta(seconds=3))
        assert later['created']and later['id']!=saved['id']
        restored=acceptance.recover_group(c.vault,saved['group_id'],root_account_id=c.account)
        assert restored['id']==saved['id']and not restored['is_current']
        with c.vault.session()as db:
            for entry in c.entries:assert external_outcome(db,'WALLET_SWAP',entry.current.source.id).acceptance_id==saved['id']
        c.vault.backup(tmp_path/'later-criterion.db')


def test_recovery_rejects_foreign_ledger_or_changed_selection(tmp_path):
    with prepared_case(tmp_path)as c:
        saved=c.confirm();before=rows(c.vault)
        with pytest.raises(ProjectionError):acceptance.recover_group(c.vault,saved['group_id'],root_account_id=c.peer)
        with pytest.raises(ProjectionError):acceptance.recover_group(c.vault,saved['group_id'],root_account_id=c.account,selection_digest='0'*64)
        assert rows(c.vault)==before


def test_v78_existing_single_acceptance_is_unchanged_by_joint_storage_migration(tmp_path):
    from tests.test_economic_external_acceptance import prepared_case as single_case
    with single_case(tmp_path)as c:
        saved=c.confirm()
        with c.vault.session()as db:
            original=db.get(m.RealEconomicAcceptance,saved['id']).payload_json
            link=db.scalar(select(m.RealDexExternalAcceptanceLink)).payload_json
            for model in(m.RealDexExternalGroupLink,m.RealDexExternalAcceptanceGroup):
                assert db.scalar(select(func.count()).select_from(model))==0
                model.__table__.drop(db.connection())
            db.connection().exec_driver_sql('PRAGMA user_version=78');db.commit()
        c.vault.lock();c.vault.unlock(PASSWORD)
        with c.vault.session()as db:
            assert db.get(m.RealEconomicAcceptance,saved['id']).payload_json==original
            assert db.scalar(select(m.RealDexExternalAcceptanceLink)).payload_json==link
        restored=single.recover_external_acceptance(c.vault,c.prepared.captured.observation_id,context_account_id=c.account)
        assert restored['id']==saved['id']and not restored.get('joint_review',False)
        c.vault.backup(tmp_path/'single-after-migration.db')
