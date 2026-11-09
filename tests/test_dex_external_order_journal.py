"""External chronology persists on owned SQLCipher and participates in history."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session

from app.portfolio import dex_external_order as orders,dex_external_observation as observations
from app.portfolio.dex_order_history import require_compatible_history,verify_history
from app.portfolio.models import RealDexExternalOrderProof,RealDexReceiptOrderProof,RealDexGasOrderProof
from app.portfolio.vault import RealVault,VaultError,SCHEMA_VERSION
from app.portfolio.wallet_funding import _encoded,_decoded
from tests.test_economic_external_effects import case
from tests.test_economic_receipt_source import counts
from tests.test_wallet_swap import PASSWORD


def proposed(c):
    with c.vault.session()as db:
        row,_=observations.observation_record(db,c.saved['id']);return orders.preview(row,c.certificate)


def save(c,review=None,*,vault=None,**changes):
    review=review or proposed(c)
    args=dict(account_id=c.account,proof=c.certificate,preview_digest=review['preview_digest'],now=c.now+timedelta(seconds=1))
    args.update(changes)
    with(vault or c.vault).session()as db:return orders.save_order(db,c.saved['id'],**args)


@pytest.mark.parametrize('mode',['TRANSFER','APPROVAL','NATIVE','CREATE'])
def test_certificate_persists_once_and_reopens_cold_without_changing_observation_or_events(tmp_path,mode):
    with case(tmp_path,mode)as c:
        before=counts(c.vault)
        with c.vault.session()as db:row,body=observations.observation_record(db,c.saved['id']);original=(row.payload_json,row.payload_digest)
        result=save(c)
        assert result['saved']and not result['applied_to_ledger']and not result['resolves_intent']and not result['trading_authority']
        assert result['proof']==c.certificate and result['preview_digest']==proposed(c)['preview_digest']
        assert save(c)==result and counts(c.vault)==before
        with c.vault.session()as db:
            row,body=observations.observation_record(db,c.saved['id']);assert(row.payload_json,row.payload_digest)==original
            assert observations.public_record((row,body))['status']=='EFFECTS_PENDING'
            assert db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof))==0
            assert db.scalar(select(func.count()).select_from(RealDexGasOrderProof))==0
            assert db.scalar(select(func.count()).select_from(RealDexExternalOrderProof))==1
            verify_history(db,version=SCHEMA_VERSION)
            stored=orders.order_record(db,result['id'])[0].payload_json
            for raw in(c.meta['owner'],c.meta['operator'],c.hash):assert raw.lower()not in stored.lower()
        backup=c.vault.backup(tmp_path/'cold.db');assert backup.read_bytes()[:16]!=b'SQLite format 3\x00'
        cold=RealVault(backup);cold.unlock(PASSWORD)
        try:
            with cold.session()as db:assert orders.public_record(orders.order_record(db,result['id']))==result
        finally:cold.lock()
        c.vault.lock();c.vault.unlock(PASSWORD)
        with c.vault.session()as db:assert orders.public_record(orders.order_record(db,result['id']))==result


def test_writer_rejects_wrong_review_source_account_and_invalidates_tampered_archive(tmp_path):
    with case(tmp_path)as c:
        review=proposed(c)
        for changes in({'account_id':str(uuid4())},{'preview_digest':'0'*64},{'proof':{**c.certificate,'common_tx_ref':'f'*64}}):
            with pytest.raises(ValueError):save(c,review,**changes)
        result=save(c,review)
        with c.vault.session()as db:row=orders.order_record(db,result['id'])[0];raw,digest=row.payload_json,row.payload_digest
        for key in('block_transactions_ref','common_tx_ref','observation_digest','source_context_digest'):
            with c.vault.session()as db:
                body=_decoded(raw,digest);body['proof'][key]='f'*64
                body['seal']=orders._seal(db,{k:v for k,v in body.items()if k!='seal'})
                row=db.get(RealDexExternalOrderProof,result['id']);row.payload_json,row.payload_digest=_encoded(body);db.commit()
            with pytest.raises(ValueError):
                with c.vault.session()as db:orders.order_record(db,result['id'])
            with pytest.raises(VaultError):c.vault.backup(tmp_path/('bad-'+key+'.db'))
            with c.vault.session()as db:
                row=db.get(RealDexExternalOrderProof,result['id']);row.payload_json,row.payload_digest=raw,digest;db.commit()
        c.vault.backup(tmp_path/'good.db')


def test_write_failure_rolls_back_and_two_connections_recover_one_exact_row(tmp_path,monkeypatch):
    with case(tmp_path)as c:
        review=proposed(c)
        with monkeypatch.context()as patch:
            patch.setattr(Session,'commit',lambda db:(_ for _ in ()).throw(OSError('owned commit failure')))
            with pytest.raises(OSError):save(c,review)
        with c.vault.session()as db:assert not list(db.scalars(select(RealDexExternalOrderProof)))
        other=RealVault(c.vault.path);other.unlock(PASSWORD)
        try:
            with ThreadPoolExecutor(max_workers=2)as pool:
                a=pool.submit(save,c,review);b=pool.submit(save,c,review,vault=other)
                assert a.result(timeout=10)==b.result(timeout=10)
        finally:other.lock()
        with c.vault.session()as db:assert db.scalar(select(func.count()).select_from(RealDexExternalOrderProof))==1


def test_global_history_rejects_reused_identity_or_conflicting_block_from_all_writers(tmp_path):
    with case(tmp_path)as c:
        result=save(c);proof=result['proof']
        candidate={**proof,'signer_ref':proof['signer_digest']}
        with c.vault.session()as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            with pytest.raises(ValueError,match='recibo|nonce'):require_compatible_history(db,candidate)
        # A different position in the same block must still use the same exact
        # block-list witness. No fabricated certificate is stored by this test.
        with c.vault.session()as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            other={**candidate,'common_tx_ref':'a'*64,'signer_ref':'a'*64,'nonce':'999',
                'transaction_index':str(int(candidate['transaction_index'])+1),'block_transactions_ref':'a'*64}
            with pytest.raises(ValueError,match='bloque'):require_compatible_history(db,other)


def test_v76_migration_preserves_observation_preimage_then_downgrade_cannot_hide_order(tmp_path):
    with case(tmp_path)as c:
        with c.vault.session()as db:
            observed=observations.observation_record(db,c.saved['id'])[0].payload_json
            from tests.external_financial_fixture import drop_external_financial_tables
            drop_external_financial_tables(db)
            db.connection().exec_driver_sql('DROP TABLE real_dex_external_order_proofs')
            db.connection().exec_driver_sql('PRAGMA user_version=76');db.commit()
        c.vault.lock();c.vault.unlock(PASSWORD)
        backup=c.vault.last_migration_backup;assert backup is not None
        preimage=RealVault(backup);preimage.unlock(PASSWORD,allow_legacy_for_verification=True)
        try:
            with preimage.session()as db:
                assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==76
                assert observations.observation_record(db,c.saved['id'])[0].payload_json==observed
                assert orders.order_record(db,orders.identifier(c.saved['id']))is None
        finally:preimage.lock()
        saved=save(c)
        with c.vault.session()as db:
            assert observations.observation_record(db,c.saved['id'])[0].payload_json==observed
            db.connection().exec_driver_sql('PRAGMA user_version=76');db.commit()
        with pytest.raises(VaultError):c.vault.backup(tmp_path/'hidden.db')
        with c.vault.session()as db:db.connection().exec_driver_sql(f'PRAGMA user_version={SCHEMA_VERSION}');db.commit()
        c.vault.backup(tmp_path/'valid.db')


@pytest.mark.parametrize('external_first',[False,True])
def test_actual_bound_bot_trade_and_external_wallet_call_share_one_block_and_one_history(tmp_path,monkeypatch,external_first):
    from types import SimpleNamespace
    from datetime import datetime,timezone
    from app.live_execution.dex_call_resolution import collect_call_resolution
    from app.live_execution.dex_external_observation import verify_external_call
    from app.live_execution.dex_external_order import common_reference,verify_external_order
    from app.live_execution.dex_gas_order import collect_order
    from app.portfolio import dex_receipt_order as legacy,dex_receipt_booking as bookings
    from app.portfolio.dex_receipt_order_source import order_source,order_reference
    from app.portfolio.economic_projection_source import _existing_salt
    from tests.test_dex_receipt_booking import case as bot_case
    from tests.test_dex_call_resolution import prepare
    from tests.test_dex_external_observation import reference
    from tests.test_dex_external_observation_journal import proposal as observe_proposal,save as save_observation
    with bot_case(tmp_path,monkeypatch,evm_backend='anvil')as(values,_,worker):
        data,_,_,_,clock,mine,_=values;vault,account,_,_,meta,rpc,_,_=data
        plan,source,_=prepare(vault,account,meta,rpc,action='APPROVE',token='WBNB')
        clock[0]+=timedelta(seconds=1);mine();rpc('miner_stop',[])
        assert worker.run_once()['last_action']=='BUY'
        sent=rpc('eth_sendTransaction',[{**plan['transaction'],'to':meta['operator'],'data':'0x',
            'value':hex(10**15),'gas':hex(21000),'gasPrice':hex(2*10**9)}])
        rpc('evm_mine',[]);rpc('miner_start',[1])
        assert worker.run_once()['last_receipt_status']=='FINALIZED';worker.stop()
        evidence=collect_call_resolution(rpc,source,sent)
        stamp=max(datetime.now(timezone.utc),clock[0])+timedelta(seconds=1)
        own=SimpleNamespace(vault=vault,account=account,meta=meta,source=source,evidence=evidence,
            reference=reference(vault),now=stamp,hash=sent)
        observed=save_observation(own,observe_proposal(own))
        with vault.session()as db:
            row,body=observations.observation_record(db,observed['id']);common=common_reference(_existing_salt(db))
            certificate=verify_external_order(source,row.id,row.payload_digest,body['proof'],evidence,
                reference=own.reference,common=common,now=stamp)
            ext_review=orders.preview(row,certificate)
            financial=next(item for item in bookings.list_receipts(db,account_id=account,wallet_address=meta['owner'])if item['source_kind']=='OPERATOR')
            trade=order_source(db,'OPERATOR',financial['source_id'],wallet_address=meta['owner'])
            block=rpc('eth_getBlockByNumber',[hex(int(certificate['block_number'])),False]);assert len(block['transactions'])==2
            tx=next(value for value in block['transactions']if order_reference(db,trade)('tx',value)==trade.transaction_ref)
        scope=dict(context_account_id=account,wallet_address=meta['owner'],evidence=collect_order(rpc,tx),now=stamp)
        with vault.session()as db:old_review=legacy.preview_order(db,'OPERATOR',trade.id,**scope)
        def old():
            with vault.session()as db:return legacy.confirm_order(db,'OPERATOR',trade.id,preview_digest=old_review['preview_digest'],**scope)
        def external():
            with vault.session()as db:return orders.save_order(db,observed['id'],account_id=account,
                proof=certificate,preview_digest=ext_review['preview_digest'],now=stamp)
        first=(external if external_first else old)();second=(old if external_first else external)()
        assert first['proof']['block_transactions_ref']==second['proof']['block_transactions_ref']
        assert first['proof']['transaction_index']!=second['proof']['transaction_index']
        with vault.session()as db:
            verify_history(db,version=SCHEMA_VERSION)
            assert observations.status(db,'WALLET_SWAP',source.id)['status']=='EFFECTS_PENDING'
            assert db.scalar(select(func.count()).select_from(RealDexExternalOrderProof))==1
            assert db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof))==1
        vault.backup(tmp_path/'mixed-cold.db')
