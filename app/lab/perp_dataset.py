"""Immutable GRVT research inputs; current risk snapshots are not history.

This format contains no trading authority and no backtest results. In particular
MARK prices never become TRADE candles or fill prices merely by being saved.
"""
from __future__ import annotations

from decimal import localcontext
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.derivatives.funding import FundingRecord, HOUR_NS
from app.derivatives.paper_math import MarginRules
from app.lab.engine import INTERVALS, LabError, number, validate_candles


SCHEMA = 'orbis.lab.perp.dataset.v1'
SYMBOLS = ('BTC/USDT:PERP', 'ETH/USDT:PERP', 'SOL/USDT:PERP')
TIMEFRAMES = ('1m', '5m', '15m', '1h', '4h', '1d')
MAX_SECONDS = 366 * 86400


class ExactModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, populate_by_name=True)


class MarkBar(ExactModel):
    t: int = Field(gt=0)
    o: str
    h: str
    l: str
    c: str

    @model_validator(mode='after')
    def valid_prices(self) -> Self:
        values = [number(getattr(self, key), positive=True) for key in ('o','h','l','c')]
        if values[1] < max(values) or values[2] > min(values):
            raise ValueError('OHLC de precio inconsistente')
        return self


class TradeBar(MarkBar):
    v: str

    @model_validator(mode='after')
    def valid_volume(self) -> Self:
        if number(self.v) < 0:
            raise ValueError('Volumen negativo')
        return self


class TradeSeries(ExactModel):
    source: Literal['grvt-perp-trade-public']
    price_kind: Literal['TRADE']
    bars: list[TradeBar] = Field(min_length=3, max_length=5000)


class MarkSeries(ExactModel):
    source: Literal['grvt-perp-mark-public']
    price_kind: Literal['MARK']
    bars: list[MarkBar] = Field(min_length=3, max_length=5000)


class FundingPoint(ExactModel):
    instrument: str = Field(max_length=40)
    funding_time: str = Field(pattern=r'^[0-9]{1,20}$')
    funding_rate: str
    mark_price: str
    funding_interval_hours: Literal[1,4,8]

    @field_validator('funding_interval_hours',mode='before')
    @classmethod
    def exact_interval(cls,value):
        if type(value) is not int:
            raise ValueError('Intervalo de financiación entero requerido')
        return value


class RiskTier(ExactModel):
    tier: int = Field(ge=1, le=100)
    notional_floor: str
    notional_cap: str | None = None
    maintenance_margin_rate: str
    initial_margin_rate: str
    max_leverage: int = Field(ge=1, le=10000)
    cumulative_maintenance_amount: str


class RiskSnapshot(ExactModel):
    instrument: str = Field(max_length=40)
    max_position_size: str
    fetched_at_ns: str = Field(pattern=r'^[0-9]{1,20}$')
    risk_brackets: list[RiskTier] = Field(min_length=1, max_length=100)


class InstrumentSnapshot(ExactModel):
    instrument: str = Field(max_length=40)
    kind: Literal['PERPETUAL']
    venues: list[Literal['ORDERBOOK','RFQ']] = Field(min_length=1, max_length=2)
    base_decimals: int = Field(ge=-18, le=18)
    min_size: str
    min_notional: str
    tick_size: str
    max_position_size: str | None
    fetched_at_ns: str = Field(pattern=r'^[0-9]{1,20}$')

    @model_validator(mode='after')
    def valid_spec(self) -> Self:
        if 'ORDERBOOK' not in self.venues or len(set(self.venues)) != len(self.venues):
            raise ValueError('Instrumento sin libro verificable')
        for key in ('min_size','min_notional','tick_size'):
            number(getattr(self,key), positive=True)
        if self.max_position_size is not None:
            number(self.max_position_size, positive=True)
        return self


def validate_rule_pair(rules: RiskSnapshot, specification: InstrumentSnapshot, instrument: str) -> None:
    if rules.instrument != instrument or specification.instrument != instrument:
        raise ValueError('Instantáneas de otro instrumento')
    with localcontext() as context:
        context.prec = 80
        MarginRules.from_payload(rules.model_dump(),instrument,int(rules.fetched_at_ns))
    if (specification.max_position_size is not None
            and number(specification.max_position_size) != number(rules.max_position_size)):
        raise ValueError('Las instantáneas discrepan en el tamaño máximo')


