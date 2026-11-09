"""Versioned LAB reductions on a single experimental net position."""
from dataclasses import replace
from decimal import Decimal

from app.lab.engine import LabError, number
from app.lab.perp_book_types import D, Order, Transition
from app.lab.perp_dataset import PerpDataset
from app.lab.perp_execution_math import adverse_price, first_margin_breach, first_trade_trigger, partial_reduction
from app.lab.perp_limit_simulation import LimitSimulation
from app.lab.perp_path import PathPoint, interpolate
from app.lab.perp_profile import PerpPartialProfile
from app.lab.perp_rules import RuleObservation
from app.lab.perp_simulation import PARTIAL_ENGINE, _Simulation, _financial_row


class PartialSimulation(_Simulation):
    profile: PerpPartialProfile

    def __init__(self, dataset: PerpDataset, profile: PerpPartialProfile, definition: dict,
                 start: int, end: int, *, rule_observation: RuleObservation | None = None):
        super().__init__(dataset,profile,definition,start,end,rule_observation=rule_observation)
        self.partial_events: list[dict] = []
        self._partial_fill: dict | None = None
        self._closed_orders: list[Order] = []

    def mark(self, point: PathPoint) -> None:
        # Completed orders have no risk or reservation effect. Keep their
        # exact records out of the active book to bound the cost of each mark.
        active=[]
        for order in self.account.orders:
            (active if order.active else self._closed_orders).append(order)
        if len(active)!=len(self.account.orders):
            self.account=replace(self.account,orders=tuple(active))
        super().mark(point)

    def record(self, transition: Transition) -> None:
        # A partial fill can trigger liquidation immediately. Record it before
        # the parent journal closes the whole trade.
        if self._partial_fill is not None:
            for offset,entry in enumerate(transition.entries):
                if entry.kind=='FILL':
                    event=self._partial_fill
                    event.update(status='FILLED',order_id=entry.order_id,journal_index=len(self.journal)+offset,
                        price=format(entry.price,'f'),fee=format(entry.fee,'f'),
                        realized_pnl=format(entry.realized_pnl,'f'),collateral_delta=format(entry.collateral_delta,'f'))
        super().record(transition)

    def finish_trade(self, reason: str, price: Decimal | None) -> None:
        assert self.opened is not None
        partials=self.opened.get('_partial_events',[])
        super().finish_trade(reason,price)
        self.trades[-1]['partial_exits']=partials

    def _next_target(self) -> tuple[int,Decimal] | None:
        assert self.opened is not None
        index=len(self.opened.get('_partial_events',[]))
        if index==len(self.profile.partial_targets):
            return None
        rule=self.profile.partial_targets[index]
        return index, D(self.opened['entry_price'])*(1+self.sign*number(rule.target))

    def _reduce(self, point: PathPoint, index: int, trigger: Decimal) -> None:
        assert self.opened is not None
        position=self.account.position(self.instrument)
        assert position is not None
        spec=self.market.specification
        portion=number(self.profile.partial_targets[index].fraction)
        price=adverse_price(point.trade,not self.long,self.slip,spec.tick_size)
        size,eligible=partial_reduction(D(self.opened['quantity']),portion,abs(position.size),price,spec)
        event={'entry_time_ns':self.opened['entry_time_ns'],'time_ns':str(point.time_ns),'target_index':index+1,
            'trigger_price':format(trigger,'f'),'fraction':format(portion,'f'),
            'quantity':format(size,'f'),'remaining_before':format(abs(position.size),'f'),
            'remaining_after':format(abs(position.size),'f'),'status':'SKIPPED_MINIMUM'}
        self.opened.setdefault('_partial_events',[]).append(event)
        self.partial_events.append(event)
        if not eligible:
            return
        self._partial_fill=event
        try:
            filled=self.execute(point,size,opening=False,reason='PARTIAL_TARGET')
        finally:
            self._partial_fill=None
        if filled is None:
            event.update(status='EXIT_HALTED',reason=self.halt_reason)
        else:
            event['remaining_after']=format(abs(position.size)-size,'f')

    def trigger_now(self, point: PathPoint) -> None:
        if self.opened is None or self.halted:
            return
        stop,final_target=self.levels()
        if (point.trade<=stop if self.long else point.trade>=stop):
            self.exit(point,'STOP_OR_TRAILING')
            return
        while self.opened is not None and not self.halted:
            target=self._next_target()
            if target is None or (point.trade<target[1] if self.long else point.trade>target[1]):
                break
            self._reduce(point,*target)
        if self.opened is not None and not self.halted and (point.trade>=final_target if self.long else point.trade<=final_target):
            self.exit(point,'TAKE_PROFIT')

    def segment(self, start: PathPoint, end: PathPoint) -> None:
        # Recalculate MARK risk after every reduction before advancing farther.
        point=start
        while self.opened is not None:
            stop,final=self.levels()
            next_target=self._next_target()
            targets=[(stop,self.long),(final,not self.long)]
            if next_target is not None:
                targets.append((next_target[1],not self.long))
            times=[first_trade_trigger(point,end,price,below=below) for price,below in targets]
            times.append(first_margin_breach(self.account,point,end))
            trigger=min((stamp for stamp in times if stamp is not None),default=end.time_ns)
            if trigger>=end.time_ns:
                break
            point=interpolate(point,end,trigger)
            prior=self._next_target()
            self.mark(point)
            self.trigger_now(point)
            if self.halted:
                return
            if self.opened is not None and self._next_target()==prior:
                raise LabError('El tramo de salidas parciales no pudo avanzar')
        self.mark(end)
        self.funding(end)
        self.trigger_now(end)

    def run(self) -> dict:
        result=super().run()
        result['engine']=PARTIAL_ENGINE
        result['partial_events']=self.partial_events
        result['metrics']['partial_exits']=sum(event['status']=='FILLED' for event in self.partial_events)
        orders=sorted((*self._closed_orders,*self.account.orders),key=lambda order:int(order.id.removeprefix('perp-')))
        result['account']['orders']=[_financial_row(order) for order in orders]
        result['assumptions'].append(
            'Partial targets reduce the original quantity in lot steps at modeled market prices; stop and final target close the residue')
        return result


class PartialLimitSimulation(LimitSimulation, PartialSimulation):
    """LIMIT entry and the same v3 reduce-only exits."""
