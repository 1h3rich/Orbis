"""Fit exit parameters in development, then replay only the winner in reserve."""
from __future__ import annotations

from decimal import Decimal
from itertools import product
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.lab.engine import LabError, number, validate_strategy
from app.lab.optimization import MIN_TRAIN_TRADES
from app.lab.perp_book_types import exact
from app.lab.perp_dataset import PerpDataset
from app.lab.perp_profile import ExecutionProfile, bind_profile, read_profile, validate_partial_targets
from app.lab.perp_simulation import simulate_perpetual, simulation_engine
from app.lab.perp_validation import PerpValidationPlan, window_specs
from app.lab.store import content_hash


SCHEMA = 'orbis.lab.perp.optimization.v1'
ENGINE = 'perp-stop-target-grid-1.0'
MIN_DEVELOPMENT_TRADES = MIN_TRAIN_TRADES
MAX_WORK = 30_000


def _canonical(value: Decimal) -> str:
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


class PerpOptimizationPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    stop_losses: tuple[str, ...] = Field(min_length=1, max_length=4)
    take_profits: tuple[str, ...] = Field(min_length=1, max_length=4)
    train_fraction: str = '0.7'

    @field_validator('stop_losses', 'take_profits', mode='before')
    @classmethod
    def parse_json_grid(cls, values):
        return tuple(values) if isinstance(values, list) else values

    @field_validator('stop_losses', 'take_profits')
    @classmethod
    def canonical_grid(cls, values: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        rates = [number(value, positive=True) for value in values]
        maximum = 1 if info.field_name == 'stop_losses' else 10
        if any(rate >= maximum for rate in rates):
            raise ValueError(f'Cada porcentaje debe ser inferior a {maximum * 100} %')
        if len(set(rates)) != len(rates):
            raise ValueError('La cuadrícula contiene porcentajes duplicados')
        return tuple(_canonical(rate) for rate in sorted(rates))

    @field_validator('train_fraction')
    @classmethod
    def valid_fraction(cls, value: str) -> str:
        fraction = number(value)
        if not Decimal('0.5') <= fraction <= Decimal('0.8'):
            raise ValueError('El desarrollo debe estar entre el 50 % y el 80 %')
        return _canonical(fraction)

    def payload(self) -> dict:
        return self.model_dump(mode='json')


def _score(run: dict, dataset: PerpDataset, spec: dict) -> dict:
    metrics = run['metrics']
    return {
        'from_t': dataset.trade.bars[spec['entry_start_index']].t,
        'to_t': dataset.trade.bars[spec['end_index'] - 1].t,
        'bars': spec['end_index'] - spec['entry_start_index'],
        'status': run['status'], 'halt_reason': run['halt_reason'],
        'metrics': metrics, 'warnings': run['warnings'], 'result_hash': content_hash(run),
    }


def _exclusions(score: dict) -> list[str]:
    metrics = score['metrics']
    return [reason for failed, reason in (
        (score['status'] != 'COMPLETE' or metrics['open_position'], 'INCOMPLETE'),
        (metrics['liquidated'], 'LIQUIDATED'),
        (metrics['trades'] < MIN_DEVELOPMENT_TRADES, 'INSUFFICIENT_TRADES'),
    ) if failed]


def variant_inputs(profile: ExecutionProfile, definition: dict,
                   stop: str, target: str) -> tuple[ExecutionProfile, dict]:
    if definition['direction'] == 'SHORT' and number(target) >= 1:
        raise LabError('El objetivo SHORT debe ser inferior al 100 %')
    variant = validate_strategy({**definition, 'stop_loss': stop, 'take_profit': target})
    validate_partial_targets(profile, variant)
    digest = content_hash(variant)
    return read_profile({
        **profile.payload(), 'strategy_hash': digest,
        'strategy_version_id': str(uuid5(UUID(profile.strategy_version_id), ENGINE + digest)),
    }), variant


def _rank(candidate: dict) -> tuple[Decimal, ...]:
    metrics = candidate['development']['metrics']
    return (-Decimal(metrics['net_equity_change']), Decimal(metrics['max_drawdown_pct']),
            Decimal(candidate['stop_loss']), Decimal(candidate['take_profit']))


@exact
def replay_perp_optimization(dataset: PerpDataset, profile: ExecutionProfile,
                             definition: dict, plan: PerpOptimizationPlan) -> tuple[dict, tuple[dict, dict] | None]:
    definition, _ = bind_profile(profile, definition, dataset)
    plan = PerpOptimizationPlan.model_validate(plan.payload())
    total = len(plan.stop_losses) * len(plan.take_profits)
    # The engine validates and hashes the full archive even for a single window.
    if (total + 1) * len(dataset.trade.bars) > MAX_WORK:
        raise LabError('Reduce las combinaciones o el histórico para ejecutar la cuadrícula')
    development, reserve = window_specs(dataset, profile, PerpValidationPlan(
        method='CHRONOLOGICAL_HOLDOUT', train_fraction=plan.train_fraction))

    variants = [(stop, target, *variant_inputs(profile, definition, stop, target))
                for stop, target in product(plan.stop_losses, plan.take_profits)]
    candidates = []
    winning: tuple[dict, dict] | None = None
    for index, (stop, target, variant_profile, variant) in enumerate(variants):
        run = simulate_perpetual(dataset, variant_profile, variant,
            entry_start_index=development['entry_start_index'], end_index=development['end_index'])
        score = _score(run, dataset, development)
        candidate = {'index': index, 'stop_loss': stop, 'take_profit': target,
                     'strategy_hash': variant_profile.strategy_hash, 'development': score,
                     'exclusions': _exclusions(score)}
        candidates.append(candidate)
        if not candidate['exclusions'] and (winning is None or _rank(candidate) < _rank(winning[0])):
            winning = candidate, run

    out_of_sample = executions = None
    if winning is not None:
        selected, selected_run = winning
        _, _, variant_profile, variant = variants[selected['index']]
        run = simulate_perpetual(dataset, variant_profile, variant,
            entry_start_index=reserve['entry_start_index'], end_index=reserve['end_index'])
        out_of_sample = _score(run, dataset, reserve)
        executions = (selected_run, run)

    return {
        'schema': SCHEMA, 'engine': ENGINE, 'simulation_engine': simulation_engine(profile),
        'dataset_hash': content_hash(dataset.payload()), 'profile_hash': content_hash(profile.payload()),
        'strategy_hash': profile.strategy_hash, 'plan': plan.payload(),
        'candidates': candidates, 'selected_index': winning[0]['index'] if winning is not None else None,
        'out_of_sample': out_of_sample, 'minimum_development_trades': MIN_DEVELOPMENT_TRADES,
        'execution_authority': 'NONE',
    }, executions


def calculate_perp_optimization(dataset: PerpDataset, profile: ExecutionProfile,
                                definition: dict, plan: PerpOptimizationPlan) -> dict:
    return replay_perp_optimization(dataset, profile, definition, plan)[0]
