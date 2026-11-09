"""Residual BNB reserves have a durable owner-only history; own EVM only."""
from contextlib import nullcontext
from datetime import timedelta
from uuid import uuid4
import json

import pytest
from eth_account import Account
from eth_utils import to_checksum_address

from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_return import read_return_state,plan_return
from app.live_execution.dex_gas_funding import collect_funding_receipt
from app.portfolio import dex_gas_return as journal
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from tests.dex_bot_worker_fixture import running_case
from tests.test_dex_deployments import PASSWORD


def test_gas_return_stops_first_reserves_once_verifies_receipt_and_restores_without_addresses(tmp_path,monkeypatch):
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        def plan():return plan_return(read_return_state(rpc,meta['owner'],draft['operator_address'],now=clock[0]),
            gas_price=10**9,max_gas_wei=21000*10**9,now=clock[0])
        current=plan();identifier=str(uuid4())
        arguments=dict(identifier=identifier,account_id=account,binding_id=binding['id'],draft_id=draft['id'],
            wallet_address=meta['owner'],drafts=manager,plan=current,now=clock[0])
        with pytest.raises(DexExecutorError,match='Detén|detenido'):
            with vault.session() as db:journal.prepare_return(db,**arguments)
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0]);worker.stop()
        clock[0]+=timedelta(seconds=1);mine();current=plan();arguments.update(plan=current,now=clock[0])
        with vault.session() as db:
            prepared=journal.prepare_return(db,**arguments)
            assert prepared['status']=='PREPARED' and prepared['amount_wei']==str(current.amount_wei)
            assert not prepared['automatic_execution']
            assert journal.prepare_return(db,**arguments)['replayed']
        with vault.session() as db:
            reserved=journal.reserve_return(db,identifier,account_id=account,wallet_address=meta['owner'],
                drafts=manager,draft_id=draft['id'],plan=current,plan_digest=prepared['plan_digest'],now=clock[0])
            assert reserved['reserved']
        with vault.session() as db:
            assert not journal.reserve_return(db,identifier,account_id=account,wallet_address=meta['owner'],
                drafts=manager,draft_id=draft['id'],plan=current,plan_digest=prepared['plan_digest'],now=clock[0])['reserved']
            with pytest.raises(DexExecutorError,match='devolución'):
                assert_wallet_nonce_available(db,draft['operator_address'],str(current.snapshot.operator_nonce))
        tx=current.transaction();signable={key:to_checksum_address(value) if key=='to' else value if key=='data' else int(value,16)
            for key,value in tx.items() if key not in ('from','type')}
        with manager._copy(draft['id'],account,meta['owner'],clock[0]) as (_,key):signed=Account.sign_transaction(signable,bytes(key))
        tx_hash='0x'+signed.hash.hex()
        with vault.session() as db:
            assert journal.record_signature(db,identifier,account_id=account,plan=current,raw=bytes(signed.raw_transaction),tx_hash=tx_hash,now=clock[0])['signature_recorded']
        with vault.session() as db:
            assert journal.mark_broadcast(db,identifier,account_id=account,plan_digest=prepared['plan_digest'],tx_hash=tx_hash,now=clock[0])['broadcast_granted']
        with pytest.raises(DexExecutorError):
            with vault.session() as db:journal.mark_broadcast(db,identifier,account_id=account,plan_digest=prepared['plan_digest'],tx_hash=tx_hash,now=clock[0])
        before=int(rpc('eth_getBalance',[meta['owner'],'latest']),16)
        assert rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex()])==tx_hash
        evidence=collect_funding_receipt(rpc,tx_hash)
        with vault.session() as db:
            confirmed=journal.check_return(db,identifier,account_id=account,evidence=evidence,now=clock[0])
            assert confirmed['status']=='FINALIZED' and confirmed['receipt']['transferred_wei']==str(current.amount_wei)
            assert confirmed['receipt']['gas_fee_wei']==str(21000*10**9)
            assert journal.check_return(db,identifier,account_id=account,evidence=evidence,now=clock[0])==confirmed
            row,terms,state=journal.return_record(db,identifier)
            assert meta['owner'].lower() not in row.payload_json+row.state_json
            assert draft['operator_address'].lower() not in row.payload_json+row.state_json
            assert tx_hash not in row.payload_json+row.state_json and signed.raw_transaction.hex() not in row.payload_json+row.state_json
        assert int(rpc('eth_getBalance',[draft['operator_address'],'latest']),16)==0
        assert int(rpc('eth_getBalance',[meta['owner'],'latest']),16)-before==current.amount_wei
        from app.portfolio import dex_receipt_booking as accounting
        from app.portfolio.repository import append_event,account_snapshot
        from app.portfolio.wallet_links import save_wallet_link
        from app.portfolio.domain import EventInput
        from decimal import Decimal
        with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
        with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','BNB',clock[0]-timedelta(days=1),amount=Decimal('1')))
        with vault.session() as db:preview=accounting.preview_booking(db,'GAS_RETURN',identifier,context_account_id=account,wallet_address=meta['owner'])
        assert preview['internal_principal_bnb']==str(Decimal(current.amount_wei)/Decimal(10**18)) and preview['trade'] is None
        with vault.session() as db:
            booked=accounting.book_receipt(db,'GAS_RETURN',identifier,context_account_id=account,wallet_address=meta['owner'],
                expected_revision=preview['revision'],preview_digest=preview['preview_digest'],now=clock[0])
            assert booked['events_created']==1
            snapshot=account_snapshot(db,account)
            assert snapshot.cash['BNB']==Decimal('1')-Decimal('0.000021') and snapshot.contributions=={'BNB':Decimal('1')}
        from app.portfolio.models import RealDexGasReturnIntent
        from app.portfolio.vault import VaultError,SCHEMA_VERSION
        from app.portfolio.wallet_funding import _decoded,_encoded
        with vault.session() as db:
            row=db.get(RealDexGasReturnIntent,identifier);saved=(row.payload_json,row.payload_digest,row.state_json,row.state_digest)
            db.connection().exec_driver_sql('PRAGMA user_version=64');db.commit()
        with pytest.raises(VaultError,match='gas return'):vault.backup(tmp_path/'hidden-return.db')
        with vault.session() as db:db.connection().exec_driver_sql(f'PRAGMA user_version={SCHEMA_VERSION}');db.commit()
        for forgery in ('amount','stopped','receipt'):
            with vault.session() as db:
                row=db.get(RealDexGasReturnIntent,identifier)
                if forgery=='receipt':
                    state=_decoded(row.state_json,row.state_digest);state['receipt']['gas_fee_wei']=str(21000*10**9+1)
                    row.state_json,row.state_digest=_encoded(state)
                else:
                    terms=_decoded(row.payload_json,row.payload_digest)
                    if forgery=='amount':terms['amount_wei']=str(int(terms['amount_wei'])+1)
                    else:terms['stopped']['runs']=[]
                    row.payload_json,row.payload_digest=_encoded(terms)
                db.commit()
            with pytest.raises(DexExecutorError):
                with vault.session() as db:journal.return_record(db,identifier)
            with pytest.raises(VaultError):vault.backup(tmp_path/('forged-'+forgery+'.db'))
            with vault.session() as db:
                row=db.get(RealDexGasReturnIntent,identifier)
                row.payload_json,row.payload_digest,row.state_json,row.state_digest=saved;db.commit()
        vault.backup(tmp_path/'returned.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:
            restored=journal.view_return(db,identifier,now=clock[0]);assert restored==confirmed
            assert not restored['automatic_execution']


def test_schema_64_migration_keeps_existing_bot_and_grant_payloads_then_adds_an_empty_return_journal(tmp_path):
    from app.portfolio.vault import SCHEMA_VERSION
    from app.portfolio.models import RealDexBotRun,RealDexOperatorGrant,RealDexGasReturnIntent
    from sqlalchemy import select
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0]);worker.stop()
        with vault.session() as db:
            before=[(row.id,row.payload_json,row.payload_digest) for model in (RealDexBotRun,RealDexOperatorGrant) for row in db.scalars(select(model))]
            db.connection().exec_driver_sql('DROP TABLE real_dex_gas_return_intents')
            db.connection().exec_driver_sql('PRAGMA user_version=64');db.commit()
        vault.lock();vault.unlock(PASSWORD)
        assert vault.last_migration_backup.is_file()
        with vault.session() as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
            assert list(db.scalars(select(RealDexGasReturnIntent)))==[]
            assert before==[(row.id,row.payload_json,row.payload_digest) for model in (RealDexBotRun,RealDexOperatorGrant) for row in db.scalars(select(model))]
        vault.backup(tmp_path/'migrated-gas-return.db')


