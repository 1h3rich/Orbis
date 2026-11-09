"""Read-only TESTNET capture for FOK bracket / reduce-only IOC experiments."""

from collections.abc import Callable
from decimal import Decimal, localcontext
import re
import time

from app.integrations.grvt_readonly import _order
from app.live_execution.grvt_mainnet_preflight import account_state, canonical, number
from app.live_execution.grvt_mainnet_remote_order import standalone_order_metadata
from app.live_execution.grvt_testnet_bracket import bracket_client_ids
from app.live_execution.grvt_testnet_fills import audit_testnet_fills
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient, GrvtTestnetOrderObservation


_ROLES = ('ENTRY', 'STOP_LOSS', 'TAKE_PROFIT')
_ACTIVE = ('OPEN', 'PENDING')
_TERMINAL = ('CANCELLED', 'FILLED', 'REJECTED')
_WINDOW_NS = 15_000_000_000


class MarketProbeError(RuntimeError):
    """The sample does not provide consistent TESTNET evidence."""


def _order_sample(raw: dict, sub: str, role: str, at: int) -> dict:
    observed = _order(raw, sub, open_only=False, now_ns=at)
    (leg,) = observed.pop('legs')
    state = raw['state']
    reason = state.get('reject_reason')
    book = state.get('book_size')
    if reason is not None and (not isinstance(reason,str) or not re.fullmatch(r'[A-Z0-9_]{1,80}', reason)):
        raise ValueError('invalid reason')
    if book is not None and (type(book) is not list or len(book) != 1 or number(book[0]) < 0):
        raise ValueError('invalid book size')
    if 'create_time_ns' not in observed or int(observed['update_time_ns']) > at:
        raise ValueError('order time outside sample')
    if observed['order_id'] == '0':
        raise ValueError('invalid exchange identity')
    tif = {'ENTRY':'FILL_OR_KILL', 'REDUCTION':'IMMEDIATE_OR_CANCEL'}.get(role, 'GOOD_TILL_TIME')
    if (raw['time_in_force'] != tif or raw['is_market'] is not False or raw['post_only'] is not False
            or observed['reduce_only'] is not (role != 'ENTRY') or leg['limit_price'] is None
            or number(leg['limit_price']) <= 0):
        raise ValueError('wrong order terms')
    if role in ('ENTRY', 'REDUCTION'):
        if not standalone_order_metadata(raw['metadata']):
            raise ValueError('unexpected trigger')
        trigger_price = None
    else:
        trigger = raw['metadata']['trigger']
        tpsl = trigger['tpsl']
        if (trigger['trigger_type'] != role or tpsl['trigger_by'] != 'LAST'
                or tpsl['close_position'] is not False or tpsl['is_split_position'] is not False
                or number(tpsl['trigger_price']) <= 0):
            raise ValueError('wrong native trigger')
        trigger_price = canonical(number(tpsl['trigger_price']))
    return {'role':role, **observed, **leg, 'tif':tif, 'trigger_price':trigger_price,
            'book_size':canonical(number(book[0])) if book is not None else None, 'reject_reason':reason}


def _fills(client, orders, *, cutoff: int) -> list[dict]:
    entry = orders[0]
    observation = GrvtTestnetOrderObservation(**{key:entry[key] for key in (
        'sub_account_id','client_order_id','order_id','status','instrument','side','size','traded_size',
        'limit_price','update_time_ns','create_time_ns')})
    audit = audit_testnet_fills(client, observation, now_ns=cutoff, end_time_ns=cutoff)
    if not audit.sizes_match:
        raise ValueError('entry fill history incomplete')
    fills = [('ENTRY', fill) for fill in audit.fills]
    exit_orders = {order['order_id']:order for order in orders[1:]}
    for fill in audit.other_fills:
        order = exit_orders.get(fill.order_id)
        if (order is None or fill.client_order_id != order['client_order_id'] or fill.is_liquidation
                or fill.instrument != order['instrument'] or fill.is_buyer is not (order['side'] == 'BUY')):
            raise ValueError('unrelated account fill')
        fills.append((order['role'], fill))
    by_role = {order['role']:order for order in orders}
    normalized = []
    with localcontext() as ctx:
        ctx.prec = 100
        totals = dict.fromkeys(by_role, Decimal(0))
        for role, fill in fills:
            order = by_role[role]
            if (not int(order['create_time_ns']) <= fill.event_time_ns <= int(order['update_time_ns'])
                    or fill.fee_currency != 'USDT' or fill.builder_fee != 0):
                raise ValueError('fill outside sample scope')
            price_bound = number(order['limit_price'])
            if fill.price > price_bound if order['side'] == 'BUY' else fill.price < price_bound:
                raise ValueError('fill exceeded limit')
            totals[role] += fill.size
            normalized.append({'role':role, 'trade_id':fill.trade_id, 'event_time_ns':fill.event_time_ns,
                'size':canonical(fill.size), 'price':canonical(fill.price), 'fee_usdt':canonical(fill.fee),
                'realized_pnl_usdt':canonical(fill.realized_pnl)})
        if any(totals[role] != number(order['traded_size']) for role,order in by_role.items()):
            raise ValueError('fill history disagrees with order sizes')
    return sorted(normalized, key=lambda fill: (fill['event_time_ns'], fill['trade_id']))


