"""Immutable internal observation state. No public session or authority format yet."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.derivatives.funding import FundingRecord
from app.derivatives.paper_math import InstrumentSpec, MarginRules, PerpQuote
from app.lab.engine import Candle
from app.lab.perp_book_types import Account, Entry
from app.lab.perp_profile import ExecutionProfile, PerpLimitProfile, PerpPartialProfile


OBSERVED_ENGINE = 'perp-observed-1.0'
LIMIT_OBSERVED_ENGINE = 'perp-observed-2.0'
PARTIAL_OBSERVED_ENGINE = 'perp-observed-3.0'
MAX_QUOTE_AGE_NS = 15_000_000_000
MAX_QUOTE_GAP_NS = 20_000_000_000
MAX_SIGNAL_DELAY_NS = 15_000_000_000
MAX_HISTORY_BARS = 100_000
Status = Literal['RUNNING','CLOSE_ONLY','STOPPED','GAP','HALTED','LIQUIDATED']


class ObservedError(ValueError):
    pass


@dataclass(frozen=True)
class OpenPosition:
    source_candle_t: int
    signal_known_at_ns: int
    entry_time_ns: int
    entry_quote_time_ns: int
    entry_price: Decimal
    quantity: Decimal
    entry_bar_index: int
    cash_before: Decimal
    realized_before: Decimal
    fees_before: Decimal
    funding_before: Decimal
    insurance_before: Decimal
    covered_before: Decimal
    trailing: Decimal


@dataclass(frozen=True)
class PartialOpenPosition(OpenPosition):
    partial_exits_json: tuple[str, ...] = ()


@dataclass(frozen=True)
class RestingLimit:
    order_id: str
    source_candle_t: int
    placed_at_ns: int
    expires_at_ns: int


def observed_version(profile: ExecutionProfile) -> tuple[int, str]:
    if isinstance(profile,PerpPartialProfile):
        return 3, PARTIAL_OBSERVED_ENGINE
    if isinstance(profile, PerpLimitProfile):
        return 2, LIMIT_OBSERVED_ENGINE
    return 1, OBSERVED_ENGINE


@dataclass(frozen=True)
class ObservedState:
    profile: ExecutionProfile
    definition_json: str
    timeframe: str
    account: Account
    history: tuple[Candle,...]
    funding_anchor: FundingRecord
    started_at_ns: int
    last_received_ns: int
    last_quote: PerpQuote
    last_control_ns: int = 0
    status: Status = 'RUNNING'
    reason: str | None = None
    opened: OpenPosition | None = None
    close_requested: bool = False
    order_count: int = 0
    closed_trades: int = 0
    resting: RestingLimit | None = None


@dataclass(frozen=True)
class Observation:
    quote: PerpQuote
    received_at_ns: int
    funding: tuple[FundingRecord,...]
    rules: MarginRules
    specification: InstrumentSpec
    closed_candles: tuple[Candle,...] = ()


@dataclass(frozen=True)
class ObservedTransition:
    state: ObservedState
    journal: tuple[Entry,...] = ()
    signals_json: tuple[str,...] = ()
    trades_json: tuple[str,...] = ()
    events_json: tuple[str,...] = ()
