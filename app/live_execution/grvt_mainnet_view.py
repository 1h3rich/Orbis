"""Presentación de un grupo ya verificado, sin consultas ni autoridad."""

from decimal import Decimal, localcontext

from app.live_execution.grvt_mainnet_preflight import canonical


TERMINAL_ORDER_STATES = frozenset({'FILLED', 'CANCELLED', 'REJECTED'})
_RECOVERABLE_REASONS = {'PARTIAL_EXIT', 'RESIDUAL_AFTER_TERMINAL_EXITS', 'EXIT_EXPIRED_LOCALLY'}


def remaining_after_iocs(state: dict | None, reductions: list[dict]) -> str | None:
    """Only a size inferred from orders. The account audit establishes settlement."""
    if not state or not reductions or set(state['review_reasons']) - _RECOVERABLE_REASONS:
        return None
    orders = [*state['orders'], *(item['state'] for item in reductions)]
    if any(order is None or order['status'] not in TERMINAL_ORDER_STATES for order in orders):
        return None
    with localcontext() as context:
        context.prec = 100
        remaining = Decimal(orders[0]['traded_size']) - sum(Decimal(order['traded_size']) for order in orders[1:])
        return canonical(remaining) if remaining >= 0 else None


def mainnet_group_view(value:dict,job:dict)->dict:
    parent=value['parent_client_order_id'];draft=value['claim']['draft']
    last=value['last_observation']['state'] if value['last_observation'] else None
    recovery=value['recovery'];receipt=value['reconciliation']
    reductions=[{'client_order_id':item['claim']['client_order_id'],'size':item['claim']['size'],
        'limit_price':item['claim']['price'],'state':item['state']} for item in recovery['reductions']] if recovery else []
    return {'network':'MAINNET','authorization':False,'parent_client_order_id':parent,'sub_account_id':value['sub_account_id'],
            'instrument':draft['instrument'],'direction':draft['direction'],'size':draft['size'],'entry_price':draft['entry_price'],
            'stop_loss':draft['stop_loss'],'take_profit':draft['take_profit'],'status':value['status'],
            'entry_cap':draft['entry_cap'],'exit_cap':draft['exit_cap'],
            'exit_expiration_ns':str(value['claim']['issued_at_ns']+draft['exit_seconds']*1_000_000_000),
            'state':last,'financial_audit_count':len(value['audits']),'result':receipt['result'] if receipt else None,
            'reductions':reductions,
            'recovery':{'requested':True,'deadline_ns':str(recovery['request']['deadline_ns']),
                'attempts':len(recovery['reductions']),'max_attempts':recovery['request']['max_attempts'],
                'exit_cap':recovery['request']['exit_cap'],
                'remaining_size_from_orders':remaining_after_iocs(last,reductions)} if recovery else None,
            'job':job}
