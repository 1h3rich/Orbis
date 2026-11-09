"""Own-chain external order certificates, profile compatibility and operator gas."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.live_execution.dex_external_observation import verify_external_call
from app.live_execution.dex_external_order import common_reference, verify_external_order, validate_external_order
from app.portfolio import dex_external_observation as stored, dex_operator_journal as operators
from app.portfolio.dex_call_resolution import DUMMY
from app.portfolio.dex_receipt_order_source import order_reference
from app.portfolio.domain import EventInput, PortfolioEvent
from app.portfolio.economic_external_effects import PartyBinding, EffectAllocation, EffectAnnotation, review_external_effects
from app.portfolio.economic_projection import ExternalOrderClaim, ExternalOrderMember
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.wallet_funding import _encoded
from tests.test_dex_call_resolution_operator import case as operator_case, lower_price, pending
from tests.test_dex_external_observation import reference
from tests.test_dex_external_observation_journal import proposal, save
from tests.test_economic_external_effects import case, uid, B


def test_external_certificate_keeps_common_domains_and_reopens_without_retaining_addresses(tmp_path):
    with case(tmp_path) as c:
        proof = c.certificate
        assert proof['schema'] == 'orbis.dex.external.order.proof.v1'
        with c.vault.session() as db:
            old = order_reference(db, SimpleNamespace(call=c.source))
            assert proof['common_tx_ref'] == old('dex-order-tx', c.hash)
            assert proof['signer_digest'] == old('dex-order-signer', c.meta['owner'])
            assert proof['owner_digest'] == old('dex-order-owner', c.meta['owner'])
            assert proof['block_transactions_ref'] == old('dex-receipt-order-block', _encoded(c.evidence['block']['transactions'])[0])
        archived_source = replace(c.source, owner=DUMMY)
        validate_external_order(archived_source, c.saved['id'], c.digest, c.proof, proof, common=c.common)
        for private in (c.meta['owner'], c.meta['operator'], c.hash): assert private.lower() not in _encoded(proof)[0].lower()
        for field in ('common_tx_ref', 'block_transactions_ref', 'signer_digest', 'observation_digest', 'source_context_digest'):
            changed = {**proof, field: 'f'*64}
            with pytest.raises(DexExecutorError):
                validate_external_order(archived_source, c.saved['id'], c.digest, c.proof, changed, common=c.common)
        for changed in (replace(archived_source, fee=3000), replace(archived_source, created_at=archived_source.created_at+timedelta(seconds=1))):
            with pytest.raises(DexExecutorError):
                validate_external_order(changed, c.saved['id'], c.digest, c.proof, proof, common=c.common)


def test_order_rejects_duplicate_block_list_and_changed_effects_and_is_stable_across_heads(tmp_path):
    with case(tmp_path, typed=True) as c:
        duplicate = deepcopy(c.evidence); duplicate['block']['transactions'] *= 2
        with pytest.raises(DexExecutorError):
            verify_external_order(c.source,c.saved['id'],c.digest,c.proof,duplicate,reference=c.reference,common=c.common,now=c.now)
        changed = deepcopy(c.proof); changed['native_value_wei'] = '1'
        with pytest.raises(DexExecutorError):
            verify_external_order(c.source,c.saved['id'],c.digest,changed,c.evidence,reference=c.reference,common=c.common,now=c.now)
        for _ in range(3): c.rpc('evm_mine', [])
        later = collect_call_resolution(c.rpc, c.source, c.hash)
        now = max(c.now,datetime.fromtimestamp(int(later['finalized']['timestamp'],16),timezone.utc))+timedelta(seconds=2)
        assert verify_external_order(c.source,c.saved['id'],c.digest,c.proof,later,reference=c.reference,common=c.common,now=now) == c.certificate


@pytest.mark.parametrize('shared_custody', [False, True])
def test_actual_operator_sender_and_owner_have_distinct_roles_with_exact_custody_accounting(tmp_path, shared_custody):
    with operator_case(tmp_path,evm_backend='anvil') as values:
        vault,account,meta,rpc,deployment,grant,_=values
        trade=lower_price(meta,rpc,int(deployment['nonce']));source,before,_=pending(values,trade)
        sent=rpc('eth_sendTransaction',[{'from':meta['operator'],'to':meta['owner'],'nonce':hex(source.nonce),
            'data':'0x','value':hex(10**15),'gas':hex(21000),'gasPrice':hex(2*10**9)}])
        rpc('miner_start',[1]);rpc('evm_mine',[])
        evidence=collect_call_resolution(rpc,source,sent)
        now=max(datetime.now(timezone.utc),datetime.fromtimestamp(int(evidence['block']['timestamp'],16),timezone.utc))+timedelta(seconds=2)
        ref=reference(vault,'OPERATOR');proof=verify_external_call(source,evidence,reference=ref,now=now)
        with vault.session() as db: operators.stop_grant(db,grant['id'],account_id=account,now=now)
        own=SimpleNamespace(vault=vault,account=account,meta=meta,source=source,evidence=evidence,hash=sent,now=now,reference=ref)
        saved=save(own,proposal(own,proof=proof))
        with vault.session() as db:
            row,body=stored.observation_record(db,saved['id']);digest=row.payload_digest;common=common_reference(_existing_salt(db))
        cert=verify_external_order(source,saved['id'],digest,proof,evidence,reference=ref,common=common,now=now)
        at=datetime.fromisoformat(cert['block_at']);sender=account if shared_custody else B
        def event(name,kind,*,acct=account,when=at,request=None,**fields):
            return PortfolioEvent(uid(name),acct,uid(request or name),now,EventInput(kind,'BNB',when,**fields))
        original=[event('operator-initial','CONTRIBUTION',when=at-timedelta(hours=1),amount=D(1))]
        if not shared_custody:
            original.extend([event('fund-out','CASH_TRANSFER_OUT',when=at-timedelta(minutes=30),amount=D('0.1'),request='operator-funding',note='Gas reservado propio'),
                event('fund-in','CASH_TRANSFER_IN',acct=B,when=at-timedelta(minutes=30),amount=D('0.1'),request='operator-funding',note='Gas reservado propio')])
        gas=event('operator-gas','FEE',acct=sender,amount=D('0.000042'),note='Gas real del firmante')
        effects=[]
        if not shared_custody:
            effects=[event('operator-out','CASH_TRANSFER_OUT',acct=B,amount=D('0.001'),request='return',note='Retorno propio'),
                event('owner-in','CASH_TRANSFER_IN',amount=D('0.001'),request='return',note='Retorno propio')]
        members=(ExternalOrderMember(gas.id,sender,'GAS'),*(ExternalOrderMember(item.id,item.account_id,'EFFECT')for item in effects))
        claim=ExternalOrderClaim('EXTERNAL_OBSERVATION',saved['id'],uid('operator-order'),'a'*64,account,account,
            int(cert['block_number']),int(cert['transaction_index']),at,cert['block_hash'],cert['block_transactions_ref'],
            int(cert['block_transaction_count']),cert['signer_digest'],int(cert['nonce']),members)
        allocation=EffectAllocation(('native:'+proof['call_ref'],),tuple(item.id for item in effects),'OWN_TRANSFER','Retorno dentro del patrimonio propio')
        bindings=(PartyBinding(proof['owner_address_ref'],account,'OWNER'),PartyBinding(proof['signer_address_ref'],sender,'SIGNER'))
        result=review_external_effects((*original,gas,*effects),(),observation_id=saved['id'],observation_digest=digest,
            observation_json=_encoded(proof)[0],claim=claim,bindings=bindings,allocations=(allocation,),
            annotations=(EffectAnnotation('unobserved-effects','MANUAL_COMPLETE','Otros efectos revisados manualmente'),))
        if shared_custody:
            assert result.projection.snapshots[account].cash['BNB']==D('0.999958')
        else:
            assert result.projection.snapshots[account].cash['BNB']==D('0.901')
            assert result.projection.snapshots[B].cash['BNB']==D('0.098958')
            assert result.projection.snapshots[B].contributions=={}
        assert result.body['gas_account_id']==sender and not result.body['strategy_executed']
        with vault.session() as db:
            row,_,_=operators.intent_record(db,source.id);assert(row.payload_json,row.state_json,row.updated_at)==before
            assert operators.grant_view(db,grant['id'],now=now)['unresolved_outcomes']
        wrong=replace(claim,members=(replace(claim.members[0],account_id=uid('wrong')), *claim.members[1:]))
        with pytest.raises(ValueError):
            review_external_effects((*original,gas,*effects),(),observation_id=saved['id'],observation_digest=digest,
                observation_json=_encoded(proof)[0],claim=wrong,bindings=bindings,allocations=(allocation,),
                annotations=(EffectAnnotation('unobserved-effects','MANUAL_COMPLETE','Revisado'),))
