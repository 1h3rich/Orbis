"""Match GRVT LIMIT status to actual fills under a pinned signed contract."""
from decimal import Decimal
from typing import Literal, Self
from collections.abc import Callable
import hmac
import re

from eth_account import Account
from eth_keys.exceptions import BadSignature
from pydantic import Field, model_validator

from app.derivatives.perp_observed_wire import ExactInput, Nanoseconds
from app.lab.perp_book_types import exact
from app.lab.store import content_hash
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError, _HEX32, _message, exact_uint
from app.live_execution.grvt_mainnet_finance import _fill
from app.live_execution.grvt_mainnet_order_history import find_order_history
from app.live_execution.grvt_mainnet_preflight import canonical, number
from app.live_execution.grvt_mainnet_remote_order import remote_contract
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient, MainnetOrderNotFound
from app.live_execution.grvt_order_codec import order_signable
from app.live_execution.grvt_perp_limit_plan import NS, PerpLimitEntryPlan, unsigned_limit_entry


TERMINAL = frozenset(('FILLED', 'CANCELLED', 'REJECTED'))
ORDER_ID = re.compile(r'[A-Za-z0-9]{1,80}\Z')
TRADE_ID = re.compile(r'[A-Za-z0-9_:\-]{1,128}\Z')
REASON = re.compile(r'[A-Z0-9_]{1,80}\Z')


class LimitObservationPending(GrvtMainnetError):
    """Readback can be retried without placing or cancelling any order."""


def lookup_limit_order(client: GrvtMainnetClient, sub: str, cid: str, *, instrument: str,
                       issued_at_ns: int, expires_at_ns: int, checked: Callable[[], int]) -> dict:
    checked()
    try:
        order = client.lookup_order_payload(sub,cid)
    except MainnetOrderNotFound:
        order = find_order_history(client,sub,instrument,windows={cid:(issued_at_ns,expires_at_ns)},checked=checked).get(cid)
        if order is None:
            raise LimitObservationPending('GRVT aún no acredita la orden LIMIT') from None
    except GrvtMainnetError:
        raise LimitObservationPending('GRVT aún no acredita la orden LIMIT') from None
    checked()
    return order


class PerpLimitContract(ExactInput):
    schema_name: Literal['orbis.grvt.perp.limit.entry.contract.v1','orbis.grvt.perp.limit.entry.contract.v2'] = Field(alias='schema')
    plan: PerpLimitEntryPlan
    client_order_id: str
    nonce: int
    issued_at_ns: Nanoseconds
    expected_signer: str = Field(pattern=r'^0x[a-fA-F0-9]{40}$')
    wire: dict = Field(repr=False)

    @model_validator(mode='after')
    def verify_wire(self) -> Self:
        if self.schema_name != f'orbis.grvt.perp.limit.entry.contract.v{self.plan.revision}':
            raise ValueError('Versión del contrato LIMIT incompatible con el plan')
        template = unsigned_limit_entry(self.plan, client_order_id=self.client_order_id,
                                        nonce=self.nonce, issued_at_ns=int(self.issued_at_ns))
        try:
            signature = self.wire['signature']
            if (set(signature) != set(template['signature']) | {'signer', 'r', 's', 'v'}
                    or type(signature['v']) is not int or signature['v'] not in (27, 28)
                    or type(signature['signer']) is not str
                    or signature['signer'].lower() != self.expected_signer.lower()
                    or any(type(signature[key]) is not str or not _HEX32.fullmatch(signature[key])
                           for key in ('r', 's'))):
                raise ValueError('Firma fuera de ámbito')
            unsigned = {**self.wire, 'signature': {key:signature[key] for key in template['signature']}}
            if content_hash(unsigned) != content_hash(template):
                raise ValueError('Contenido distinto del plan')
            instrument = self.plan.source.instrument.instrument()
            recovered = Account.recover_message(order_signable(_message(self.wire, instrument), chain_id=325),
                vrs=(signature['v'], int(signature['r'], 16), int(signature['s'], 16)))
            if recovered.lower() != self.expected_signer.lower():
                raise ValueError('Firmante distinto del revisado')
        except (ValueError, KeyError, TypeError, OverflowError, BadSignature):
            raise ValueError('Contrato firmado LIMIT REAL no verificable') from None
        return self


class PerpLimitOrderRead(ExactInput):
    received_at_ns: Nanoseconds
    order: dict = Field(repr=False)


class PerpLimitReceiptInput(ExactInput):
    schema_name: Literal['orbis.grvt.perp.limit.entry.observations.v1'] = Field(alias='schema')
    first: PerpLimitOrderRead
    last: PerpLimitOrderRead
    fill_cutoff_ns: Nanoseconds
    fill_history_drained: Literal[True]
    fills: list[dict] = Field(max_length=10000, repr=False)

    @model_validator(mode='before')
    @classmethod
    def require_complete_history(cls, raw):
        if isinstance(raw, dict) and raw.get('fill_history_drained') is not True:
            raise ValueError('Historial LIMIT incompleto')
        return raw