def capture_market_probe(client: GrvtTestnetOrderClient, sub: str, parent: str, *,
                         ioc_client_id: str | None = None, ioc_order_id: str | None = None,
                         clock_ns: Callable[[],int] = time.time_ns) -> dict:
    """Capture an isolated account with a bracket and at most one known IOC.

    Direct lookups and open-list membership are separate observations. A leg
    omitted by open_orders is preserved as evidence, even when still reported
    by an individual lookup.
    """
    try:
        if not isinstance(client, GrvtTestnetOrderClient) or (ioc_client_id is not None and ioc_order_id is not None):
            raise ValueError('TESTNET scope required')
        ids = bracket_client_ids(parent)
        start = last = clock_ns()

        def checked():
            nonlocal last
            now = clock_ns()
            if type(now) is not int or not 0 < start <= last <= now < 2**63 or now-start > _WINDOW_NS:
                raise ValueError('sample time invalid')
            last = now
            return now

        def read_orders():
            orders = []
            for cid,role in zip(ids,_ROLES):
                sample = _order_sample(client._lookup_order_payload(sub,cid), sub, role, checked())
                if sample['client_order_id'] != cid:
                    raise ValueError('wrong bracket identity')
                orders.append(sample)
            if ioc_client_id is not None:
                raw = client._lookup_order_payload(sub,ioc_client_id)
                sample = _order_sample(raw,sub,'REDUCTION',checked())
                if sample['client_order_id'] != ioc_client_id:
                    raise ValueError('wrong IOC identity')
                orders.append(sample)
            elif ioc_order_id is not None:
                raw = client.lookup_order_payload_by_id(sub,ioc_order_id)
                orders.append(_order_sample(raw,sub,'REDUCTION',checked()))
            return orders

        orders = read_orders()
        entry = orders[0]
        instrument = entry['instrument']
        if (not instrument.endswith('_USDT_Perp') or entry['status'] != 'FILLED'
                or number(entry['size']) != number(entry['traded_size'])
                or len({order['order_id'] for order in orders}) != len(orders)
                or len({order['client_order_id'] for order in orders}) != len(orders)
                or any(order['instrument'] != instrument or order['side'] == entry['side'] for order in orders[1:])):
            raise ValueError('not a fully filled isolated bracket')
        raw_account = client.read_testnet('account_summary',sub)['result']
        raw_open = client.read_testnet('open_orders',sub)['result']
        at = checked()
        by_id = {order['order_id']:order for order in orders}
        if type(raw_open) is not list or len(raw_open) > len(orders):
            raise ValueError('unexpected open order scope')
        listed = set()
        for raw in raw_open:
            expected = by_id[raw['order_id']]
            if (expected['role'] in listed or expected['status'] not in _ACTIVE
                    or _order_sample(raw,sub,expected['role'],at) != expected):
                raise ValueError('open order disagrees with direct lookup')
            listed.add(expected['role'])
        account = account_state(raw_account,sub,at,allow_position=instrument,open_order_count=len(listed))
        cutoff = account['event_time_ns']
        if (not 0 <= at-cutoff <= _WINDOW_NS
                or any(int(order['update_time_ns']) > cutoff for order in orders)):
            raise ValueError('account time stale or inconsistent')
        fills = _fills(client,orders,cutoff=cutoff)
        if orders != read_orders():
            raise ValueError('orders changed during capture')
        end = checked()
        entry_completed = max(fill['event_time_ns'] for fill in fills if fill['role'] == 'ENTRY')
        if any(fill['event_time_ns'] < entry_completed for fill in fills if fill['role'] != 'ENTRY'):
            raise ValueError('exit precedes completed entry')
        with localcontext() as ctx:
            ctx.prec = 100
            remaining = number(entry['traded_size']) - sum(number(row['traded_size']) for row in orders[1:])
            signed = remaining if entry['side'] == 'BUY' else -remaining
            if (number(account['settle_balance']) + number(account['unrealized_pnl']) != number(account['total_equity'])
                    or min(number(account['initial_margin']), number(account['maintenance_margin'])) < 0):
                raise ValueError('account equity disagrees with balance')
        expected_position = [[instrument, canonical(signed)]] if signed else []
        if remaining < 0 or account['positions'] != expected_position:
            raise ValueError('signed account position disagrees with fills')
        stop = orders[1]
        return {'schema':'orbis.grvt.testnet.market-probe.v1', 'network':'TESTNET', 'observed_at_ns':end,
                'remaining_size':canonical(remaining), 'stop_order_size':stop['size'],
                'stop_book_size':stop['book_size'], 'ioc_terminal':orders[-1]['status'] in _TERMINAL if len(orders)==4 else None,
                'open_order_roles':sorted(listed), 'account':account, 'orders':orders, 'fills':fills,
                'authorization':False}
    except Exception:
        raise MarketProbeError('La muestra GRVT Testnet no se pudo conciliar; vuelve a capturar por los mismos IDs') from None
