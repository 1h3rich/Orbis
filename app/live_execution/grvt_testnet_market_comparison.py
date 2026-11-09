"""Compare exported Testnet observations without contacting GRVT or granting execution."""

import json
from decimal import Decimal, localcontext
import re

from app.live_execution.grvt_mainnet_preflight import canonical, number
from app.live_execution.grvt_testnet_bracket import bracket_client_ids
from app.live_execution.grvt_testnet_market_probe import (
    MarketProbeError, _ACTIVE, _ROLES, _TERMINAL, _WINDOW_NS,
)

MAX_SAMPLE_BYTES = 4 * 1024 * 1024
_SAMPLE_FIELDS = {'schema','network','observed_at_ns','remaining_size','stop_order_size','stop_book_size',
                  'ioc_terminal','open_order_roles','account','orders','fills','authorization'}
_ACCOUNT_FIELDS = {'sub_account_id','event_time_ns','observed_at_ns','margin_type','sub_account_mode',
                   'settle_currency','positions','open_order_count','other_collateral','settle_balance',
                   'total_equity','initial_margin','maintenance_margin','unrealized_pnl'}
_ORDER_FIELDS = {'role','sub_account_id','client_order_id','order_id','instrument','status','side','size',
                 'traded_size','reduce_only','limit_price','trigger_price','create_time_ns','update_time_ns',
                 'tif','book_size','reject_reason'}
_FILL_FIELDS = {'role','trade_id','event_time_ns','size','price','fee_usdt','realized_pnl_usdt'}
_ORDER_IDENTITY = ('order_id','client_order_id','instrument','side','create_time_ns','limit_price','trigger_price')
_TIFS = ('FILL_OR_KILL','GOOD_TILL_TIME','GOOD_TILL_TIME','IMMEDIATE_OR_CANCEL')


def _unique_object(pairs):
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError('duplicate key')
    return data


def _ns(value):
    if isinstance(value,str) and re.fullmatch(r'[1-9][0-9]{0,18}',value):
        value = int(value)
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError('invalid timestamp')
    return value


