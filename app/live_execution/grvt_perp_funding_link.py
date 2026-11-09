"""Funding-to-execution association for the new perpetual claim version.

GRVT documents a common numeric component before '-' for fills from one taker
execution. Only claim v3 accepts this explicit group alias; old claims retain
their exact trade_id association. This does not accept external transfer or
liquidation funding as belonging to the bot.
"""
from __future__ import annotations

import re


_TRADE = re.compile(r'([1-9][0-9]{0,39})(?:-([0-9]{1,20}))?')


def funding_trade_times(claim: dict, fills: list[dict]) -> dict[str, int]:
    times = {fill['trade_id']:fill['event_time_ns'] for fill in fills}
    if claim.get('schema') not in ('orbis.grvt.mainnet.bracket.claim.v3','orbis.grvt.mainnet.perp.limit.claim.v1'):
        return times
    for fill in fills:
        match = _TRADE.fullmatch(fill['trade_id'])
        if match is None:
            continue
        execution = match[1]
        at = fill['event_time_ns']
        if execution in times and times[execution] != at:
            raise ValueError('funding execution alias has contradictory trade times')
        times[execution] = at
    return times