def _amount(raw: dict, name: str) -> Decimal:
    items = raw[name]
    if type(items) is not list or len(items) != 1:
        raise ValueError('Cantidad de estado LIMIT incompatible')
    value = number(items[0])
    if value < 0:
        raise ValueError('Cantidad de estado LIMIT negativa')
    return value


def _standalone_order(metadata: dict) -> bool:
    # These venue fields affect execution but are outside the EIP-712 signature.
    if (type(metadata) is not dict or metadata.get('is_ecn', False) is not False
            or metadata.get('is_private', False) is not False or metadata.get('rfq_id') not in (None, '')
            or type(metadata.get('slippage_bps', 0)) is not int or metadata.get('slippage_bps', 0) != 0):
        return False
    trigger = metadata.get('trigger')
    if trigger in (None, ''):
        return True
    if type(trigger) is not dict or trigger.get('trigger_type') != 'UNSPECIFIED':
        return False
    details = trigger.get('tpsl')
    return (details is None or type(details) is dict
            and details.get('trigger_by') == 'UNSPECIFIED'
            and number(details.get('trigger_price')) == 0
            and details.get('close_position') is False and details.get('is_split_position') is False)


def _read_order(contract: PerpLimitContract, reading: PerpLimitOrderRead) -> tuple:
    return read_bound_order(contract.wire, int(contract.issued_at_ns), reading)


def read_bound_order(wire: dict, issued_at_ns: int, reading: PerpLimitOrderRead) -> tuple:
    row = reading.order
    if (type(row.get('order_id')) is not str or not ORDER_ID.fullmatch(row['order_id'])
            or row['order_id'] == '0' or not _standalone_order(row.get('metadata'))
            or content_hash(remote_contract(row, wire)) != content_hash(wire)):
        raise ValueError('Estado LIMIT corresponde a otra orden')
    raw = row['state']
    status = raw['status']
    reason = raw['reject_reason']
    if type(reason) is not str or not REASON.fullmatch(reason):
        raise ValueError('Motivo de estado LIMIT no verificable')
    update = raw['update_time']
    if (type(update) is not str or not re.fullmatch(r'[1-9][0-9]{0,18}', update)
            or not issued_at_ns <= int(update) <= int(reading.received_at_ns) < 2**63):
        raise ValueError('Instante de estado LIMIT fuera del intervalo observado')
    filled = _amount(raw, 'traded_size')
    resting = _amount(raw, 'book_size')
    size = number(wire['legs'][0]['size'])
    if (status not in TERMINAL | {'PENDING', 'OPEN'}
            or filled > size or resting > size - filled
            or status in TERMINAL and resting != 0
            or status in ('PENDING', 'REJECTED') and filled != 0
            or status in ('OPEN', 'CANCELLED') and filled >= size
            or status == 'FILLED' and filled != size
            or status == 'OPEN' and resting != size - filled):
        raise ValueError('Estado y cantidades LIMIT incompatibles')
    return row['order_id'], status, reason, int(update), filled, resting


