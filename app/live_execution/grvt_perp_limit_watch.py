"""Deterministic LIMIT supervision over archived venue fills and public data."""
from dataclasses import dataclass, replace

from app.derivatives.perp_observed_wire import quote_input
from app.live_execution.grvt_mainnet_preflight import number
from app.live_execution.grvt_perp_exit import (
    PerpExitCursor, observe_perp_exit, perp_exit_checkpoint, start_perp_exit,
)
from app.live_execution.grvt_perp_limit_plan import NS, PerpLimitEntryPlan
from app.live_execution.grvt_perp_signals import (
    MAX_CONSUMER_DELAY_NS, PerpLimitSignalSeed, PerpPartialSignalSeed, PerpSignalState, replay_signal_records,
    signal_checkpoint, verify_signal_record,
)


@dataclass(frozen=True)
class LimitWatch:
    feed: PerpSignalState
    cursor: PerpExitCursor | None = None
    close_reason: str | None = None
    close_at_ns: int | None = None


def start_limit_watch(claim: dict) -> LimitWatch:
    # The entry claim validates the version pairing before a watch is replayed.
    seed_type = PerpPartialSignalSeed if claim['seed'].get('schema')=='orbis.grvt.perp.signals.seed.v3' else PerpLimitSignalSeed
    seed = seed_type.model_validate(claim['seed'])
    return LimitWatch(replay_signal_records(seed,claim['records']).state)


def advance_limit_watch(watch: LimitWatch, plan: PerpLimitEntryPlan, events: dict,
                        record: dict | None, *, at_ns: int) -> tuple[LimitWatch, dict]:
    feed = watch.feed
    if watch.close_reason is not None or not feed.last_accepted_ns <= at_ns < 2**63:
        raise ValueError('Supervisión LIMIT cerrada o fuera del tiempo de la fuente')
    if record is not None:
        feed = verify_signal_record(feed,record).state
        if not 0 <= at_ns-feed.last_accepted_ns <= MAX_CONSUMER_DELAY_NS:
            raise ValueError('Observación de supervisión LIMIT vencida o futura')
    watch = replace(watch,feed=feed)
    receipt = events['receipt']
    filled = receipt is not None and bool(receipt['fills'])
    terminal = receipt is not None and receipt['terminal']
    if filled and terminal:
        if watch.cursor is None:
            watch = replace(watch,cursor=start_perp_exit(feed,size=number(receipt['filled_size']),
                entry_notional=number(receipt['entry_notional']),
                last_fill_ns=max(fill['event_time_ns'] for fill in receipt['fills']),
                confirmed_at_ns=events['observation_at_ns']))
        watch = replace(watch,cursor=observe_perp_exit(watch.cursor,feed,now_ns=at_ns))

    reason = None
    if filled and receipt['policy_breaches']:
        reason = 'ENTRY_LIMIT_BREACH'
    elif feed.status != 'RUNNING':
        reason = 'DATA_GAP'
    elif at_ns-feed.last_observed_ns > MAX_CONSUMER_DELAY_NS:
        reason = 'DATA_STALE'
    elif watch.cursor is not None:
        if watch.cursor.action == 'CLOSE':
            reason = watch.cursor.reason
        elif at_ns-watch.cursor.last_fill_ns >= plan.source.terms.budget.exit_seconds*NS:
            reason = 'MAX_EXPOSURE_TIME'
    if reason is not None:
        watch = replace(watch,close_reason=reason,close_at_ns=at_ns)
    return watch, limit_watch_checkpoint(watch,plan,events,at_ns=at_ns)


def limit_watch_checkpoint(watch: LimitWatch, plan: PerpLimitEntryPlan, events: dict, *, at_ns: int) -> dict:
    receipt = events['receipt']
    action, reason = 'WAIT_ENTRY', 'AWAIT_ENTRY'
    if receipt is not None:
        if receipt['terminal']:
            if receipt['filled_size'] == '0':
                action, reason = 'SETTLE_EMPTY', 'ENTRY_TERMINAL_EMPTY'
            elif watch.close_reason is not None:
                action, reason = 'CLOSE', watch.close_reason
            else:
                action, reason = 'HOLD', 'POSITION_OPEN'
        elif events['cancel_claimed']:
            action, reason = 'WAIT_TERMINAL', 'CANCEL_RECORDED'
        elif receipt['status'] == 'OPEN':
            if watch.close_reason is not None:
                action, reason = 'CANCEL_REMAINDER', watch.close_reason
            elif receipt['fills']:
                action, reason = 'CANCEL_REMAINDER', 'PARTIAL_ENTRY'
            elif at_ns >= int(plan.result['expires_at_ns']):
                action, reason = 'CANCEL_REMAINDER', 'ENTRY_EXPIRED'
    checkpoint = {'schema':f'orbis.grvt.perp.limit.watch.v{plan.revision}','action':action,'reason':reason,
            'close_reason':watch.close_reason,'close_at_ns':watch.close_at_ns,
            'entry_quantity':receipt['filled_size'] if receipt is not None else '0',
            'feed':signal_checkpoint(watch.feed),
            'exit':perp_exit_checkpoint(watch.cursor) if watch.cursor is not None else None,
            'execution_authority':'NONE'}
    if plan.revision==2:
        checkpoint['quote'] = quote_input(watch.feed.last_quote).payload()
    return checkpoint
