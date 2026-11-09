"""Comparison uses exported observations, decimal arithmetic and original order scope."""

from copy import deepcopy
import json

import pytest

from app.live_execution.grvt_testnet_market_comparison import _stop_ioc_fill_order, compare_market_probes
from app.live_execution.grvt_testnet_market_probe import MarketProbeError, capture_market_probe
from tests.test_grvt_testnet_market_probe import NOW, PARENT, REDUCTION, SUB, _capture, _venue


def pair(direction='LONG'):
    venue = _venue(direction)
    before = _capture(venue)
    new_stamp = NOW + 100
    order = venue.orders['REDUCTION']
    order['metadata']['create_time'] = str(new_stamp)
    order['state'].update(traded_size=['0.1'],update_time=str(new_stamp))
    stop = venue.orders['STOP_LOSS']
    stop['state'].update(status='OPEN',book_size=['0.4'],update_time=str(new_stamp))
    venue.open.append(stop)
    fill = venue.fill('REDUCTION','0.1')
    fill.update(event_time=str(new_stamp),realized_pnl='0.05',fee='-0.01')
    venue.fills.append(fill)
    venue.account.update(positions=venue.position('0.4'),event_time=str(new_stamp+1),total_equity='10000.05')
    venue.account['spot_balances'][0]['balance'] = '10000.05'
    after = capture_market_probe(venue.client,SUB,PARENT,ioc_client_id=REDUCTION,clock_ns=lambda:new_stamp+2)
    group = {'sub_account_id':SUB,'instrument':'BTC_USDT_Perp',
             'legs':[{key:row[key] for key in ('role','client_order_id','side','limit_price','trigger_price','size')}
                     for row in before['orders']],
             'acknowledged_orders':[{key:row[key] for key in ('client_order_id','order_id')} for row in before['orders']]}
    return before,after,group


def compare(before,after,group):
    return compare_market_probes(json.dumps(before),json.dumps(after),parent=PARENT,group=group)


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_compare_reduction_preserves_stop_and_exact_cash_observation(direction):
    before,after,group = pair(direction)
    # Browser JSON serializes every nanosecond as text; CLI output uses both representations.
    after['observed_at_ns'] = str(after['observed_at_ns'])
    after['account']['event_time_ns'] = str(after['account']['event_time_ns'])
    after['fills'][-1]['event_time_ns'] = str(after['fills'][-1]['event_time_ns'])
    result = compare(before,after,group)
    assert result['before']['position_size']=='0.5' and result['after']['position_size']=='0.4'
    assert result['before']['stop_listed'] is False and result['after']['stop_listed'] is True
    assert result['after']['stop_size']=='0.5' and result['after']['stop_book_size']=='0.4'
    assert result['after']['ioc_filled']=='0.1'
    assert result['new_fills']==1 and result['authorization'] is False
    assert result['cash_delta_usdt']==result['fill_delta_usdt']=='0.06'
    assert result['unexplained_cash_delta_usdt']=='0'


def test_compare_surfaces_unexplained_cash_without_discarding_order_evidence():
    before,after,group = pair()
    after['account']['settle_balance'] = after['account']['total_equity'] = '10000.06'
    report = compare(before,after,group)
    assert report['cash_delta_usdt']=='0.07' and report['unexplained_cash_delta_usdt']=='0.01'
    assert report['after']['ioc_terminal'] is True


@pytest.mark.parametrize('fault',['reverse','mainnet','subaccount','parent','other_ioc','changed_fill','backdated_fill',
                                  'changed_position','trade_dup','planned_size','funds','order_scope'])
def test_compare_rejects_cross_scope_or_discontinuous_samples(fault):
    before,after,group = pair()
    if fault=='reverse': before,after=after,before
    if fault=='mainnet': after['network']='MAINNET'
    if fault=='subaccount': after['account']['sub_account_id']='22'
    if fault=='parent': after['orders'][0]['client_order_id']=str(2**63+700)
    if fault=='other_ioc':
        before=deepcopy(after)
        before['observed_at_ns']+=100; before['account']['event_time_ns']+=100
        before['account']['observed_at_ns']+=100
        before['orders'][3]['order_id']='0xdifferent'
        before,after=after,before
    if fault=='changed_fill': after['fills'][0]['fee_usdt']='-0.01'
    if fault=='backdated_fill':
        after['orders'][3]['create_time_ns']=str(NOW-1)
        after['fills'][1]['event_time_ns']=NOW
    if fault=='changed_position': after['account']['positions']=[]
    if fault=='trade_dup': after['fills'].append(deepcopy(after['fills'][0]))
    if fault=='planned_size': after['planned_exit_sizes']={'STOP_LOSS':'0.4','TAKE_PROFIT':'0.5'}
    if fault=='funds': after['account']['settle_balance']='10001'
    if fault=='order_scope': after['orders'][1]['limit_price']='99'
    with pytest.raises(MarketProbeError,match='Muestras incompatibles'):
        compare(before,after,group)


