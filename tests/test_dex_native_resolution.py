"""Recover mined native replacements on a disposable, owned EVM."""
from datetime import timedelta
from uuid import uuid4
import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_native_resolution import collect_resolution
from app.portfolio import dex_native_resolution as resolution,dex_gas_funding as funding,dex_receipt_booking as accounting
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from tests.test_dex_gas_funding import case,prepare,claim,now
from tests.test_dex_deployments import PASSWORD
from eth_account import Account
from eth_utils import to_checksum_address


@pytest.mark.parametrize('typed',[False,True])
def test_cancelled_funding_releases_only_later_nonce_and_books_gas_once(tmp_path,typed):
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        plan=prepare(values);assert claim(values,plan)['claim_granted']
        with vault.session() as db:
            original=funding.funding_record(db,plan['id'])[0]
            before=(original.state_json,original.state_digest,original.status)
            with pytest.raises(ValueError,match='pendiente'):
                assert_wallet_nonce_available(db,meta['owner'],str(int(plan['nonce'])+1))
        tx={'from':meta['owner'],'to':meta['owner'],'nonce':hex(int(plan['nonce'])),'gas':'0x5208','gasPrice':hex(10**9),'value':'0x0','data':'0x'}
        if typed:
            price=tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=price,maxPriorityFeePerGas=hex(10**8))
        tx_hash=rpc('eth_sendTransaction',[tx])
        evidence=collect_resolution(rpc,tx_hash)
        scope=dict(account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())
        with vault.session() as db:preview=resolution.preview_resolution(db,'GAS_FUNDING',plan['id'],**scope)
        assert preview['outcome']=='CANCELLED' and preview['receipt']['transferred_wei']=='0' and preview['available']
        rpc('evm_mine',[])
        scope.update(evidence=collect_resolution(rpc,tx_hash),now=now())
        with vault.session() as db:
            done=resolution.confirm_resolution(db,'GAS_FUNDING',plan['id'],preview_digest=preview['preview_digest'],
                source_state_digest=preview['source_state_digest'],**scope)
            row=funding.funding_record(db,plan['id'])[0]
            assert (row.state_json,row.state_digest,row.status)==before
            assert_wallet_nonce_available(db,meta['owner'],str(int(plan['nonce'])+1))
            with pytest.raises(ValueError,match='nonce|Nonce'):assert_wallet_nonce_available(db,meta['owner'],plan['nonce'])
            assert funding.list_funding(db,account,now=now())[0]['resolution']==done
        with pytest.raises(ValueError,match='resuelt'):
            with vault.session() as db:funding.report_funding(db,plan['id'],account_id=account,tx_hash=tx_hash,now=now())
        from app.portfolio.wallet_links import save_wallet_link
        from app.portfolio.repository import append_event,account_snapshot
        from app.portfolio.domain import EventInput
        from decimal import Decimal
        with vault.session() as db:save_wallet_link(db,account,meta['owner'],'0x38','Propia');db.commit()
        with vault.session() as db:append_event(db,account,str(uuid4()),EventInput('CONTRIBUTION','BNB',now()-timedelta(days=1),amount=Decimal('1')))
        with vault.session() as db:booking=accounting.preview_booking(db,'NATIVE_RESOLUTION',done['id'],context_account_id=account,wallet_address=meta['owner'])
        assert booking['contract']=='DEX_RECEIPT_BOOKING_V3' and booking['trade'] is None and booking['internal_principal_bnb']=='0'
        with vault.session() as db:
            booked=accounting.book_receipt(db,'NATIVE_RESOLUTION',done['id'],context_account_id=account,wallet_address=meta['owner'],
                expected_revision=booking['revision'],preview_digest=booking['preview_digest'],now=now())
            assert booked['events_created']==1 and account_snapshot(db,account).cash['BNB']==Decimal('1')-Decimal(done['receipt']['gas_fee_wei'])/Decimal(10**18)
        # The normal funding flow is available again and uses the next nonce.
        fresh=prepare(values);assert int(fresh['nonce'])==int(plan['nonce'])+1
        assert claim(values,fresh)['claim_granted']
        vault.backup(tmp_path/'resolved.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:
            assert resolution.status(db,'GAS_FUNDING',plan['id'])==done
            assert accounting.booking_record(db,booked['booking_id'])


@pytest.mark.parametrize('cancel',[True,False])
@pytest.mark.parametrize('typed',[False,True])
def test_operator_return_replacement_keeps_original_and_explicitly_reviews_changed_amount_and_fee(tmp_path,cancel,typed):
    from app.portfolio import dex_gas_return as returns
    from app.live_execution.dex_gas_return import read_return_state,plan_return
    from app.live_execution.dex_gas_funding import collect_funding_receipt
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        funding_plan=prepare(values);assert claim(values,funding_plan)['claim_granted']
        funding_hash=rpc('eth_sendTransaction',[funding_plan['transaction']])
        with vault.session() as db:funding.check_funding(db,funding_plan['id'],account_id=account,evidence=collect_funding_receipt(rpc,funding_hash),now=now())
        plan=plan_return(read_return_state(rpc,meta['owner'],draft['operator_address'],now=now()),gas_price=10**9,max_gas_wei=21000*10**9,now=now())
        arguments=dict(identifier=str(uuid4()),account_id=account,binding_id=binding['id'],draft_id=draft['id'],wallet_address=meta['owner'],drafts=manager,plan=plan,now=now())
        with vault.session() as db:prepared=returns.prepare_return(db,**arguments)
        with vault.session() as db:returns.reserve_return(db,prepared['id'],account_id=account,wallet_address=meta['owner'],drafts=manager,draft_id=draft['id'],plan=plan,plan_digest=prepared['plan_digest'],now=now())
        def signed(transaction):
            fields={key:to_checksum_address(value) if key=='to' else value if key=='data' else int(value,16)
                for key,value in transaction.items() if key not in ('from','type')}
            if typed and 'maxFeePerGas' in fields:fields['type']=2
            with manager._copy(draft['id'],account,meta['owner'],now()) as (_,key):return Account.sign_transaction(fields,bytes(key))
        original=signed(plan.transaction());original_hash='0x'+original.hash.hex()
        with vault.session() as db:returns.record_signature(db,prepared['id'],account_id=account,plan=plan,raw=bytes(original.raw_transaction),tx_hash=original_hash,now=now())
        with vault.session() as db:returns.mark_broadcast(db,prepared['id'],account_id=account,plan_digest=prepared['plan_digest'],tx_hash=original_hash,now=now())
        # Simulate a claimed POST that never arrived, followed by an external
        # same-nonce cancellation or accelerated owner-only replacement.
        transfer=0 if cancel else plan.amount_wei-21000*10**9
        tx={**plan.transaction(),'to':draft['operator_address'] if cancel else meta['owner'],'value':hex(transfer),'gasPrice':hex(10**9 if cancel else 2*10**9)}
        if typed:
            price=tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=price,maxPriorityFeePerGas=hex(10**8))
        replacement=signed(tx);tx_hash=rpc('eth_sendRawTransaction',['0x'+replacement.raw_transaction.hex()])
        evidence=collect_resolution(rpc,tx_hash)
        scope=dict(account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())
        with vault.session() as db:
            parent=returns.return_record(db,prepared['id'])[0];before=(parent.state_json,parent.state_digest,parent.updated_at)
            with pytest.raises(ValueError,match='pendiente'):returns.require_available(db,draft['operator_address'])
            preview=resolution.preview_resolution(db,'GAS_RETURN',prepared['id'],**scope)
        assert preview['outcome']==('CANCELLED' if cancel else 'TRANSFERRED')
        assert preview['amount_changed'] is (not cancel) and preview['fee_overrun'] is (not cancel)
        if not cancel:
            assert not preview['available']
            with pytest.raises(ValueError,match='Acepta'):
                with vault.session() as db:resolution.confirm_resolution(db,'GAS_RETURN',prepared['id'],preview_digest=preview['preview_digest'],source_state_digest=preview['source_state_digest'],**scope)
            scope.update(accept_changes=True)
            with vault.session() as db:preview=resolution.preview_resolution(db,'GAS_RETURN',prepared['id'],**scope)
        with vault.session() as db:
            done=resolution.confirm_resolution(db,'GAS_RETURN',prepared['id'],preview_digest=preview['preview_digest'],source_state_digest=preview['source_state_digest'],**scope)
            parent=returns.return_record(db,prepared['id'])[0]
            assert (parent.state_json,parent.state_digest,parent.updated_at)==before
            returns.require_available(db,draft['operator_address'],str(plan.snapshot.operator_nonce+1))
            with pytest.raises(ValueError,match='nonce'):returns.require_available(db,draft['operator_address'],str(plan.snapshot.operator_nonce))
            assert returns.view_return(db,prepared['id'],now=now())['resolution']==done
            for text in (meta['owner'],draft['operator_address'],tx_hash,original_hash,replacement.raw_transaction.hex()):
                resolved=resolution.resolution_record(db,done['id'])[0]
                assert text.lower() not in resolved.payload_json.lower()
        with pytest.raises(ValueError,match='resuelt'):
            with vault.session() as db:returns.check_return(db,prepared['id'],account_id=account,evidence=evidence,now=now())
        source=accounting.source_record
        with vault.session() as db:
            receipt=source(db,'NATIVE_RESOLUTION',done['id'])
            assert receipt.principal_wei==str(transfer) and receipt.signer_digest!=receipt.wallet_digest
        vault.backup(tmp_path/'return-resolution.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:assert resolution.status(db,'GAS_RETURN',prepared['id'])==done


def test_resolution_rejects_wrong_evidence_is_idempotent_concurrent_and_backup_detects_rehashed_forgery(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    from app.portfolio.models import RealDexNativeResolution
    from app.portfolio.wallet_funding import _decoded,_encoded
    from app.portfolio.vault import VaultError
    with case(tmp_path) as values:
        vault,account,manager,draft,meta,rpc,deployment,binding=values
        plan=prepare(values);assert claim(values,plan)['claim_granted']
        tx_hash=rpc('eth_sendTransaction',[{**plan['transaction'],'to':meta['owner'],'value':'0x0'}]);evidence=collect_resolution(rpc,tx_hash)
        for changed in ('nonce','from','to','blockHash','chain','logs','gas','source_code','source_nonce','hash'):
            bad=deepcopy(evidence)
            if changed in {'nonce','from','to','blockHash'}:bad['transaction'][changed]={'nonce':'0x123','from':meta['operator'],'to':meta['operator'],'blockHash':'0x'+'ab'*32}[changed]
            elif changed=='chain':bad['chain_id']='0x1'
            elif changed=='logs':bad['receipt']['logs']=[{}]
            elif changed=='gas':bad['receipt']['gasUsed']='0x5209'
            elif changed=='source_code':bad['source_code']='0x00'
            elif changed=='source_nonce':bad['source_nonce']=hex(int(plan['nonce']))
            else:bad['tx_hash']='0x'+'ab'*32
            with pytest.raises(ValueError):
                with vault.session() as db:resolution.preview_resolution(db,'GAS_FUNDING',plan['id'],account_id=account,wallet_address=meta['owner'],evidence=bad,now=now())
        arguments=dict(account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())
        with vault.session() as db:review=resolution.preview_resolution(db,'GAS_FUNDING',plan['id'],**arguments)
        def confirm(_):
            with vault.session() as db:return resolution.confirm_resolution(db,'GAS_FUNDING',plan['id'],preview_digest=review['preview_digest'],source_state_digest=review['source_state_digest'],**arguments)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(confirm,range(2)))
        assert results[0]==results[1]
        with vault.session() as db:
            row=db.get(RealDexNativeResolution,results[0]['id']);payload=_decoded(row.payload_json,row.payload_digest)
            payload['proof']['receipt']['transferred_wei']='1';row.payload_json,row.payload_digest=_encoded(payload);db.commit()
        with pytest.raises(ValueError):
            with vault.session() as db:resolution.status(db,'GAS_FUNDING',plan['id'])
        with pytest.raises(VaultError):vault.backup(tmp_path/'forged.db')


def test_schema_67_migration_preserves_native_originals(tmp_path):
    from app.portfolio.vault import SCHEMA_VERSION
    with case(tmp_path) as values:
        vault,account,_,_,_,_,_,_=values;plan=prepare(values)
        with vault.session() as db:
            row=funding.funding_record(db,plan['id'])[0];before=(row.payload_json,row.payload_digest,row.state_json,row.state_digest)
            db.connection().exec_driver_sql('DROP TABLE real_dex_native_resolutions')
            db.connection().exec_driver_sql('PRAGMA user_version=67');db.commit()
        vault.lock();vault.unlock(PASSWORD)
        assert vault.last_migration_backup.is_file()
        with vault.session() as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
            row=funding.funding_record(db,plan['id'])[0];assert (row.payload_json,row.payload_digest,row.state_json,row.state_digest)==before
            assert resolution.status(db,'GAS_FUNDING',plan['id']) is None
        vault.backup(tmp_path/'migrated.db')


@pytest.mark.parametrize('amount',[5*10**14,15*10**14])
def test_external_funding_replacement_with_different_native_amount_requires_review_without_increasing_bot_budget(tmp_path,amount):
    from app.portfolio import dex_bot_bindings as bindings
    with case(tmp_path) as values:
        vault,account,_,_,meta,rpc,_,binding=values;plan=prepare(values);assert claim(values,plan)['claim_granted']
        tx_hash=rpc('eth_sendTransaction',[{**plan['transaction'],'value':hex(amount)}]);evidence=collect_resolution(rpc,tx_hash)
        arguments=dict(account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now())
        with vault.session() as db:
            before=bindings.binding_view(db,binding['id'],now=now())['gas']
            preview=resolution.preview_resolution(db,'GAS_FUNDING',plan['id'],**arguments)
        assert preview['amount_changed'] and not preview['fee_overrun'] and not preview['available']
        with pytest.raises(ValueError,match='Acepta'):
            with vault.session() as db:resolution.confirm_resolution(db,'GAS_FUNDING',plan['id'],preview_digest=preview['preview_digest'],source_state_digest=preview['source_state_digest'],**arguments)
        arguments['accept_changes']=True
        with vault.session() as db:preview=resolution.preview_resolution(db,'GAS_FUNDING',plan['id'],**arguments)
        with vault.session() as db:
            done=resolution.confirm_resolution(db,'GAS_FUNDING',plan['id'],preview_digest=preview['preview_digest'],source_state_digest=preview['source_state_digest'],**arguments)
            assert done['outcome']=='TRANSFERRED' and done['receipt']['transferred_wei']==str(amount)
            assert bindings.binding_view(db,binding['id'],now=now())['gas']==before
        vault.backup(tmp_path/'changed-funding.db')