class PerpDataset(ExactModel):
    schema_name: Literal['orbis.lab.perp.dataset.v1'] = Field(alias='schema')
    provider: Literal['grvt']
    symbol: Literal['BTC/USDT:PERP','ETH/USDT:PERP','SOL/USDT:PERP']
    instrument: str = Field(max_length=40)
    timeframe: Literal['1m','5m','15m','1h','4h','1d']
    start: int = Field(gt=0)
    end: int = Field(gt=0)
    captured_start_ns: str = Field(pattern=r'^[0-9]{1,20}$')
    captured_end_ns: str = Field(pattern=r'^[0-9]{1,20}$')
    trade: TradeSeries
    mark: MarkSeries
    funding_source: Literal['grvt-public-funding-interval']
    funding: list[FundingPoint] = Field(min_length=1, max_length=8786)
    rules: RiskSnapshot
    specification: InstrumentSnapshot
    risk_rules_scope: Literal['CURRENT_SNAPSHOT_NOT_HISTORICAL']
    historical_execution_model: Literal['NOT_RUN']

    @model_validator(mode='after')
    def consistent(self) -> Self:
        step = INTERVALS[self.timeframe]
        begun, finished = int(self.captured_start_ns), int(self.captured_end_ns)
        if (begun <= 0 or self.instrument != self.symbol.replace('/','_').replace(':PERP','_Perp')
                or not self.start < self.end <= begun//10**9
                or self.end-self.start > MAX_SECONDS or self.start % step or self.end % step
                or finished < begun
                or finished-begun > 3600*10**9):
            raise ValueError('Identidad o periodo del dataset perpetuo inconsistente')
        expected = list(range(self.start,self.end,step))
        if not 3 <= len(expected) <= 5000:
            raise ValueError('Cobertura perpetua fuera de rango')
        for series in (self.trade,self.mark):
            if [bar.t for bar in series.bars] != expected:
                raise ValueError('Cobertura TRADE/MARK incompleta o desordenada')
        for snapshot in (self.rules,self.specification):
            if (snapshot.instrument != self.instrument
                    or not begun <= int(snapshot.fetched_at_ns) <= finished):
                raise ValueError('Instantánea de reglas fuera de su captura')
        validate_rule_pair(self.rules,self.specification,self.instrument)
        records = [FundingRecord.from_payload(row.model_dump(),self.instrument) for row in self.funding]
        times = [r.time_ns for r in records]
        lower, upper = self.start*10**9, self.end*10**9
        if (times != sorted(set(times)) or not times[0] < lower
                or len(times)>1 and times[1] < lower or times[-1] >= upper):
            raise ValueError('Financiación sin ancla única o con tiempos inconsistentes')
        for previous,current in zip(records,records[1:]):
            if current.time_ns-previous.time_ns != current.interval_hours*HOUR_NS:
                raise ValueError('Hueco o cambio temporal de financiación no verificable')
        if upper-1 >= records[-1].time_ns+records[-1].interval_hours*HOUR_NS:
            raise ValueError('Falta una liquidación de financiación esperada')
        return self

    def payload(self) -> dict:
        return self.model_dump(mode='json',by_alias=True)


def read_perp_dataset(value: object) -> PerpDataset:
    try:
        return PerpDataset.model_validate(value)
    except (ValidationError,ValueError,KeyError,TypeError) as exc:
        raise LabError('Dataset perpetuo no verificable: '+str(exc)[:350]) from exc


def prepare_perp_dataset(symbol: str, timeframe: str, start: int, end: int, *,
                         adapter=None, clock=None) -> PerpDataset:
    """Fetch everything before storing anything; no write lock during HTTP."""
    import time
    from app.marketdata.adapters.grvt import GrvtAdapter
    clock = clock or time.time_ns
    begun = clock()
    if (symbol not in SYMBOLS or timeframe not in TIMEFRAMES
            or any(type(v) is not int for v in (start,end,begun))):
        raise LabError('Mercado o periodo perpetuo inválidos')
    step = INTERVALS[timeframe]
    if (start<=0 or end<=start or end>begun//10**9 or end-start>MAX_SECONDS
            or start%step or end%step or not 3<=(end-start)//step<=5000):
        raise LabError('Rango perpetuo inválido, excesivo o con vela abierta')
    source = adapter or GrvtAdapter()
    trade = source.get_historical_candles(symbol,timeframe,start,end,now=begun//10**9)
    # TRADE has its own legacy adapter contract; validate it again at the boundary.
    bars,gaps = validate_candles(trade,timeframe,now=begun//10**9)
    if gaps or [bar.t for bar in bars] != list(range(start,end,step)):
        raise LabError('Histórico TRADE incompleto')
    mark = source.get_mark_history(symbol,timeframe,start,end,now=begun//10**9)
    raw_funding = source.get_funding_history(symbol,start*10**9,end*10**9-1)
    instrument = symbol.replace('/','_').replace(':PERP','_Perp')
    records = [FundingRecord.from_payload(row,instrument) for row in raw_funding]
    records.sort(key=lambda r:r.time_ns)
    anchors = [r.time_ns for r in records if r.time_ns<start*10**9]
    if not anchors:
        raise LabError('Financiación sin ancla anterior al periodo')
    anchor = max(anchors)
    selected = [r for r in records if r.time_ns>=anchor]
    funding = [{'instrument':r.instrument,'funding_time':str(r.time_ns),
        'funding_rate':format(r.rate_percent,'f'),'mark_price':format(r.mark,'f'),
        'funding_interval_hours':r.interval_hours} for r in selected]
    rules = source.get_margin_rules(symbol)
    spec = source.get_instrument_spec(symbol)
    if rules is None or spec is None:
        raise LabError('Faltan instantáneas públicas de reglas o instrumento')
    for snapshot in (rules,spec):
        if type(snapshot.get('fetched_at_ns')) is not int:
            raise LabError('Instantánea sin tiempo exacto')
    rules = {**rules,'fetched_at_ns':str(rules['fetched_at_ns'])}
    spec = {**spec,'fetched_at_ns':str(spec['fetched_at_ns'])}
    return read_perp_dataset({'schema':SCHEMA,'provider':'grvt','symbol':symbol,
        'instrument':instrument,'timeframe':timeframe,'start':start,'end':end,
        'captured_start_ns':str(begun),'captured_end_ns':str(clock()),
        'trade':{'source':'grvt-perp-trade-public','price_kind':'TRADE','bars':[bar.json() for bar in bars]},
        'mark':{'source':'grvt-perp-mark-public','price_kind':'MARK','bars':mark},
        'funding_source':'grvt-public-funding-interval','funding':funding,
        'rules':rules,'specification':spec,'risk_rules_scope':'CURRENT_SNAPSHOT_NOT_HISTORICAL',
        'historical_execution_model':'NOT_RUN'})
