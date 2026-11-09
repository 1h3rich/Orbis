"""Durable owner-wallet DEX controls, reviewed against current on-chain state."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.dex_artifact import DexExecutorError, ARTIFACT_SHA256, PROTOCOL
from app.live_execution.dex_policy import PolicyLimits, configure_transaction, stop_transaction
from app.live_execution.dex_state import ExecutorState, ZERO
from app.live_execution.dex_allowance import allowance_cap, cap_from_limits, approval_transaction
from app.portfolio.wallet_swap import TOKENS
from app.portfolio.dex_deployments import deployment_record, _identity as deployment_identity, _hex_ref
from app.portfolio.models import RealDexControlIntent
from app.portfolio.wallet_funding import _encoded, _decoded, _time, _uint, exact_units
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address
from app.live_execution.dex_versions import (ROLLOVER_CONTROLS,RECOVERED_CONTROL,
    RECOVERED_LONG_ROLLOVER,RECOVERED_DCA_ROLLOVER,EXTERNAL_CONTROL,EXTERNAL_LONG_ROLLOVER,EXTERNAL_DCA_ROLLOVER)


COUNTERS = ('policy_id', 'next_nonce', 'buy_spent', 'entries', 'owned_wbnb', 'cost_usdt',
            'realized_loss', 'last_entry_at', 'acquired_wbnb')
STATES = {'PREPARED', 'WALLET_REQUESTED', 'HASH_REPORTED', 'WALLET_REJECTED', 'FINALIZED', 'REVERTED', 'NONCE_OBSERVED'}
ALLOWANCE_ACTIONS = frozenset({'APPROVE', 'REVOKE'})


def _validate_allowance(terms: dict, counters: dict, created_at: datetime) -> None:
    value = terms['allowance']
    if (not isinstance(value, dict) or set(value) != {'token', 'units', 'prior_units', 'cap_units', 'operator_eoa_confirmed'}
            or value['token'] not in TOKENS):raise ValueError('allowance terms')
    units = _uint(value['units'], 'Permiso', positive=False)
    cap = _uint(value['cap_units'], 'Cuota', positive=False)
    prior = _uint(value['prior_units'], 'Permiso anterior', positive=False)
    if terms['action'] == 'APPROVE':
        limits = PolicyLimits.parse(terms['limits'])
        limits.validate_at(int(created_at.timestamp()), counters['owned_wbnb'])
        if (limits.as_dict() != terms['limits'] or not _hex_ref(terms['operator_ref'])
                or value['operator_eoa_confirmed'] is not True or prior != 0 or not 0 < units <= cap
                or cap != cap_from_limits(limits, counters, value['token'])):raise ValueError('allowance cap')
    elif (units != 0 or cap != 0 or terms['limits'] is not None or terms['operator_ref'] is not None
            or value['operator_eoa_confirmed'] is not None):raise ValueError('revocation terms')


def _identity(db: Session, kind: str, value: str) -> str:
    if kind == 'wallet':return deployment_identity(db, kind, value)
    return hmac.new(_salt(db), f'dex-control-v1:{kind}:0x38:{value.lower()}'.encode(), hashlib.sha256).hexdigest()


def _anchor(db: Session, snapshot: ExecutorState) -> dict:
    return {'block_number': str(snapshot.block_number), 'block_hash': snapshot.block_hash,
        'block_at': snapshot.block_at.isoformat(), 'observed_at': snapshot.observed_at.isoformat(),
        'owner_nonce': str(snapshot.owner_nonce), 'pending_nonce': str(snapshot.pending_nonce),
        'operator_ref': _identity(db, 'operator', snapshot.operator) if snapshot.operator != ZERO else None,
        'counters': {key: str(getattr(snapshot, key)) for key in COUNTERS}}


def _validate_anchor(anchor: dict, creation_nonce: str, reference_at: datetime) -> dict:
    if (set(anchor) != {'block_number', 'block_hash', 'block_at', 'observed_at', 'owner_nonce', 'pending_nonce', 'operator_ref', 'counters'}
            or set(anchor['counters']) != set(COUNTERS) or anchor['operator_ref'] is not None and not _hex_ref(anchor['operator_ref'])):
        raise ValueError('anchor')
    from app.portfolio.dex_deployment_receipt import word
    _uint(anchor['block_number'], 'Bloque');word(anchor['block_hash'])
    owner_nonce = _uint(anchor['owner_nonce'], 'Nonce owner', maximum=2**64-1, positive=False)
    if not int(creation_nonce) < owner_nonce <= _uint(anchor['pending_nonce'], 'Pending nonce', maximum=2**64-1, positive=False):
        raise ValueError('anchor nonce')
    for stamp_key in ('block_at', 'observed_at'):
        stamp = _time(datetime.fromisoformat(anchor[stamp_key]))
        age = (reference_at-stamp).total_seconds()
        if stamp.isoformat() != anchor[stamp_key] or not (-15 if stamp_key == 'block_at' else 0) <= age <= 30:
            raise ValueError('anchor clock')
    numbers = {key: _uint(value, key, positive=False) for key, value in anchor['counters'].items()}
    if ((numbers['owned_wbnb'] == 0) != (numbers['cost_usdt'] == 0)
            or numbers['acquired_wbnb'] < numbers['owned_wbnb']):raise ValueError('position')
    return numbers


def _expiry(created: datetime, limits: dict | None) -> datetime:
    expiry = created+timedelta(seconds=120)
    return min(expiry, datetime.fromtimestamp(limits['expires_at'], timezone.utc)) if limits is not None else expiry


def control_record(db: Session, identifier: str, *, _verify_rollover: bool = True) -> tuple[RealDexControlIntent, dict, dict]:
    if not _verify_rollover:return _control_record(db,identifier,_verify_rollover=False)
    from app.portfolio.dex_verified_read import read_record
    identifier=str(UUID(identifier));row=db.get(RealDexControlIntent,identifier)
    if row is None:raise DexExecutorError('Control DEX no encontrado')
    terms=_decoded(row.payload_json,row.payload_digest)
    warm=terms['bot_rollover']['previous_binding_id'] if terms.get('schema') in ROLLOVER_CONTROLS else None
    return read_record(db,'control',identifier,lambda current:_control_record(db,identifier),warm=warm)


def _control_record(db: Session, identifier: str, *, _verify_rollover: bool = True) -> tuple[RealDexControlIntent, dict, dict]:
    row = db.get(RealDexControlIntent, str(UUID(identifier)))
    if row is None:raise DexExecutorError('Control DEX no encontrado')
    terms, state = _decoded(row.payload_json, row.payload_digest), _decoded(row.state_json, row.state_digest)
    try:
        allowance = terms.get('schema') == 'orbis.dex.control.v2'
        rollover = terms.get('schema') in ROLLOVER_CONTROLS
        if terms.get('schema')==RECOVERED_CONTROL and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<71:
            raise ValueError('recovered renewal schema')
        if terms.get('schema')==EXTERNAL_CONTROL and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<78:
            raise ValueError('external renewal schema')
        if allowance and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() < 60:raise ValueError('allowance schema')
        keys = {'schema', 'id', 'account_id', 'deployment_id', 'wallet_digest', 'operator_ref', 'action', 'limits',
                'protocol', 'artifact_sha256', 'creation_nonce', 'nonce', 'gas', 'gas_price', 'max_gas_wei', 'anchor', 'expires_at'}
        if (set(terms) != keys | ({'allowance'} if allowance else {'bot_rollover'} if rollover else set())
                or terms['schema'] not in {'orbis.dex.control.v1', 'orbis.dex.control.v2',*ROLLOVER_CONTROLS} or terms['id'] != row.id or terms['account_id'] != row.account_id
                or terms['deployment_id'] != row.deployment_id or terms['wallet_digest'] != row.wallet_digest
                or not _hex_ref(row.wallet_digest) or terms['protocol'] != PROTOCOL or terms['artifact_sha256'] != ARTIFACT_SHA256
                or terms['action'] not in (ALLOWANCE_ACTIONS if allowance else {'CONFIGURE'} if rollover else {'CONFIGURE', 'STOP'})):raise ValueError('terms')
        parent, deployment, evidence = deployment_record(db, row.deployment_id)
        if (parent.account_id != row.account_id or parent.wallet_digest != row.wallet_digest
                or not (parent.status == 'FINALIZED' or evidence['code_observation'] is not None)
                or terms['creation_nonce'] != deployment['nonce']):raise ValueError('deployment')
        nonce = _uint(terms['nonce'], 'Nonce', maximum=2**64-1, positive=False)
        gas = _uint(terms['gas'], 'Gas', maximum=2_000_000)
        price = _uint(terms['gas_price'], 'Gas price', maximum=10**12)
        if gas*price > _uint(terms['max_gas_wei'], 'Gas máximo'):raise ValueError('gas budget')
        anchor = terms['anchor']
        numbers = _validate_anchor(anchor, deployment['nonce'], row.created_at)
        if anchor['pending_nonce'] != terms['nonce']:raise ValueError('anchor nonce')
        if allowance:
            _validate_allowance(terms, numbers, row.created_at)
        elif terms['action'] == 'CONFIGURE':
            if not _hex_ref(terms['operator_ref']):raise ValueError('operator')
            limits = PolicyLimits.parse(terms['limits'])
            if limits.as_dict() != terms['limits']:raise ValueError('canonical limits')
            limits.validate_at(int(row.created_at.timestamp()), numbers['owned_wbnb'])
        elif terms['limits'] is not None or terms['operator_ref'] is not None:raise ValueError('STOP terms')
        if terms['expires_at'] != _expiry(row.created_at, terms['limits']).isoformat():raise ValueError('expiry')
        if (set(state) != {'status', 'tx_ref', 'history', 'receipt', 'nonce_observation'} or row.status not in STATES or state['status'] != row.status
                ):raise ValueError('state')
        history = state['history']
        if not isinstance(history, list) or not 1 <= len(history) <= 6 or any(set(event) != {'status', 'at'} for event in history):
            raise ValueError('history')
        statuses = [event['status'] for event in history]
        transitions = {'PREPARED': {'WALLET_REQUESTED'}, 'WALLET_REQUESTED': {'HASH_REPORTED', 'WALLET_REJECTED', 'NONCE_OBSERVED'},
            'HASH_REPORTED': {'NONCE_OBSERVED', 'FINALIZED', 'REVERTED'}, 'NONCE_OBSERVED': {'HASH_REPORTED', 'FINALIZED', 'REVERTED'}}
        if (statuses[0] != 'PREPARED' or statuses[-1] != row.status or len(statuses) != len(set(statuses))
                or any(right not in transitions.get(left, set()) for left, right in zip(statuses, statuses[1:]))):raise ValueError('history')
        hashed = 'HASH_REPORTED' in statuses
        if (hashed and not _hex_ref(state['tx_ref']) or not hashed and state['tx_ref'] is not None
                or row.status in {'FINALIZED', 'REVERTED'} and not hashed):raise ValueError('hash report')
        if row.status in {'FINALIZED', 'REVERTED'}:
            from app.portfolio.dex_control_receipt import validate_control_receipt
            validate_control_receipt(state['receipt'], terms, row)
        elif state['receipt'] is not None:raise ValueError('unexpected receipt')
        stamps = [_time(datetime.fromisoformat(event['at'])) for event in history]
        if (stamps != sorted(stamps) or stamps[0] != row.created_at or stamps[-1] != row.updated_at
                or any(stamp.isoformat() != event['at'] for stamp, event in zip(stamps, history))):raise ValueError('history clock')
        if ('NONCE_OBSERVED' in statuses) != (state['nonce_observation'] is not None):raise ValueError('nonce observation')
        if state['nonce_observation'] is not None:
            observation = state['nonce_observation']
            if (set(observation) != {'source', 'finalized', 'anchor', 'limits', 'effect_matches'} | ({'allowance_units'} if allowance else set())
                    or observation['source'] != 'PUBLIC_RPC_NONCE_OBSERVED' or observation['finalized'] is not True
                    or type(observation['effect_matches']) is not bool):raise ValueError('nonce observation')
            _validate_anchor(observation['anchor'], deployment['nonce'], stamps[statuses.index('NONCE_OBSERVED')])
            if (int(observation['anchor']['owner_nonce']) <= nonce
                    or int(observation['anchor']['block_number']) < int(terms['anchor']['block_number'])):raise ValueError('nonce not consumed')
            if observation['limits'] is not None:PolicyLimits.parse(observation['limits'])
            if allowance:_uint(observation['allowance_units'], 'Permiso observado', positive=False)
            if observation['effect_matches'] != _effect_matches(terms, observation['anchor'], observation['limits'],
                    allowance_units=observation.get('allowance_units')):raise ValueError('control effect')
            if state['receipt'] is not None:
                after, receipt = observation['anchor'], state['receipt']
                if (int(after['block_number']) < int(receipt['block_number'])
                        or after['block_number'] == receipt['block_number'] and after['block_hash'] != receipt['block_hash']):raise ValueError('observation contradicts receipt')

        if rollover and _verify_rollover:
            from app.portfolio.dex_bot_rollover import validate_control
            validate_control(db,row,terms)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        raise DexExecutorError('El registro de control DEX no es coherente') from None
    return row, terms, state


def _public(row, terms, state, now: datetime) -> dict:
    return {'id': row.id, 'account_id': row.account_id, 'deployment_id': row.deployment_id, 'action': terms['action'],
        'plan_digest': row.payload_digest, 'status': row.status, 'limits': terms['limits'], 'nonce': terms['nonce'],
        'expected_policy_id': terms['anchor']['counters']['policy_id'], 'expires_at': terms['expires_at'],
        'expired': now >= datetime.fromisoformat(terms['expires_at']), 'created_at': row.created_at.isoformat(),
        'estimated_gas_wei': str(int(terms['gas'])*int(terms['gas_price'])), 'max_gas_wei': terms['max_gas_wei'],
        'automatic_execution': False, 'receipt': state['receipt'], 'nonce_observation': state['nonce_observation'],
        'allowance': terms.get('allowance'),**({'rollover':terms['bot_rollover']} if 'bot_rollover' in terms else {})}


def _context(db, deployment_id, account_id, wallet, snapshot, now):
    parent, deployment, evidence = deployment_record(db, deployment_id)
    if (parent.account_id != account_id or deployment_identity(db, 'wallet', wallet) != parent.wallet_digest
            or not (parent.status == 'FINALIZED' or evidence['code_observation'] is not None)
            or type(snapshot) is not ExecutorState or snapshot.owner != wallet or snapshot.fee != deployment['fee']):
        raise DexExecutorError('Wallet, cuenta, despliegue o estado DEX distinto')
    from app.live_execution.dex_artifact import executor_address
    if snapshot.executor_address != executor_address(wallet, int(deployment['nonce'])):
        raise DexExecutorError('La observación corresponde a otro ejecutor')
    snapshot.require_fresh(now)
    return deployment


def require_settled_policy_controls(db:Session,deployment_id:str)->None:
    """An uncertain owner configuration/STOP cannot coexist with new trading."""
    for identifier in db.scalars(select(RealDexControlIntent.id).where(
            RealDexControlIntent.deployment_id==deployment_id,
            RealDexControlIntent.status.in_(('WALLET_REQUESTED','HASH_REPORTED','NONCE_OBSERVED')))):
        _,terms,_=control_record(db,identifier)
        if terms['action'] in {'CONFIGURE','STOP'}:
            raise DexExecutorError('Hay un cambio de política pendiente; comprueba su resultado original')


def _compile(terms, wallet, operator, *, now):
    common = {'nonce': int(terms['nonce']), 'gas': int(terms['gas']), 'gas_price': int(terms['gas_price'])}
    if terms['action'] in ALLOWANCE_ACTIONS:
        value = terms['allowance']
        return approval_transaction(wallet, int(terms['creation_nonce']), value['token'], int(value['units']), cap=int(value['cap_units']), **common)
    if terms['action'] == 'STOP':return stop_transaction(wallet, int(terms['creation_nonce']), **common)
    return configure_transaction(wallet, int(terms['creation_nonce']), operator, int(terms['anchor']['counters']['policy_id']),
        PolicyLimits.parse(terms['limits']), now=int(now.timestamp()), owned_wbnb=int(terms['anchor']['counters']['owned_wbnb']), **common)


def prepare_control(db: Session, *, identifier: str, account_id: str, deployment_id: str, wallet_address: str, action: str,
                    operator: str | None, limits: dict | None, snapshot: ExecutorState,
                    gas: str, gas_price: str, max_gas_bnb: str, now: datetime,
                    token: str | None = None, allowance_units: str | None = None,bot_rollover:dict|None=None,
                    begin:bool=True) -> dict:
    now = _time(now);wallet = canonical_wallet_address(wallet_address);identifier = str(UUID(identifier))
    account_id, deployment_id = str(UUID(account_id)), str(UUID(deployment_id))
    maximum = exact_units(max_gas_bnb, 18)
    gas_n, price = _uint(gas, 'Gas', maximum=2_000_000), _uint(gas_price, 'Gas price', maximum=10**12)
    if gas_n*price > maximum:raise DexExecutorError('El límite de gas no cubre la operación')
    is_allowance = action in ALLOWANCE_ACTIONS
    if action not in {'CONFIGURE', 'STOP'} | ALLOWANCE_ACTIONS:raise DexExecutorError('Acción de control DEX no válida')
    if bot_rollover is not None and action!='CONFIGURE':raise DexExecutorError('La renovación requiere configurar una política')
    if not is_allowance and (token is not None or allowance_units is not None):raise DexExecutorError('Esta acción no modifica permisos ERC-20')
    if action == 'CONFIGURE':
        operator = canonical_wallet_address(operator)
        normalized = PolicyLimits.parse(limits).as_dict()
    else:
        if operator is not None or limits is not None:raise DexExecutorError('Esta acción no configura una política')
        normalized = None
    if is_allowance:
        units = _uint(allowance_units, 'Permiso ERC-20', positive=False)
        if token not in TOKENS or (action == 'APPROVE' and units == 0) or (action == 'REVOKE' and units != 0):
            raise DexExecutorError('Token o permiso ERC-20 no válido')
    if begin:db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() < (60 if is_allowance else 59):raise DexExecutorError('Actualiza la bóveda DEX')
    deployment = _context(db, deployment_id, account_id, wallet, snapshot, now)
    row = db.get(RealDexControlIntent, identifier)
    if row is not None:
        row, terms, state = control_record(db, identifier)
        if (row.account_id != account_id or row.deployment_id != deployment_id or row.wallet_digest != _identity(db, 'wallet', wallet)
                or terms['action'] != action or terms['gas'] != gas or terms['gas_price'] != gas_price or terms['max_gas_wei'] != str(maximum)
                or action == 'CONFIGURE' and (terms['limits'] != normalized or terms['operator_ref'] != _identity(db, 'operator', operator))
                or is_allowance and (terms['allowance']['token'] != token or terms['allowance']['units'] != allowance_units)
                or terms.get('bot_rollover')!=bot_rollover):
            raise DexExecutorError('El ID de control DEX ya se usó con otros datos')
        result = {**_public(row, terms, state, now), 'transaction': _compile(terms, wallet, operator, now=row.created_at)}
        db.commit();return result
    if gas_n*price > snapshot.native_balance:raise DexExecutorError('BNB insuficiente para el gas revisado')
    operator_ref = _identity(db, 'operator', operator) if operator else None
    if action == 'CONFIGURE':
        PolicyLimits.parse(normalized).validate_at(int(now.timestamp()), snapshot.owned_wbnb)
        if not snapshot.position_covered or snapshot.candidate_operator != operator:raise DexExecutorError('Revisa la posición o la cuenta operadora')
    allowance = None
    if is_allowance:
        prior = getattr(snapshot, 'allowance_'+token.lower())
        cap = 0
        if action == 'APPROVE':
            cap = allowance_cap(snapshot, token, now=now)
            if prior != 0:raise DexExecutorError('Revoca primero el permiso ERC-20 existente')
            if not 0 < units <= cap:raise DexExecutorError('El permiso supera la cuota restante de la política')
            normalized = snapshot.policy_limits.as_dict()
            operator_ref = _identity(db, 'operator', snapshot.operator)
        allowance = {'token': token, 'units': allowance_units, 'prior_units': str(prior), 'cap_units': str(cap),
            'operator_eoa_confirmed': True if action == 'APPROVE' else None}
    terms = {'schema': 'orbis.dex.control.v2' if is_allowance else 'orbis.dex.control.v1', 'id': identifier,
        'account_id': account_id, 'deployment_id': deployment_id, 'wallet_digest': _identity(db, 'wallet', wallet),
        'operator_ref': operator_ref, 'action': action, 'limits': normalized, 'protocol': PROTOCOL, 'artifact_sha256': ARTIFACT_SHA256,
        'creation_nonce': deployment['nonce'], 'nonce': str(snapshot.pending_nonce), 'gas': gas, 'gas_price': gas_price,
        'max_gas_wei': str(maximum), 'anchor': _anchor(db, snapshot), 'expires_at': _expiry(now, normalized).isoformat()}
    if is_allowance:terms['allowance'] = allowance
    if bot_rollover is not None:
        terms['schema']=EXTERNAL_CONTROL if bot_rollover.get('schema')in{EXTERNAL_LONG_ROLLOVER,EXTERNAL_DCA_ROLLOVER} else RECOVERED_CONTROL if bot_rollover.get('schema') in {RECOVERED_LONG_ROLLOVER,RECOVERED_DCA_ROLLOVER} else 'orbis.dex.control.v3'
        terms['bot_rollover']=bot_rollover
    state = {'status': 'PREPARED', 'tx_ref': None, 'history': [{'status': 'PREPARED', 'at': now.isoformat()}], 'receipt': None, 'nonce_observation': None}
    raw, digest = _encoded(terms);state_raw, state_digest = _encoded(state)
    row = RealDexControlIntent(id=identifier, account_id=account_id, deployment_id=deployment_id, wallet_digest=terms['wallet_digest'],
        payload_json=raw, payload_digest=digest, state_json=state_raw, state_digest=state_digest, status='PREPARED', created_at=now, updated_at=now)
    db.add(row);db.flush();control_record(db, identifier)
    result = {**_public(row, terms, state, now), 'transaction': _compile(terms, wallet, operator, now=row.created_at)}
    db.commit();return result


def list_controls(db: Session, account_id: str, *, now: datetime) -> list[dict]:
    from app.portfolio.models import RealAccount
    if db.get(RealAccount, account_id) is None:raise DexExecutorError('Cuenta REAL no encontrada')
    rows = db.scalars(select(RealDexControlIntent).where(RealDexControlIntent.account_id == account_id)
        .order_by(RealDexControlIntent.status.in_(['WALLET_REQUESTED', 'HASH_REPORTED', 'NONCE_OBSERVED']).desc(),
            RealDexControlIntent.created_at.desc(), RealDexControlIntent.id).limit(100)).all()
    return [_public(*control_record(db, row.id), _time(now)) for row in rows]


def _advance(db, row, state, status, now):
    if now < row.updated_at:raise DexExecutorError('El reloj de control DEX retrocedió')
    row.status = state['status'] = status;row.updated_at = now
    state['history'].append({'status': status, 'at': now.isoformat()})
    row.state_json, row.state_digest = _encoded(state);db.flush();control_record(db, row.id);db.commit()


def claim_control(db: Session, identifier: str, *, account_id: str, wallet_address: str, operator: str | None,
                  plan_digest: str, snapshot: ExecutorState, now: datetime) -> dict:
    from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
    now = _time(now);wallet = canonical_wallet_address(wallet_address)
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row, terms, state = control_record(db, identifier)
    if row.account_id != account_id or _identity(db, 'wallet', wallet) != row.wallet_digest or plan_digest != row.payload_digest:
        raise DexExecutorError('Wallet, cuenta o revisión de control distinta')
    if row.status != 'PREPARED':return {**_public(row, terms, state, now), 'claim_granted': False}
    _context(db, row.deployment_id, account_id, wallet, snapshot, now)
    if now >= datetime.fromisoformat(terms['expires_at']):raise DexExecutorError('La revisión de control DEX caducó')
    fresh = _anchor(db, snapshot)
    if str(snapshot.pending_nonce) != terms['nonce']:raise DexExecutorError('El nonce de la wallet cambió; revisa de nuevo')
    if terms['action'] == 'CONFIGURE':
        if (fresh['counters'] != terms['anchor']['counters'] or fresh['operator_ref'] != terms['anchor']['operator_ref']):
            raise DexExecutorError('El estado DEX cambió; revisa de nuevo')
        operator = canonical_wallet_address(operator)
        if (_identity(db, 'operator', operator) != terms['operator_ref'] or snapshot.candidate_operator != operator
                or not snapshot.position_covered):raise DexExecutorError('El operador o la posición DEX cambió')
    elif operator is not None:raise DexExecutorError('Esta acción no configura un operador')
    if terms['action'] == 'APPROVE':
        value = terms['allowance']
        if (fresh['counters'] != terms['anchor']['counters'] or fresh['operator_ref'] != terms['anchor']['operator_ref']
                or snapshot.policy_limits.as_dict() != terms['limits'] or getattr(snapshot, 'allowance_'+value['token'].lower()) != 0
                or allowance_cap(snapshot, value['token'], now=now) < int(value['units'])):
            raise DexExecutorError('La autoridad o el permiso ERC-20 cambió; vuelve a revisar')
    _compile(terms, wallet, operator, now=now)
    if int(terms['gas'])*int(terms['gas_price']) > snapshot.native_balance:raise DexExecutorError('BNB insuficiente para el gas revisado')
    if terms['schema'] in ROLLOVER_CONTROLS:
        from app.portfolio.dex_bot_rollover import require_current_review
        require_current_review(db,terms,now)
    assert_wallet_nonce_available(db, wallet, terms['nonce'])
    _advance(db, row, state, 'WALLET_REQUESTED', now)
    return {**_public(row, terms, state, now), 'claim_granted': True}


def report_control(db: Session, identifier: str, *, account_id: str, now: datetime,
                   tx_hash: str | None = None, rejected: bool = False) -> dict:
    import re
    now = _time(now)
    if (type(rejected) is not bool or rejected and tx_hash is not None
            or not rejected and (not isinstance(tx_hash, str) or re.fullmatch(r'0x[0-9a-fA-F]{64}', tx_hash) is None)):
        raise DexExecutorError('Indica el hash original o el rechazo de la wallet')
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row, terms, state = control_record(db, identifier)
    if row.account_id != account_id:raise DexExecutorError('Cuenta de control DEX distinta')
    reference = _identity(db, 'tx', tx_hash) if tx_hash else None
    target = 'WALLET_REJECTED' if rejected else 'HASH_REPORTED'
    if (row.status == target or not rejected and row.status in {'FINALIZED', 'REVERTED', 'NONCE_OBSERVED'}) and state['tx_ref'] == reference:
        return _public(row, terms, state, now)
    if row.status != 'WALLET_REQUESTED' and not (row.status == 'NONCE_OBSERVED' and state['tx_ref'] is None and not rejected):
        raise DexExecutorError('La respuesta original ya se registró o no se solicitó')
    if reference is not None:
        for item in db.scalars(select(RealDexControlIntent).where(RealDexControlIntent.id != row.id)):
            if control_record(db, item.id)[2]['tx_ref'] == reference:
                raise DexExecutorError('La transacción ya corresponde a otro control DEX')
    state['tx_ref'] = reference
    _advance(db, row, state, target, now)
    return _public(row, terms, state, now)


def check_control(db: Session, identifier: str, *, account_id: str, evidence: dict, now: datetime) -> dict:
    from app.portfolio.dex_control_receipt import observe_control
    now = _time(now)
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row, terms, state = control_record(db, identifier)
    if row.account_id != account_id or row.status not in {'HASH_REPORTED', 'FINALIZED', 'REVERTED', 'NONCE_OBSERVED'} or state['tx_ref'] is None:
        raise DexExecutorError('Registra primero el hash original del control DEX')
    receipt = observe_control(db, row, terms, state, evidence, now)
    if row.status in {'FINALIZED', 'REVERTED'}:
        if {k: v for k, v in receipt.items() if k != 'observation_ref'} != {k: v for k, v in state['receipt'].items() if k != 'observation_ref'}:
            raise DexExecutorError('El recibo de control ya registrado es distinto')
        return _public(row, terms, state, now)
    state['receipt'] = receipt
    _advance(db, row, state, receipt['status'], now)
    return _public(row, terms, state, now)


def _effect_matches(terms: dict, anchor: dict, limits: dict | None, *, allowance_units: str | None = None) -> bool:
    if terms['action'] in ALLOWANCE_ACTIONS:return allowance_units == terms['allowance']['units']
    if terms['action'] == 'STOP':
        return anchor['operator_ref'] is None and int(anchor['counters']['policy_id']) > int(terms['anchor']['counters']['policy_id'])
    return (int(anchor['counters']['policy_id']) == int(terms['anchor']['counters']['policy_id'])+1
        and anchor['operator_ref'] == terms['operator_ref'] and limits == terms['limits'])


def recover_control(db: Session, identifier: str, *, account_id: str, wallet_address: str,
                    snapshot: ExecutorState, now: datetime) -> dict:
    now = _time(now);wallet = canonical_wallet_address(wallet_address)
    db.connection().exec_driver_sql('BEGIN IMMEDIATE');row, terms, state = control_record(db, identifier)
    if row.account_id != account_id or _identity(db, 'wallet', wallet) != row.wallet_digest:
        raise DexExecutorError('Wallet o cuenta de control distinta')
    if row.status not in {'WALLET_REQUESTED', 'HASH_REPORTED', 'NONCE_OBSERVED'}:
        raise DexExecutorError('Revisa la solicitud original del control DEX')
    if state['nonce_observation'] is not None:return _public(row, terms, state, now)
    _context(db, row.deployment_id, account_id, wallet, snapshot, now)
    if (not snapshot.finalized or snapshot.owner_nonce <= int(terms['nonce'])
            or snapshot.block_number < int(terms['anchor']['block_number'])):
        raise DexExecutorError('El nonce original aún no está consumido en un bloque finalizado')
    anchor = _anchor(db, snapshot)
    limits = snapshot.policy_limits.as_dict() if snapshot.policy_limits else None
    allowance_units = str(getattr(snapshot, 'allowance_'+terms['allowance']['token'].lower())) if terms['action'] in ALLOWANCE_ACTIONS else None
    state['nonce_observation'] = {'source': 'PUBLIC_RPC_NONCE_OBSERVED', 'finalized': True,
        'anchor': anchor, 'limits': limits, 'effect_matches': _effect_matches(terms, anchor, limits, allowance_units=allowance_units)}
    if allowance_units is not None:state['nonce_observation']['allowance_units'] = allowance_units
    _advance(db, row, state, 'NONCE_OBSERVED', now)
    return _public(row, terms, state, now)