def test_competing_return_reservations_share_nonce_and_only_unsent_abandonment_releases_it(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        worker=DexBotWorker(vault,connection,run_id=started['id'],account_id=account,claim=claim,wallet_address=meta['owner'],
            market=market,rpc_factory=lambda:nullcontext(rpc),clock=lambda:clock[0]);worker.stop()
        clock[0]+=timedelta(seconds=1);mine()
        current=plan_return(read_return_state(rpc,meta['owner'],draft['operator_address'],now=clock[0]),
            gas_price=10**9,max_gas_wei=21000*10**9,now=clock[0])
        reviews=[]
        for _ in range(2):
            with vault.session() as db:reviews.append(journal.prepare_return(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],
                draft_id=draft['id'],wallet_address=meta['owner'],drafts=manager,plan=current,now=clock[0]))
        barrier=Barrier(2)
        def reserve(review):
            barrier.wait()
            try:
                with vault.session() as db:return journal.reserve_return(db,review['id'],account_id=account,wallet_address=meta['owner'],
                    drafts=manager,draft_id=draft['id'],plan=current,plan_digest=review['plan_digest'],now=clock[0])
            except DexExecutorError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(reserve,reviews))
        assert sum(item is not None and item['reserved'] for item in result)==1
        winner=next(item for item in result if item is not None);loser=next(item for item in reviews if item['id']!=winner['id'])
        with pytest.raises(DexExecutorError):
            with vault.session() as db:journal.cancel_return(db,winner['id'],account_id=account,now=clock[0])
        with vault.session() as db:
            assert journal.abandon_unsent(db,winner['id'],account_id=account,now=clock[0])['status']=='ABANDONED_UNSENT'
        with vault.session() as db:
            assert journal.reserve_return(db,loser['id'],account_id=account,wallet_address=meta['owner'],drafts=manager,
                draft_id=draft['id'],plan=current,plan_digest=loser['plan_digest'],now=clock[0])['reserved']
        with vault.session() as db:journal.abandon_unsent(db,loser['id'],account_id=account,now=clock[0])
        with vault.session() as db:
            extra=journal.prepare_return(db,identifier=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],
                wallet_address=meta['owner'],drafts=manager,plan=current,now=clock[0])
        with vault.session() as db:assert journal.cancel_return(db,extra['id'],account_id=account,now=clock[0])['status']=='CANCELLED'
        vault.backup(tmp_path/'competing-returns.db')
