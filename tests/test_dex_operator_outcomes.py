"""Actual signed replacement facts, historical cutoffs and immutable consumers."""
from copy import deepcopy
from datetime import timedelta

import pytest
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.portfolio import dex_operator_journal as original,dex_call_resolution as resolution
from app.portfolio.dex_operator_outcomes import operator_outcome,outcome_witnesses,fold_operator_position
from app.portfolio.dex_bot_replay_cache import receipt_witnesses
from app.portfolio.models import RealDexCallResolution
from tests.test_dex_call_resolution_operator import case,lower_price,pending,now
from tests.test_dex_call_resolution_journal import args,review,confirm


@pytest.mark.parametrize('kind',['CANCELLED','EXECUTED','REVERTED'])
def test_resolution_is_economic_fact_only_after_acceptance_and_never_mutates_original(tmp_path,kind):
    with case(tmp_path,evm_backend='anvil') as values:
        vault,account,meta,rpc,deployment,grant,_=values
        trade=lower_price(meta,rpc,int(deployment['nonce']));source,before,original_hash=pending(values,trade)
        with vault.session() as db:
            unseen=operator_outcome(db,source.id);fingerprint=receipt_witnesses(db,(source.id,))
            assert unseen.outcome=='PENDING' and unseen.gas_fee_wei==0 and not unseen.settled
        tx={**trade.transaction(),'gasPrice':hex(10**9 if kind!='REVERTED' else 2*10**9)}
        if kind=='CANCELLED':tx.update(to=meta['operator'],data='0x',gas='0x5208')
        sent=rpc('eth_sendTransaction',[tx]);rpc('miner_start',[1]);rpc('evm_mine',[])
        assert rpc('eth_getTransactionReceipt',[original_hash]) is None
        with vault.session() as db:original.stop_grant(db,grant['id'],account_id=account,now=now())
        cutoff=now();scope=args(account,meta,collect_call_resolution(rpc,source,sent),accept_changes=True)
        done=confirm(vault,'OPERATOR',source.id,review(vault,'OPERATOR',source.id,scope),scope)
        with vault.session() as db:
            fact=operator_outcome(db,source.id);past=operator_outcome(db,source.id,as_of=cutoff)
            assert past.outcome=='PENDING' and past.receipt is None and past.gas_fee_wei==0
            assert fact.outcome==kind and fact.settled and fact.filled is (kind=='EXECUTED')
            assert fact.receipt_kind=='CALL_RESOLUTION' and fact.gas_fee_wei==int(done['receipt']['gas_fee_wei'])
            assert fact.receipt['status']==('REVERTED' if kind=='REVERTED' else 'FINALIZED')
            assert (fact.effect is None) is (kind!='EXECUTED')
            assert outcome_witnesses(db,[source.id])==[fact.witness()]
            assert receipt_witnesses(db,(source.id,))!=fingerprint
            projection=fold_operator_position(source.terms['anchor']['counters'],((fact,123456),))
            baseline={key:int(value) for key,value in source.terms['anchor']['counters'].items()}
            assert projection.gas_spent_wei==fact.gas_fee_wei
            if kind=='EXECUTED':
                assert projection.counters['owned_wbnb']==projection.counters['cost_usdt']==10**16
                assert projection.counters['entries']==baseline['entries']+1 and projection.entry_candle_t==123456
                assert projection.counters['next_nonce']==baseline['next_nonce']+1
            else:assert projection.counters==baseline and projection.entry_candle_t is None
            with pytest.raises(TypeError):fact.receipt['gas_fee_wei']='1'
            with pytest.raises(TypeError):fact.definition['anchor']['counters']['entries']='999'
            row,_,_=original.intent_record(db,source.id);assert (row.payload_json,row.state_json,row.updated_at)==before
            with pytest.raises(ValueError):fold_operator_position(baseline,((fact,123),(fact,123)))
            with pytest.raises(ValueError):fold_operator_position(baseline,((past,123),))
            with pytest.raises(ValueError):operator_outcome(db,source.id,as_of=row.created_at-timedelta(seconds=1))
        from tests.test_dex_deployments import PASSWORD
        vault.backup(tmp_path/'projected.db');vault.lock();vault.unlock(PASSWORD)
        with vault.session() as db:
            assert operator_outcome(db,source.id)==fact
            before_edit=receipt_witnesses(db,(source.id,));row=db.get(RealDexCallResolution,fact.resolution_id)
            row.payload_json+=' ';db.commit()
        with vault.session() as db:
            assert receipt_witnesses(db,(source.id,))!=before_edit
            with pytest.raises(ValueError):operator_outcome(db,source.id)


def test_original_receipts_project_without_a_resolution_and_respect_the_same_cutoff(tmp_path):
    from tests.test_dex_operator_journal import prepared_execution
    from app.live_execution.dex_operator_transport import collect_operator_receipt
    with case(tmp_path,evm_backend='anvil') as values:
        vault,account,meta,rpc,deployment,grant,trade=values
        session,prepared=prepared_execution(vault,account,grant,trade);sent=[]
        def post(raw):
            tx=rpc('eth_sendRawTransaction',['0x'+raw.hex()]);sent.append(tx);return tx
        prepared.broadcast_once(post,now=now());session.revoke();cutoff=now()
        proof=collect_operator_receipt(rpc,meta['owner'],int(deployment['nonce']),sent[0])
        with vault.session() as db:original.check_intent(db,prepared.identifier,account_id=account,evidence=proof,now=now())
        with vault.session() as db:
            fact=operator_outcome(db,prepared.identifier)
            assert fact.outcome=='EXECUTED' and fact.receipt_kind=='ORIGINAL' and fact.witness() is None
            assert fact.effect['amount_out_units']=='10000000000000000'
            assert not operator_outcome(db,prepared.identifier,as_of=cutoff).settled
            assert outcome_witnesses(db,[prepared.identifier])==[]
