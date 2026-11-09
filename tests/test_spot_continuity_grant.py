"""Concesión explícita: duración y límites fijos, no un temporizador renovable."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4
import pytest

from app.live_execution.spot_bot_permission import make_continuity_grant, validate_continuity_grant
from tests.test_mexc_execution_state import setup


def test_grant_fixes_scope_and_end_without_changing_daily_policy(tmp_path):
    vault,policy,account,_,clock = setup(tmp_path)
    draft = replace(policy,policy_id=str(uuid4()),enabled=False,reconciliation_anchor_id=None,
                    allow_risk_reducing_exits=False,expires_at=clock.value+timedelta(days=7))
    scope = {"bot_id":str(uuid4()),"account_id":account,"account_ref":policy.account_ref,
        "source":{"version_id":policy.strategy_version_id,"symbol":"BTC/USDT"},
        "entry_budget_quote":"20","dca_limits":None,"dca_initial_reference":None,
        "dca_start_candle_t":None,"ownership_baseline":"0"*64}
    at,until = clock.value,clock.value+timedelta(days=3)
    grant = make_continuity_grant(policy,draft,scope,str(uuid4()),now=at,until=until)
    assert grant["protocol"] == "CONTINUOUS_V1" and grant["expires_at"] == until.isoformat()
    assert grant["scope"] == scope and policy.expires_at < until
    validate_continuity_grant(grant,policy,draft,scope)
    scope["entry_budget_quote"] = "21"
    assert grant["scope"]["entry_budget_quote"] == "20"
    vault.lock()


@pytest.mark.parametrize("change",["over_30_days","over_budget","scope","risk","missing_exit_permission"])
def test_grant_rejects_expansion_of_reviewed_scope_or_time(tmp_path,change):
    vault,policy,account,_,clock = setup(tmp_path)
    draft = replace(policy,policy_id=str(uuid4()),enabled=False,reconciliation_anchor_id=None,
                    allow_risk_reducing_exits=False,expires_at=clock.value+timedelta(days=90))
    scope = {"bot_id":str(uuid4()),"account_id":account,"account_ref":policy.account_ref,
        "source":{"version_id":policy.strategy_version_id,"symbol":"BTC/USDT"},
        "entry_budget_quote":"20","dca_limits":None,"dca_initial_reference":None,
        "dca_start_candle_t":None,"ownership_baseline":"0"*64}
    until = clock.value+timedelta(days=3)
    if change == "over_30_days": until = clock.value+timedelta(days=30,seconds=1)
    elif change == "over_budget": scope["entry_budget_quote"] = str(policy.max_order_quote+1)
    elif change == "scope": scope["source"]["symbol"] = "ETH/USDT"
    elif change == "risk": policy = replace(policy,max_order_quote=policy.max_order_quote+1)
    else: policy = replace(policy,allow_risk_reducing_exits=False)
    with pytest.raises(ValueError):
        make_continuity_grant(policy,draft,scope,str(uuid4()),now=clock.value,until=until)
    vault.lock()
