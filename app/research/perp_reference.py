"""New perpetual research identity. Existing price-only reference v1 is unchanged."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.lab.engine import INTERVALS
from app.lab.perp_profile import PerpLimitProfile, PerpPartialProfile, PerpProfile
from app.lab.store import content_hash


class PerpReserveWindow(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    from_t: int=Field(gt=0)
    to_t: int=Field(gt=0)
    bars: int=Field(ge=30,le=5000)
    result_hash: str=Field(pattern=r'^[0-9a-f]{64}$')


class PerpReferencePayload(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True,populate_by_name=True)
    schema_name: Literal['orbis.research.perp.reference.v1']=Field(alias='schema')
    family: Literal['PERPETUAL']
    artifact_id: str
    artifact_hash: str=Field(pattern=r'^[0-9a-f]{64}$')
    created_at: str
    dataset_id: str
    dataset_hash: str=Field(pattern=r'^[0-9a-f]{64}$')
    profile_id: str
    profile_hash: str=Field(pattern=r'^[0-9a-f]{64}$')
    profile: PerpProfile
    symbol: str=Field(min_length=3,max_length=100)
    timeframe: str
    dataset_from_t: int=Field(gt=0)
    dataset_to_t: int=Field(gt=0)
    method: Literal['CHRONOLOGICAL_HOLDOUT','ROLLING_FIXED_VERSION']
    validation_engine: Literal['perp-validation-1.0']
    simulation_engine: Literal['perp-ohlc-1.0']
    mode: Literal['EXPERIMENTAL_CURRENT_RULES']
    windows: list[PerpReserveWindow]=Field(min_length=1,max_length=5)
    sample_bars: int=Field(ge=30,le=5000)
    warnings: list[str]
    execution_authority: Literal['NONE']

    @model_validator(mode='after')
    def coherent(self) -> Self:
        for identifier in (self.artifact_id,self.dataset_id,self.profile_id):
            if str(UUID(identifier))!=identifier:raise ValueError('Identificador no canónico')
        if datetime.fromisoformat(self.created_at).utcoffset() is None:
            raise ValueError('Fecha sin zona horaria')
        if content_hash(self.profile.payload())!=self.profile_hash:
            raise ValueError('Perfil no coincide con su huella')
        step=INTERVALS.get(self.timeframe)
        if (step is None or self.dataset_to_t<self.dataset_from_t
                or (self.method=='CHRONOLOGICAL_HOLDOUT' and len(self.windows)!=1)
                or (self.method=='ROLLING_FIXED_VERSION' and len(self.windows)<2)):
            raise ValueError('Identidad de validación incompatible')
        previous=None
        for window in self.windows:
            if (not self.dataset_from_t<=window.from_t<=window.to_t<=self.dataset_to_t
                    or window.to_t-window.from_t!=(window.bars-1)*step
                    or previous is not None and window.from_t<=previous):
                raise ValueError('Ventanas de reserva incompatibles')
            previous=window.to_t
        if self.sample_bars!=sum(window.bars for window in self.windows):
            raise ValueError('Muestra incompatible')
        return self

    def payload(self) -> dict:
        return self.model_dump(mode='json',by_alias=True,exclude={'sha256'})


class PerpResearchReference(PerpReferencePayload):
    sha256: str=Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def digest_matches(self) -> Self:
        if content_hash(self.payload())!=self.sha256:
            raise ValueError('Huella de referencia perpetua alterada')
        return self


class PerpLimitReferencePayload(PerpReferencePayload):
    schema_name: Literal['orbis.research.perp.reference.v2'] = Field(alias='schema')
    profile: PerpLimitProfile
    validation_engine: Literal['perp-validation-2.0']
    simulation_engine: Literal['perp-ohlc-2.0']


class PerpLimitResearchReference(PerpLimitReferencePayload):
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def digest_matches(self) -> Self:
        if content_hash(self.payload()) != self.sha256:
            raise ValueError('Huella de referencia perpetua alterada')
        return self


class PerpPartialReferencePayload(PerpReferencePayload):
    schema_name: Literal['orbis.research.perp.reference.v3'] = Field(alias='schema')
    profile: PerpPartialProfile
    validation_engine: Literal['perp-validation-3.0']
    simulation_engine: Literal['perp-ohlc-3.0']


class PerpPartialResearchReference(PerpPartialReferencePayload):
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def digest_matches(self) -> Self:
        if content_hash(self.payload()) != self.sha256:
            raise ValueError('Huella de referencia perpetua alterada')
        return self


ObservedResearchReference = Annotated[PerpResearchReference | PerpLimitResearchReference | PerpPartialResearchReference,
                                     Field(discriminator='schema_name')]
OBSERVED_REFERENCE = TypeAdapter(ObservedResearchReference)


def reference_from_verified_validation(saved: dict) -> ObservedResearchReference:
    result=saved['payload']['result']
    reserves=[window for window in result['windows'] if window['role']=='RESERVE']
    # A sensitivity report is useful, but it never becomes out-of-sample proof.
    if not reserves:
        raise ValueError('La sensibilidad de recorridos no contiene una reserva fuera de muestra')
    if isinstance(saved['profile'], PerpPartialProfile):
        payload_type, reference_type = PerpPartialReferencePayload, PerpPartialResearchReference
        schema = 'orbis.research.perp.reference.v3'
    elif isinstance(saved['profile'], PerpLimitProfile):
        payload_type, reference_type = PerpLimitReferencePayload, PerpLimitResearchReference
        schema = 'orbis.research.perp.reference.v2'
    else:
        payload_type, reference_type = PerpReferencePayload, PerpResearchReference
        schema = 'orbis.research.perp.reference.v1'
    payload=payload_type(schema_name=schema,
        family='PERPETUAL',artifact_id=saved['id'],artifact_hash=saved['hash'],created_at=saved['created_at'],
        dataset_id=saved['dataset_id'],dataset_hash=result['dataset_hash'],profile_id=saved['profile_id'],
        profile_hash=result['profile_hash'],profile=saved['profile'],symbol=saved['dataset'].symbol,
        timeframe=saved['dataset'].timeframe,dataset_from_t=saved['dataset'].start,
        dataset_to_t=saved['dataset'].trade.bars[-1].t,method=saved['plan'].method,
        validation_engine=result['engine'],simulation_engine=result['simulation_engine'],mode=result['mode'],
        windows=[PerpReserveWindow(**{key:window[key] for key in ('from_t','to_t','bars','result_hash')}) for window in reserves],
        sample_bars=result['reserve_bars'],warnings=result['warnings'],execution_authority='NONE')
    return reference_type.model_validate({**payload.payload(),'sha256':content_hash(payload.payload())})
