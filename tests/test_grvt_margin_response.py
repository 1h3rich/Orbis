"""El DTO público de margen no usa el mismo sobre que ticker/instrument."""

import httpx
import pytest

from app.live_execution.grvt_margin_response import margin_rules_response


@pytest.mark.parametrize('payload,status', [
    ({'error':'unavailable'},200),
    ({'code':0,'instrument':'BTC_USDT_Perp','max_position_size':'10','risk_brackets':[]},200),
    ({'result':{},'instrument':'BTC_USDT_Perp','max_position_size':'10','risk_brackets':[]},200),
    ({'result':{'error':'nested'}},200),
    ({'result':{'result':{}}},200),
    ([],200),
    ({'instrument':'BTC_USDT_Perp','max_position_size':'10','risk_brackets':[]},503),
])
def test_errors_and_ambiguous_margin_responses_never_become_rules(payload,status):
    with pytest.raises(ValueError):
        margin_rules_response(httpx.Response(status,json=payload))
