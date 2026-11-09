"""Fixed-profile chronological tests. No fitting, portfolio concatenation or IO.

Each child uses an independent account and the profile's simulation engine. Full child-result hashes
bind events as well as metrics without duplicating every journal in the report.
"""
from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.lab.engine import LabError, number
from app.lab.perp_book_types import exact
from app.lab.perp_dataset import PerpDataset, read_perp_dataset
from app.lab.perp_profile import ExecutionProfile, profile_version, read_profile
from app.lab.perp_simulation import simulate_perpetual, simulation_engine
from app.lab.store import content_hash


VALIDATION_ENGINE = 'perp-validation-1.0'
LIMIT_VALIDATION_ENGINE = 'perp-validation-2.0'
PARTIAL_VALIDATION_ENGINE = 'perp-validation-3.0'
Method = Literal['CHRONOLOGICAL_HOLDOUT', 'ROLLING_FIXED_VERSION', 'PATH_SENSITIVITY']


def validation_contract(profile: ExecutionProfile) -> tuple[int, str, str]:
    version = profile_version(profile)
    engines = (VALIDATION_ENGINE,LIMIT_VALIDATION_ENGINE,PARTIAL_VALIDATION_ENGINE)
    return version, engines[version-1], simulation_engine(profile)


class PerpValidationPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    method: Method
    train_fraction: str | None = None
    folds: int | None = Field(default=None, ge=2, le=5)

    @model_validator(mode='after')
    def coherent(self) -> Self:
        if self.method == 'CHRONOLOGICAL_HOLDOUT':
            if self.train_fraction is None or self.folds is not None:
                raise ValueError('La reserva requiere fracción y no ventanas sucesivas')
            if not number('0.5') <= number(self.train_fraction) <= number('0.8'):
                raise ValueError('La fracción de desarrollo debe estar entre 0.5 y 0.8')
        elif self.method == 'ROLLING_FIXED_VERSION':
            if self.folds is None or self.train_fraction is not None:
                raise ValueError('Las ventanas requieren número y no fracción')
        elif self.train_fraction is not None or self.folds is not None:
            raise ValueError('La sensibilidad de recorridos no acepta corte ni ventanas')
        return self

    def payload(self) -> dict:
        return self.model_dump(mode='json')


@exact
def window_specs(dataset: PerpDataset, profile: ExecutionProfile,
                 plan: PerpValidationPlan) -> list[dict]:
    """Indices are [entry_start,end), on the original, immutable dataset."""
    plan = PerpValidationPlan.model_validate(plan.payload())
    count = len(dataset.trade.bars)
    windows: list[dict] = []

    def add(role: str, start: int, end: int, *, fold: int | None = None,
            selected: ExecutionProfile = profile) -> None:
        windows.append({'index':len(windows), 'role':role, 'fold':fold,
                        'entry_start_index':start, 'end_index':end,
                        'profile':selected.payload()})

    if plan.method == 'CHRONOLOGICAL_HOLDOUT':
        split = int(number(str(count)) * number(plan.train_fraction))
        if min(split, count-split) < 30:
            raise LabError('Cada tramo requiere al menos 30 velas cerradas')
        add('DEVELOPMENT', 0, split)
        add('RESERVE', split, count)
    elif plan.method == 'ROLLING_FIXED_VERSION':
        width = count // (plan.folds+1)
        if width < 30:
            raise LabError('Cada ventana requiere al menos 30 velas cerradas')
        for index in range(plan.folds):
            split = width*(index+1)
            end = width*(index+2) if index < plan.folds-1 else count
            add('DEVELOPMENT', 0, split, fold=index+1)
            add('RESERVE', split, end, fold=index+1)
    else:
        for trade in ('O_H_L_C','O_L_H_C'):
            for mark in ('O_H_L_C','O_L_H_C'):
                configured = read_profile({**profile.payload(),
                    'trade_path':trade, 'mark_path':mark})
                add('SCENARIO', 0, count, selected=configured)
    return windows


@exact
def calculate_perp_validation(dataset: PerpDataset, profile: ExecutionProfile,
                              definition: dict, plan: PerpValidationPlan) -> dict:
    dataset = read_perp_dataset(dataset.payload())
    profile = read_profile(profile.payload())
    plan = PerpValidationPlan.model_validate(plan.payload())
    windows = []
    for spec in window_specs(dataset,profile,plan):
        selected = read_profile(spec['profile'])
        run = simulate_perpetual(dataset,selected,definition,
            entry_start_index=spec['entry_start_index'],end_index=spec['end_index'])
        windows.append({**spec, 'from_t':dataset.trade.bars[spec['entry_start_index']].t,
            'to_t':dataset.trade.bars[spec['end_index']-1].t,
            'bars':spec['end_index']-spec['entry_start_index'],
            'result_hash':content_hash(run), 'status':run['status'],
            'metrics':run['metrics'], 'warnings':run['warnings']})
    reserves = [window for window in windows if window['role']=='RESERVE']
    warnings = ['EXPERIMENTAL_CURRENT_RULES', 'INDEPENDENT_ACCOUNTS_NOT_CONTINUOUS_EQUITY',
                'ARTIFICIAL_WINDOW_END_CLOSE', 'FULL_PREFIX_ONLY_FOR_CAUSAL_INDICATORS',
                'NO_EXECUTION_AUTHORITY']
    if reserves:
        warnings.extend(['FIXED_PARAMETERS_NO_REFITTING', 'RESERVE_CAN_BE_CONTAMINATED_BY_PRIOR_RESEARCH',
                         'UNEQUAL_WINDOW_RETURNS_NOT_DIRECTLY_COMPARABLE'])
    else:
        warnings.extend(['PATH_SCENARIOS_ARE_NOT_OUT_OF_SAMPLE', 'SCENARIOS_DO_NOT_BOUND_ALL_REAL_PATHS'])
    if any('SMALL_SAMPLE' in window['warnings'] for window in windows):
        warnings.append('SMALL_SAMPLE')
    if any(window['status'] != 'COMPLETE' for window in windows):
        warnings.append('INCOMPLETE_WINDOW')
    version, engine, simulated = validation_contract(profile)
    if getattr(profile,'entry_order',None) is not None:
        warnings.extend(['LIMIT_FULL_FILL_TOUCH_HYPOTHESIS', 'LIMIT_ORDERS_CANCELLED_AT_WINDOW_END'])
    if version == 3:
        warnings.append('PARTIAL_TARGETS_EXPERIMENTAL_LAB_ONLY')
    return {'schema':f'orbis.lab.perp.validation.result.v{version}', 'engine':engine,
        'simulation_engine':simulated, 'mode':'EXPERIMENTAL_CURRENT_RULES',
        'plan':plan.payload(), 'dataset_hash':content_hash(dataset.payload()),
        'profile_hash':content_hash(profile.payload()), 'strategy_hash':profile.strategy_hash,
        'windows':windows, 'reserve_bars':sum(window['bars'] for window in reserves),
        'reserve_trades':sum(window['metrics']['trades'] for window in reserves),
        'warnings':warnings, 'execution_authority':'NONE'}
