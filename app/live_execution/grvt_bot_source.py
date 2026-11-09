"""Identidad congelada de la sesión PAPER que alimenta un bot GRVT."""

import json
import re
from uuid import UUID

from app.lab.engine import CURRENT_ENGINE_VERSION, INTERVALS, validate_strategy, validate_rule_timeframes
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.marketdata.adapters.grvt import GrvtAdapter


def bot_source(paper, lab, session_id: str) -> dict:
    """No toma tamaño/capital PAPER ni consulta el proveedor privado."""
    try:
        if str(UUID(session_id)) != session_id: raise ValueError('session identity')
        with paper.connect() as db:
            session = db.execute('''SELECT s.*,a.stopped_at FROM paper_signal_sessions s
                JOIN paper_perp_accounts a ON a.id=s.account_id WHERE s.id=?''', (session_id,)).fetchone()
            if (session is None or session['execution_mode'] != 'AUTO_PAPER'
                    or session['status'] != 'ACTIVE' or session['stopped_at'] is not None
                    or session['engine_version'] != CURRENT_ENGINE_VERSION):
                raise ValueError('inactive source')
            definition = validate_strategy(json.loads(session['definition_json']))
            validate_rule_timeframes(definition, session['timeframe'])
            instrument = GrvtAdapter()._instrument(session['symbol'])
            version = lab.version(session['version_id']); dataset, _ = lab.dataset(session['dataset_id'])
            if (instrument is None or validate_strategy(version['definition']) != definition
                    or dataset['source'] != 'grvt-perp-trade-public' or not dataset['verified_public']
                    or dataset['hash'] != session['dataset_hash'] or dataset['symbol'] != session['symbol']
                    or dataset['timeframe'] != session['timeframe']):
                raise ValueError('non-reproducible source')
            return {'session_id': session_id, 'account_id': session['account_id'],
                'version_id': session['version_id'], 'dataset_id': session['dataset_id'],
                'dataset_hash': session['dataset_hash'], 'symbol': session['symbol'], 'instrument': instrument,
                'timeframe': session['timeframe'], 'engine_version': session['engine_version'],
                'session_started_at_ns': session['started_at_ns'], 'definition': definition}
    except Exception:
        raise GrvtTestnetBracketError('Origen del bot GRVT PAPER no verificable') from None


def next_bot_open(paper, source: dict, *, armed_at_ns: int, now_ns: int) -> dict | None:
    """Sólo la última OPEN ejecutada y posterior a la autorización.

    La selección no sustituye el replay causal completo realizado al enviar.
    Una señal caducada se descarta, nunca se rejuvenece al iniciar el bot.
    """
    from app.live_execution.grvt_testnet_preflight import client_id_for_signal
    from app.live_execution.grvt_testnet_signal import SIGNAL_MAX_AGE_NS
    with paper.connect() as db:
        session = db.execute('''SELECT s.*,a.stopped_at FROM paper_signal_sessions s
            JOIN paper_perp_accounts a ON a.id=s.account_id WHERE s.id=?''', (source['session_id'],)).fetchone()
        if (session is None or session['status'] != 'ACTIVE' or session['execution_mode'] != 'AUTO_PAPER'
                or session['stopped_at'] is not None
                or any(session[key] != source[key] for key in
                    ('account_id','version_id','dataset_id','dataset_hash','symbol','timeframe','engine_version'))
                or session['started_at_ns'] != source['session_started_at_ns']
                or validate_strategy(json.loads(session['definition_json'])) != source['definition']):
            raise GrvtTestnetBracketError('La sesión fuente del bot cambió o se detuvo')
        action = db.execute('''SELECT * FROM paper_signal_actions WHERE session_id=?
            ORDER BY recorded_at_ns DESC,candle_t DESC LIMIT 1''', (source['session_id'],)).fetchone()
        if (action is None or action['action'] != 'OPEN' or action['status'] != 'EXECUTED'
                or action['recorded_at_ns'] <= armed_at_ns
                or not 0 <= now_ns-action['recorded_at_ns'] <= SIGNAL_MAX_AGE_NS):
            return None
        return {'action_request_id': action['request_id'],
            'parent_client_order_id': client_id_for_signal(source['session_id'], action['request_id']),
            'recorded_at_ns': action['recorded_at_ns']}


_SOURCE_KEYS = {'session_id','account_id','version_id','dataset_id','dataset_hash','symbol','instrument',
    'timeframe','engine_version','session_started_at_ns','definition'}

def verify_bot_source(value):
    if set(value) != _SOURCE_KEYS: raise ValueError('source fields')
    for key in ('session_id','account_id','version_id','dataset_id'):
        if str(UUID(value[key])) != value[key]: raise ValueError('source identity')
    if (not re.fullmatch(r'[a-f0-9]{64}', value['dataset_hash'])
            or GrvtAdapter()._instrument(value['symbol']) != value['instrument']
            or value['timeframe'] not in INTERVALS or value['engine_version'] != CURRENT_ENGINE_VERSION
            or validate_strategy(value['definition']) != value['definition']):
        raise ValueError('source semantics')
    validate_rule_timeframes(value['definition'], value['timeframe'])
    if type(value['session_started_at_ns']) is not int or not 0<value['session_started_at_ns']<2**63:raise ValueError('source time')

