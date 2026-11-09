"""Estado de contratos GRVT compartido: no consulta una red ni acredita cuenta."""

from decimal import Decimal,localcontext

def native_recovery_reason(state: dict) -> str | None:
    """Recuperación compatible desde contratos; no acredita saldo ni exposición."""
    if (state['review_reasons']==['ORPHANED_EXITS']
            and all(Decimal(row['traded_size'])==0 for row in state['orders'])):
        return 'ORPHANED_EXITS'
    if (not state['needs_review'] and state['orders'][0]['status'] in ('CANCELLED','REJECTED')
            and all(row['status'] in ('FILLED','CANCELLED','REJECTED')
                and Decimal(row['traded_size'])==0 for row in state['orders'])):
        return 'ENTRY_UNFILLED'
    if (state['entry_fully_filled'] and 'EXIT_EXPIRED_LOCALLY' in state['review_reasons']
            and not set(state['review_reasons'])-{'PARTIAL_EXIT','RESIDUAL_AFTER_TERMINAL_EXITS','EXIT_EXPIRED_LOCALLY'}
            and sum(Decimal(row['traded_size'])>0 for row in state['orders'][1:])<=1):
        return 'EXIT_PROTECTION_EXPIRED'
    if (not state['needs_review'] and state['entry_fully_filled']
            and Decimal(state['residual_size_from_orders'])==0
            and any(row['status'] in ('OPEN','PENDING') for row in state['orders'][1:])):
        return 'NATIVE_SIBLING_ACTIVE'
    if state['entry_fully_filled'] and state['review_reasons']==['RESIDUAL_AFTER_TERMINAL_EXITS']:
        return 'NATIVE_PROTECTION_ENDED'
    return None


def native_partial_role(state: dict) -> str | None:
    """Estado de contratos compatible con un parcial; no prueba saldo ni fills.

    Las incidencias restantes y la ejecución simultánea de ambas salidas
    requieren revisión. El residuo sólo puede operarse tras conciliación.
    """
    if (not state['entry_fully_filled'] or 'PARTIAL_EXIT' not in state['review_reasons']
            or set(state['review_reasons'])-{'PARTIAL_EXIT','RESIDUAL_AFTER_TERMINAL_EXITS'}):
        return None
    positive=[row['role'] for row in state['orders'][1:] if Decimal(row['traded_size'])>0]
    return positive[0] if len(positive)==1 else None


def summarise_bracket_state(orders: list[dict],*,observed_at_ns: int,exit_expiration_ns: int) -> dict:
    """Deriva avisos de estados previamente validados; no consulta ni autoriza."""
    with localcontext() as ctx:
        ctx.prec = 100
        filled = Decimal(orders[0]['traded_size'])
        closed = sum((Decimal(row['traded_size']) for row in orders[1:]),Decimal(0))
        residual = filled-closed
        partial = Decimal(0) < filled < Decimal(orders[0]['size'])
        full = orders[0]['status'] == 'FILLED' and filled == Decimal(orders[0]['size'])
        reasons = []
        if partial: reasons.append('FOK_PARTIAL_VIOLATION')
        if orders[0]['status'] in ('OPEN','PENDING'): reasons.append('ENTRY_NOT_TERMINAL')
        if closed > filled: reasons.append('EXITS_EXCEED_ENTRY')
        if any(row['status'] == 'FILLED' and Decimal(row['traded_size']) != Decimal(row['size'])
               or row['status'] == 'REJECTED' and Decimal(row['traded_size']) != 0 for row in orders):
            reasons.append('FILL_STATUS_MISMATCH')
        if filled and orders[0]['status'] != 'FILLED': reasons.append('FOK_STATUS_MISMATCH')
        if Decimal(0) < closed < filled: reasons.append('PARTIAL_EXIT')
        if residual > 0 and all(row['status'] in ('FILLED','CANCELLED','REJECTED') for row in orders[1:]):
            reasons.append('RESIDUAL_AFTER_TERMINAL_EXITS')
        if residual > 0 and observed_at_ns >= exit_expiration_ns: reasons.append('EXIT_EXPIRED_LOCALLY')
        if filled == 0 and orders[0]['status'] in ('CANCELLED','REJECTED') and any(
                row['status'] in ('PENDING','OPEN') for row in orders[1:]): reasons.append('ORPHANED_EXITS')
    return {'scope':'ORDER_STATE_ONLY','orders':orders,'observed_at_ns':observed_at_ns,
        'activation_condition':'PARENT_FULL_FILL_REQUIRED','entry_fully_filled':full,
        'fok_partial_violation':partial,'residual_size_from_orders':format(residual,'f'),
        'account_position_verified':False,'needs_review':bool(reasons),'review_reasons':reasons,
        'protection_guaranteed':False,'can_submit':False}
