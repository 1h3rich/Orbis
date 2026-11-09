"""Deterministic single-market experimental perpetual backtest.

This engine uses declared synthetic TRADE/MARK paths. It cannot prove actual
intrabar order, liquidity, historical risk rules or executable venue results.
Existing lab-1.16 and PAPER calculations are never routed through this module.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from app.lab.engine import (
    Candle, IndicatorCache, INTERVALS, LabError, evaluate, number,
    validate_rule_timeframes,
)
from app.lab.perp_account import (
    equity, fill_order, mark_all, new_account, settle_funding, submit_order,
)
from app.lab.perp_book_types import Cursor, D, Entry, Transition, ZERO, exact
from app.lab.perp_dataset import PerpDataset, read_perp_dataset
from app.lab.perp_execution_math import (
    adverse_price, first_margin_breach, first_trade_trigger, opening_size,
)
from app.lab.perp_path import PathPoint, bar_path, interpolate
from app.lab.perp_profile import ExecutionProfile, PerpLimitProfile, PerpPartialProfile, PerpProfile, bind_profile, read_profile
from app.lab.perp_rules import RuleObservation
from app.lab.store import content_hash


ENGINE = 'perp-ohlc-1.0'
LIMIT_ENGINE = 'perp-ohlc-2.0'
PARTIAL_ENGINE = 'perp-ohlc-3.0'


def simulation_engine(profile: ExecutionProfile) -> str:
    if isinstance(profile,PerpPartialProfile):
        return PARTIAL_ENGINE
    return LIMIT_ENGINE if isinstance(profile, PerpLimitProfile) else ENGINE


def _entry_payload(entry: Entry, index: int) -> dict:
    raw = asdict(entry)
    raw['cursor']['time_ns'] = str(raw['cursor']['time_ns'])
    return {'index':index, **{key:format(value,'f') if isinstance(value,Decimal) else value
                            for key,value in raw.items()}}


def _financial_row(row: object) -> dict:
    return {key:format(value,'f') if isinstance(value,Decimal) else value
            for key,value in asdict(row).items()}


class _Simulation:
    def __init__(self, dataset: PerpDataset, profile: PerpProfile, definition: dict,
                 start: int, end: int, *, rule_observation: RuleObservation | None = None):
        self.data, self.profile = dataset, profile
        self.definition, self.market = bind_profile(profile,definition,dataset,rule_observation=rule_observation)
        validate_rule_timeframes(self.definition,dataset.timeframe)
        self.account = new_account(number(self.definition['initial_capital']),(self.market,))
        self.start, self.end = start, end
        self.instrument = dataset.instrument
        self.long = self.definition['direction']=='LONG'
        self.sign = D(1) if self.long else D(-1)
        self.slip = number(self.definition['slippage_rate'])
        self.candles = [Candle(b.t,*(number(getattr(b,k)) for k in ('o','h','l','c','v'))) for b in dataset.trade.bars]
        self.cache = IndicatorCache(self.candles)
        self.time_ns, self.ordinal = 0, -1
        self.journal: list[dict] = []
        self.signals: list[dict] = []
        self.exit_signals: list[dict] = []
        self.trades: list[dict] = []
        self.curve: list[dict] = []
        self.opened: dict | None = None
        self.pending: dict | None = None
        self.exit_pending: dict | None = None
        self.trailing: Decimal | None = None
        self.liquidated = False
        self.halted = False
        self.halt_reason: str | None = None
        self.order_count = 0
        self.peak = self.account.initial_cash
        self.max_drawdown = ZERO

    def cursor(self, time_ns: int) -> Cursor:
        if time_ns < self.time_ns:
            raise LabError('El plan de ejecución retrocede en el tiempo')
        self.ordinal = self.ordinal+1 if time_ns==self.time_ns else 0
        self.time_ns = time_ns
        return Cursor(time_ns,self.ordinal)

    def record(self, transition: Transition) -> None:
        self.account = transition.account
        first_index = len(self.journal)
        self.journal.extend(_entry_payload(entry,first_index+offset)
                            for offset,entry in enumerate(transition.entries))
        liquidations = [e for e in transition.entries if e.kind in ('ISOLATED_LIQUIDATION','CROSS_ACCOUNT_LIQUIDATION')]
        if liquidations:
            self.liquidated = True
            if self.opened is not None:
                self.finish_trade('LIQUIDATION',None)
        if self.account.marks:
            marked = equity(self.account)
            self.peak = max(self.peak,marked)
            drawdown = (self.peak-marked)/self.peak*100 if self.peak>0 else ZERO
            self.max_drawdown = max(self.max_drawdown,drawdown)
            self.curve.append({'time_ns':str(self.time_ns),'ordinal':self.ordinal,
                'equity':format(marked,'f'),'cash':format(self.account.cash,'f'),
                'drawdown_pct':format(drawdown,'f')})

    def mark(self, point: PathPoint) -> None:
        self.record(mark_all(self.account,self.cursor(point.time_ns),{self.instrument:point.mark}))

    def funding(self, point: PathPoint) -> None:
        if point.funding is None:
            return
        funding_mark = number(point.funding.mark_price)
        if self.account.mark(self.instrument)!=funding_mark:
            self.mark(PathPoint(point.time_ns,point.trade,funding_mark))
        self.record(settle_funding(self.account,self.cursor(point.time_ns),instrument=self.instrument,
            rate_percent=number(point.funding.funding_rate),settlement_mark=funding_mark))

    def finish_trade(self, reason: str, price: Decimal | None) -> None:
        assert self.opened is not None
        opened = self.opened
        result = {key:value for key,value in opened.items() if not key.startswith('_')}
        result.update(exit_time_ns=str(self.time_ns),exit_price=format(price,'f') if price is not None else None,
            exit_reason=reason,net_profit=format(self.account.cash-opened['_cash_before'],'f'),
            realized_pnl=format(self.account.realized_pnl-opened['_realized_before'],'f'),
            fees=format(self.account.fees_paid-opened['_fees_before'],'f'),
            funding_paid=format(self.account.funding_paid-opened['_funding_before'],'f'),
            insurance_transfer=format(self.account.insurance_transferred-opened['_insurance_before'],'f'),
            insurance_cover=format(self.account.insurance_covered-opened['_covered_before'],'f'))
        self.trades.append(result)
        self.opened, self.trailing, self.exit_pending = None, None, None

    def execute(self, point: PathPoint, size: Decimal, *, opening: bool, reason: str) -> Decimal | None:
        buying = self.long if opening else not self.long
        price = adverse_price(point.trade,buying,self.slip,self.market.specification.tick_size)
        if price<=0:
            self.halted,self.halt_reason = True,'EXECUTION_PRICE_BELOW_TICK'
            return None
        self.order_count += 1
        identifier = 'perp-'+str(self.order_count)
        self.record(submit_order(self.account,self.cursor(point.time_ns),identifier=identifier,
            instrument=self.instrument,side='BUY' if buying else 'SELL',quantity=size,
            price_bound=price,reduce_only=not opening))
        order = self.account.order(identifier)
        if not order.active:
            if opening:
                assert self.pending is not None
                self.pending['action']='ORDER_REJECTED'
                self.pending['reason']=order.reason
            else:
                self.halted,self.halt_reason = True,'EXIT_ORDER_REJECTED:'+str(order.reason)
            return None
        self.record(fill_order(self.account,self.cursor(point.time_ns),identifier,quantity=size,price=price))
        if self.account.order(identifier).status!='FILLED':
            if opening:
                assert self.pending is not None
                self.pending['action']='ENTRY_PRE_TRADE_CANCELLED'
                self.pending['reason']=self.account.order(identifier).reason
            else:
                self.halted,self.halt_reason = True,'EXIT_PRE_TRADE_CANCELLED:'+str(self.account.order(identifier).reason)
            return None
        return price

    def enter(self, point: PathPoint, index: int) -> None:
        if self.pending is None or self.liquidated or self.halted:
            return
        size_price = adverse_price(point.trade,self.long,self.slip,self.market.specification.tick_size)
        if size_price<=0:
            self.pending['action']='SKIPPED_PRICE_BELOW_TICK'
            self.pending = None
            return
        size = opening_size(self.account.cash,number(self.definition['allocation']),self.market,
                            size_price,point.mark,long=self.long)
        if not size:
            self.pending['action']='SKIPPED_SIZE_OR_MARGIN'
            self.pending = None
            return
        before = self.account
        signal = self.pending
        # Install the owner snapshot before a fill: a fee-triggered immediate
        # liquidation must be attributed to the trade that caused it.
        self.opened = {'source_candle_t':signal['source_candle_t'],'signal_known_at_ns':signal['known_at_ns'],
            'entry_time_ns':str(point.time_ns),'entry_price':format(size_price,'f'),
            'quantity':format(size,'f'),'direction':self.definition['direction'],
            '_index':index,'_cash_before':before.cash,'_realized_before':before.realized_pnl,
            '_fees_before':before.fees_paid,'_funding_before':before.funding_paid,
            '_insurance_before':before.insurance_transferred,'_covered_before':before.insurance_covered}
        price = self.execute(point,size,opening=True,reason='ENTRY_SIGNAL')
        if price is None:
            self.opened = None
        else:
            signal['action']='ENTERED_NEXT_OPEN'
            signal['entry_time_ns']=str(point.time_ns)
            signal['entry_price']=format(price,'f')
            self.trailing = price if self.opened is not None else None
        self.pending = None

    def levels(self) -> tuple[Decimal,Decimal]:
        assert self.opened is not None
        entry = D(self.opened['entry_price'])
        stop = entry*(D(1)-self.sign*number(self.definition['stop_loss']))
        target = entry*(D(1)+self.sign*number(self.definition['take_profit']))
        if self.definition.get('trailing_stop') is not None and self.trailing is not None:
            trailing = self.trailing*(D(1)-self.sign*number(self.definition['trailing_stop']))
            stop = max(stop,trailing) if self.long else min(stop,trailing)
        return stop,target

    def exit(self, point: PathPoint, reason: str) -> None:
        position = self.account.position(self.instrument)
        if position is None:
            return
        price = self.execute(point,abs(position.size),opening=False,reason=reason)
        if price is not None and self.opened is not None:
            self.finish_trade(reason,price)

    def trigger_now(self, point: PathPoint) -> None:
        if self.opened is None:
            return
        stop,target = self.levels()
        stop_hit = point.trade<=stop if self.long else point.trade>=stop
        target_hit = point.trade>=target if self.long else point.trade<=target
        if stop_hit:
            self.exit(point,'STOP_OR_TRAILING')
        elif target_hit:
            self.exit(point,'TAKE_PROFIT')

    def segment(self, start: PathPoint, end: PathPoint) -> None:
        trigger: int | None = None
        if self.opened is not None:
            stop,target = self.levels()
            times = [t for t in (first_trade_trigger(start,end,stop,below=self.long),
                first_trade_trigger(start,end,target,below=not self.long),
                first_margin_breach(self.account,start,end)) if t is not None]
            trigger = min(times) if times else None
        if trigger is not None and trigger<end.time_ns:
            point = interpolate(start,end,trigger)
            self.mark(point)  # MARK liquidation has priority over a tied trigger.
            if self.opened is not None:
                self.trigger_now(point)
            if self.halted:
                return
        self.mark(end)
        self.funding(end)     # Funding at this endpoint precedes trade exits.
        if self.opened is not None:
            self.trigger_now(end)

    def signal(self, index: int, *, warmup: bool = False) -> None:
        matched,explanation = evaluate(self.definition['entry'],self.candles,index,self.cache,
                                      base_timeframe=self.data.timeframe)
        if not matched:
            return
        action = ('SKIPPED_LIQUIDATED' if self.liquidated else 'SKIPPED_POSITION_OPEN' if self.opened
                  else 'SKIPPED_NO_NEXT_CANDLE' if index==self.end-1 else 'PLANNED_NEXT_OPEN')
        signal = {'source_candle_t':self.candles[index].t,
            'known_at_ns':str((self.candles[index].t+INTERVALS[self.data.timeframe])*10**9),
            'action':action,'warmup_context':warmup,'explanation':explanation}
        self.signals.append(signal)
        if action=='PLANNED_NEXT_OPEN':
            self.pending=signal

    def update_trailing(self, index: int) -> None:
        bar = self.candles[index]
        self.trailing = max(self.trailing,bar.h) if self.long else min(self.trailing,bar.l)

    def run(self) -> dict:
        if self.start:
            self.signal(self.start-1,warmup=True)
        for index in range(self.start,self.end):
            points = bar_path(self.data,index,self.profile.trade_path,self.profile.mark_path)
            first = points[0]
            if first.funding is not None:
                # Open boundary sequence is explicit, not guessed from OHLC.
                actual = PathPoint(first.time_ns,first.trade,number(first.funding.mark_price))
                self.mark(actual)
                self.funding(first)
            self.mark(first)
            if self.opened is not None:
                holding = self.definition.get('max_holding_bars')
                if self.exit_pending:
                    self.opened['exit_signal_known_at_ns']=self.exit_pending['known_at_ns']
                    self.opened['exit_explanation']=self.exit_pending['explanation']
                    self.exit(first,'EXIT_RULE_NEXT_OPEN')
                elif holding is not None and index-self.opened['_index']>=holding:
                    self.exit(first,'TIME_LIMIT')
                else:
                    self.trigger_now(first)
            self.enter(first,index)
            if self.opened is not None:
                self.trigger_now(first)
            if self.halted:
                break
            for start,end in zip(points,points[1:]):
                self.segment(start,end)
                if self.halted:
                    break
            if self.halted:
                break
            if index==self.end-1 and self.opened is not None:
                self.exit(points[-1],'END_OF_WINDOW_ARTIFICIAL_CLOSE')
                if self.halted:
                    break
            if self.opened is not None:
                bar = self.candles[index]
                self.update_trailing(index)
                if self.definition.get('exit') is not None:
                    matched, explanation = evaluate(self.definition['exit'],self.candles,index,self.cache,
                                                    base_timeframe=self.data.timeframe)
                    self.exit_pending = ({'source_candle_t':bar.t,
                        'known_at_ns':str((bar.t+INTERVALS[self.data.timeframe])*10**9),
                        'explanation':explanation} if matched else None)
                    if self.exit_pending is not None:
                        self.exit_signals.append(self.exit_pending)
            self.signal(index)
        final_equity = equity(self.account)
        closed = [D(t['net_profit']) for t in self.trades]
        wins,losses = [v for v in closed if v>0],[v for v in closed if v<0]
        exposure_ns=sum(int(t['exit_time_ns'])-int(t['entry_time_ns']) for t in self.trades)
        if self.opened is not None:exposure_ns+=self.time_ns-int(self.opened['entry_time_ns'])
        elapsed_ns=self.time_ns-self.candles[self.start].t*10**9
        return {'engine':ENGINE,'mode':'EXPERIMENTAL_CURRENT_RULES','status':'HALTED_EXECUTION' if self.halted else 'COMPLETE',
            'halt_reason':self.halt_reason,'profile':self.profile.payload(),'profile_hash':content_hash(self.profile.payload()),
            'dataset_hash':content_hash(self.data.payload()),'strategy_hash':self.profile.strategy_hash,
            'window':{'entry_start_index':self.start,'end_index':self.end,
                'requested_start_ns':str(self.candles[self.start].t*10**9),
                'requested_end_ns':str((self.candles[self.end-1].t+INTERVALS[self.data.timeframe])*10**9),
                'processed_through_ns':str(self.time_ns)},
            'signals':self.signals,'exit_signals':self.exit_signals,'trades':self.trades,'journal':self.journal,'equity':self.curve,
            'account':{'cash':format(self.account.cash,'f'),
                'positions':[_financial_row(p) for p in self.account.positions],
                'orders':[_financial_row(o) for o in self.account.orders],
                'funding_through':[{ 'instrument':name,'time_ns':str(stamp)} for name,stamp in self.account.funding_through]},
            'metrics':{'trades':len(closed),'final_equity':format(final_equity,'f'),
                'cash':format(self.account.cash,'f'),'net_equity_change':format(final_equity-self.account.initial_cash,'f'),
                'return_pct':format((final_equity/self.account.initial_cash-1)*100,'f'),
                'closed_net_profit':format(sum(closed,ZERO),'f'),'fees_paid':format(self.account.fees_paid,'f'),
                'funding_paid':format(self.account.funding_paid,'f'),
                'insurance_transfer':format(self.account.insurance_transferred,'f'),
                'insurance_cover':format(self.account.insurance_covered,'f'),
                'win_rate':format(D(len(wins))/D(len(closed)),'f') if closed else None,
                'win_rate_pct':format(D(len(wins))*100/D(len(closed)),'f') if closed else None,
                'expectancy':format(sum(closed,ZERO)/D(len(closed)),'f') if closed else None,
                'average_duration_seconds':format(D(sum(int(t['exit_time_ns'])-int(t['entry_time_ns']) for t in self.trades))/D(len(closed))/D(10**9),'f') if closed else None,
                'exposure_pct':format(D(exposure_ns)*100/D(elapsed_ns),'f') if elapsed_ns>0 else '0',
                'signal_count':len(self.signals),
                'profit_factor':format(sum(wins,ZERO)/-sum(losses,ZERO),'f') if losses else None,
                'max_drawdown_pct':format(self.max_drawdown,'f'),'liquidated':self.liquidated,
                'open_position':self.account.position(self.instrument) is not None},
            'assumptions':['Current rule/instrument snapshots applied to the past: experimental, not historical venue proof',
                'TRADE and MARK follow independently declared synthetic OHLC paths; funding anchors interior MARK',
                'Extremes at one third/two thirds; same-time funding collision shifts the synthetic MARK extreme by1ns',
                'Previous close precedes funding, then next open; MARK liquidation precedes tied trade exit',
                'Signals use closed candles only; next-open market fills with adverse slippage and tick rounding',
                'MARK interpolation and money use18dp; intrabar event times resolved to nanoseconds across risk brackets',
                'No liquidity, orderbook queue, historical spread or partial-fill evidence is claimed',
                'One net position; stop/trailing/target are market triggers; trailing updates only after candle close',
                'New entries stop after liquidation; failed exit halts with open exposure rather than inventing a close',
                'Final close is artificial and precedes any unknown funding at the end-exclusive boundary'],
            'warnings':(['SMALL_SAMPLE'] if len(closed)<30 else [])+(['HALTED_WITH_EXPOSURE'] if self.halted and self.opened else []),
            'paper_handoff_available':False,'real_authority':False}


@exact
def simulate_perpetual(dataset: PerpDataset, profile: ExecutionProfile, definition: dict, *,
                       entry_start_index: int = 0, end_index: int | None = None,
                       rule_observation: RuleObservation | None = None) -> dict:
    # Nested source lists can be mutated after Pydantic construction. Reparse
    # owned canonical inputs before using coverage or price-kind assertions.
    dataset = read_perp_dataset(dataset.payload())
    profile = read_profile(profile.payload())
    if rule_observation is not None:
        rule_observation = RuleObservation.model_validate(rule_observation.payload())
    end = len(dataset.trade.bars) if end_index is None else end_index
    if (type(entry_start_index) is not int or type(end) is not int
            or not 0<=entry_start_index<end<=len(dataset.trade.bars)):
        raise LabError('Ventana de ejecución fuera del archivo')
    engine: type[_Simulation]
    if isinstance(profile,PerpPartialProfile):
        from app.lab.perp_partial_simulation import PartialLimitSimulation, PartialSimulation
        engine = PartialLimitSimulation if profile.entry_order is not None else PartialSimulation
    elif isinstance(profile, PerpLimitProfile):
        from app.lab.perp_limit_simulation import LimitSimulation
        engine = LimitSimulation
    else:
        engine = _Simulation
    result = engine(dataset,profile,definition,entry_start_index,end,rule_observation=rule_observation).run()
    if rule_observation is not None:
        result['mode'] = 'EXPERIMENTAL_CAPTURED_RULE_SCENARIO'
        result['rule_observation_hash'] = content_hash(rule_observation.payload())
        result['assumptions'][0] = 'Alternative rule and instrument capture applied as a counterfactual throughout the window'
    return result
