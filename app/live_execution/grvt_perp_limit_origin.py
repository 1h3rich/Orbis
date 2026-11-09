"""Pin a completed LIMIT trial before preparing separate REAL terms."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from typing import ClassVar

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.perp_observed_archive import ObservedArchive, _AccessStamp, identifier
from app.derivatives.perp_observed_archive_format import OriginSnapshot, replay_observed_session
from app.derivatives.perp_observed_origin import VerifiedObservedOrigin, verified_observed_origin
from app.derivatives.perp_observed_wire import read_observed_seed
from app.lab.store import LabStore, content_hash
from app.live_execution.grvt_perp_bot_origin import _PINNED, _TAIL
from app.live_execution.grvt_perp_limit_plan import PerpLimitEntryTerms, PerpPartialLimitEntryTerms
from app.live_execution.grvt_perp_partial_targets import PerpPartialExitTerms
from app.live_execution.grvt_perp_plan import PerpRealBudget
from app.research.perp_live_review import ObservedReviewPrefix
from app.research.perp_reference import PerpLimitResearchReference, PerpPartialResearchReference


def _header(db, session_id: str):
    return db.execute('SELECT * FROM paper_observed_sessions WHERE id=?', (session_id,)).fetchone()


def _estimated(db, session_id: str) -> bool:
    return db.execute('SELECT 1 FROM paper_observed_gap_closes WHERE session_id=?',
                      (session_id,)).fetchone() is not None


@dataclass(frozen=True)
class _VerifiedPaperOrigin:
    origin: VerifiedObservedOrigin = field(repr=False)
    archive: ObservedArchive = field(repr=False)
    stamp: _AccessStamp = field(repr=False)
    paper: ObservedReviewPrefix
    pinned: tuple = field(repr=False)
    label: ClassVar[str] = 'LIMIT'

    @contextmanager
    def hold_current(self):
        with self.origin.hold_current(), paper_perp_access.guard(self.archive.paper.path, self.stamp.epoch):
            self.archive._stamp(self.stamp)
            with self.archive.paper.connect() as db:
                db.execute('BEGIN')
                current = _header(db, self.paper.session_id)
                if (current is None or tuple(current[key] for key in (*_PINNED, *_TAIL)) != self.pinned
                        or _estimated(db, self.paper.session_id)):
                    raise ValueError(f'La prueba PAPER {self.label} cambió durante la preparación')
                replayed = replay_observed_session(db, current)
                if replayed.head_hash != self.paper.head_hash:
                    raise ValueError(f'La prueba PAPER {self.label} perdió su prefijo')
                self.archive._stamp(self.stamp)
                yield


class VerifiedLimitPaperOrigin(_VerifiedPaperOrigin):
    def terms(self, budget: PerpRealBudget, *, max_pending_seconds: int) -> PerpLimitEntryTerms:
        with self.hold_current():
            reference = self.origin.reference()
            if not isinstance(reference, PerpLimitResearchReference):
                raise ValueError('La referencia revisada no es LIMIT')
            return PerpLimitEntryTerms(schema_name='orbis.grvt.perp.limit.entry.terms.v1',
                research=reference, paper=self.paper, definition=json.loads(self.origin.definition_json),
                budget=PerpRealBudget.model_validate(budget.payload()), max_pending_seconds=max_pending_seconds)


@dataclass(frozen=True)
class VerifiedPartialPaperOrigin(_VerifiedPaperOrigin):
    label: ClassVar[str] = 'con parciales'
    partial_fills: int

    def terms(self, budget: PerpRealBudget, *, max_attempts: int,
              reserved_close_attempts: int) -> PerpPartialExitTerms:
        with self.hold_current():
            reference = self.origin.reference()
            if not isinstance(reference, PerpPartialResearchReference):
                raise ValueError('La referencia revisada no define parciales')
            return PerpPartialExitTerms(schema_name='orbis.grvt.perp.partial.exit.terms.v1',
                research=reference, paper=self.paper, paper_partial_fills=self.partial_fills,
                definition=json.loads(self.origin.definition_json), budget=PerpRealBudget.model_validate(budget.payload()),
                max_attempts=max_attempts, reserved_close_attempts=reserved_close_attempts, execution_authority='NONE')

    def limit_terms(self, budget: PerpRealBudget, *, max_attempts: int, reserved_close_attempts: int,
                    max_pending_seconds: int) -> PerpPartialLimitEntryTerms:
        base = self.terms(budget,max_attempts=max_attempts,reserved_close_attempts=reserved_close_attempts)
        return PerpPartialLimitEntryTerms.model_validate({**base.payload(),
            'schema':'orbis.grvt.perp.limit.entry.terms.v2','max_pending_seconds':max_pending_seconds})


def _verified_paper_origin(lab: LabStore, archive: ObservedArchive, session_id: str, *,
                           partial: bool) -> tuple[VerifiedObservedOrigin,ObservedArchive,_AccessStamp,ObservedReviewPrefix,tuple,int]:
    session_id = identifier(session_id)
    stamp = archive._stamp()
    label = VerifiedPartialPaperOrigin.label if partial else VerifiedLimitPaperOrigin.label
    reference_type = PerpPartialResearchReference if partial else PerpLimitResearchReference
    partial_fills = 0

    def count_fills(_sequence, evidence, _state):
        nonlocal partial_fills
        partial_fills += sum(row['kind']=='PARTIAL_TARGET_FILLED' for row in evidence['events'])

    with archive.paper.connect() as db:
        db.execute('BEGIN')
        row = _header(db, session_id)
        replayed = replay_observed_session(db, row, on_verified_event=count_fills if partial else None)
        state = replayed.state
        header = dict(row)
        source = OriginSnapshot.model_validate(json.loads(header['origin_json']))
        seed = read_observed_seed(json.loads(header['seed_json']))
        if (not isinstance(source.reference, reference_type)
                or state.status != 'STOPPED' or state.resting is not None
                or state.opened is not None or state.account.positions
                or any(order.active for order in state.account.orders)
                or not state.closed_trades or len(state.history) <= len(seed.history)
                or _estimated(db, session_id) or partial and not partial_fills):
            raise ValueError(f'Completa y detén una prueba PAPER {label} con operaciones observadas')
    origin = verified_observed_origin(lab, source.reference.artifact_id)
    prefix = json.loads(origin.trade_prefix_json)
    if (origin.reference() != source.reference or json.loads(origin.definition_json) != seed.definition
            or len(prefix) != source.trade_prefix_count or content_hash(prefix) != source.trade_prefix_hash
            or [bar.model_dump() for bar in seed.history[:len(prefix)]] != prefix):
        raise ValueError(f'La prueba PAPER {label} no corresponde a la validación LAB original')
    paper = ObservedReviewPrefix(session_id=session_id, sequence=replayed.sequence, head_hash=replayed.head_hash,
        seed_hash=replayed.seed_hash, origin_hash=replayed.origin_hash, stored_status=state.status,
        started_at_ns=str(state.started_at_ns), last_received_at_ns=str(state.last_received_ns),
        observed_bars=len(state.history)-len(seed.history), closed_trades=state.closed_trades,
        has_estimated_closure=False, source_advanced=False)
    pinned = tuple(header[key] for key in (*_PINNED, *_TAIL))
    return (origin, archive, stamp, paper, pinned, partial_fills)


def verified_limit_paper_origin(lab: LabStore, archive: ObservedArchive, session_id: str) -> VerifiedLimitPaperOrigin:
    origin, owned, stamp, paper, pinned, _ = _verified_paper_origin(lab, archive, session_id, partial=False)
    capability = VerifiedLimitPaperOrigin(origin, owned, stamp, paper, pinned)
    with capability.hold_current():
        return capability


def verified_partial_paper_origin(lab: LabStore, archive: ObservedArchive, session_id: str) -> VerifiedPartialPaperOrigin:
    capability = VerifiedPartialPaperOrigin(*_verified_paper_origin(lab, archive, session_id, partial=True))
    with capability.hold_current():
        return capability
