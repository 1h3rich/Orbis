"""Typed, replayed research identity. A reference never grants execution rights."""

from __future__ import annotations

from datetime import datetime
from decimal import Context, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, model_validator

from app.automation.store import AutomationStore
from app.automation.verification import verify_dca_validation
from app.lab.engine import INTERVALS, LabError
from app.lab.experiment_verification import verify_lab_experiment
from app.lab.store import LabStore, content_hash


Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Timestamp = Annotated[StrictInt, Field(gt=0, le=253402300799)]


class ResearchReferenceError(ValueError):
    """The artifact or its transported identity is not reproducible/current."""


class EvaluationWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    from_t: Timestamp
    to_t: Timestamp
    bars: Annotated[StrictInt, Field(gt=0, le=100000)]


class ReferencePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True)
    schema_name: Literal['orbis.research.reference.v1'] = Field(alias='schema')
    family: Literal['STRATEGY', 'DCA']
    artifact_id: UUID
    artifact_digest: Digest
    created_at: datetime
    definition_id: UUID
    version_id: UUID
    definition_hash: Digest
    direction: Literal['LONG', 'SHORT'] | None
    dataset_id: UUID
    dataset_hash: Digest
    symbol: Annotated[str, Field(min_length=3, max_length=120)]
    timeframe: str
    source: Annotated[str, Field(min_length=1, max_length=200)]
    public_data: StrictBool
    dataset_from_t: Timestamp
    dataset_to_t: Timestamp
    method: Literal['CHRONOLOGICAL_HOLDOUT', 'ROLLING_FIXED_VERSION', 'DCA_CHRONOLOGICAL_HOLDOUT']
    validation_engine: str
    simulation_engine: str
    windows: tuple[EvaluationWindow, ...]
    sample_bars: Annotated[StrictInt, Field(gt=0, le=100000)]
    warnings: tuple[str, ...]
    execution_authority: Literal['NONE']

    @model_validator(mode='after')
    def coherent_identity(self) -> ReferencePayload:
        step = INTERVALS.get(self.timeframe)
        if (step is None or not self.windows or self.created_at.utcoffset() is None
                or self.dataset_to_t < self.dataset_from_t
                or (self.family == 'DCA') != (self.method == 'DCA_CHRONOLOGICAL_HOLDOUT')
                or (self.family == 'DCA') != (self.direction is None)):
            raise ValueError('Identidad de investigación incompatible')
        previous = None
        for window in self.windows:
            if (not self.dataset_from_t <= window.from_t <= window.to_t <= self.dataset_to_t
                    or window.to_t - window.from_t != (window.bars - 1) * step
                    or previous is not None and window.from_t <= previous):
                raise ValueError('Ventanas de investigación incompatibles')
            previous = window.to_t
        if sum(window.bars for window in self.windows) != self.sample_bars:
            raise ValueError('Tamaño de reserva incompatible')
        if self.method != 'ROLLING_FIXED_VERSION' and len(self.windows) != 1:
            raise ValueError('La reserva simple requiere una ventana')
        return self

    def payload(self) -> dict:
        return self.model_dump(mode='json', by_alias=True, exclude={'sha256'})


class ResearchReference(ReferencePayload):
    sha256: Digest

    @model_validator(mode='after')
    def verified_digest(self) -> ResearchReference:
        if content_hash(self.payload()) != self.sha256:
            raise ValueError('Huella de referencia alterada')
        return self

    def exported(self) -> dict:
        return self.model_dump(mode='json', by_alias=True)


def _make_reference(saved: dict, dataset: dict, version: dict,
                    family: Literal['STRATEGY', 'DCA']) -> ResearchReference:
    if saved['method'] == 'ROLLING_FIXED_VERSION':
        windows = [{'from_t': row['test_start_t'], 'to_t': row['test_end_t'],
                    'bars': row['test_bars']} for row in saved['windows']]
    else:
        windows = [{'from_t': saved['test_start_t'], 'to_t': saved['test_end_t'], 'bars': saved['test_bars']}]
    payload = {'schema': 'orbis.research.reference.v1', 'family': family,
        'artifact_id': saved['id'], 'artifact_digest': content_hash(saved),
        'created_at': datetime.fromisoformat(saved['created_at']).isoformat().replace('+00:00', 'Z'),
        'definition_id': version['strategy_id' if family == 'STRATEGY' else 'rule_id'],
        'version_id': saved['version_id'], 'definition_hash': saved['strategy_hash' if family == 'STRATEGY' else 'definition_hash'],
        'direction': version['definition']['direction'] if family == 'STRATEGY' else None,
        'dataset_id': dataset['id'], 'dataset_hash': dataset['hash'], 'symbol': dataset['symbol'],
        'timeframe': dataset['timeframe'], 'source': dataset['source'], 'public_data': dataset['verified_public'],
        'dataset_from_t': dataset['start_t'], 'dataset_to_t': dataset['end_t'],
        'method': saved['method'], 'validation_engine': saved['engine_version'],
        'simulation_engine': saved['lab_engine_version' if family == 'STRATEGY' else 'dca_engine_version'],
        'windows': windows, 'sample_bars': sum(row['bars'] for row in windows),
        'warnings': saved['warnings'], 'execution_authority': 'NONE'}
    canonical = ReferencePayload.model_validate(payload).model_dump(mode='json', by_alias=True)
    return ResearchReference.model_validate({**canonical, 'sha256': content_hash(canonical)})


