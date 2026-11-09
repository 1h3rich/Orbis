"""Atomic external financial recovery with actual signed receipts and SQLCipher."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session

from app.portfolio import economic_external_acceptance as acceptance,economic_external_booking as booking,economic_acceptance as archive
from app.portfolio import models as m
from app.portfolio.economic_external_capture import capture_external_effects,WalletParticipant
from app.portfolio.economic_external_api import ExternalReview,_prepare
from app.portfolio.economic_projection import ProjectionError
from app.portfolio.economic_projection_review import EXTERNAL_RECEIPTS
from app.portfolio.repository import account_snapshot,append_event
from app.portfolio.domain import EventInput
from app.portfolio.vault import RealVault,VaultError
from app.portfolio.wallet_funding import _encoded,_decoded
from tests.test_economic_external_api import effects_case
from tests.test_economic_receipt_source import counts
from tests.test_wallet_swap import PASSWORD


def rows(vault):
    with vault.session()as db:
        return tuple(db.scalar(select(func.count()).select_from(cls))for cls in
            (m.RealEvent,m.RealDexExternalBooking,m.RealDexExternalBookingMember,m.RealDexExternalBookingAccount,m.RealEconomicAcceptance,m.RealDexExternalAcceptanceLink))


@contextmanager
def prepared_case(path,*,mode='TRANSFER'):
    with effects_case(path,mode=mode)as c:
        body=ExternalReview(**c.body_review)
        wallets=tuple(WalletParticipant(item.context_account_id,item.address)for item in body.wallets)
        def capture(db):return capture_external_effects(db,'WALLET_SWAP',c.source.id,context_account_id=c.account,owner_address=body.address,wallets=wallets)
        with c.vault.session()as db:captured=capture(db)
        stamp=max(datetime.now(timezone.utc),c.now+timedelta(seconds=2))
        plan=_prepare(captured,body,stamp);before=rows(c.vault)
        generation=c.vault.session_generation
        prepared=acceptance.prepare_external_acceptance(c.vault,captured,plan,capture=capture,generation=generation,selection_digest=plan.digest)
        assert rows(c.vault)==before
        def confirm(*,vault=None,**changes):
            values=dict(capture=capture,preview_digest=prepared.digest,now=max(datetime.now(timezone.utc),stamp)+timedelta(seconds=1),
                generation=(vault or c.vault).session_generation,wallet_addresses=(c.meta['owner'],c.meta['operator']))
            values.update(changes)
            return acceptance.confirm_external_acceptance(vault or c.vault,prepared,**values)
        yield SimpleNamespace(**vars(c),prepared=prepared,capture=capture,confirm=confirm,before_rows=before,stamp=stamp)


def test_manual_transfer_acceptance_is_one_commit_and_cold_replay_keeps_both_wallets(tmp_path):
    with prepared_case(tmp_path)as c:
        result=c.confirm();assert result['created']and result['saved']and result['accounting_method_active']
        assert result['events_created']==3 and result['is_current']and result['external_review_accepted']
        assert not result['strategy_executed']and not result['resolves_intent']and not result['moves_funds']
        assert rows(c.vault)==(c.before_rows[0]+3,1,3,2,1,1)
        with c.vault.session()as db:
            left=account_snapshot(db,c.ledger);right=account_snapshot(db,c.peer)
            assert left.cash['USDT']==D('9.995')and right.cash['USDT']==D('3.005')
            assert left.contributions['USDT']==10 and right.contributions['USDT']==3
            archived=archive.capture_acceptance(db,result['id']);assert archived.inputs.source_policy==EXTERNAL_RECEIPTS
        assert archive.replay_acceptance(archived).projection.engine=='portfolio-economic-order-3.0'
        assert c.confirm()['created']is False
        with c.vault.session()as db:
            append_event(db,c.peer,str(uuid4()),EventInput('CONTRIBUTION','USDT',c.at+timedelta(days=1),amount=D('1')))
        assert c.confirm()['created']is False
        copied=c.vault.backup(tmp_path/'cold.db');assert copied.read_bytes()[:16]!=b'SQLite format 3\x00'
        cold=RealVault(copied);cold.unlock(PASSWORD)
        try:
            restored=acceptance.recover_external_acceptance(cold,c.prepared.captured.observation_id,context_account_id=c.account)
            assert restored['id']==result['id']and not restored['created']
            with cold.session()as db:
                assert account_snapshot(db,c.peer).cash['USDT']==D('4.005')
        finally:cold.lock()


@pytest.mark.parametrize('failure',['COMMIT','CRITERION','LINK'])
def test_failure_at_each_boundary_leaves_no_original_booking_or_criterion(tmp_path,monkeypatch,failure):
    with prepared_case(tmp_path)as c:
        before=rows(c.vault)
        if failure=='COMMIT':monkeypatch.setattr(Session,'commit',lambda db:(_ for _ in ()).throw(OSError('owned failure')))
        elif failure=='CRITERION':monkeypatch.setattr(archive,'_store_reviewed_draft',lambda *a,**kw:(_ for _ in ()).throw(OSError('owned failure')))
        else:
            original=Session.flush
            def flush(db,*args,**kwargs):
                if any(isinstance(item,m.RealDexExternalAcceptanceLink)for item in db.new):raise OSError('owned failure')
                return original(db,*args,**kwargs)
            monkeypatch.setattr(Session,'flush',flush)
        with pytest.raises(OSError):c.confirm()
        assert rows(c.vault)==before


def test_two_connections_commit_once_and_recover_the_same_exact_prefix(tmp_path):
    with prepared_case(tmp_path)as c:
        second=RealVault(c.vault.path);second.unlock(PASSWORD)
        try:
            with ThreadPoolExecutor(max_workers=2)as pool:
                a=pool.submit(c.confirm);b=pool.submit(c.confirm,vault=second)
                replies=[a.result(timeout=20),b.result(timeout=20)]
            assert replies[0]['id']==replies[1]['id']and sorted(item['created']for item in replies)==[False,True]
        finally:second.lock()
        assert rows(c.vault)[1:]==(1,3,2,1,1)


def test_following_accounting_review_keeps_external_sources_and_cold_lineage(tmp_path):
    from app.portfolio.economic_projection_review import review_projection,TRADES
    from dataclasses import replace
    with prepared_case(tmp_path)as c:
        first=c.confirm()
        with c.vault.session()as db:witness=booking.archived_witness(db,c.prepared.captured.observation_id)
        preview=archive.preview_acceptance(c.vault,c.ledger,[witness],wallet_addresses=(c.meta['owner'],))
        assert preview['draft']['schema']==archive.EXTERNAL_DRAFT
        second=archive.confirm_acceptance(c.vault,c.ledger,[witness],wallet_addresses=(c.meta['owner'],),
            preview_digest=preview['preview_digest'],now=c.stamp+timedelta(seconds=10))
        assert second['created']and second['id']!=first['id']
        restored=acceptance.recover_external_acceptance(c.vault,c.prepared.captured.observation_id,context_account_id=c.account)
        assert not restored['is_current']and restored['id']==first['id']
        with c.vault.session()as db:
            current=archive.capture_acceptance(db,second['id'])
            assert account_snapshot(db,c.peer).cash['USDT']==D('3.005')
        with pytest.raises(ProjectionError):review_projection(replace(current.inputs,source_policy=TRADES))
        c.vault.backup(tmp_path/'two-criteria.db')


def test_v77_migration_preserves_originals_observation_order_and_encrypted_preimage(tmp_path):
    from tests.external_financial_fixture import drop_external_financial_tables
    from app.portfolio.vault import SCHEMA_VERSION
    with prepared_case(tmp_path)as c:
        with c.vault.session()as db:
            observed=db.get(m.RealDexExternalObservation,c.prepared.captured.observation_id).payload_json
            order=db.get(m.RealDexExternalOrderProof,c.prepared.captured.order_id).payload_json
            drop_external_financial_tables(db);db.connection().exec_driver_sql('PRAGMA user_version=77');db.commit()
        c.vault.lock();c.vault.unlock(PASSWORD)
        preimage=RealVault(c.vault.last_migration_backup);preimage.unlock(PASSWORD,allow_legacy_for_verification=True)
        try:
            with preimage.session()as db:
                assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==77
                assert db.get(m.RealDexExternalObservation,c.prepared.captured.observation_id).payload_json==observed
        finally:preimage.lock()
        with c.vault.session()as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
            assert db.get(m.RealDexExternalOrderProof,c.prepared.captured.order_id).payload_json==order
        assert c.confirm()['created']


def test_actual_zero_fee_permission_receipt_accepts_empty_prefix_without_fake_capital(tmp_path):
    from tests.test_economic_external_effects import case
    from tests.test_dex_external_order_journal import save
    from app.portfolio.wallet_links import save_wallet_link
    with case(tmp_path,'APPROVAL',zero_gas=True)as c:
        save(c)
        with c.vault.session()as db:save_wallet_link(db,c.account,c.meta['owner'],'0x38','Principal');db.commit()
        def capture(db):return capture_external_effects(db,'WALLET_SWAP',c.source.id,context_account_id=c.account,owner_address=c.meta['owner'])
        with c.vault.session()as db:inputs=capture(db)
        body=ExternalReview(address=c.meta['owner'],review_id=str(uuid4()),annotations=[
            {'reference':item.reference,'disposition':item.disposition,'note':item.note}for item in c.annotations])
        stamp=max(datetime.now(timezone.utc),inputs.not_before)+timedelta(seconds=1)
        plan=_prepare(inputs,body,stamp)
        prepared=acceptance.prepare_external_acceptance(c.vault,inputs,plan,capture=capture,
            generation=c.vault.session_generation,selection_digest=plan.digest)
        result=acceptance.confirm_external_acceptance(c.vault,prepared,capture=capture,preview_digest=prepared.digest,
            now=stamp+timedelta(seconds=1),generation=c.vault.session_generation,wallet_addresses=(c.meta['owner'],))
        assert result['events_created']==0 and rows(c.vault)==(0,1,0,1,1,1)
        with c.vault.session()as db:
            assert account_snapshot(db,c.account).cash=={}
            archived=archive.capture_acceptance(db,result['id'])
        assert archive.replay_acceptance(archived).body['events_count']==0
        c.vault.backup(tmp_path/'zero-events.db')


def test_later_actual_known_receipt_batch_preserves_external_claims_and_both_accounts(tmp_path):
    from tests.test_dex_call_resolution import prepare
    from tests.test_dex_operator import write
    from app.live_execution.dex_call_resolution import collect_call_resolution
    from app.live_execution.dex_receipt_order import collect_order
    from app.portfolio import wallet_swap as swaps,dex_receipt_order as orders,economic_receipt_acceptance as batches
    from app.portfolio.economic_receipt_source import ReceiptSelection
    with prepared_case(tmp_path)as c:
        first=c.confirm();rpc=c.rpc;meta={**c.meta,'owner':c.meta['operator']}
        rpc('evm_increaseTime',[5]);rpc('evm_mine',[])
        write(rpc,meta,meta['usdt'],'mint(address,uint256)',['address','uint256'],[meta['owner'],10**18])
        write(rpc,meta,meta['usdt'],'approve(address,uint256)',['address','uint256'],[meta['router'],10**18])
        with c.vault.session()as db:
            append_event(db,c.peer,str(uuid4()),EventInput('CONTRIBUTION','BNB',c.at+timedelta(seconds=1),amount=D('0.01')))
        planned,source,_=prepare(c.vault,c.peer,meta,rpc)
        sent=rpc('eth_sendTransaction',[planned['transaction']]);evidence=collect_call_resolution(rpc,source,sent)
        block_at=datetime.fromtimestamp(int(evidence['block']['timestamp'],16),timezone.utc)
        stamp=max(datetime.now(timezone.utc),block_at,c.stamp+timedelta(seconds=3))+timedelta(seconds=1)
        with c.vault.session()as db:swaps.report_swap(db,source.id,account_id=c.peer,tx_hash=sent,now=stamp)
        legacy={key:evidence[key]for key in ('chain_id','tx_hash','transaction','receipt','block','finalized','pool','post_allowance')}
        with c.vault.session()as db:swaps.check_swap(db,source.id,account_id=c.peer,evidence=legacy,now=stamp)
        arguments=dict(context_account_id=c.peer,wallet_address=meta['owner'],evidence=collect_order(rpc,sent),now=stamp)
        with c.vault.session()as db:review=orders.preview_order(db,'WALLET_SWAP',source.id,**arguments)
        with c.vault.session()as db:orders.confirm_order(db,'WALLET_SWAP',source.id,preview_digest=review['preview_digest'],**arguments)
        selected=(ReceiptSelection('WALLET_SWAP',source.id,c.peer,meta['owner']),)
        batch_id=str(uuid4());recorded_at=stamp+timedelta(seconds=1)
        proposed=batches.preview_receipt_acceptance(c.vault,c.ledger,selected,batch_id=batch_id,
            recorded_at=recorded_at,wallet_addresses=(c.meta['owner'],meta['owner']))
        assert proposed['draft']['schema']==archive.EXTERNAL_BATCH_DRAFT
        assert len(proposed['draft']['review']['witnesses'])==2
        saved=batches.confirm_receipt_acceptance(c.vault,c.ledger,selected,batch_id=batch_id,
            recorded_at=recorded_at,preview_digest=proposed['preview_digest'],now=recorded_at+timedelta(seconds=1),
            wallet_addresses=(c.meta['owner'],meta['owner']))
        assert saved['created']and saved['events_created']==2
        with c.vault.session()as db:
            assert account_snapshot(db,c.peer).cash['USDT']==D('2.995')
            assert account_snapshot(db,c.peer).positions['WBNB','USDT'].quantity==D('0.01')
            assert account_snapshot(db,c.ledger).cash['USDT']==D('9.995')
        assert not acceptance.recover_external_acceptance(c.vault,c.prepared.captured.observation_id,context_account_id=c.account)['is_current']
        c.vault.backup(tmp_path/'known-after-external.db')


@pytest.mark.parametrize('change',['ORIGINAL','CUSTODY','REVIEW','LINK','HIDDEN'])
def test_resealed_outer_data_cannot_hide_changed_originals_custody_or_missing_link(tmp_path,change):
    with prepared_case(tmp_path)as c:
        result=c.confirm()
        with c.vault.session()as db:
            row=db.get(m.RealDexExternalBooking,result['booking_id']);body=_decoded(row.payload_json,row.payload_digest)
            if change=='ORIGINAL':db.get(m.RealEvent,body['plan']['created_events'][0]['id']).note='Editada'
            elif change=='CUSTODY':body['plan']['custody'][0]['account_id']=c.account
            elif change=='REVIEW':body['plan']['review']['gas_fee_wei']='1'
            elif change=='LINK':db.delete(db.get(m.RealDexExternalAcceptanceLink,row.id))
            else:db.connection().exec_driver_sql('PRAGMA user_version=77')
            if change in('CUSTODY','REVIEW'):
                body['plan_digest']=_encoded(body['plan'])[1];body['seal']=booking.seal(db,{k:v for k,v in body.items()if k!='seal'})
                row.payload_json,row.payload_digest=_encoded(body)
            db.commit()
        with pytest.raises((ProjectionError,VaultError)):
            if change=='HIDDEN':c.vault.backup(tmp_path/'hidden.db')
            else:acceptance.recover_external_acceptance(c.vault,c.prepared.captured.observation_id,context_account_id=c.account)
        with pytest.raises(VaultError):c.vault.backup(tmp_path/('bad-'+change+'.db'))