@exact
def reconcile_limit_entry(contract: PerpLimitContract, observations: PerpLimitReceiptInput) -> dict:
    contract = PerpLimitContract.model_validate(contract.payload())
    observations = PerpLimitReceiptInput.model_validate(observations.payload())
    first = _read_order(contract, observations.first)
    last = _read_order(contract, observations.last)
    order_id, status, reason, updated_at, filled, resting = last
    cutoff = int(observations.fill_cutoff_ns)
    received = int(observations.last.received_at_ns)
    if (not int(observations.first.received_at_ns) <= cutoff <= received
            or received - int(observations.first.received_at_ns) > 120 * NS
            or first[0] != order_id or first[3] > updated_at or first[4] != filled
            or first[1] in TERMINAL and first != last
            or first[1] == 'OPEN' and status in ('PENDING', 'REJECTED')
            or first[3] == updated_at and first != last):
        raise ValueError('La orden LIMIT cambió durante la conciliación')
    plan = contract.plan
    budget = plan.source.terms.budget
    instrument = plan.source.instrument.instrument()
    buying = plan.result['side'] == 'BUY'
    bound = number(plan.result['limit_price'])
    expiry = int(plan.result['expires_at_ns'])
    quantity = notional = fees = Decimal(0)
    rows = []
    ids = set()
    breaches = set()
    for raw in observations.fills:
        fill = _fill(raw, budget.sub_account_id)
        trade_id = fill['trade_id']
        size, price, fee = (number(fill[key]) for key in ('size', 'price', 'fee'))
        stamp = fill['event_time_ns']
        if (type(trade_id) is not str or not TRADE_ID.fullmatch(trade_id) or trade_id in ids
                or fill['order_id'] != order_id or fill['client_order_id'] != contract.client_order_id
                or fill['instrument'] != instrument.name or fill['is_buyer'] is not buying
                or type(fill['is_taker']) is not bool or fill['is_liquidation'] is not False
                or fill['fee_currency'] != 'USDT' or number(fill['realized_pnl']) != 0
                or number(fill['builder_fee']) != 0
                or not int(contract.issued_at_ns) <= stamp <= min(cutoff, updated_at)
                or size <= 0 or price <= 0 or price % instrument.tick_size
                or (price > bound if buying else price < bound)):
            raise ValueError('Fill LIMIT no corresponde a la orden firmada')
        exact_uint(size, instrument.base_decimals, 2**64 - 1)
        exact_uint(price, 9, 2**64 - 1)
        ids.add(trade_id)
        rows.append(fill)
        quantity += size
        notional += size * price
        fees += fee
        if stamp >= expiry:
            breaches.add('FILL_AT_OR_AFTER_EXPIRY')
        if fee > size * price * number(budget.fee_rate_ceiling):
            breaches.add('FEE_CEILING_EXCEEDED')
    if quantity != filled:
        raise ValueError('Los fills LIMIT no explican toda la cantidad ejecutada')
    if notional > number(budget.entry_notional_limit):
        breaches.add('ENTRY_NOTIONAL_EXCEEDED')
    if notional > number(budget.exit_notional_limit):
        breaches.add('EXIT_NOTIONAL_EXCEEDED_AT_ENTRY')
    if status in TERMINAL:
        phase = 'ENTRY_TERMINAL_WITH_FILLS' if quantity else 'ENTRY_TERMINAL_EMPTY'
    elif received >= expiry:
        phase = 'AWAIT_TERMINAL_PROOF'
    elif status == 'PENDING':
        phase = 'AWAIT_ORDER_STATE'
    elif quantity:
        phase = 'ENTRY_OPEN_PARTIAL'
    else:
        phase = 'ENTRY_OPEN_UNFILLED'
    return {
        'schema': 'orbis.grvt.perp.limit.entry.receipt.v1', 'plan_hash': plan.sha256,
        'contract_hash': content_hash(contract.payload()),
        'observations_hash': content_hash(observations.payload()),
        'order_id': order_id, 'client_order_id': contract.client_order_id,
        'status': status, 'reason': reason, 'phase': phase, 'terminal': status in TERMINAL,
        'order_updated_at_ns': str(updated_at),
        'filled_size': canonical(quantity), 'book_size': canonical(resting),
        'entry_notional': canonical(notional), 'fees_usdt': canonical(fees),
        'fills': sorted(rows, key=lambda fill: (fill['event_time_ns'], fill['trade_id'])),
        'policy_breaches': sorted(breaches), 'execution_authority': 'NONE',
    }


def collect_limit_observations(client: GrvtMainnetClient, contract: PerpLimitContract, *,
                               identity_salt: str, account_ref: str, clock_ns: Callable[[], int],
                               require_permission: Callable[[], None]) -> PerpLimitReceiptInput:
    """Read two snapshots around fully drained fill history for this entry."""
    contract = PerpLimitContract.model_validate(contract.payload())
    sub = contract.plan.source.terms.budget.sub_account_id
    if (not isinstance(client, GrvtMainnetClient) or client.network != 'MAINNET'
            or client.sub_account_id != sub
            or not hmac.compare_digest(client.identity_ref(identity_salt), account_ref)):
        raise ValueError('Identidad de la subcuenta LIMIT distinta del intento')
    started = int(contract.issued_at_ns)
    previous = started

    def now():
        nonlocal previous
        require_permission()
        value = clock_ns()
        if type(value) is not int or not previous <= value < 2**63:
            raise ValueError('Reloj de conciliación LIMIT regresado')
        previous = value
        return value

    def read():
        reply = lookup_limit_order(client,sub,contract.client_order_id,
            instrument=contract.plan.source.terms.instrument,issued_at_ns=started,
            expires_at_ns=int(contract.wire['signature']['expiration']),checked=now)
        return PerpLimitOrderRead(received_at_ns=str(now()), order=reply)

    first = read()
    _read_order(contract, first)
    cutoff = now()
    rows = []
    cursor = ''
    seen = set()
    for _ in range(20):
        now()
        page = client.history_page('fill_history', sub, start_ns=started, end_ns=cutoff, cursor=cursor)
        now()
        batch = page.get('result')
        following = page.get('next')
        if (type(batch) is not list or len(batch) > 500 or type(following) is not str
                or len(following) > 1024 or not following.isascii() or any(ord(char)<32 for char in following)
                or following in seen):
            raise ValueError('Paginación de fills LIMIT incompleta o repetida')
        rows.extend(batch)
        if not following:
            break
        seen.add(following)
        cursor = following
    else:
        raise ValueError('Historial de fills LIMIT fuera de capacidad')
    last = read()
    observations = PerpLimitReceiptInput(schema_name='orbis.grvt.perp.limit.entry.observations.v1',
        first=first, last=last, fill_cutoff_ns=str(cutoff), fill_history_drained=True, fills=rows)
    reconcile_limit_entry(contract, observations)
    require_permission()
    return observations
