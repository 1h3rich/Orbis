"""Owned candles for the independent rule feed; no account or network access."""
import json

from app.derivatives.perp_observed_wire import quote_input
from app.live_execution.grvt_perp_signals import PerpSignalInput, PerpSignalSeed
from tests.perp_observed_fixture import NS, bar, initial


def signal_seed(direction='LONG', mode='CROSS', **changes):
    state = initial(direction, mode, **changes)
    return PerpSignalSeed.model_validate({'schema':'orbis.grvt.perp.signals.seed.v1',
        'engine':'grvt-perp-signals-1.0', 'origin_reference_hash':'a'*64,
        'profile':state.profile.payload(), 'definition':json.loads(state.definition_json),
        'instrument':state.last_quote.instrument, 'timeframe':state.timeframe,
        'requested_at_ns':str(state.started_at_ns), 'prepared_at_ns':str(state.started_at_ns),
        'quote':quote_input(state.last_quote).payload(), 'history':[b.json() for b in state.history],
        'execution_authority':'NONE'})


def signal_input(state, seconds, *, candles=(), delay=0, quote_time=None, **quote_changes):
    at = state.started_at_ns+seconds*NS
    return PerpSignalInput.model_validate({'schema':'orbis.grvt.perp.signals.input.v1',
        'collected_at_ns':str(at), 'accepted_at_ns':str(at+delay),
        'quote':{**quote_input(state.last_quote).payload(),
                 'event_time_ns':str(at if quote_time is None else quote_time), **quote_changes},
        'closed_candles':[b.json() for b in candles]})
