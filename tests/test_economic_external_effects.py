"""Signed own-chain receipts plus independent manual ledger/cost examples.

Order certificates are verified live here. The financial declaration is still
a pure proposal, not a stored financial acceptance or user-mainnet operation.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D, Context, localcontext, ROUND_FLOOR
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from app.live_execution.dex_external_order import common_reference, verify_external_order
from app.portfolio import dex_external_observation as journal
from app.portfolio.domain import EventInput, PortfolioEvent, CALC_PRECISION
from app.portfolio.economic_external_effects import (
    PartyBinding, AssetScale, ResourceDelta, ManualEffect, EffectAllocation,
    EffectAnnotation, review_external_effects,
)
from app.portfolio.economic_projection import ExternalOrderClaim, ExternalOrderMember, ProjectionError, EXTERNAL_ENGINE, TieOrder
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.wallet_funding import _encoded
from tests.test_dex_external_observation import external_case
from tests.test_dex_external_observation_journal import proposal, save

NS = UUID('25b83d43-6712-4bd4-954c-60a1cbfb510b')
def uid(label): return str(uuid5(NS, label))
B = uid('peer-account')


@contextmanager
def case(directory, mode='TRANSFER', *, typed=False, zero_gas=False, position_gas=False):
    with external_case(directory, mode, typed, zero_gas=zero_gas) as chain:
        reviewed = proposal(chain); saved = save(chain, reviewed)
        with chain.vault.session() as db:
            row, body = journal.observation_record(db, saved['id']); digest = row.payload_digest
            common = common_reference(_existing_salt(db))
        proof = body['proof']; at = datetime.fromisoformat(proof['receipt']['block_at'])
        cert = verify_external_order(chain.source, saved['id'], digest, proof, chain.evidence,
            reference=chain.reference, common=common, now=chain.now)
        sequence = [0]
        def event(label, kind, *, account=None, at=None, quote='USDT', request=None, **values):
            sequence[0] += 1
            return PortfolioEvent(uid(label), account or chain.account, uid(request or label),
                chain.now+timedelta(microseconds=sequence[0]), EventInput(kind, quote, at or base_at, **values))
        base_at = at
        base = [event('cash', 'CONTRIBUTION', at=at-timedelta(hours=1), amount=D('1000')),
            event('holding', 'BUY', at=at-timedelta(minutes=59), asset='WBNB', quantity=D('1'), unit_price=D('100'))]
        if position_gas:
            base.extend([event('eur', 'CONTRIBUTION', at=at-timedelta(hours=1), quote='EUR', amount=D('100')),
                event('bnb-position', 'BUY', at=at-timedelta(minutes=59), quote='EUR', asset='BNB', quantity=D('1'), unit_price=D('100'))])
        else:
            base.append(event('bnb', 'CONTRIBUTION', at=at-timedelta(hours=1), quote='BNB', amount=D('1')))
        fee = D(int(proof['receipt']['gas_fee_wei'])).scaleb(-18)
        gas = [event('gas', 'FEE', quote='EUR' if position_gas else 'BNB', note='Gas externo observado',
            **({'asset': 'BNB', 'quantity': fee} if position_gas else {'amount': fee}))] if fee else []
        bindings = (PartyBinding(proof['owner_address_ref'], chain.account, 'OWNER'),)
        annotations = [EffectAnnotation('unobserved-effects', 'MANUAL_COMPLETE', 'Revisión manual de los demás efectos')]
        annotations += [EffectAnnotation('approval:'+item['log_ref'], 'PERMISSION_REVIEWED', 'Permiso revisado sin contabilizar compra') for item in proof['approvals']]
        annotations += [EffectAnnotation('uninterpreted:'+item['log_ref'], 'UNKNOWN', 'Efecto todavía desconocido') for item in proof['uninterpreted_logs']]
        def claim(effect_events=(), *, gas_events=None):
            members = [ExternalOrderMember(item.id, item.account_id, 'GAS') for item in (gas if gas_events is None else gas_events)]
            members += [ExternalOrderMember(item.id, item.account_id, 'EFFECT') for item in effect_events]
            return ExternalOrderClaim('EXTERNAL_OBSERVATION', saved['id'], uid('order-record'), 'b'*64,
                chain.account, chain.account, int(cert['block_number']), int(cert['transaction_index']), at,
                cert['block_hash'], cert['block_transactions_ref'], int(cert['block_transaction_count']),
                cert['signer_digest'], int(cert['nonce']), tuple(members))
        def run(effect_events=(), allocations=(), *, events=None, gas_events=None, **changes):
            args = dict(observation_id=saved['id'], observation_digest=digest, observation_json=_encoded(proof)[0],
                claim=claim(effect_events, gas_events=gas_events), bindings=bindings, allocations=allocations, annotations=tuple(annotations))
            args.update(changes)
            all_events = (*base, *(gas if gas_events is None else gas_events), *effect_events) if events is None else events
            return review_external_effects(all_events, (), **args)
        yield SimpleNamespace(**vars(chain), proof=proof, digest=digest, certificate=cert, common=common, saved=saved,
            at=at, event=event, base=base, gas=gas, fee=fee, bindings=bindings, annotations=annotations, claim=claim, run=run)


def own_transfer(c, *, asset=False):
    observed = c.proof['transfers'][0]; amount = D(int(observed['units'])).scaleb(-18)
    peer = PartyBinding(observed['right_ref'], B, 'PROVEN_WALLET')
    if asset:
        out = c.event('out', 'TRANSFER_OUT', asset='WBNB', quantity=amount, gross_override=D('999'), note='Traslado propio', request='pair')
        incoming = c.event('in', 'TRANSFER_IN', account=B, asset='WBNB', quantity=amount, gross_override=D('999'), note='Traslado propio', request='pair')
    else:
        out = c.event('out', 'CASH_TRANSFER_OUT', amount=amount, note='Traslado propio', request='pair')
        incoming = c.event('in', 'CASH_TRANSFER_IN', account=B, amount=amount, note='Traslado propio', request='pair')
    allocation = EffectAllocation(('token:'+observed['log_ref'],), (out.id, incoming.id), 'OWN_TRANSFER', 'Movimiento entre wallets propias')
    return (out, incoming), allocation, (*c.bindings, peer)


@pytest.mark.parametrize('typed', [False, True])
def test_signed_cash_transfer_is_internal_and_never_a_contribution_profit_or_strategy_fill(tmp_path, typed):
    with case(tmp_path, typed=typed) as c:
        events, allocation, bindings = own_transfer(c)
        reviewed = c.run(events, (allocation,), bindings=bindings)
        a, b = reviewed.projection.snapshots[c.account], reviewed.projection.snapshots[B]
        assert a.cash['USDT'] == D('899.995') and b.cash['USDT'] == D('0.005')
        assert a.cash['BNB'] == D('1')-c.fee
        assert b.contributions == b.withdrawals == {} and a.withdrawals == {}
        assert a.positions['WBNB','USDT'].realized_pnl == 0
        body = reviewed.body; assert body['manual_review_complete'] and body['projection_engine'] == EXTERNAL_ENGINE
        assert all(body[key] is False for key in ('strategy_executed','effects_proven_complete','saved','resolves_intent','accounting_acceptance_active','trading_authority'))
        assert [row.original.id for row in reviewed.projection.events if row.order_record_id] == [c.gas[0].id,events[0].id,events[1].id]
        for private in (c.meta['owner'], c.meta['operator'], c.hash): assert private.lower() not in reviewed.body_json.lower()
        with c.vault.session() as db: assert journal.status(db,c.source.kind,c.source.id)['status'] == 'EFFECTS_PENDING'


def test_signed_asset_transfer_recomputes_both_cost_legs_and_following_sale_without_editing_originals(tmp_path):
    with case(tmp_path, 'WBNB_TRANSFER') as c:
        events, allocation, bindings = own_transfer(c, asset=True)
        sale = c.event('peer-sale', 'SELL', account=B, at=c.at+timedelta(seconds=1), asset='WBNB', quantity=D('0.002'), unit_price=D('300'))
        originals = (*c.base,*c.gas,*events,sale)
        result = c.run(events,(allocation,),bindings=bindings,events=originals).projection
        left, right = result.snapshots[c.account].positions['WBNB','USDT'], result.snapshots[B].positions['WBNB','USDT']
        assert (left.quantity,left.cost_basis)==(D('0.995'),D('99.5'))
        assert (right.quantity,right.cost_basis,right.realized_pnl)==(D('0.003'),D('0.3'),D('0.4'))
        assert result.snapshots[B].contributions=={} and result.adjusted_transfer_legs==2
        moved = [row for row in result.events if row.transfer_basis_derived]
        assert all(row.original.input.gross_override==D('999') and row.accounting_input.gross_override==D('0.5') for row in moved)
        assert tuple(originals)==(*c.base,*c.gas,*events,sale)


@pytest.mark.parametrize('mode', ['ZERO','SELF','APPROVAL','NO_LOGS','CREATE'])
def test_nonfinancial_signed_calls_only_book_declared_gas_and_keep_theory_flags_false(tmp_path, mode):
    with case(tmp_path, mode) as c:
        allocations=tuple(EffectAllocation(('token:'+item['log_ref'],),(), 'NO_LEDGER_CHANGE','Transferencia nula o dentro de la misma cuenta') for item in c.proof['transfers'])
        reviewed=c.run(allocations=allocations)
        assert reviewed.body['manual_review_complete'] and not reviewed.body['strategy_executed']
        assert reviewed.projection.snapshots[c.account].cash['USDT']==D('900')
        assert reviewed.projection.snapshots[c.account].cash['BNB']==D('1')-c.fee


def test_unknown_log_remains_incomplete_until_its_effects_are_explicitly_declared(tmp_path):
    with case(tmp_path,'UNKNOWN_TOKEN') as c:
        first=c.run();assert not first.body['manual_review_complete']
        assert ('XYZ','USDT')not in first.projection.snapshots[c.account].positions
        ref='uninterpreted:'+c.proof['uninterpreted_logs'][0]['log_ref']
        manual=ManualEffect(uid('manual-mint'),ref,'Identificación y cantidad declaradas manualmente',
            (ResourceDelta(c.account,'XYZ',5000),))
        minted=c.event('minted','ADJUSTMENT_IN',asset='XYZ',quantity=D('0.005'),unit_price=D('0'),note='Coste de adquisición declarado manualmente')
        allocation=EffectAllocation(('manual:'+manual.id,),(minted.id,),'ADJUSTMENT','Alta manual de unidades, sin deducir precio del log')
        annotations=tuple(replace(item,disposition='MANUAL_EFFECTS_DECLARED')if item.reference==ref else item for item in c.annotations)
        result=c.run((minted,),(allocation,),manual_effects=(manual,),asset_scales=(AssetScale('XYZ',6),),annotations=annotations)
        assert result.body['manual_review_complete'] and not result.body['effects_proven_complete']
        position=result.projection.snapshots[c.account].positions['XYZ','USDT'];assert(position.quantity,position.cost_basis)==(D('0.005'),D(0))
        assert result.body['asset_scales'][-1]=={'asset':'XYZ','decimals':6,'basis':'USER_DECLARED'}


def test_unobserved_native_return_is_manual_and_cannot_be_mistaken_for_new_capital(tmp_path):
    with case(tmp_path,'NATIVE') as c:
        manual=ManualEffect(uid('native-refund'),'unobserved-effects','Retorno interno declarado a partir de revisión propia',
            (ResourceDelta(c.account,'BNB',2*10**14),))
        withdrawal=c.event('native-out','WITHDRAWAL',quote='BNB',amount=D('0.0008'))
        allocation=EffectAllocation(('native:'+c.proof['call_ref'],'manual:'+manual.id),(withdrawal.id,),'EXTERNAL_FLOW','Salida neta declarada, gas aparte')
        result=c.run((withdrawal,),(allocation,),manual_effects=(manual,))
        snapshot=result.projection.snapshots[c.account]
        assert snapshot.cash['BNB']==D(1)-D('0.0008')-c.fee
        assert snapshot.contributions['BNB']==1 and snapshot.withdrawals['BNB']==D('0.0008')
        assert not result.body['effects_proven_complete'] and c.proof['native_value_wei']==str(10**15)


def test_gas_consumes_original_bnb_cost_without_inventing_currency_conversion(tmp_path):
    with case(tmp_path,'APPROVAL',position_gas=True) as c:
        result=c.run();position=result.projection.snapshots[c.account].positions['BNB','EUR']
        assert position.quantity==D(1)-c.fee and position.cost_basis==D(100)*(D(1)-c.fee)
        assert position.realized_pnl==-D(100)*c.fee and position.fees_by_asset['BNB']==c.fee


def test_zero_fee_signed_receipt_creates_no_fake_gas_or_ledger_event(tmp_path):
    with case(tmp_path,'APPROVAL',zero_gas=True) as c:
        assert c.proof['receipt']['gas_fee_wei']=='0' and c.gas==[]
        reviewed=c.run(events=())
        assert reviewed.projection.engine==EXTERNAL_ENGINE and reviewed.projection.claimed_transactions==1
        assert reviewed.projection.events==() and reviewed.projection.snapshots[c.account].cash=={}
        assert reviewed.body['selected_events']==[] and reviewed.body['manual_review_complete']


def test_malicious_allocations_cannot_omit_duplicate_reattribute_or_turn_owned_funds_into_capital(tmp_path):
    with case(tmp_path) as c:
        events,allocation,bindings=own_transfer(c)
        trials=[dict(allocations=()),dict(allocations=(allocation,allocation)),
            dict(bindings=c.bindings),dict(allocations=(replace(allocation,event_ids=(events[0].id,)),)),
            dict(allocations=(replace(allocation,classification='EXTERNAL_FLOW'),)),
            dict(unavailable_event_ids=frozenset((events[1].id,))),
            dict(bindings=tuple(replace(item,role='PROVEN_WALLET')if item.role=='OWNER'else item for item in bindings))]
        for changes in trials:
            args=dict(bindings=bindings,allocations=(allocation,));args.update(changes)
            with pytest.raises(ProjectionError):c.run(events,**args)
        wrong=c.event('wrong-gas','FEE',account=B,quote='BNB',amount=c.fee,note='Gas de otra cuenta')
        with pytest.raises(ProjectionError,match='pagador'):c.run(events,(allocation,),bindings=bindings,gas_events=(wrong,))
        bad=c.event('wrong-units','CASH_TRANSFER_IN',account=B,amount=D('0.004'),note='Traslado propio',request='pair')
        with pytest.raises(ProjectionError,match='unidades'):c.run((events[0],bad),(replace(allocation,event_ids=(events[0].id,bad.id)),),bindings=bindings)


def test_gas_subunits_missing_coverage_wrong_order_and_invented_funding_are_rejected(tmp_path):
    with case(tmp_path,'NATIVE') as c:
        out=c.event('withdraw','WITHDRAWAL',quote='BNB',amount=D('0.001'))
        allocation=EffectAllocation(('native:'+c.proof['call_ref'],),(out.id,),'EXTERNAL_FLOW','Salida explícita')
        with pytest.raises(ProjectionError,match='cobertura'):c.run((out,),(allocation,),annotations=())
        wrong=c.event('subunit','FEE',quote='BNB',amount=c.fee+D('0.0000000000000000001'),note='Fracción no representable')
        with pytest.raises(ProjectionError,match='unidad mínima'):c.run((out,),(allocation,),gas_events=(wrong,))
        short=c.event('short','FEE',quote='BNB',amount=c.fee-D('0.000000000000000001'),note='Gas incompleto')
        with pytest.raises(ProjectionError,match='exactamente'):c.run((out,),(allocation,),gas_events=(short,))
        with pytest.raises(ProjectionError,match='preceder'):
            replace(c.claim((out,)),members=tuple(reversed(c.claim((out,)).members)))
        only_gas=[*c.gas,out]
        with pytest.raises(ProjectionError):c.run((out,),(allocation,),events=only_gas)


def test_review_is_deterministic_under_hostile_decimal_context_and_detaches_its_body(tmp_path):
    with case(tmp_path) as c:
        events,allocation,bindings=own_transfer(c)
        first=c.run(events,(allocation,),bindings=bindings)
        with localcontext(Context(prec=6,rounding=ROUND_FLOOR)):
            later=c.run(events,(allocation,),bindings=bindings)
        assert first.body_json==later.body_json and first.digest==later.digest and first.projection==later.projection
        detached=later.body;detached['bindings'].clear();assert later.body['bindings']
        assert all(c.meta['owner'].lower()not in value.lower()for value in(first.body_json,later.body_json))


def test_one_receipt_participates_in_ties_of_both_accounts_and_rejects_a_transfer_cycle(tmp_path):
    with case(tmp_path,'WBNB_TRANSFER') as c:
        events,allocation,bindings=own_transfer(c,asset=True)
        sale=c.event('same-time-sale','SELL',account=B,asset='WBNB',quantity=D('0.005'),unit_price=D('200'))
        sale=replace(sale,recorded_at=c.now-timedelta(days=1))
        original=(*c.base,*c.gas,*events,sale)
        order=TieOrder(B,c.at,(c.claim(events).node_id,'event:'+sale.id))
        result=c.run(events,(allocation,),bindings=bindings,events=original,tie_orders=(order,))
        assert result.projection.snapshots[B].positions['WBNB','USDT'].realized_pnl==D('0.5')
        out=c.event('manual-out','CASH_TRANSFER_OUT',amount=D(1),request='manual-transfer',note='Propio manual')
        incoming=c.event('manual-in','CASH_TRANSFER_IN',account=B,amount=D(1),request='manual-transfer',note='Propio manual')
        cyclic=(TieOrder(c.account,c.at,(c.claim(events).node_id,'event:'+out.id)),
            TieOrder(B,c.at,('event:'+incoming.id,c.claim(events).node_id)))
        with pytest.raises(ProjectionError,match='ciclo'):
            c.run(events,(allocation,),bindings=bindings,events=(*c.base,*c.gas,*events,out,incoming),tie_orders=cyclic)


def test_later_manual_receipt_credit_cannot_finance_its_own_gas_retroactively(tmp_path):
    with case(tmp_path,'APPROVAL') as c:
        manual=ManualEffect(uid('later-native'),'unobserved-effects','Ingreso declarado durante la llamada',
            (ResourceDelta(c.account,'BNB',10**15),))
        credit=c.event('late-credit','CONTRIBUTION',quote='BNB',amount=D('0.001'))
        allocated=EffectAllocation(('manual:'+manual.id,),(credit.id,),'EXTERNAL_FLOW','Ingreso explícito posterior al gas')
        with pytest.raises(ProjectionError):
            c.run((credit,),(allocated,),manual_effects=(manual,),events=(*c.gas,credit))


@pytest.mark.parametrize('secret',['0x'+'12'*20,'0x'+'00'*32,'ab'*32,'sk-'+'x'*24])
def test_new_declarations_reject_raw_addresses_transaction_hashes_and_secrets(secret):
    with pytest.raises(ProjectionError):
        EffectAnnotation('unobserved-effects','MANUAL_COMPLETE',secret)


def test_selected_event_identity_does_not_copy_existing_private_free_text(tmp_path):
    with case(tmp_path,'APPROVAL') as c:
        private='0x'+'0'*64
        gas=replace(c.gas[0],input=replace(c.gas[0].input,note=private))
        reviewed=c.run(gas_events=(gas,))
        assert private not in reviewed.body_json
        assert 'input_digest' in reviewed.body['selected_events'][0] and 'input'not in reviewed.body['selected_events'][0]


def test_manual_units_scales_and_registry_cannot_be_mutated_or_coerced():
    from app.portfolio.economic_external_effects import SCALES
    for asset,decimals in [('BNB',18),('USDT',6),('XYZ',True),('XYZ',37)]:
        with pytest.raises(ProjectionError):AssetScale(asset,decimals)
    with pytest.raises(ProjectionError):ResourceDelta(B,'BNB',True)
    with pytest.raises(ProjectionError):ResourceDelta(B,'BNB',2**256)
    with pytest.raises(ProjectionError):ManualEffect(uid('invalid-own'),'unobserved-effects','Movimiento manual',
        (ResourceDelta(B,'BNB',1),),own_transfer=True)
    with pytest.raises(TypeError):SCALES['BNB']=6


def test_review_identity_pins_full_funding_history_cost_order_and_event_metadata(tmp_path):
    with case(tmp_path,'WBNB_TRANSFER') as c:
        events,allocation,bindings=own_transfer(c,asset=True)
        original=(*c.base,*c.gas,*events)
        first=c.run(events,(allocation,),bindings=bindings,events=original)
        changed=[replace(item,input=replace(item.input,unit_price=D('200')))if item.id==uid('holding')else item for item in original]
        later=c.run(events,(allocation,),bindings=bindings,events=changed)
        assert first.digest!=later.digest
        assert first.body['ledger_input_digest']!=later.body['ledger_input_digest']
        assert first.body['projection_digest']!=later.body['projection_digest']
        assert first.projection.snapshots[B].positions['WBNB','USDT'].cost_basis==D('0.5')
        assert later.projection.snapshots[B].positions['WBNB','USDT'].cost_basis==D('1')
        same=c.run(events,(allocation,),bindings=tuple(reversed(bindings)),events=tuple(reversed(original)))
        assert same.digest==first.digest and same.body_json==first.body_json
        changed_stamp=[replace(item,recorded_at=item.recorded_at+timedelta(microseconds=1))if item.id==uid('cash')else item for item in original]
        assert c.run(events,(allocation,),bindings=bindings,events=changed_stamp).digest!=first.digest