def test_compare_rejects_duplicate_keys_in_uploaded_file():
    before,after,group = pair()
    data = json.dumps(after).replace('"network": "TESTNET"','"network": "MAINNET", "network": "TESTNET"')
    with pytest.raises(MarketProbeError):
        compare_market_probes(json.dumps(before),data,parent=PARENT,group=group)


def final_after_stop(middle):
    final = deepcopy(middle)
    stamp = middle['observed_at_ns'] + 100
    stop = final['orders'][1]
    stop.update(status='CANCELLED', traded_size='0.4', book_size='0',
                reject_reason='REDUCE_ONLY_LIMIT', update_time_ns=str(stamp))
    final['observed_at_ns'] = stamp + 2
    final['account'].update(positions=[], open_order_count=0,
                            event_time_ns=stamp + 1, observed_at_ns=stamp + 2,
                            settle_balance='10000.03', total_equity='10000.03')
    final['open_order_roles'] = []
    final['stop_book_size'] = final['remaining_size'] = '0'
    final['fills'].append({'role':'STOP_LOSS', 'trade_id':'stop-fill', 'event_time_ns':stamp,
                           'size':'0.4', 'price':'100', 'fee_usdt':'0.01', 'realized_pnl_usdt':'-0.01'})
    return final


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_three_samples_reconcile_each_interval_and_preserve_fill_order(direction):
    before, middle, group = pair(direction)
    final = final_after_stop(middle)
    result = compare_market_probes(json.dumps(before), json.dumps(middle), parent=PARENT, group=group,
                                   final_json=json.dumps(final))
    assert result['schema'] == 'orbis.grvt.testnet.market-comparison.v2'
    assert result['authorization'] is False and result['network'] == 'TESTNET'
    assert [result[key]['position_size'] for key in ('before', 'middle', 'after')] == ['0.5', '0.4', '0']
    assert result['after']['stop_reason'] == 'REDUCE_ONLY_LIMIT'
    assert result['stop_ioc_fill_order'] == 'IOC_THEN_STOP'
    assert result['intervals'] == [
        {'new_fills':1, 'cash_delta_usdt':'0.06', 'fill_delta_usdt':'0.06', 'unexplained_cash_delta_usdt':'0'},
        {'new_fills':1, 'cash_delta_usdt':'-0.02', 'fill_delta_usdt':'-0.02', 'unexplained_cash_delta_usdt':'0'},
    ]

    # Checking endpoints alone would hide these opposite unexplained differences.
    middle['account']['settle_balance'] = middle['account']['total_equity'] = '10000.07'
    result = compare_market_probes(json.dumps(before), json.dumps(middle), parent=PARENT, group=group,
                                   final_json=json.dumps(final))
    assert [step['unexplained_cash_delta_usdt'] for step in result['intervals']] == ['0.02', '-0.02']


@pytest.mark.parametrize('fault',['repeated','changed_prior_fill','changed_ioc'])
def test_three_samples_reject_discontinuous_final_cut(fault):
    before, middle, group = pair()
    final = final_after_stop(middle)
    if fault == 'repeated':
        final = middle
    elif fault == 'changed_prior_fill':
        final['fills'][1]['fee_usdt'] = '0'
    else:
        final['orders'][3]['order_id'] = '0xChanged'
    with pytest.raises(MarketProbeError, match='Muestras incompatibles'):
        compare_market_probes(json.dumps(before), json.dumps(middle), parent=PARENT, group=group,
                              final_json=json.dumps(final))


@pytest.mark.parametrize(('stop','ioc','order'), [
    ([], [], 'NONE'),
    ([], [7], 'IOC_ONLY'),
    ([5], [], 'STOP_ONLY'),
    ([7, 9], [4, 5], 'IOC_THEN_STOP'),
    ([4, 5], [7], 'STOP_THEN_IOC'),
    ([7], [7], 'INTERLEAVED_OR_TIED'),
    ([5, 9], [7], 'INTERLEAVED_OR_TIED'),
])
def test_stop_and_ioc_order_requires_disjoint_fill_times(stop, ioc, order):
    fills = [{'role':role, 'event_time_ns':stamp}
             for role, times in (('STOP_LOSS', stop), ('REDUCTION', ioc), ('TAKE_PROFIT', [6]))
             for stamp in times]
    assert _stop_ioc_fill_order(fills) == order
