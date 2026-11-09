"""Servicio interno de una entrada MAINNET protegida y con claim previo a red.

No activa bots, reintenta grupos, transfiere fondos ni confunde acuse con fill.
El llamador debe aportar autorización RAM revocable en require_permission.
"""

from collections.abc import Callable
import secrets
import time

from eth_account import Account

from app.live_execution.grvt_mainnet_bracket import (GrvtMainnetError,MainnetBracketDraft,sign_mainnet_bracket,
    unsigned_mainnet_bracket,verify_mainnet_bracket)
from app.live_execution.grvt_mainnet_transport import (GrvtMainnetClient,fetch_mainnet_instrument,public_post as _public_post)
from app.live_execution.grvt_mainnet_preflight import collect_mainnet_preflight,verify_mainnet_preflight
from app.live_execution.grvt_mainnet_journal import (claim_mainnet_bracket,read_mainnet_bracket,
    require_new_mainnet_bracket,verified_mainnet_bracket,encode,_control)
from app.portfolio.models import GrvtMainnetBracket


def _transmit_prepared(vault,draft,instrument,preflight,client,*,generation,parent_client_order_id,
        nonces,private_key,expected_signer,checked,clock_ns,bot_run_id=None):
    """Shared durable single-send boundary, after each version's own preflight."""
    issued=checked();verify_mainnet_preflight(preflight,draft,now_ns=issued)
    orders=sign_mainnet_bracket(draft,instrument,parent_client_order_id=parent_client_order_id,nonces=nonces,
        private_key=private_key,expected_signer=expected_signer,now_ns=issued)
    with vault.guard_session_generation(generation):
        checked()
        claim_mainnet_bracket(vault,draft,instrument,orders,preflight,issued_at_ns=issued,
            claimed_at_ns=checked(),client=client,expected_signer=expected_signer,bot_run_id=bot_run_id)
        with vault.session() as db:
            try:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                row=db.get(GrvtMainnetBracket,parent_client_order_id);saved=verified_mainnet_bracket(db,row)
                control=_control(db,draft.sub_account_id);now=checked()
                from app.live_execution.grvt_mainnet_bot_entry import require_bot_submission
                require_bot_submission(db,draft.sub_account_id,parent_client_order_id,bot_run_id,now_ns=now)
                if (row.status!='POST_CLAIMED' or control.stopped
                        or saved['claim']['wire_digests']!=[encode(order)[1] for order in orders]):
                    raise GrvtMainnetError('Intento MAINNET revocado o cambiado antes de enviar')
                verify_mainnet_preflight(preflight,draft,now_ns=now)
                verify_mainnet_bracket(orders,draft,instrument,issued_at_ns=issued,now_ns=now)
                ack=client.submit_bracket_once(orders,draft,instrument,issued_at_ns=issued,
                    clock_ns=checked,require_permission=lambda:checked())
                received=clock_ns()
                if type(received) is not int or not now<=received<2**63:received=now
                row.ack_json,row.ack_digest=encode({'claim_digest':row.payload_digest,'received_at_ns':received,**ack})
                row.status='ACKNOWLEDGED';db.flush();verified_mainnet_bracket(db,row);db.commit()
            except Exception:
                db.rollback();raise
    return read_mainnet_bracket(vault,parent_client_order_id)


def submit_mainnet_bracket_once(vault,draft: MainnetBracketDraft,*,parent_client_order_id: str,
        client_factory: Callable[[],GrvtMainnetClient],private_key: bytes,expected_signer: str,
        require_permission: Callable[[],None],confirmation: str,public_post: Callable=_public_post,
        clock_ns: Callable[[],int]=time.time_ns,bot_run_id: str|None=None) -> dict:
    generation=vault.session_generation
    if generation is None: raise GrvtMainnetError('Bóveda bloqueada')
    if confirmation!='ENVIAR_ENTRADA_PROTEGIDA_GRVT_MAINNET' or not callable(require_permission):
        raise GrvtMainnetError('Falta confirmación MAINNET expresa')
    try:
        if (type(private_key) is not bytes or len(private_key)!=32 or not isinstance(expected_signer,str)
                or Account.from_key(private_key).address.lower()!=expected_signer.lower()): raise ValueError('dedicated signer')
    except Exception:
        raise GrvtMainnetError('Clave dedicada y firmante MAINNET no coinciden') from None
    start=clock_ns(); last=start
    def checked():
        nonlocal last
        require_permission()
        at=clock_ns()
        if (vault.session_generation!=generation or type(at) is not int or not 0<at<2**63
                or type(last) is not int or not last<=at<=start+120_000_000_000):
            raise GrvtMainnetError('Permiso MAINNET vencido, revocado o con reloj regresado')
        last=at; return at
    checked()
    with vault.guard_session_generation(generation),vault.session() as db:
            require_new_mainnet_bracket(db,draft.sub_account_id,parent_client_order_id,bot_run_id=bot_run_id)
    nonces=tuple(secrets.randbits(32) for _ in range(3))
    while len(set(nonces))!=3: nonces=tuple(secrets.randbits(32) for _ in range(3))
    try:
        instrument=fetch_mainnet_instrument(draft.instrument,http_post=public_post); checked()
        unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=parent_client_order_id,nonces=nonces,issued_at_ns=checked())
        client=client_factory(); checked()
        if not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET' or client.sub_account_id!=draft.sub_account_id:
            raise GrvtMainnetError('Sesión fuera del ámbito MAINNET revisado')
        preflight=collect_mainnet_preflight(client,draft,public_post=public_post,clock_ns=checked)
        current=fetch_mainnet_instrument(draft.instrument,http_post=public_post); checked()
        if current!=instrument: raise GrvtMainnetError('Reglas MAINNET cambiadas durante la revisión')
        return _transmit_prepared(vault,draft,instrument,preflight,client,generation=generation,
            parent_client_order_id=parent_client_order_id,nonces=nonces,private_key=private_key,
            expected_signer=expected_signer,checked=checked,clock_ns=clock_ns,bot_run_id=bot_run_id)
    except GrvtMainnetError: raise
    except Exception:
        raise GrvtMainnetError('Entrada MAINNET no confirmada; consulta el diario antes de otra acción') from None
    finally: del private_key
