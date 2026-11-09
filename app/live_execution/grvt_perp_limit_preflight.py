"""Read-only GRVT preflight for a LIMIT plan and its replayed causal decision."""
from collections.abc import Callable

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient
from app.live_execution.grvt_perp_limit_plan import (
    LimitSignalSeed, LimitTerms, PerpLimitEntryPlan, PerpLimitPlanInput,
    calculate_limit_plan, limit_plan_from_signal, read_limit_terms,
)
from app.live_execution.grvt_perp_preflight import SOURCES, collect_perp_market, verify_perp_market


SCHEMA = 'orbis.grvt.mainnet.perp.limit.preflight.v1'


def _market(captured: dict, terms: LimitTerms, now_ns: int):
    profile = terms.research.profile
    return verify_perp_market(captured, instrument=terms.instrument, budget=terms.budget,
                              margin_mode=profile.margin_mode, leverage_value=profile.leverage, now_ns=now_ns)


def collect_limit_preflight(client: GrvtMainnetClient, terms: LimitTerms,
                            seed: LimitSignalSeed, records: list[dict], *, entry_deadline_ns: str,
                            public_post: Callable, clock_ns: Callable) -> dict:
    try:
        terms = read_limit_terms(terms.payload())
        if (not isinstance(client, GrvtMainnetClient) or client.network != 'MAINNET'
                or client.sub_account_id != terms.budget.sub_account_id):
            raise ValueError('Subcuenta LIMIT REAL fuera de ámbito')
        captured = collect_perp_market(client, terms.instrument, terms.budget.sub_account_id,
                                       public_post=public_post, clock_ns=clock_ns)
        market = _market(captured, terms, clock_ns())
        plan = limit_plan_from_signal(terms, market, seed, records, entry_deadline_ns=entry_deadline_ns)
        proof = {'schema': SCHEMA, 'network': 'MAINNET', 'sources': dict(SOURCES),
                 'market': captured, 'plan': plan.payload(), 'execution_authority': 'NONE'}
        verify_limit_preflight(proof, terms, seed, records, entry_deadline_ns=entry_deadline_ns, now_ns=clock_ns())
        return proof
    except Exception:
        raise GrvtMainnetError('Preflight LIMIT REAL no verificable o vencido') from None


def verify_limit_preflight(proof: dict, terms: LimitTerms, seed: LimitSignalSeed,
                           records: list[dict], *, entry_deadline_ns: str, now_ns: int) -> PerpLimitEntryPlan:
    terms = read_limit_terms(terms.payload())
    if (type(proof) is not dict
            or set(proof) != {'schema', 'network', 'sources', 'market', 'plan', 'execution_authority'}
            or proof['schema'] != SCHEMA or proof['network'] != 'MAINNET'
            or proof['sources'] != SOURCES or proof['execution_authority'] != 'NONE'):
        raise ValueError('Formato de preflight LIMIT REAL incompatible')
    market = _market(proof['market'], terms, now_ns)
    plan = PerpLimitEntryPlan.model_validate(proof['plan'])
    expected = limit_plan_from_signal(terms, market, seed, records, entry_deadline_ns=entry_deadline_ns)
    rechecked = PerpLimitPlanInput.model_validate({**expected.source.payload(), 'checked_at_ns':str(now_ns)})
    if plan.payload() != expected.payload() or calculate_limit_plan(rechecked) != plan.result:
        raise ValueError('Plan LIMIT REAL distinto de los originales o vencido')
    return plan