def _strategy_reference(lab: LabStore, artifact_id: str) -> ResearchReference:
    """A fixed-version validation; optimizer winners are deliberately separate."""
    try:
        saved = lab.validation(str(artifact_id))
        checked = verify_lab_experiment(lab, saved, 'validation')
        if not checked['verified']:
            raise ResearchReferenceError(checked['reason'])
        dataset, _ = lab.dataset(saved['dataset_id'])
        version = lab.version(saved['version_id'])
        if (dataset['hash'] != saved['dataset_hash'] or dataset['gaps']
                or content_hash(version['definition']) != saved['strategy_hash']
                or lab.validation(str(artifact_id)) != saved):
            raise ResearchReferenceError('La validación cambió durante la lectura de referencia')
        reference = _make_reference(saved, dataset, version, 'STRATEGY')
        current_dataset, _ = lab.dataset(dataset['id'])
        if current_dataset != dataset or lab.version(version['id']) != version:
            raise ResearchReferenceError('Las entradas cambiaron durante la lectura de referencia')
        return reference
    except ValidationError:
        raise ResearchReferenceError('Metadatos de investigación no válidos') from None
    except (LabError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ResearchReferenceError):
            raise
        raise ResearchReferenceError('No se pudo comprobar la referencia de estrategia') from exc


def _dca_reference(store: AutomationStore, lab: LabStore, artifact_id: str) -> ResearchReference:
    try:
        saved = store.validation(str(artifact_id))
        checked = verify_dca_validation(store, lab, saved)
        if not checked['verified']:
            raise ResearchReferenceError(checked['reason'])
        dataset, _ = lab.dataset(saved['dataset_id'])
        version = store.version(saved['version_id'])
        if (dataset['hash'] != saved['dataset_hash'] or dataset['gaps']
                or content_hash(version['definition']) != saved['definition_hash']
                or store.validation(str(artifact_id)) != saved):
            raise ResearchReferenceError('La reserva DCA cambió durante la lectura de referencia')
        reference = _make_reference(saved, dataset, version, 'DCA')
        current_dataset, _ = lab.dataset(dataset['id'])
        if current_dataset != dataset or store.version(version['id']) != version:
            raise ResearchReferenceError('Las entradas DCA cambiaron durante la lectura de referencia')
        return reference
    except ValidationError:
        raise ResearchReferenceError('Metadatos de investigación DCA no válidos') from None
    except (LabError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ResearchReferenceError):
            raise
        raise ResearchReferenceError('No se pudo comprobar la referencia DCA') from exc


def _api_context() -> Context:
    # Published LAB experiments used the standard API context around engines
    # that select their own archived precision. Do not change their semantics.
    return Context(prec=28, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
                   capitals=1, clamp=0, flags=[], traps=[InvalidOperation, DivisionByZero, Overflow])


def strategy_reference(lab: LabStore, artifact_id: str) -> ResearchReference:
    with localcontext(_api_context()):
        return _strategy_reference(lab, artifact_id)


def dca_reference(store: AutomationStore, lab: LabStore, artifact_id: str) -> ResearchReference:
    with localcontext(_api_context()):
        return _dca_reference(store, lab, artifact_id)


def revalidate_reference(value: dict, lab: LabStore,
                         automation: AutomationStore | None = None) -> ResearchReference:
    """Rebuild from local originals; a self-consistent exported hash is insufficient."""
    try:
        supplied = ResearchReference.model_validate(value)
    except ValidationError:
        raise ResearchReferenceError('Formato o huella de referencia no válidos') from None
    if supplied.family == 'STRATEGY':
        actual = strategy_reference(lab, str(supplied.artifact_id))
    else:
        if automation is None:
            raise ResearchReferenceError('Se necesita la base DCA para comprobar su referencia')
        actual = dca_reference(automation, lab, str(supplied.artifact_id))
    if supplied != actual:
        raise ResearchReferenceError('La referencia ya no corresponde a su investigación original')
    return actual