def read_market_probe(text, parent, group):
    if not isinstance(text,str) or len(text.encode('utf-8')) > MAX_SAMPLE_BYTES:
        raise ValueError('sample size')
    sample = json.loads(text,object_pairs_hook=_unique_object)
    if type(sample) is not dict:
        raise ValueError('sample must be a JSON object')
    keys = set(sample)
    if (keys not in (_SAMPLE_FIELDS,_SAMPLE_FIELDS|{'planned_exit_sizes'})
            or sample['schema'] != 'orbis.grvt.testnet.market-probe.v1'
            or sample['network'] != 'TESTNET' or sample['authorization'] is not False):
        raise ValueError('wrong sample schema')
    orders, account, fills = sample['orders'], sample['account'], sample['fills']
    sub = group['sub_account_id']
    if (type(orders) is not list or not 3 <= len(orders) <= 4
            or type(account) is not dict or set(account) != _ACCOUNT_FIELDS
            or type(fills) is not list or not 1 <= len(fills) <= 10_000
            or account['sub_account_id'] != sub or account['settle_currency'] != 'USDT'
            or account['margin_type'] != 'SIMPLE_CROSS_MARGIN'
            or account['sub_account_mode'] != 'SINGLE_ASSET_MODE' or account['other_collateral'] != []):
        raise ValueError('invalid sample scope')
    sample['observed_at_ns'] = _ns(sample['observed_at_ns'])
    cutoff = account['event_time_ns'] = _ns(account['event_time_ns'])
    account['observed_at_ns'] = _ns(account['observed_at_ns'])
    if not cutoff <= account['observed_at_ns'] <= sample['observed_at_ns'] <= cutoff + 2 * _WINDOW_NS:
        raise ValueError('inconsistent account time')
    if (number(account['settle_balance']) + number(account['unrealized_pnl']) != number(account['total_equity'])
            or min(number(account['initial_margin']), number(account['maintenance_margin'])) < 0):
        raise ValueError('inconsistent balances')

    roles = (*_ROLES, 'REDUCTION')[:len(orders)]
    for role,tif,order in zip(roles,_TIFS[:len(orders)],orders,strict=True):
        if (type(order) is not dict or set(order) != _ORDER_FIELDS or order['role'] != role
                or order['sub_account_id'] != sub or order['instrument'] != group['instrument']
                or not re.fullmatch(r'[A-Za-z0-9]{1,80}',order['order_id']) or order['order_id'] == '0'
                or not re.fullmatch(r'[0-9]{1,20}',order['client_order_id'])
                or order['side'] not in ('BUY','SELL') or order['tif'] != tif
                or order['reduce_only'] is not (role != 'ENTRY') or order['status'] not in (*_ACTIVE,*_TERMINAL)
                or not 0 <= number(order['traded_size']) <= number(order['size']) or number(order['size']) <= 0
                or number(order['limit_price']) <= 0
                or (order['book_size'] is not None and number(order['book_size']) < 0)
                or (order['reject_reason'] is not None and not re.fullmatch(r'[A-Z0-9_]{1,80}',order['reject_reason']))):
            raise ValueError('invalid order')
        order['create_time_ns'], order['update_time_ns'] = _ns(order['create_time_ns']), _ns(order['update_time_ns'])
        if not order['create_time_ns'] <= order['update_time_ns'] <= cutoff:
            raise ValueError('order outside account cutoff')
    if (tuple(order['client_order_id'] for order in orders[:3]) != bracket_client_ids(parent)
            or len({order['client_order_id'] for order in orders}) != len(orders)
            or len({order['order_id'] for order in orders}) != len(orders)):
        raise ValueError('order identity changed')
    entry, stop, target = orders[:3]
    if (entry['status'] != 'FILLED' or number(entry['size']) != number(entry['traded_size'])
            or entry['trigger_price'] is not None or any(order['side'] == entry['side'] for order in orders[1:])
            or any(number(order['trigger_price']) <= 0 for order in (stop,target))
            or (len(orders) == 4 and orders[3]['trigger_price'] is not None)):
        raise ValueError('invalid protected entry')
    match_market_probe_plan(sample, group)
    listed = sample['open_order_roles']
    active_roles = {order['role'] for order in orders if order['status'] in _ACTIVE}
    if (type(listed) is not list or len(listed) != len(set(listed))
            or not set(listed) <= active_roles
            or type(account['open_order_count']) is not int or account['open_order_count'] != len(listed)):
        raise ValueError('invalid open order list')
    if (sample['stop_order_size'] != stop['size'] or sample['stop_book_size'] != stop['book_size']
            or sample['ioc_terminal'] is not (orders[3]['status'] in _TERMINAL if len(orders) == 4 else None)):
        raise ValueError('inconsistent order summary')

    by_role = dict(zip(roles,orders))
    totals = dict.fromkeys(roles,Decimal(0))
    trade_ids = set()
    for fill in fills:
        if type(fill) is not dict or set(fill) != _FILL_FIELDS:
            raise ValueError('invalid fill fields')
        order = by_role[fill['role']]
        stamp = fill['event_time_ns'] = _ns(fill['event_time_ns'])
        trade_id, size, price = fill['trade_id'], number(fill['size']), number(fill['price'])
        number(fill['fee_usdt']); number(fill['realized_pnl_usdt'])
        limit = number(order['limit_price'])
        if (not re.fullmatch(r'[A-Za-z0-9_:\-]{1,100}',trade_id) or trade_id in trade_ids
                or not order['create_time_ns'] <= stamp <= order['update_time_ns']
                or size <= 0 or price <= 0 or (price > limit if order['side'] == 'BUY' else price < limit)):
            raise ValueError('invalid fill evidence')
        trade_ids.add(trade_id)
        totals[fill['role']] += size
    if any(totals[role] != number(by_role[role]['traded_size']) for role in roles):
        raise ValueError('fill sizes do not match')
    entry_completed = max(fill['event_time_ns'] for fill in fills if fill['role'] == 'ENTRY')
    if any(fill['event_time_ns'] < entry_completed for fill in fills if fill['role'] != 'ENTRY'):
        raise ValueError('exit precedes entry')
    remaining = totals['ENTRY'] - sum(totals[role] for role in roles[1:])
    signed = remaining if entry['side'] == 'BUY' else -remaining
    if (not 0 <= remaining == number(sample['remaining_size'])
            or account['positions'] != ([[entry['instrument'],canonical(signed)]] if signed else [])):
        raise ValueError('position does not match')
    return sample


def match_market_probe_plan(sample, group):
    """Verify the original bracket scope, while allowing an observed child size change."""
    acknowledgements = {row['client_order_id']:row['order_id'] for row in group['acknowledged_orders']}
    for plan, observed in zip(group['legs'],sample['orders'][:3],strict=True):
        cid = plan['client_order_id']
        if (observed['client_order_id'] != cid or observed['side'] != plan['side']
                or observed['instrument'] != group['instrument'] or observed['role'] != plan['role']
                or observed['order_id'] != acknowledgements.get(cid,observed['order_id'])
                or number(observed['limit_price']) != number(plan['limit_price'])
                or (number(observed['trigger_price']) if observed['trigger_price'] is not None else None)
                   != (number(plan['trigger_price']) if plan['trigger_price'] is not None else None)
                or (plan['role'] == 'ENTRY' and number(observed['size']) != number(plan['size']))):
            raise ValueError('sample differs from local plan')
    if 'planned_exit_sizes' in sample and sample['planned_exit_sizes'] != {
            leg['role']:leg['size'] for leg in group['legs'][1:]}:
        raise ValueError('sample exit plan changed')


