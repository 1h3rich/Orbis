"""Bounded readback for orders whose original signed placement is archived."""

from collections.abc import Callable, Mapping
import re

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient, MainnetOrderNotFound


def find_order_history(
    client: GrvtMainnetClient, sub: str, instrument: str, *,
    windows: Mapping[str, tuple[int, int]], checked: Callable[[], int],
) -> dict[str, dict]:
    """Read every page in a bounded placement window to reject ambiguous IDs."""
    start_ns = min(start for start, _ in windows.values())
    end_ns = min(checked(), max(end for _, end in windows.values()))
    cursor = ''
    seen = {cursor}
    orders = {}
    for _ in range(4):
        checked()
        page = client.order_history_page(sub, instrument, start_ns=start_ns, end_ns=end_ns, cursor=cursor)
        checked()
        rows, following = page.get('result'), page.get('next')
        if (type(rows) is not list or len(rows) > 500 or type(following) is not str
                or len(following) > 1024 or not following.isascii() or any(ord(c) < 32 for c in following)
                or following and following in seen):
            raise GrvtMainnetError('Paginación del historial de órdenes MAINNET no verificable')
        for row in rows:
            metadata = row.get('metadata') if isinstance(row, dict) else None
            if not isinstance(metadata, dict) or not isinstance(metadata.get('client_order_id'), str):
                raise GrvtMainnetError('Orden del historial MAINNET sin identificador verificable')
            cid = metadata['client_order_id']
            if cid in windows:
                created = metadata.get('create_time')
                start, end = windows[cid]
                if (cid in orders or row.get('sub_account_id') != sub
                        or not isinstance(created, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', created)
                        or not start <= int(created) <= min(end, end_ns)):
                    raise GrvtMainnetError('Identidad de orden ambigua en el historial MAINNET')
                orders[cid] = row
        if not following:
            return orders
        seen.add(following)
        cursor = following
    raise GrvtMainnetError('Historial de órdenes MAINNET fuera de capacidad')


def lookup_recovery_orders(
    client: GrvtMainnetClient, sub: str, instrument: str, *,
    windows: Mapping[str, tuple[int, int]], checked: Callable[[], int],
) -> dict[str, dict]:
    """Use history only on a documented missing-order response, for terminal rows."""
    orders, missing = {}, {}
    for cid, window in windows.items():
        checked()
        try:
            orders[cid] = client.lookup_order_payload(sub, cid)
        except MainnetOrderNotFound:
            missing[cid] = window
        checked()
    if missing:
        historical = find_order_history(client, sub, instrument, windows=missing, checked=checked)
        if historical.keys() != missing.keys() or any(
            not isinstance(row.get('state'), dict)
            or row['state'].get('status') not in ('FILLED', 'CANCELLED', 'REJECTED')
            for row in historical.values()
        ):
            raise GrvtMainnetError('El historial MAINNET aún no acredita todas las órdenes terminales')
        orders.update(historical)
    return orders
