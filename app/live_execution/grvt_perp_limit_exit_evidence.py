"""Replayed evidence for a bounded reduction of a terminal GTT entry."""
from decimal import Decimal

from app.lab.perp_book_types import exact
from app.live_execution.grvt_mainnet_bracket import exact_uint
from app.live_execution.grvt_mainnet_finance import _ACCOUNT_FIELDS, _FILL_FIELDS, _FUND_FIELDS
from app.live_execution.grvt_mainnet_journal import encode, signer_ref
from app.live_execution.grvt_mainnet_preflight import canonical, number
from app.live_execution.grvt_mainnet_reduction_order import (
    BoundedReductionScope, bounded_reduction, unsigned_reduction, verify_reduction,
)
from app.live_execution.grvt_mainnet_remote_order import remote_contract
from app.live_execution.grvt_mainnet_transport import HISTORY_URLS
from app.live_execution.grvt_perp_funding_link import funding_trade_times
from app.live_execution.grvt_perp_limit_plan import FRESH_NS, NS, PerpLimitEntryPlan
from app.live_execution.grvt_perp_limit_receipt import (
    TERMINAL, TRADE_ID, LimitObservationPending, PerpLimitOrderRead, read_bound_order,
)


def _time(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError('Instante de salida LIMIT inválido')
    return value


def reduction_scope(plan: PerpLimitEntryPlan, remaining: Decimal) -> BoundedReductionScope:
    terms = plan.source.terms
    budget = terms.budget
    return BoundedReductionScope(budget.sub_account_id, terms.instrument, terms.definition['direction'],
                                 remaining, number(budget.exit_notional_limit),
                                 budget.max_spread_bps, budget.max_slippage_bps)


@exact
def limit_reduction_template(plan: PerpLimitEntryPlan, remaining: Decimal, quote: dict, *,
                             cid: str, nonce: int, issued_at_ns: int, expiration_ns: int) -> dict:
    scope = reduction_scope(plan, remaining)
    instrument = plan.source.instrument.instrument()
    size, price = bounded_reduction(quote, scope, instrument, remaining, at_ns=issued_at_ns)
    return unsigned_reduction(scope, instrument, cid=cid, size=size, price=price, nonce=nonce,
                              issued_at_ns=issued_at_ns, expiration_ns=expiration_ns)


def _read_reduction(plan, reduction, raw, salt, cutoff):
    claim, template = reduction['claim'], reduction['template']
    reading = PerpLimitOrderRead.model_validate(raw)
    wire = remote_contract(reading.order, template)
    signer = verify_reduction(wire, template, plan.source.instrument.instrument())
    if encode(wire)[1] != claim['wire_digest'] or signer_ref(salt, signer) != claim['signer_ref']:
        raise ValueError('El eco de salida LIMIT no corresponde al contrato firmado')
    order_id, status, reason, update, size, _ = read_bound_order(wire, claim['issued_at_ns'], reading)
    if not update <= int(reading.received_at_ns) <= cutoff or reduction['ack'] not in (None,order_id):
        raise ValueError('Salida LIMIT sin estado terminal confirmado')
    if status not in TERMINAL:
        raise LimitObservationPending('La reducción LIMIT sigue pendiente de confirmación terminal')
    return {'client_order_id':claim['client_order_id'], 'order_id':order_id, 'status':status,
            'reason':reason, 'updated_at_ns':update, 'filled_size':canonical(size)}


def _verify_histories(histories: dict) -> None:
    if set(histories) != set(HISTORY_URLS):
        raise ValueError('Faltan historiales financieros de salida LIMIT')
    for name, section in histories.items():
        if (set(section) != {'pages','drained','rows'} or type(section['pages']) is not int
                or not 1 <= section['pages'] <= 20 or section['drained'] is not True
                or type(section['rows']) is not list or len(section['rows']) > section['pages'] * 500
                or name not in ('fill_history','funding_payment_history') and section['rows']):
            raise ValueError('Historial LIMIT incompleto o actividad externa')


def _account_snapshot(account, sub, positions, cutoff, at, baseline, net):
    received = _time(account['observed_at_ns'])
    event = _time(account['event_time_ns'])
    balance = number(account['settle_balance'])
    unrealized = number(account['unrealized_pnl'])
    if (set(account) != _ACCOUNT_FIELDS or account['sub_account_id'] != sub
            or account['settle_currency'] != 'USDT' or account['margin_type'] != 'SIMPLE_CROSS_MARGIN'
            or account['sub_account_mode'] != 'SINGLE_ASSET_MODE' or account['other_collateral'] != []
            or account['positions'] != positions or type(account['open_order_count']) is not int
            or account['open_order_count'] != 0 or not cutoff <= event <= received <= at
            or min(number(account['initial_margin']), number(account['maintenance_margin'])) < 0
            or not positions and any(number(account[key]) != 0 for key in
                ('initial_margin','maintenance_margin','unrealized_pnl'))
            or balance != baseline + net or number(account['total_equity']) != balance + unrealized):
        raise ValueError('La posición o la caja GRVT no cuadran con los fills LIMIT')
    return balance


@exact
def calculate_limit_exit_audit(plan: PerpLimitEntryPlan, claim: dict, entry: dict, reductions: list[dict],
                               source: dict, *, salt: str) -> dict:
    if (set(source) != {'cutoff_ns','observed_at_ns','orders','histories','account'}
            or entry is None or not entry['terminal'] or number(entry['filled_size']) <= 0
            or type(source['orders']) is not list or len(source['orders']) != len(reductions)):
        raise ValueError('La salida LIMIT requiere una entrada terminal con exposición')
    cutoff, at = _time(source['cutoff_ns']), _time(source['observed_at_ns'])
    start = _time(claim['preflight']['market']['states'][-1]['observed_at_ns'])
    if not start <= int(entry['order_updated_at_ns']) <= cutoff <= at <= cutoff + FRESH_NS:
        raise ValueError('Corte financiero LIMIT no verificable')
    histories = source['histories']
    _verify_histories(histories)
    instrument = plan.source.instrument.instrument()
    sub = plan.source.terms.budget.sub_account_id
    long = plan.result['side'] == 'BUY'
    normalized = [_read_reduction(plan,reduction,row,salt,cutoff)
                  for reduction,row in zip(reductions,source['orders'])]
    ids = {entry['order_id'],*(row['order_id'] for row in normalized)}
    if len(ids) != len(normalized)+1:
        raise ValueError('Identificadores GRVT repetidos en la salida LIMIT')
    by_cid = {row['client_order_id']:(row,reduction)
              for row,reduction in zip(normalized,reductions)}
    entry_fills, exit_fills, seen = [], [], set()
    amounts = {cid:Decimal(0) for cid in by_cid}
    notionals = amounts.copy()
    breaches = set()
    budget = plan.source.terms.budget
    exits_notional = fees = reported = Decimal(0)
    entry_last = max(_time(fill['event_time_ns']) for fill in entry['fills'])
    for fill in histories['fill_history']['rows']:
        cid, trade = fill['client_order_id'], fill['trade_id']
        ts = _time(fill['event_time_ns'])
        if (set(fill) != _FILL_FIELDS or type(trade) is not str or not TRADE_ID.fullmatch(trade)
                or trade in seen or not start <= ts <= cutoff):
            raise ValueError('Fill duplicado o fuera del corte LIMIT')
        seen.add(trade)
        if cid == entry['client_order_id']:
            entry_fills.append(fill)
            continue
        if cid not in by_cid:
            raise ValueError('Fill externo en la subcuenta dedicada LIMIT')
        order, reduction = by_cid[cid]
        leg = reduction['template']['legs'][0]
        size, price = number(fill['size']), number(fill['price'])
        bound = number(leg['limit_price'])
        exact_uint(size,instrument.base_decimals,2**64-1)
        exact_uint(price,9,2**64-1)
        if (fill['order_id'] != order['order_id'] or fill['instrument'] != instrument.name
                or fill['is_buyer'] is not (not long) or fill['is_taker'] is not True
                or fill['is_liquidation'] is not False or fill['fee_currency'] != 'USDT'
                or number(fill['builder_fee']) != 0 or price % instrument.tick_size
                or (price < bound if long else price > bound)
                or not max(entry_last,reduction['claim']['issued_at_ns']) <= ts <= order['updated_at_ns']):
            raise ValueError('Fill de salida fuera del contrato reduce-only')
        notional = size*price
        fee = number(fill['fee'])
        amounts[cid] += size
        notionals[cid] += notional
        exits_notional += notional
        fees += fee
        reported += number(fill['realized_pnl'])
        exit_fills.append(fill)
        if ts >= reduction['claim']['expiration_ns']:
            breaches.add('FILL_AT_OR_AFTER_EXPIRY')
        if fee > notional*number(budget.fee_rate_ceiling):
            breaches.add('FEE_CEILING_EXCEEDED')
    if (sorted(entry_fills,key=lambda row:(row['event_time_ns'],row['trade_id'])) != entry['fills']
            or any(amounts[cid] != number(order['filled_size']) for cid,(order,_) in by_cid.items())):
        raise ValueError('Las cantidades LIMIT no se explican con todos sus fills')
    remaining = number(entry['filled_size']) - sum(amounts.values(),Decimal(0))
    if remaining < 0:
        raise ValueError('Las salidas LIMIT superan la cantidad ejecutada')
    if any(notional > number(budget.exit_notional_limit) for notional in notionals.values()):
        breaches.add('EXIT_NOTIONAL_EXCEEDED')
    paid = Decimal(0)
    funding_seen = set()
    times = funding_trade_times(claim,[*entry_fills,*exit_fills])
    for payment in histories['funding_payment_history']['rows']:
        identity = (payment['tx_id'],_time(payment['event_time_ns']),payment['instrument'],payment['currency'])
        if (set(payment) != _FUND_FIELDS or identity in funding_seen or payment['tx_id'] not in times
                or payment['instrument'] != instrument.name or payment['currency'] != 'USDT'
                or not times[payment['tx_id']] <= payment['event_time_ns'] <= cutoff):
            raise ValueError('Financiación LIMIT sin ejecución vinculada')
        funding_seen.add(identity)
        paid += number(payment['amount_paid'])
    fees += number(entry['fees_usdt'])
    if not remaining and reported != (exits_notional-number(entry['entry_notional']))*(1 if long else -1):
        raise ValueError('PNL final LIMIT distinto de los nocionales ejecutados')
    positions = [[instrument.name,canonical(remaining if long else -remaining)]] if remaining else []
    baseline = number(claim['preflight']['market']['states'][-1]['settle_balance'])
    net = reported-fees-paid
    balance = _account_snapshot(source['account'],sub,positions,cutoff,at,baseline,net)
    return {'schema':'orbis.grvt.perp.limit.exit.audit.v1','orders':normalized,
            'remaining_size':canonical(remaining),'positions':positions,'settle_balance':canonical(balance),
            'exit_notional':canonical(exits_notional),'realized_pnl_usdt':canonical(reported),
            'fees_usdt':canonical(fees),'funding_paid_usdt':canonical(paid),
            'net_change_usdt':canonical(net),'policy_breaches':sorted(breaches),'execution_authority':'NONE'}


def stable_exit_audits(audits: list[dict], *, now_ns: int) -> tuple[dict, dict]:
    if len(audits) < 2:
        raise ValueError('Se requieren dos auditorías LIMIT consecutivas')
    first,last = audits[-2:]
    if (last['at_ns']-first['at_ns'] < FRESH_NS or not 0 <= now_ns-last['at_ns'] <= FRESH_NS
            or last['source']['cutoff_ns']-first['source']['cutoff_ns'] < FRESH_NS
            or first['result'] != last['result']
            or any(first['source']['histories'][name]['rows'] != last['source']['histories'][name]['rows']
                   for name in HISTORY_URLS)):
        raise ValueError('Las dos auditorías LIMIT discrepan o han vencido')
    return first,last
