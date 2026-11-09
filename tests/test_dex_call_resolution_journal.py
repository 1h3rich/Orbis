"""Sealed call outcomes on isolated SQLCipher/Anvil; no user funds or keys."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.portfolio import dex_call_resolution as resolution,wallet_swap as swaps,dex_operator_journal as operators
from app.portfolio.models import RealDexCallResolution
from app.portfolio.vault import RealVault,VaultError,SCHEMA_VERSION
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from tests.dex_evm_fixture import evm
from tests.test_wallet_swap import setup,PASSWORD
from tests.test_dex_deployments import PASSWORD as OPERATOR_PASSWORD
from tests.test_dex_call_resolution import prepare,now
from tests.test_dex_call_resolution_operator import case,lower_price,pending,assert_unchanged


def args(account,meta,evidence,**extra):return dict(account_id=account,wallet_address=meta['owner'],evidence=evidence,now=now(),**extra)


def review(vault,kind,source,scope):
    with vault.session() as db:return resolution.preview_resolution(db,kind,source,**scope)


def confirm(vault,kind,source,proposal,scope):
    with vault.session() as db:return resolution.confirm_resolution(db,kind,source,preview_digest=proposal['preview_digest'],source_state_digest=proposal['source_state_digest'],**scope)


@pytest.mark.parametrize('typed',[False,True])
def test_wallet_cancel_survives_reopen_and_only_releases_later_nonce_without_changing_original(tmp_path,typed):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            plan,source,before=prepare(vault,account,meta,rpc)
            rpc('miner_stop',[]);original=rpc('eth_sendTransaction',[plan['transaction']])
            tx={'from':meta['owner'],'to':meta['owner'],'nonce':hex(source.nonce),'data':'0x','value':'0x0','gas':'0x5208','gasPrice':hex(2*10**9)}
            if typed:
                tx.pop('gasPrice');tx.update(type='0x2',maxFeePerGas=hex(2*10**9),maxPriorityFeePerGas=hex(10**9))
            sent=rpc('eth_sendTransaction',[tx]);rpc('miner_start',[1]);rpc('evm_mine',[])
            scope=args(account,meta,collect_call_resolution(rpc,source,sent));proposal=review(vault,'WALLET_SWAP',source.id,scope)
            assert not proposal['available'] and proposal['fee_changed']
            with pytest.raises(DexExecutorError,match='Acepta'):confirm(vault,'WALLET_SWAP',source.id,proposal,scope)
            scope['accept_changes']=True;proposal=review(vault,'WALLET_SWAP',source.id,scope)
            # Reviewing across new finalized blocks must not invalidate a fixed
            # receipt/fee plan or silently produce another economic result.
            rpc('evm_mine',[]);scope.update(evidence=collect_call_resolution(rpc,source,sent),now=now())
            done=confirm(vault,'WALLET_SWAP',source.id,proposal,scope)
            assert done['outcome']=='CANCELLED' and done['effect'] is None
            assert confirm(vault,'WALLET_SWAP',source.id,proposal,scope)==done
            with vault.session() as db:
                row,_,_=swaps.swap_record(db,source.id);assert (row.payload_json,row.state_json,row.updated_at)==before
                assert_wallet_nonce_available(db,meta['owner'],str(source.nonce+1))
                with pytest.raises(ValueError,match='nonce'):assert_wallet_nonce_available(db,meta['owner'],str(source.nonce))
            with pytest.raises(ValueError,match='resuelta'):
                with vault.session() as db:swaps.report_swap(db,source.id,account_id=account,tx_hash=original,now=now())
            vault.backup(tmp_path/'cancelled.db');vault.lock();vault.unlock(PASSWORD)
            with vault.session() as db:
                assert resolution.status(db,'WALLET_SWAP',source.id)==done
                row=resolution.resolution_record(db,done['id'])[0]
                for value in (meta['owner'],meta['operator'],sent,original):assert value.lower() not in row.payload_json.lower()
            assert b'SQLite format 3' not in (tmp_path/'cancelled.db').read_bytes()[:16]
    finally:vault.lock()


@pytest.mark.parametrize('cancel',[False,True])
def test_operator_resolution_requires_stopped_grant_and_accounts_actual_gas_once(tmp_path,cancel):
    with case(tmp_path,evm_backend='anvil') as values:
        vault,account,meta,rpc,deployment,grant,_=values
        trade=lower_price(meta,rpc,int(deployment['nonce']));source,before,original=pending(values,trade)
        tx={**trade.transaction(),'gasPrice':hex(10**9)}
        if cancel:tx.update(to=meta['operator'],data='0x',gas='0x5208')
        sent=rpc('eth_sendTransaction',[tx]);rpc('miner_start',[1]);rpc('evm_mine',[])
        scope=args(account,meta,collect_call_resolution(rpc,source,sent),accept_changes=True)
        with pytest.raises(ValueError,match='Detén'):review(vault,'OPERATOR',source.id,scope)
        with vault.session() as db:operators.stop_grant(db,grant['id'],account_id=account,now=now())
        scope['now']=now()
        proposal=review(vault,'OPERATOR',source.id,scope);done=confirm(vault,'OPERATOR',source.id,proposal,scope)
        assert done['outcome']==('CANCELLED' if cancel else 'EXECUTED')
        if not cancel:assert done['effect']['owned_wbnb']=='10000000000000000'
        assert_unchanged(vault,source,before)
        with vault.session() as db:
            view=operators.grant_view(db,grant['id'],now=now());assert not view['unresolved_outcomes'] and view['status']=='STOPPED'
            assert view['gas_reserved_wei']=='0' and view['gas_spent_wei']==done['receipt']['gas_fee_wei']
            assert view['remaining_gas_wei']==str(int(grant['gas_budget_wei'])-int(done['receipt']['gas_fee_wei']))
            assert_wallet_nonce_available(db,meta['operator'],str(source.nonce+1))
            with pytest.raises(ValueError):assert_wallet_nonce_available(db,meta['operator'],str(source.nonce))
        vault.backup(tmp_path/'operator-resolved.db');vault.lock();vault.unlock(OPERATOR_PASSWORD)
        with vault.session() as db:assert resolution.status(db,'OPERATOR',source.id)==done


def test_confirm_write_failure_rolls_back_entire_resolution_and_leaves_source_pending(tmp_path,monkeypatch):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            plan,source,before=prepare(vault,account,meta,rpc)
            sent=rpc('eth_sendTransaction',[{'from':meta['owner'],'to':meta['owner'],'nonce':hex(source.nonce),'value':'0x0','data':'0x','gas':'0x5208','gasPrice':hex(10**9)}])
            scope=args(account,meta,collect_call_resolution(rpc,source,sent),accept_changes=True);proposal=review(vault,'WALLET_SWAP',source.id,scope)
            from sqlalchemy.orm import Session
            with monkeypatch.context() as patch:
                patch.setattr(Session,'commit',lambda db:(_ for _ in ()).throw(OSError('Own fixture commit failure')))
                with pytest.raises(OSError):confirm(vault,'WALLET_SWAP',source.id,proposal,scope)
            with vault.session() as db:
                assert not list(db.scalars(select(RealDexCallResolution)))
                row,_,_=swaps.swap_record(db,source.id);assert (row.payload_json,row.state_json,row.updated_at)==before
                with pytest.raises(ValueError,match='pendiente'):assert_wallet_nonce_available(db,meta['owner'],str(source.nonce+1))
            assert confirm(vault,'WALLET_SWAP',source.id,proposal,scope)['resolved']
    finally:vault.lock()


def cancelled(vault,account,meta,rpc):
    plan,source,before=prepare(vault,account,meta,rpc)
    sent=rpc('eth_sendTransaction',[{'from':meta['owner'],'to':meta['owner'],'nonce':hex(source.nonce),'value':'0x0','data':'0x','gas':'0x5208','gasPrice':hex(10**9)}])
    scope=args(account,meta,collect_call_resolution(rpc,source,sent),accept_changes=True)
    proposal=review(vault,'WALLET_SWAP',source.id,scope)
    return source,proposal,scope,before


def test_resolution_seal_math_source_binding_and_old_schema_are_checked_by_reads_and_backup(tmp_path):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            source,proposal,scope,before=cancelled(vault,account,meta,rpc);done=confirm(vault,'WALLET_SWAP',source.id,proposal,scope)
            from app.portfolio.wallet_funding import _encoded,_decoded
            with vault.session() as db:
                row=db.get(RealDexCallResolution,done['id']);raw,digest=row.payload_json,row.payload_digest
            for reseal in (False,True):
                with vault.session() as db:
                    row=db.get(RealDexCallResolution,done['id']);payload=_decoded(raw,digest);payload['proof']['receipt']['gas_fee_wei']='1'
                    if reseal:payload['seal']=resolution._seal(db,{key:value for key,value in payload.items() if key!='seal'})
                    row.payload_json,row.payload_digest=_encoded(payload);db.commit()
                with pytest.raises(ValueError):
                    with vault.session() as db:resolution.resolution_record(db,done['id'])
                with pytest.raises(VaultError):vault.backup(tmp_path/f'bad-{reseal}.db')
                with vault.session() as db:
                    row=db.get(RealDexCallResolution,done['id']);row.payload_json,row.payload_digest=raw,digest;db.commit()
            with vault.session() as db:
                parent,_,state=swaps.swap_record(db,source.id);old_state=parent.state_json;old_digest=parent.state_digest;old_at=parent.updated_at
                stamp=now();state['history'].append({'status':'HASH_REPORTED','at':stamp.isoformat()});state['status']='HASH_REPORTED'
                state['tx_digest']=swaps._identity(db,'tx',scope['evidence']['tx_hash']);parent.status='HASH_REPORTED';parent.updated_at=stamp
                parent.state_json,parent.state_digest=_encoded(state);db.commit()
            with pytest.raises(ValueError):
                with vault.session() as db:resolution.resolution_record(db,done['id'])
            with vault.session() as db:
                parent=db.get(type(parent),source.id);parent.state_json=old_state;parent.state_digest=old_digest;parent.updated_at=old_at;parent.status='WALLET_REQUESTED';db.commit()
                db.connection().exec_driver_sql('PRAGMA user_version = 70');db.commit()
            with pytest.raises(VaultError):vault.backup(tmp_path/'hidden-under-v70.db')
            with vault.session() as db:db.connection().exec_driver_sql(f'PRAGMA user_version = {SCHEMA_VERSION}');db.commit()
            vault.backup(tmp_path/'valid-again.db')
    finally:vault.lock()


def test_same_nonce_or_receipt_cannot_later_resolve_a_native_or_create_source(tmp_path):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            source,proposal,scope,_=cancelled(vault,account,meta,rpc);done=confirm(vault,'WALLET_SWAP',source.id,proposal,scope)
            from app.portfolio.dex_native_resolution import require_unused
            from app.portfolio.dex_deployments import _identity
            with vault.session() as db:
                with pytest.raises(ValueError,match='nonce|recibo'):require_unused(db,_identity(db,'wallet',meta['owner']),str(source.nonce),'a'*64)
                with pytest.raises(ValueError,match='nonce|recibo'):require_unused(db,'a'*64,'999',_identity(db,'tx',scope['evidence']['tx_hash']))
            with vault.session() as db:assert resolution.status(db,'WALLET_SWAP',source.id)==done
    finally:vault.lock()


def test_two_connections_confirm_once_and_recover_the_same_persisted_outcome(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    vault,account,_=setup(tmp_path);other=None
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            source,proposal,scope,_=cancelled(vault,account,meta,rpc)
            other=RealVault(vault.path);other.unlock(PASSWORD)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(confirm,vault,'WALLET_SWAP',source.id,proposal,scope)
                second=pool.submit(confirm,other,'WALLET_SWAP',source.id,proposal,scope)
                assert first.result()==second.result()
            with vault.session() as db:assert len(list(db.scalars(select(RealDexCallResolution))))==1
    finally:
        if other:other.lock()
        vault.lock()


def test_legacy_v70_migration_uses_an_encrypted_preimage_and_preserves_original_data(tmp_path):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            plan,source,before=prepare(vault,account,meta,rpc)
            with vault.session() as db:
                db.connection().exec_driver_sql('DROP TABLE real_dex_call_resolutions')
                db.connection().exec_driver_sql('PRAGMA user_version = 70');db.commit()
            vault.lock();vault.unlock(PASSWORD)
            assert vault.last_migration_backup is not None
            with vault.session() as db:
                assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
                row,_,_=swaps.swap_record(db,source.id);assert (row.payload_json,row.state_json,row.updated_at)==before
                assert not list(db.scalars(select(RealDexCallResolution)))
            preimage=RealVault(vault.last_migration_backup);preimage.unlock(PASSWORD,allow_legacy_for_verification=True)
            try:
                with preimage.session() as db:
                    assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==70
                    row,_,_=swaps.swap_record(db,source.id);assert (row.payload_json,row.state_json,row.updated_at)==before
            finally:preimage.lock()
    finally:vault.lock()


def test_verified_external_gas_over_budget_is_retained_and_never_becomes_new_authority(tmp_path):
    with case(tmp_path,evm_backend='anvil') as values:
        vault,account,meta,rpc,deployment,grant,_=values
        trade=lower_price(meta,rpc,int(deployment['nonce']));source,before,_=pending(values,trade)
        sent=rpc('eth_sendTransaction',[{'from':meta['operator'],'to':meta['operator'],'nonce':hex(source.nonce),'data':'0x','value':'0x0','gas':'0x5208','gasPrice':hex(10**12)}])
        rpc('miner_start',[1]);rpc('evm_mine',[])
        with vault.session() as db:operators.stop_grant(db,grant['id'],account_id=account,now=now())
        scope=args(account,meta,collect_call_resolution(rpc,source,sent),accept_changes=True)
        done=confirm(vault,'OPERATOR',source.id,review(vault,'OPERATOR',source.id,scope),scope)
        with vault.session() as db:
            view=operators.grant_view(db,grant['id'],now=now())
            assert view['remaining_gas_wei']=='0' and view['gas_reserved_wei']=='0' and not view['unresolved_outcomes']
            assert view['gas_spent_wei']==done['receipt']['gas_fee_wei']
            assert view['gas_overrun_wei']==str(int(done['receipt']['gas_fee_wei'])-int(grant['gas_budget_wei']))
        assert_unchanged(vault,source,before);vault.backup(tmp_path/'overrun.db');vault.lock();vault.unlock(OPERATOR_PASSWORD)
        with vault.session() as db:assert operators.grant_view(db,grant['id'],now=now())['remaining_gas_wei']=='0'


def test_live_writer_rejects_a_second_original_claim_for_the_same_wallet_nonce(tmp_path):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            source,proposal,scope,_=cancelled(vault,account,meta,rpc)
            # Simulate a malformed legacy import with a second claimed source.
            # The public claim API would reject it through the nonce guard.
            with vault.session() as db:
                other=swaps.prepare_swap(db,identifier=str(uuid4()),account_id=account,wallet_address=meta['owner'],action='SWAP',
                    token_in='USDT',amount='0.01',quote_units='10000000000000000',fee=500,slippage_bps=25,
                    nonce=str(source.nonce),gas='200000',gas_price='1000000000',max_gas_bnb='0.0003',now=now())
            with vault.session() as db:
                row,_,state=swaps.swap_record(db,other['id']);swaps._advance(db,row,state,'WALLET_REQUESTED',now())
            with pytest.raises(ValueError,match='otro original'):confirm(vault,'WALLET_SWAP',source.id,proposal,scope)
            with vault.session() as db:assert not list(db.scalars(select(RealDexCallResolution)))
    finally:vault.lock()


def test_executed_manual_swap_keeps_hash_changed_fee_output_and_source_exact_across_reopen(tmp_path):
    from tests.test_dex_operator import write
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            write(rpc,meta,meta['router'],'set(uint256,bool)',['uint256','bool'],[10000,False])
            for token,target in [('usdt',meta['owner']),('wbnb',meta['pool'])]:
                write(rpc,meta,meta[token],'mint(address,uint256)',['address','uint256'],[target,10**18])
            write(rpc,{**meta,'operator':meta['owner']},meta['usdt'],'approve(address,uint256)',['address','uint256'],[meta['router'],10**18])
            plan,source,_=prepare(vault,account,meta,rpc)
            sent=rpc('eth_sendTransaction',[{**plan['transaction'],'gasPrice':hex(2*10**9)}])
            with vault.session() as db:swaps.report_swap(db,source.id,account_id=account,tx_hash=sent,now=now())
            with vault.session() as db:
                row,_,_=swaps.swap_record(db,source.id);before=(row.payload_json,row.state_json,row.updated_at)
            scope=args(account,meta,collect_call_resolution(rpc,source,sent),accept_changes=True)
            for bad in ({'account_id':str(uuid4())},{'wallet_address':meta['operator']},{'accept_changes':1}):
                with pytest.raises(ValueError):review(vault,'WALLET_SWAP',source.id,{**scope,**bad})
            proposal=review(vault,'WALLET_SWAP',source.id,scope)
            with pytest.raises(ValueError,match='cambió'):confirm(vault,'WALLET_SWAP',source.id,{**proposal,'preview_digest':'a'*64},scope)
            done=confirm(vault,'WALLET_SWAP',source.id,proposal,scope)
            assert done['outcome']=='EXECUTED' and done['matches_original'] is True and done['effect']['amount_out_units']=='10000000000000000'
            with vault.session() as db:
                row,_,_=swaps.swap_record(db,source.id);assert (row.payload_json,row.state_json,row.updated_at)==before
            vault.backup(tmp_path/'swap-resolved.db');vault.lock();vault.unlock(PASSWORD)
            with vault.session() as db:assert resolution.status(db,'WALLET_SWAP',source.id)==done
            from app.portfolio.wallet_funding import _encoded,_decoded
            with vault.session() as db:
                row=db.get(RealDexCallResolution,done['id']);raw,digest=row.payload_json,row.payload_digest
                forged=_decoded(raw,digest);forged['proof']['receipt']['observation_ref']='a'*64
                forged['seal']=resolution._seal(db,{key:value for key,value in forged.items() if key!='seal'})
                row.payload_json,row.payload_digest=_encoded(forged);db.commit()
            with pytest.raises(ValueError):
                with vault.session() as db:resolution.resolution_record(db,done['id'])
            with vault.session() as db:
                row=db.get(RealDexCallResolution,done['id']);row.payload_json,row.payload_digest=raw,digest;db.commit()
                assert resolution.status(db,'WALLET_SWAP',source.id)==done
    finally:vault.lock()


def test_failed_distinct_call_requires_explicit_review_even_when_fees_did_not_change(tmp_path):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            plan,source,before=prepare(vault,account,meta,rpc)
            sent=rpc('eth_sendTransaction',[{**plan['transaction'],'data':'0xdeadbeef','value':'0x1'}])
            scope=args(account,meta,collect_call_resolution(rpc,source,sent));proposal=review(vault,'WALLET_SWAP',source.id,scope)
            assert proposal['outcome']=='REVERTED' and proposal['call_kind']=='OTHER_CALL' and proposal['effect'] is None
            assert not proposal['fee_changed'] and not proposal['fee_overrun'] and not proposal['available']
            with pytest.raises(ValueError,match='Acepta'):confirm(vault,'WALLET_SWAP',source.id,proposal,scope)
            scope['accept_changes']=True;proposal=review(vault,'WALLET_SWAP',source.id,scope)
            done=confirm(vault,'WALLET_SWAP',source.id,proposal,scope)
            assert done['receipt']['status']=='REVERTED' and int(done['receipt']['gas_fee_wei'])>0
            with vault.session() as db:
                assert_wallet_nonce_available(db,meta['owner'],str(source.nonce+1))
                row,_,_=swaps.swap_record(db,source.id);assert (row.payload_json,row.state_json,row.updated_at)==before
            changed=deepcopy(scope);changed['evidence']['tx_hash']='0x'+'ab'*32
            with pytest.raises(ValueError,match='otro recibo'):confirm(vault,'WALLET_SWAP',source.id,proposal,changed)
            with pytest.raises(ValueError,match='Aceptación'):confirm(vault,'WALLET_SWAP',source.id,proposal,{**scope,'accept_changes':1})
            vault.backup(tmp_path/'reverted-other.db');vault.lock();vault.unlock(PASSWORD)
            with vault.session() as db:assert resolution.status(db,'WALLET_SWAP',source.id)==done
    finally:vault.lock()


def test_optional_rpc_metadata_does_not_invalidate_an_unchanged_verified_receipt(tmp_path):
    vault,account,_=setup(tmp_path)
    try:
        with evm(tmp_path/'evm',backend='anvil') as (meta,rpc):
            source,proposal,scope,_=cancelled(vault,account,meta,rpc)
            rpc('evm_mine',[])
            changed={**scope,'now':now(),'evidence':collect_call_resolution(rpc,source,scope['evidence']['tx_hash'])}
            changed['evidence']['transaction']['provider_extra']='non_financial_metadata'
            changed['evidence']['receipt']['provider_extra']='another_response_annotation'
            assert confirm(vault,'WALLET_SWAP',source.id,proposal,changed)['outcome']=='CANCELLED'
    finally:vault.lock()


def test_legacy_hooks_do_not_import_the_v71_module_before_migration(tmp_path,monkeypatch):
    import builtins
    from app.live_execution.dex_operator import DexOperatorSession
    from tests.test_dex_operator import KEY
    from tests.test_dex_operator_journal import reserve
    from app.portfolio.dex_native_resolution import require_unused
    with case(tmp_path,evm_backend='anvil') as values:
        vault,account,meta,rpc,deployment,grant,trade=values
        nonce=str(int(rpc('eth_getTransactionCount',[meta['owner'],'latest']),16))
        with vault.session() as db:
            db.connection().exec_driver_sql('DROP TABLE real_dex_call_resolutions')
            db.connection().exec_driver_sql('PRAGMA user_version = 70');db.commit()
        original_import=builtins.__import__
        def old_runtime(name,*args,**kwargs):
            if name=='app.portfolio.dex_call_resolution':raise AssertionError('A pre-migration runtime cannot import its new model')
            return original_import(name,*args,**kwargs)
        signer=DexOperatorSession(vault)
        signer.connect(private_key=KEY,snapshot=trade.snapshot,ttl_seconds=120,gas_budget_wei=2*10**15,backup_confirmed=True,now=now())
        try:
            with monkeypatch.context() as patch:
                patch.setattr(builtins,'__import__',old_runtime)
                reserved=reserve(vault,account,grant,trade)
                with vault.session() as db:
                    operators.record_signature(db,reserved['id'],account_id=account,signed=signer.sign_swap(trade,now=now()),plan=trade,now=now())
                with vault.session() as db:
                    assert operators.grant_view(db,grant['id'],now=now())['gas_reserved_wei']==str(trade.gas*trade.gas_price)
                    require_unused(db,'a'*64,'999','b'*64)
                with vault.session() as db:
                    plan=swaps.prepare_swap(db,identifier=str(uuid4()),account_id=account,wallet_address=meta['owner'],action='APPROVE',
                        token_in='USDT',amount='0.01',quote_units='10000000000000000',fee=500,slippage_bps=25,nonce=nonce,
                        gas='200000',gas_price='1000000000',max_gas_bnb='0.0003',now=now())
                with vault.session() as db:assert swaps.claim_swap(db,plan['id'],account_id=account,wallet_address=meta['owner'],plan_digest=plan['plan_digest'],now=now())['claim_granted']
        finally:signer.revoke()
