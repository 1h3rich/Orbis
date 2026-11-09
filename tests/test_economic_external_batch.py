"""Coupled historical costs from actual signed receipts, without storage writes."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import importlib
import json
from uuid import uuid4

import pytest

from app.portfolio.economic_external_plan import prepare_external_effects
from app.portfolio.economic_external_effects import EffectAnnotation,AssetScale
from app.portfolio.economic_projection import ProjectionError
from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
from tests.external_batch_fixture import batch_case
from tests.test_economic_receipt_source import counts


def test_the_return_transfer_cannot_be_calculated_as_an_isolated_receipt(tmp_path):
    with batch_case(tmp_path)as c:
        second=dict(c.declarations[1]);inputs=second.pop('inputs')
        with pytest.raises(ProjectionError):prepare_external_effects(inputs,recorded_at=c.recorded_at,**second)


@pytest.mark.parametrize('asset',[False,True],ids=['CASH','ASSET'])
def test_joint_proposal_derives_both_transfers_in_real_order_without_capital_or_strategy_profit(tmp_path,asset):
    with batch_case(tmp_path,asset=asset)as c:
        batch=importlib.import_module('app.portfolio.economic_external_batch')
        declarations=tuple(batch.ExternalDeclaration(**item)for item in c.declarations)
        before=counts(c.vault);identifier=str(uuid4())
        result=batch.prepare_external_batch(declarations,batch_id=identifier,root_account_id=c.account,recorded_at=c.recorded_at)
        assert counts(c.vault)==before
        assert batch.prepare_external_batch(tuple(reversed(declarations)),batch_id=identifier,
            root_account_id=c.account,recorded_at=c.recorded_at).body_json==result.body_json
        left,right=result.projection.snapshots[c.account],result.projection.snapshots[c.peer]
        if asset:
            assert(left.positions['WBNB','USDT'].quantity,left.positions['WBNB','USDT'].cost_basis)==(D('0.997'),D('99.7'))
            assert(right.positions['WBNB','USDT'].quantity,right.positions['WBNB','USDT'].cost_basis)==(D('0.003'),D('0.3'))
            assert sorted(event.input.gross_amount for event in result.created_events if event.input.kind=='TRANSFER_IN')==[D('0.2'),D('0.5')]
        else:assert left.cash['USDT']==D('999.997')and right.cash['USDT']==D('0.003')
        assert right.contributions=={'BNB':D(1)} and left.withdrawals==right.withdrawals=={}
        for account,entry in zip((c.account,c.peer),c.entries,strict=True):
            assert result.projection.snapshots[account].cash['BNB']==D(1)-D(entry.proof['receipt']['gas_fee_wei']).scaleb(-18)
        assert len(result.created_events)==6 and len(result.plans)==2
        body=json.loads(result.body_json)
        assert body['manual_review_complete']and not body['saved']and not body['trading_authority']and not body['strategy_executed']
        for entry in c.entries:
            with c.vault.session()as db:
                with pytest.raises(ValueError):assert_wallet_nonce_available(db,entry.current.meta['owner'],str(entry.current.source.nonce+1))
        for raw in(c.meta['owner'],c.meta['operator'],*(entry.current.hash for entry in c.entries)):
            assert raw.lower()not in result.body_json.lower()


def test_joint_cost_derivation_flows_into_an_existing_later_sale_without_editing_it(tmp_path):
    with batch_case(tmp_path,later_sale=True)as c:
        batch=importlib.import_module('app.portfolio.economic_external_batch');originals=c.inputs[0].events
        result=batch.prepare_external_batch(tuple(batch.ExternalDeclaration(**item)for item in c.declarations),
            batch_id=str(uuid4()),root_account_id=c.account,recorded_at=c.recorded_at)
        peer=result.projection.snapshots[c.peer].positions['WBNB','USDT']
        assert(peer.quantity,peer.cost_basis,peer.realized_pnl)==(D('0.006'),D('0.9'),D('0.3'))
        assert result.projection.snapshots[c.account].positions['WBNB','USDT'].cost_basis==D('99.8')
        assert c.inputs[0].events==originals


def test_transfers_between_two_wallets_of_one_ledger_change_only_its_actual_gas(tmp_path):
    with batch_case(tmp_path,shared_custody=True)as c:
        batch=importlib.import_module('app.portfolio.economic_external_batch')
        result=batch.prepare_external_batch(tuple(batch.ExternalDeclaration(**item)for item in c.declarations),
            batch_id=str(uuid4()),root_account_id=c.account,recorded_at=c.recorded_at)
        assert set(result.projection.snapshots)=={c.account}and len(result.created_events)==2
        account=result.projection.snapshots[c.account]
        assert account.positions['WBNB','USDT'].quantity==D(1)and account.positions['WBNB','USDT'].cost_basis==D(100)
        assert account.cash['BNB']==D(1)-sum(D(entry.proof['receipt']['gas_fee_wei']).scaleb(-18)for entry in c.entries)
        assert account.contributions=={'USDT':D(1000),'BNB':D(1)}and not account.withdrawals


@pytest.mark.parametrize('field',['observation_id','order_id','review_id'])
def test_duplicate_source_identity_is_rejected_before_any_proposal_is_saved(tmp_path,field):
    with batch_case(tmp_path)as c:
        batch=importlib.import_module('app.portfolio.economic_external_batch')
        values=[dict(item)for item in c.declarations]
        if field=='review_id':values[1][field]=values[0][field]
        else:values[1]['inputs']=replace(values[1]['inputs'],**{field:getattr(values[0]['inputs'],field)})
        before=counts(c.vault)
        with pytest.raises(ProjectionError):batch.prepare_external_batch(tuple(batch.ExternalDeclaration(**item)for item in values),
            batch_id=str(uuid4()),root_account_id=c.account,recorded_at=c.recorded_at)
        assert counts(c.vault)==before


@pytest.mark.parametrize('field',['events','currents','account_names','unavailable_event_ids'])
def test_captures_from_different_book_snapshots_cannot_be_combined(tmp_path,field):
    with batch_case(tmp_path)as c:
        batch=importlib.import_module('app.portfolio.economic_external_batch');values=[dict(item)for item in c.declarations]
        old=getattr(values[1]['inputs'],field)
        changed=frozenset({'00000000-0000-4000-8000-000000000000'})if field=='unavailable_event_ids'else old[:-1]
        values[1]['inputs']=replace(values[1]['inputs'],**{field:changed})
        with pytest.raises(ProjectionError):batch.prepare_external_batch(tuple(batch.ExternalDeclaration(**item)for item in values),
            batch_id=str(uuid4()),root_account_id=c.account,recorded_at=c.recorded_at)


@pytest.mark.parametrize('mode',['NO_GAS','DOUBLE_GAS','SHARED_EVENT','UNASSIGNED_EVENT'])
def test_every_observed_gas_unit_and_event_belongs_to_exactly_one_receipt(tmp_path,mode):
    with batch_case(tmp_path)as c:
        batch=importlib.import_module('app.portfolio.economic_external_batch');values=[dict(item)for item in c.declarations]
        second=values[1];gas=second['new_events'][0]
        if mode=='NO_GAS':
            second['new_events']=second['new_events'][1:];second['members']=second['members'][1:]
        elif mode=='DOUBLE_GAS':
            second['new_events']=(replace(gas,input=replace(gas.input,amount=gas.input.amount*2)),*second['new_events'][1:])
        elif mode=='SHARED_EVENT':
            second['new_events']=second['new_events'][1:];second['members']=(values[0]['members'][0],*second['members'][1:])
        else:second['members']=second['members'][:-1]
        with pytest.raises(ProjectionError):batch.prepare_external_batch(tuple(batch.ExternalDeclaration(**item)for item in values),
            batch_id=str(uuid4()),root_account_id=c.account,recorded_at=c.recorded_at)


def test_unknown_manual_coverage_keeps_the_joint_result_incomplete_without_authority(tmp_path):
    with batch_case(tmp_path)as c:
        batch=importlib.import_module('app.portfolio.economic_external_batch');values=[dict(item)for item in c.declarations]
        values[1]['annotations']=(EffectAnnotation('unobserved-effects','UNKNOWN','Cobertura pendiente de revisar'),)
        result=batch.prepare_external_batch(tuple(batch.ExternalDeclaration(**item)for item in values),batch_id=str(uuid4()),
            root_account_id=c.account,recorded_at=c.recorded_at)
        body=json.loads(result.body_json)
        assert not body['manual_review_complete']and not body['acceptance_available']and not body['trading_authority']


def test_conflicting_manual_asset_precisions_cannot_share_a_financial_projection(tmp_path):
    with batch_case(tmp_path)as c:
        batch=importlib.import_module('app.portfolio.economic_external_batch');values=[dict(item)for item in c.declarations]
        values[0]['asset_scales']=(AssetScale('FOO',8),);values[1]['asset_scales']=(AssetScale('FOO',18),)
        with pytest.raises(ProjectionError):batch.prepare_external_batch(tuple(batch.ExternalDeclaration(**item)for item in values),
            batch_id=str(uuid4()),root_account_id=c.account,recorded_at=c.recorded_at)