def _view(sample):
    stop, target = sample['orders'][1:3]
    return {'observed_at_ns':sample['observed_at_ns'], 'position_size':sample['remaining_size'],
            'stop_status':stop['status'], 'stop_size':stop['size'], 'stop_book_size':stop['book_size'],
            'stop_listed':stop['role'] in sample['open_order_roles'], 'stop_filled':stop['traded_size'],
            'stop_reason':stop['reject_reason'], 'target_status':target['status'],
            'target_filled':target['traded_size'], 'ioc_terminal':sample['ioc_terminal'],
            'ioc_filled':sample['orders'][3]['traded_size'] if len(sample['orders']) == 4 else None}


def _compare_interval(before, after):
    cutoff = before['account']['event_time_ns']
    if (before['observed_at_ns'] >= after['observed_at_ns'] or cutoff > after['account']['event_time_ns']
            or len(before['orders']) > len(after['orders'])):
        raise ValueError('samples out of sequence')
    if len(before['orders']) == 3 and len(after['orders']) == 4 and after['orders'][3]['create_time_ns'] <= cutoff:
        raise ValueError('IOC predated initial sample')
    for old, new in zip(before['orders'], after['orders']):
        if (any(old[key] != new[key] for key in _ORDER_IDENTITY)
                or old['update_time_ns'] > new['update_time_ns']
                or number(old['traded_size']) > number(new['traded_size'])
                or old['role'] in ('ENTRY', 'REDUCTION') and number(old['size']) != number(new['size'])
                or old['status'] == 'OPEN' and new['status'] == 'PENDING'
                or old['status'] in _TERMINAL and any(old[key] != new[key] for key in
                    ('status', 'size', 'traded_size', 'book_size', 'reject_reason'))
                or old['update_time_ns'] == new['update_time_ns'] and old != new):
            raise ValueError('order history changed')
    old_fills = {row['trade_id']: row for row in before['fills']}
    new_fills = {row['trade_id']: row for row in after['fills']}
    if any(new_fills.get(key) != row for key, row in old_fills.items()):
        raise ValueError('fill history changed')
    added = [row for key, row in new_fills.items() if key not in old_fills]
    if any(row['event_time_ns'] <= cutoff for row in added):
        raise ValueError('fill arrived before first cutoff')
    observed = number(after['account']['settle_balance']) - number(before['account']['settle_balance'])
    if observed and cutoff == after['account']['event_time_ns']:
        raise ValueError('balance changed at the same cutoff')
    from_fills = sum(number(row['realized_pnl_usdt']) - number(row['fee_usdt']) for row in added)
    return {
        'new_fills': len(added),
        'cash_delta_usdt': canonical(observed),
        'fill_delta_usdt': canonical(from_fills),
        'unexplained_cash_delta_usdt': canonical(observed - from_fills),
    }, added


def _stop_ioc_fill_order(fills) -> str:
    stop = [row['event_time_ns'] for row in fills if row['role'] == 'STOP_LOSS']
    ioc = [row['event_time_ns'] for row in fills if row['role'] == 'REDUCTION']
    if not stop:
        return 'IOC_ONLY' if ioc else 'NONE'
    if not ioc:
        return 'STOP_ONLY'
    if max(ioc) < min(stop):
        return 'IOC_THEN_STOP'
    if max(stop) < min(ioc):
        return 'STOP_THEN_IOC'
    return 'INTERLEAVED_OR_TIED'


def compare_market_probes(before_json: str, after_json: str, *, parent: str, group: dict,
                          final_json: str | None = None) -> dict:
    """Verify each interval and account for the delta of its newly observed fills."""
    try:
        with localcontext() as context:
            context.prec = 100
            before = read_market_probe(before_json, parent, group)
            after = read_market_probe(after_json, parent, group)
            first, first_fills = _compare_interval(before, after)
            shared = {'network': 'TESTNET', 'before': _view(before), 'authorization': False}
            if final_json is None:
                return {'schema': 'orbis.grvt.testnet.market-comparison.v1',
                        **shared, 'after': _view(after), **first}
            final = read_market_probe(final_json, parent, group)
            last, last_fills = _compare_interval(after, final)
            return {'schema': 'orbis.grvt.testnet.market-comparison.v2', **shared,
                    'middle': _view(after), 'after': _view(final), 'intervals': [first, last],
                    'stop_ioc_fill_order': _stop_ioc_fill_order(first_fills + last_fills)}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise MarketProbeError('Muestras incompatibles o incompletas. Revisa el grupo, el orden temporal y los fills.') from None
