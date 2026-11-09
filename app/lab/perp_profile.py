"""Strict execution assumptions, separate from the published LAB definitions."""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from app.derivatives.paper_math import InstrumentSpec, MarginRules
from app.lab.engine import LabError, number, supports_engine_version, validate_strategy
from app.lab.perp_book_types import INTERNAL_BOOK_VERSION, Market, exact, money, value
from app.lab.perp_dataset import PerpDataset
from app.lab.store import content_hash

if TYPE_CHECKING:
    from app.lab.perp_rules import RuleObservation


PathKind = Literal['O_H_L_C', 'O_L_H_C']


class PerpProfile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, populate_by_name=True)
    schema_name: Literal['orbis.lab.perp.profile.v1'] = Field(alias='schema')
    strategy_version_id: str = Field(min_length=36, max_length=36)
    strategy_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    signal_engine: Literal['lab-1.16']
    account_engine: Literal['perp-account-0.1']
    mode: Literal['EXPERIMENTAL_CURRENT_RULES']
    margin_mode: Literal['CROSS','ISOLATED']
    leverage: str
    trade_path: PathKind
    mark_path: PathKind
    boundary_order: Literal['PREVIOUS_CLOSE_THEN_FUNDING_THEN_OPEN']
    execution: Literal['MARKET_WITH_ADVERSE_SLIPPAGE']
    sizing: Literal['CASH_ALLOCATION_INCLUDING_MARGIN_LOSS_BUFFER_AND_FEES']
    simultaneous_positions: Literal[1]
    liquidation_action: Literal['STOP_NEW_ENTRIES']
    end_action: Literal['CLOSE_AT_LAST_MODELED_TRADE']

    @field_validator('simultaneous_positions',mode='before')
    @classmethod
    def exact_count(cls, value):
        if type(value) is not int:
            raise ValueError('Número entero de posiciones requerido')
        return value

    @model_validator(mode='after')
    def valid(self) -> Self:
        if str(UUID(self.strategy_version_id)) != self.strategy_version_id:
            raise ValueError('Versión de estrategia no canónica')
        leverage = number(self.leverage, positive=True)
        if not 1 <= leverage <= 10000:
            raise ValueError('Apalancamiento fuera de rango')
        if (self.account_engine != INTERNAL_BOOK_VERSION
                or type(self.simultaneous_positions) is not int):
            raise ValueError('Versión/precisión de ejecución no disponible')
        return self

    def payload(self) -> dict:
        return self.model_dump(mode='json', by_alias=True)


class LimitEntry(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    reference: Literal['CLOSED_SIGNAL_TRADE_CLOSE'] = 'CLOSED_SIGNAL_TRADE_CLOSE'
    offset: str = Field(min_length=1, max_length=80)
    valid_bars: int = Field(ge=1, le=10000)

    @field_validator('offset')
    @classmethod
    def valid_offset(cls, raw: str) -> str:
        parsed = value(number(raw))
        if not 0 <= parsed < 1:
            raise ValueError('Distancia límite fuera de rango')
        return '0' if parsed.is_zero() else raw


class PerpLimitProfile(PerpProfile):
    schema_name: Literal['orbis.lab.perp.profile.v2'] = Field(alias='schema')
    execution: Literal['LIMIT_TOUCH_WITH_BOUND_AND_ADVERSE_SLIPPAGE']
    end_action: Literal['CANCEL_LIMIT_THEN_CLOSE_AT_LAST_MODELED_TRADE']
    entry_order: LimitEntry


class PartialTarget(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True,frozen=True)
    target: str = Field(min_length=1,max_length=80)
    fraction: str = Field(min_length=1,max_length=80)

    @field_validator('target','fraction')
    @classmethod
    def positive_fraction(cls, raw: str) -> str:
        if value(number(raw),positive=True) >= 1:
            raise ValueError('El objetivo y la parte deben ser menores que el 100 %')
        return raw


class PerpPartialProfile(PerpProfile):
    schema_name: Literal['orbis.lab.perp.profile.v3'] = Field(alias='schema')
    execution: Literal['MARKET_WITH_ADVERSE_SLIPPAGE','LIMIT_TOUCH_WITH_BOUND_AND_ADVERSE_SLIPPAGE']
    end_action: Literal['CLOSE_AT_LAST_MODELED_TRADE','CANCEL_LIMIT_THEN_CLOSE_AT_LAST_MODELED_TRADE']
    entry_order: LimitEntry | None = None
    partial_targets: list[PartialTarget] = Field(min_length=1,max_length=5)

    @model_validator(mode='after')
    @exact
    def verify_partial_plan(self) -> Self:
        limit = self.entry_order is not None
        if (limit != (self.execution=='LIMIT_TOUCH_WITH_BOUND_AND_ADVERSE_SLIPPAGE')
                or limit != (self.end_action=='CANCEL_LIMIT_THEN_CLOSE_AT_LAST_MODELED_TRADE')):
            raise ValueError('Perfil de entrada y cierre incompatible')
        targets = [number(row.target) for row in self.partial_targets]
        if targets!=sorted(set(targets)) or sum(number(row.fraction) for row in self.partial_targets)>=1:
            raise ValueError('Los objetivos deben subir y las partes deben dejar posición para el cierre final')
        return self


ExecutionProfile = PerpProfile | PerpLimitProfile | PerpPartialProfile
_PROFILE = TypeAdapter(Annotated[ExecutionProfile, Field(discriminator='schema_name')])


def read_profile(payload: dict) -> ExecutionProfile:
    return _PROFILE.validate_python(payload)


def profile_version(profile: ExecutionProfile) -> int:
    if isinstance(profile,PerpPartialProfile):
        return 3
    return 2 if isinstance(profile,PerpLimitProfile) else 1


def validate_partial_targets(profile: ExecutionProfile, definition: dict) -> None:
    if isinstance(profile,PerpPartialProfile) and number(profile.partial_targets[-1].target)>=number(definition['take_profit']):
        raise LabError('Cada objetivo parcial debe quedar antes del objetivo final de la estrategia')


@exact
def bind_profile(profile: ExecutionProfile, definition: dict, dataset: PerpDataset, *,
                 rule_observation: RuleObservation | None = None) -> tuple[dict, Market]:
    """No store reads or authority: caller still verifies version/dataset IDs."""
    normalized = validate_strategy(definition)
    if not supports_engine_version(profile.signal_engine,normalized):
        raise LabError('Evaluador de señales no compatible con esta definición')
    if content_hash(normalized) != profile.strategy_hash:
        raise LabError('El perfil no corresponde a esta definición de estrategia')
    validate_partial_targets(profile,normalized)
    capital = value(number(normalized['initial_capital']), positive=True)
    if money(capital) != capital:
        raise LabError('El capital supera los 18 decimales del modelo')
    if rule_observation is not None and rule_observation.symbol != dataset.symbol:
        raise LabError('La captura de reglas pertenece a otro mercado')
    source = dataset if rule_observation is None else rule_observation
    raw = source.specification.model_dump()
    spec = InstrumentSpec.from_payload(raw, dataset.instrument, int(raw['fetched_at_ns']))
    raw_rules = source.rules.model_dump()
    rules = MarginRules.from_payload(raw_rules, dataset.instrument, int(raw_rules['fetched_at_ns']))
    market = Market(dataset.instrument, profile.margin_mode, number(profile.leverage),
                    number(normalized['fee_rate']), spec, rules)
    return normalized, market
