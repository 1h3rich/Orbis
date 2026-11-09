"""Streaming closure bundle, containing the unchanged original JSONL stream."""
from __future__ import annotations

import hashlib
import json

from app.derivatives.perp_gap_close_format import GapCloseLink, GapCloseReceipt, verify_result
from app.derivatives.perp_observed_export import MAX_LINE_BYTES, _line, verify_observed_export
from app.derivatives.perp_observed_types import ObservedError
from app.lab.store import content_hash, encoded


EXPORT_SCHEMA = 'orbis.paper.perp.gap-close.export.v1'


def export_gap_close(store, session_id: str):
    report = store.read(session_id)  # Replay both calculations before first byte.
    stamp = store.archive._stamp()
    original = store.archive.export_lines(session_id)
    try:
        first = next(original)  # Verify original before offering HTTP success.
        header = json.loads(first)
        link = report['receipt']['source']
        if header['head_hash'] != link['head_hash'] or header['sequence'] != link['sequence']:
            raise ObservedError('El prefijo cambió antes de exportar el cierre')
        outer = {'kind': 'GAP_CLOSE_HEADER', 'schema': EXPORT_SCHEMA,
                 'receipt': report['receipt'], 'result': report['result'], 'hash': report['hash']}
        digest = hashlib.sha256()
        line = (encoded(outer) + '\n').encode('utf-8')
        digest.update(line)
        yield line
        digest.update(first)
        yield first
        for line in original:
            store.archive._stamp(stamp)
            digest.update(line)
            yield line
        store.archive._stamp(stamp)
        yield (encoded({'kind': 'GAP_CLOSE_COMPLETE', 'sha256': digest.hexdigest(),
                        'hash': report['hash']}) + '\n').encode('utf-8')
    finally:
        original.close()


class _OriginalStream:
    """Expose only the embedded origin; its EOF does not consume our footer."""
    def __init__(self, source, digest):
        self.source, self.digest, self.complete = source, digest, False
        self.header = None

    def readline(self, size: int) -> bytes:
        if self.complete:
            return b''
        raw = self.source.readline(size)
        if len(raw) > MAX_LINE_BYTES:
            raise ObservedError('Línea original excesiva')
        if raw:
            value = json.loads(raw)
            if self.header is None:
                self.header = value
            if value.get('kind') == 'COMPLETE':
                self.complete = True
            self.digest.update(raw)
        return raw

    def read(self, size: int) -> bytes:
        if not self.complete:
            raise ObservedError('El original no llegó a su cierre obligatorio')
        return b''


def verify_gap_close_export(source) -> dict:
    try:
        raw, header = _line(source)
        if set(header) != {'kind', 'schema', 'receipt', 'result', 'hash'} or header['kind'] != 'GAP_CLOSE_HEADER' or header['schema'] != EXPORT_SCHEMA:
            raise ObservedError('Cabecera de cierre incompatible')
        receipt = GapCloseReceipt.model_validate(header['receipt'])
        if receipt.payload() != header['receipt']:
            raise ObservedError('Recibo de cierre no canónico')
        digest = hashlib.sha256(raw)
        stream = _OriginalStream(source, digest)
        states = []
        origin = verify_observed_export(stream, on_verified_state=states.append)
        original_header = stream.header
        link = GapCloseLink(session_id=origin['session_id'], sequence=original_header['sequence'],
                            head_hash=origin['head_hash'], seed_hash=content_hash(original_header['seed']),
                            origin_hash=content_hash(original_header['origin']))
        result = verify_result(states[0], link, receipt, header['result'], header['hash'])
        _, footer = _line(source)
        if (set(footer) != {'kind', 'sha256', 'hash'} or footer['kind'] != 'GAP_CLOSE_COMPLETE'
                or footer['sha256'] != digest.hexdigest() or footer['hash'] != header['hash'] or source.read(1) != b''):
            raise ObservedError('Exportación de cierre incompleta o alterada')
        return {'verified': True, 'verification': 'GAP_CLOSE_REPLAY_VERIFIED', 'environment': 'PAPER',
                'quality': 'ESTIMATED_GAP_CLOSE', 'execution_authority': 'NONE',
                'receipt': receipt.payload(), 'result': result, 'hash': header['hash'], 'session_activated': False}
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
        if isinstance(exc, ObservedError):
            raise
        raise ObservedError('Exportación de cierre no verificable') from exc
