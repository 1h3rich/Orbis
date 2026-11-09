"""Durable, idempotent estimated closures, separate from observed accounts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.perp_gap_close import GapCloseInput, estimate_gap_close
from app.derivatives.perp_gap_close_format import (
    GapCloseReceipt, original_link, replay_gap_close,
)
from app.derivatives.perp_observed_archive import ObservedArchive, identifier
from app.derivatives.perp_observed_archive_format import pack
from app.derivatives.perp_observed_source import GrvtObservedSource
from app.derivatives.perp_observed_types import ObservedError, ObservedState
from app.derivatives.perp_observed_lease import ObservedSessionLease
from app.derivatives.perp_observed_wire import funding_input, quote_input, rules_input, specification_input
from app.lab.store import content_hash


class GapCloseStore:
    def __init__(self, archive: ObservedArchive, *, source_factory: Callable[[], GrvtObservedSource] | None = None):
        self.archive = archive
        self.source_factory = source_factory or (lambda: GrvtObservedSource(clock_ns=archive.clock_ns))

    def collect(self, state: ObservedState) -> GapCloseInput:
        instrument = state.account.markets[0].instrument
        symbol = instrument.removesuffix('_USDT_Perp') + '/USDT:PERP'
        provider = self.source_factory()
        quote, received, rules, spec, records = provider._point(symbol, state.funding_anchor)
        return GapCloseInput.model_validate({
            'schema': 'orbis.paper.perp.gap-close.input.v1', 'engine': 'perp-gap-close-1.0',
            'acknowledgement': 'UNKNOWN_INTERVAL_ACCEPTED', 'received_at_ns': str(received),
            'quote': quote_input(quote).payload(), 'rules': rules_input(rules).model_dump(),
            'specification': specification_input(spec).payload(),
            'funding': [funding_input(row).model_dump() for row in records],
        })

    def _existing(self, session_id: str, request_id: str, expected_head: str):
        with self.archive.paper.connect() as db:
            row = db.execute('SELECT * FROM paper_observed_gap_closes WHERE session_id=? OR request_id=?',
                             (session_id, request_id)).fetchone()
        if row is None:
            return None
        if row['session_id'] != session_id or row['request_id'] != request_id:
            raise ObservedError('La sesión ya tiene cierre o la petición pertenece a otra sesión')
        result = self.read(session_id)
        if result['receipt']['source']['head_hash'] != expected_head:
            raise ObservedError('Petición de cierre reutilizada con otro prefijo')
        result['duplicate'] = True
        return result

    def close(self, session_id: str, *, request_id: str, expected_head: str) -> dict:
        session_id, request_id = identifier(session_id), identifier(request_id)
        stamp = self.archive._stamp()
        old = self._existing(session_id, request_id, expected_head)
        if old is not None:
            self.archive._stamp(stamp)
            return old
        self.archive.active_ids()  # Release this instance's invalid old-epoch ownership.
        # Ownership lease spans HTTP, but no Python mutex or SQL transaction does.
        with ObservedSessionLease(self.archive.paper.path, session_id, exclusive=True):
            return self._close_owned(session_id, request_id=request_id,
                                     expected_head=expected_head, expected_access=stamp)

    def _close_owned(self, session_id: str, *, request_id: str, expected_head: str, expected_access) -> dict:
        session_id, request_id = identifier(session_id), identifier(request_id)
        archive = self.archive
        stamp = archive._stamp(expected_access)
        with archive._session_lock(session_id):
            old = self._existing(session_id, request_id, expected_head)
            if old is not None:
                archive._stamp(stamp)
                return old
            header, original, _ = archive.verified_inactive(session_id, expected_access=stamp)
            link = original_link(header, original)
            if link.head_hash != expected_head:
                raise ObservedError('El prefijo cambió desde la revisión; vuelve a inspeccionarlo')
        # Public collection never holds a session/key/SQL lock.
        source = self.collect(original.state)
        with archive._session_lock(session_id):
            archive._stamp(stamp)
            old = self._existing(session_id, request_id, expected_head)
            if old is not None:
                archive._stamp(stamp)
                return old
            current_header, current, _ = archive.verified_inactive(session_id, expected_access=stamp)
            if original_link(current_header, current) != link or current_header != header:
                raise ObservedError('El prefijo cambió durante la recogida del cierre')
            archive._fresh(source.received_at_ns)
            result = estimate_gap_close(current.state, source)
            receipt = GapCloseReceipt.model_validate({
                'schema': 'orbis.paper.perp.gap-close.receipt.v1', 'request_id': request_id,
                'created_at': datetime.now(timezone.utc).isoformat(), 'source': link.payload(),
                'input': source.payload(),
            })
            digest = content_hash({'receipt': receipt.payload(), 'result': result})
            receipt_blob, result_blob = pack(receipt.payload()), pack(result)
            with paper_perp_access.guard(archive.paper.path, stamp.epoch):
                archive._stamp(stamp)
                with archive.paper.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    actual = db.execute('SELECT * FROM paper_observed_sessions WHERE id=?', (session_id,)).fetchone()
                    if actual is None or any(actual[key] != current_header[key] for key in current_header):
                        raise ObservedError('El original cambió antes de publicar el cierre')
                    db.execute('INSERT INTO paper_observed_gap_closes VALUES (?,?,?,?,?)',
                               (session_id, request_id, receipt_blob, result_blob, digest))
                    archive._stamp(stamp)
            return self._response(receipt, result, digest, duplicate=False)

    @staticmethod
    def _response(receipt, result, digest, *, duplicate: bool) -> dict:
        return {'schema': 'orbis.paper.perp.gap-close.verified.v1',
                'verification': 'GAP_CLOSE_REPLAY_VERIFIED', 'environment': 'PAPER',
                'quality': 'ESTIMATED_GAP_CLOSE', 'execution_authority': 'NONE',
                'receipt': receipt.payload(), 'result': result, 'hash': digest, 'duplicate': duplicate}

    def read(self, session_id: str) -> dict:
        session_id = identifier(session_id)
        archive = self.archive
        stamp = archive._stamp()
        with archive._session_lock(session_id), archive.paper.connect() as db:
            row = db.execute('SELECT * FROM paper_observed_gap_closes WHERE session_id=?', (session_id,)).fetchone()
            receipt, result, original = replay_gap_close(db, row)
        archive._stamp(stamp)
        return self._response(receipt, result, row['hash'], duplicate=False)

    def pointers(self, session_ids: list[str]) -> dict:
        if len(session_ids) > 100:
            raise ObservedError('Demasiadas sesiones en la selección de cierres')
        if not session_ids:
            return {}
        session_ids = [identifier(value) for value in session_ids]
        archive = self.archive
        stamp = archive._stamp()
        with archive.paper.connect() as db:
            rows = db.execute('SELECT session_id,request_id,hash FROM paper_observed_gap_closes WHERE session_id IN (' +
                              ','.join('?' for _ in session_ids) + ')', session_ids).fetchall()
        archive._stamp(stamp)
        return {row['session_id']: {'request_id': row['request_id'], 'hash': row['hash'],
                                  'verification': 'NOT_REPLAYED'} for row in rows}
