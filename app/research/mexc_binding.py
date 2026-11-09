"""Research evidence pinned to a MEXC authorization, separate from its authority.

Historical datasets may precede the PAPER seed. Same-market overlapping bars
must agree; a later research dataset cannot qualify an earlier PAPER seed.
Opening a new DCA starts another price reference, never imports PAPER capital.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.lab.engine import INTERVALS
from app.lab.store import content_hash
from app.research.references import (Digest, Timestamp, ResearchReference,
    ResearchReferenceError, dca_reference, strategy_reference)


class PaperIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    source_digest: Digest
    session_id: UUID
    version_id: UUID
    definition_hash: Digest
    dataset_id: UUID
    dataset_hash: Digest
    symbol: str
    timeframe: str
    source: Literal['mexc-spot-public']
    seed_from_t: Timestamp
    seed_to_t: Timestamp
    observation_t: Timestamp
    observation_digest: Digest | None = None
    simulation_engine: str


class DcaAnchor(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    policy: Literal['RESET_AT_REAL_ACTIVATION']
    candle_t: Timestamp
    price: Annotated[str, Field(pattern=r'^\d+(?:\.\d+)?$', max_length=100)]

    @model_validator(mode='after')
    def positive(self):
        if Decimal(self.price) <= 0:
            raise ValueError('Referencia DCA no positiva')
        return self


class MexcResearchPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True)
    schema_name: Literal['orbis.mexc.bot.research.v1'] = Field(alias='schema')
    research: ResearchReference
    paper: PaperIdentity
    dca_anchor: DcaAnchor | None

    @model_validator(mode='after')
    def same_definition_and_market(self):
        r, p = self.research, self.paper
        step = INTERVALS.get(p.timeframe)
        if (step is None or not r.public_data or r.source != p.source
                or r.version_id != p.version_id or r.definition_hash != p.definition_hash
                or r.symbol != p.symbol or r.timeframe != p.timeframe
                or r.simulation_engine != p.simulation_engine
                or not p.symbol.endswith('/USDT') or r.direction not in (None, 'LONG')
                or p.seed_from_t > p.seed_to_t or p.seed_to_t >= p.observation_t
                or (p.seed_to_t - p.seed_from_t) % step or (p.observation_t - p.seed_to_t) % step
                or r.dataset_to_t > p.seed_to_t or any(w.bars < 30 for w in r.windows)
                or (r.family == 'DCA') != (self.dca_anchor is not None)
                or r.dataset_id == p.dataset_id and r.dataset_hash != p.dataset_hash):
            raise ValueError('Validación incompatible con el origen PAPER del bot')
        if self.dca_anchor is not None:
            if p.timeframe != '1d' or self.dca_anchor.candle_t > p.observation_t:
                raise ValueError('Referencia inicial DCA incompatible')
        return self

    def payload(self):
        result = self.model_dump(mode='json', by_alias=True, exclude={'sha256'})
        if self.paper.observation_digest is None:
            result['paper'].pop('observation_digest')
        return result


class MexcResearchBinding(MexcResearchPayload):
    sha256: Digest

    @model_validator(mode='after')
    def digest(self):
        if content_hash(self.payload()) != self.sha256:
            raise ValueError('Huella de vinculación de investigación alterada')
        return self

    def exported(self):
        return {**self.payload(), 'sha256': self.sha256}


def read_mexc_research_binding(value: dict, source: dict, *, dca_anchor: dict | None = None) -> MexcResearchBinding:
    """Validate the archived snapshot without requiring research stores unlocked.

    This checks the evidence archived at authorization, not a new live replay.
    New authorization always builds/replays from the original research stores.
    """
    try:
        binding = MexcResearchBinding.model_validate(value)
        p, r = binding.paper, binding.research
        if (p.source_digest != content_hash(source)
                or str(p.session_id) != source['session_id']
                or str(p.version_id) != source['version_id']
                or str(p.dataset_id) != source['dataset_id'] or p.dataset_hash != source['dataset_hash']
                or p.definition_hash != source['definition_hash'] or p.symbol != source['symbol']
                or p.timeframe != source['timeframe'] or p.simulation_engine != source['engine_version']
                or (r.family == 'DCA') != (source.get('kind') == 'DCA')
                or (binding.dca_anchor.model_dump(mode='json') if binding.dca_anchor else None) != dca_anchor):
            raise ValueError('source')
        return binding
    except (ValidationError, KeyError, TypeError, ValueError):
        raise ResearchReferenceError('La evidencia de investigación del bot no coincide con su autorización') from None


@dataclass(frozen=True)
class CheckedMexcResearch:
    binding: MexcResearchBinding
    repository: object
    version_digest: str
    dataset_digest: str
    seed_digest: str

    def assert_review(self, expected: str | None) -> None:
        if expected is not None and self.binding.sha256 != expected:
            raise ResearchReferenceError('Cambió la evidencia LAB o PAPER. Revisa el origen de nuevo')

    def assert_current(self) -> None:
        """Cheap final re-read after exchange I/O, before writing authority."""
        r, p = self.binding.research, self.binding.paper
        lab = self.repository.lab
        store = self.repository.store if r.family == 'DCA' else lab
        saved = store.validation(str(r.artifact_id))
        dataset, _ = lab.dataset(str(r.dataset_id))
        seed, _ = lab.dataset(str(p.dataset_id))
        if (content_hash(saved) != r.artifact_digest
                or content_hash(store.version(str(r.version_id))) != self.version_digest
                or content_hash(dataset) != self.dataset_digest or content_hash(seed) != self.seed_digest):
            raise ResearchReferenceError('La investigación cambió durante la autorización del bot')


def archived_dca_anchor(config: dict) -> dict | None:
    if config['source'].get('kind') != 'DCA':
        return None
    return {'policy': 'RESET_AT_REAL_ACTIVATION', 'candle_t': config['dca_start_candle_t'],
            'price': config['dca_initial_reference']}


def check_mexc_research(repository, observation, validation_id: str, *, dca_anchor: dict | None = None) -> CheckedMexcResearch:
    """Replay an exact original validation and bind it to verified observed PAPER."""
    try:
        validation_id = str(UUID(validation_id))
        source = observation.binding()
        family = 'DCA' if source.get('kind') == 'DCA' else 'STRATEGY'
        lab = repository.lab
        store = repository.store if family == 'DCA' else lab
        reference = (dca_reference(store, lab, validation_id) if family == 'DCA'
                     else strategy_reference(lab, validation_id))
        dataset, history = lab.dataset(str(reference.dataset_id))
        seed, seed_rows = lab.dataset(observation.dataset_id)
        version = store.version(observation.version_id)
        if (dataset['hash'] != reference.dataset_hash or dataset['gaps']
                or dataset['symbol'] != reference.symbol or dataset['timeframe'] != reference.timeframe
                or dataset['source'] != reference.source or dataset['verified_public'] != reference.public_data
                or dataset['start_t'] != reference.dataset_from_t or dataset['end_t'] != reference.dataset_to_t
                or content_hash(version['definition']) != reference.definition_hash):
            raise ResearchReferenceError('La validación cambió durante la vinculación')
        if (seed['hash'] != observation.dataset_hash or not seed['verified_public'] or seed['gaps']
                or seed['symbol'] != observation.symbol or seed['timeframe'] != observation.timeframe
                or seed['source'] != 'mexc-spot-public'):
            raise ResearchReferenceError('El histórico inicial PAPER no es verificable')
        past = {row['t']: row for row in history}
        if any(row['t'] in past and row != past[row['t']] for row in seed_rows):
            raise ResearchReferenceError('Las velas comunes de investigación y PAPER difieren')
        if family == 'DCA' and dca_anchor is None:
            dca_anchor = {'policy': 'RESET_AT_REAL_ACTIVATION', 'candle_t': observation.candle_t,
                          'price': str(observation.candles[-1].c)}
        payload = MexcResearchPayload.model_validate({'schema': 'orbis.mexc.bot.research.v1',
            'research': reference.exported(), 'paper': {
                'source_digest': content_hash(source), 'session_id': observation.session_id,
                'version_id': observation.version_id, 'definition_hash': observation.definition_hash,
                'dataset_id': observation.dataset_id, 'dataset_hash': observation.dataset_hash,
                'symbol': observation.symbol, 'timeframe': observation.timeframe, 'source': seed['source'],
                'seed_from_t': seed['start_t'], 'seed_to_t': seed['end_t'],
                'observation_t': observation.candle_t, 'observation_digest': observation.observation_digest,
                'simulation_engine': observation.engine_version},
            'dca_anchor': dca_anchor}).model_dump(mode='json', by_alias=True)
        binding = MexcResearchBinding.model_validate({**payload, 'sha256': content_hash(payload)})
        checked = CheckedMexcResearch(binding, repository, content_hash(version),
                                      content_hash(dataset), content_hash(seed))
        checked.assert_current()
        return checked
    except ResearchReferenceError:
        raise
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ResearchReferenceError('Selecciona una validación reproducible de la misma versión, mercado y temporalidad, anterior a PAPER') from None


def mexc_research_options(repository, observation) -> list[dict]:
    """Discover compatible metadata. Actual replay is mandatory at activation."""
    source = observation.binding()
    dca = source.get('kind') == 'DCA'
    store, lab = (repository.store if dca else repository.lab), repository.lab
    seed, _ = lab.dataset(observation.dataset_id)
    result = []
    for row in store.validations():
        if row['version_id'] != observation.version_id:
            continue
        try:
            saved = store.validation(row['id'])
            dataset, _ = lab.dataset(row['dataset_id'])
            method = saved['method']
            if (saved.get('definition_hash' if dca else 'strategy_hash') != observation.definition_hash
                    or saved.get('dca_engine_version' if dca else 'lab_engine_version') != observation.engine_version
                    or saved.get('result_hash') is None or dataset['hash'] != saved['dataset_hash']
                    or not dataset['verified_public'] or dataset['gaps']
                    or dataset['symbol'] != observation.symbol or dataset['timeframe'] != observation.timeframe
                    or dataset['source'] != 'mexc-spot-public' or dataset['end_t'] > seed['end_t']
                    or method not in (('DCA_CHRONOLOGICAL_HOLDOUT',) if dca else
                                      ('CHRONOLOGICAL_HOLDOUT','ROLLING_FIXED_VERSION'))):
                continue
            result.append({'id':row['id'],'method':method,'created_at':row['created_at'],
                           'from_t':saved['windows'][0]['test_start_t'] if method == 'ROLLING_FIXED_VERSION' else saved['test_start_t'],
                           'to_t':saved['windows'][-1]['test_end_t'] if method == 'ROLLING_FIXED_VERSION' else saved['test_end_t'],
                           'test_bars':saved['test_bars'],'verification':'ON_ACTIVATION'})
        except (KeyError,TypeError,ValueError):
            continue
    return result
