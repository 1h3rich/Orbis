"""Plaintext research projection from a single verified GRVT MAINNET archive."""

from hashlib import sha256

from app.lab.store import content_hash, encoded
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError, client_ids
from app.live_execution.grvt_mainnet_executions import read_executions_with_origin
from app.live_execution.grvt_perp_limit_executions import limit_executions_view
from app.live_execution.grvt_perp_limit_journal import limit_schema, verified_limit_attempt
from app.portfolio.models import GrvtMainnetLimitAttempt

SCHEMA = 'orbis.grvt.real.fills.v1'
MAX_ROWS = 5000
MAX_BYTES = 4 * 1024 * 1024


def _reserve(reference: dict, digest_key: str) -> dict:
    return {'artifact_id':reference['artifact_id'],'artifact_sha256':reference[digest_key],
            'method':reference['method'],'windows':reference['windows']}


def _observed_origin(paper: dict, research: dict) -> dict:
    profile = research['profile']
    return {'kind':'PAPER_OBSERVED','strategy_version_id':profile['strategy_version_id'],
            'definition_sha256':profile['strategy_hash'],'dataset_sha256':research['dataset_hash'],
            'paper_session_id':paper['session_id'],'paper_head_sha256':paper['head_hash'],
            'reserve':_reserve(research,'artifact_hash')}


def _protected_origin(grant: dict | None) -> dict:
    if grant is None:
        return {'kind':'MANUAL'}
    source = grant['source']
    if source.get('schema') == 'orbis.grvt.perp.bot.source.v1':
        return _observed_origin(source['paper'],source['research'])
    binding = grant.get('research_binding')
    return {'kind':'PAPER_STRATEGY','strategy_version_id':source['version_id'],
            'definition_sha256':content_hash(source['definition']),'dataset_sha256':source['dataset_hash'],
            'paper_session_id':source['session_id'],
            'reserve':_reserve(binding['research'],'artifact_digest') if binding else None}


def export_grvt_fills(db, identifier: str, *, sub_account_id: str, revision: str, limit: bool) -> dict:
    client_ids(identifier)
    if limit:
        limit_schema(db,required=True)
        archived = verified_limit_attempt(db,db.get(GrvtMainnetLimitAttempt,identifier))
        view = limit_executions_view(archived)
        terms = archived['claim']['preflight']['plan']['source']['terms']
        direction = terms['definition']['direction']
        provenance = _observed_origin(terms['paper'],terms['research'])
    else:
        view,grant,direction = read_executions_with_origin(db,identifier)
        provenance = _protected_origin(grant)
    if view['sub_account_id'] != sub_account_id:
        raise GrvtMainnetError('El registro pertenece a otra subcuenta GRVT')
    if view['revision'] != revision:
        raise GrvtMainnetError('Los fills cambiaron; actualiza el gráfico')
    if not view['financially_reconciled'] or not view['total']:
        raise GrvtMainnetError('La descarga requiere fills de una operación conciliada')
    if view['total'] > MAX_ROWS:
        raise GrvtMainnetError('La operación supera el máximo de fills de la exportación')

    market = {'symbol':view['symbol'],'timeframe':view['timeframe'],'direction':direction}
    order_numbers: dict[str,int] = {}
    lines: list[bytes] = []
    size = 0
    for index,fill in enumerate(view['executions'],1):
        order = order_numbers.setdefault(fill['client_order_id'],len(order_numbers)+1)
        sample = {'schema':SCHEMA,'environment':'REAL','network':'MAINNET','provider':'grvt',
                  'execution_authority':'NONE','evidence':'OWN_VERIFIED_FILLS_RECONCILED',
                  'entry_kind':'LIMIT' if limit else 'PROTECTED','market':market,'provenance':provenance,
                  'fill_index':index,'order_index':order,
                  'fill':{key:fill[key] for key in ('side','role','time_ns','price','quantity_base','gross_quote','fee','fee_asset')}}
        if 'reduction_reason' in fill:
            sample['fill'].update(target_index=fill['target_index'],reduction_reason=fill['reduction_reason'])
        line = (encoded({**sample,'row_sha256':content_hash(sample)})+'\n').encode('utf-8')
        size += len(line)
        if size > MAX_BYTES:
            raise GrvtMainnetError('El archivo de fills supera el tamaño máximo de exportación')
        lines.append(line)
    payload = b''.join(lines)
    return {'schema':SCHEMA,'revision':revision,'record_count':len(lines),
            'sha256':sha256(payload).hexdigest(),'jsonl':payload.decode('utf-8')}
