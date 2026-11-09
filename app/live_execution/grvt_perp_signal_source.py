"""Public closed candles/quotes for the independent REAL rule feed.

No fictitious position or funding balance is constructed. Actual sizing and
funding reconciliation belong to the preflight/execution/financial modules.
"""
from __future__ import annotations

import json
import time
from typing import Callable, TypeVar

from app.derivatives.paper_math import PerpQuote
from app.derivatives.perp_observed_origin import VerifiedObservedOrigin
from app.derivatives.perp_observed_source import GrvtObservedSource
from app.derivatives.perp_observed_wire import quote_input
from app.lab.engine import INTERVALS
from app.lab.perp_dataset import TradeBar
from app.live_execution.grvt_perp_limit_origin import VerifiedPartialPaperOrigin
from app.live_execution.grvt_perp_partial_targets import PerpPartialExitTerms
from app.live_execution.grvt_perp_signals import (
    MAX_CONSUMER_DELAY_NS, PerpLimitSignalSeed, PerpPartialSignalSeed, PerpSignalInput, PerpSignalSeed, PerpSignalState,
)
from app.marketdata import http as market_http
from app.marketdata.adapters.grvt import GrvtAdapter
from app.research.perp_reference import ObservedResearchReference, PerpLimitResearchReference, PerpPartialResearchReference


_Seed = TypeVar('_Seed', bound=PerpSignalSeed)


class GrvtPerpSignalSource:
    def __init__(self, *, adapter=None, public_post=None, clock_ns: Callable=time.time_ns,
                 require_permission: Callable=lambda: None):
        self.clock = clock_ns
        self.permission = require_permission
        self.last_ns = 0
        post = public_post if public_post is not None else market_http.http_post_json

        def checked_post(url, body):
            self._checked()
            result = post(url, body)
            self._checked()
            return result

        self.adapter = adapter if adapter is not None else GrvtAdapter(http_post=checked_post)
        # Reuse the exact overlap/pagination validation, not its PAPER book.
        self.history = GrvtObservedSource(adapter=self.adapter, clock_ns=self._checked)

    def _checked(self) -> int:
        self.permission()
        at = self.clock()
        if type(at) is not int or not 0 < at < 2**63 or at < self.last_ns:
            raise ValueError('Reloj de la fuente REAL regresado o inválido')
        self.last_ns = at
        return at

    def _collect(self, symbol: str, timeframe: str, history: list[TradeBar]):
        instrument = self.history._instrument(symbol)
        step = INTERVALS[timeframe]
        # A boundary crossed by HTTP is recollected; intermediate points never
        # become trade signals or fresh copies of an earlier quote.
        for _ in range(3):
            end = self._checked()//10**9//step*step
            history = self.history._extend(symbol, timeframe, history, end)
            self._checked()
            raw = self.adapter.get_ticker(symbol)
            received = self._checked()
            quote = PerpQuote.from_ticker(raw, instrument, received)
            if received//10**9//step*step == end:
                return history, quote_input(quote), received
        raise ValueError('La fuente REAL no alcanzó una última vela cerrada estable')

    def prepare(self, origin: VerifiedObservedOrigin) -> PerpSignalSeed:
        if not isinstance(origin, VerifiedObservedOrigin):
            raise ValueError('Falta el original LAB verificado')
        requested = self._checked()
        origin.check_session()
        reference = origin.reference()
        if isinstance(reference, PerpPartialResearchReference):
            raise ValueError('Las salidas parciales de derivados requieren un perfil REAL compatible')
        if isinstance(reference, PerpLimitResearchReference):
            return self._prepare_seed(origin,reference,requested,PerpLimitSignalSeed,2,origin.hold_current)
        return self._prepare_seed(origin,reference,requested,PerpSignalSeed,1,origin.hold_current)

    def prepare_partial(self, source: VerifiedPartialPaperOrigin, terms: PerpPartialExitTerms) -> PerpPartialSignalSeed:
        if not isinstance(source, VerifiedPartialPaperOrigin):
            raise ValueError('Falta la prueba PAPER de parciales verificada')
        requested = self._checked()
        terms = PerpPartialExitTerms.model_validate(terms.payload())
        reviewed = source.terms(terms.budget,max_attempts=terms.max_attempts,
                                reserved_close_attempts=terms.reserved_close_attempts)
        if terms != reviewed or requested < int(terms.paper.last_received_at_ns):
            raise ValueError('Los términos parciales no corresponden al prefijo PAPER revisado')
        return self._prepare_seed(source.origin,terms.research,requested,PerpPartialSignalSeed,3,source.hold_current)

    def _prepare_seed(self, origin: VerifiedObservedOrigin, reference: ObservedResearchReference, requested: int,
                      seed_type: type[_Seed], revision: int, hold_current: Callable) -> _Seed:
        history = [TradeBar.model_validate(row) for row in json.loads(origin.trade_prefix_json)]
        rows, quote, received = self._collect(reference.symbol, reference.timeframe, history)
        with hold_current():
            accepted = self._checked()
            if accepted-received > MAX_CONSUMER_DELAY_NS:
                raise ValueError('Preparación REAL antigua antes de publicarla')
            return seed_type.model_validate({'schema':f'orbis.grvt.perp.signals.seed.v{revision}',
                'engine':'grvt-perp-signals-1.0', 'origin_reference_hash':reference.sha256,
                'profile':reference.profile.payload(), 'definition':json.loads(origin.definition_json),
                'instrument':quote.instrument, 'timeframe':reference.timeframe,
                'requested_at_ns':str(requested), 'prepared_at_ns':str(accepted), 'quote':quote.payload(),
                'history':[bar.model_dump() for bar in rows], 'execution_authority':'NONE'})

    def poll(self, state: PerpSignalState) -> PerpSignalInput:
        if not isinstance(state, PerpSignalState) or state.status != 'RUNNING':
            raise ValueError('Fuente REAL detenida o estado de señales ausente')
        self._checked()
        symbol = state.instrument.replace('_USDT_Perp', '/USDT:PERP')
        tail = [TradeBar.model_validate(bar.json()) for bar in state.history[-3:]]
        rows, quote, received = self._collect(symbol, state.timeframe, tail)
        return PerpSignalInput.model_validate({'schema':'orbis.grvt.perp.signals.input.v1',
            'collected_at_ns':str(received), 'accepted_at_ns':str(self._checked()),
            'quote':quote.payload(), 'closed_candles':[bar.model_dump() for bar in rows[len(tail):]]})
