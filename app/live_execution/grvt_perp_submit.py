"""Internal profile entry, independently authorized and durably claimed once.

No HTTP route accepts this yet. A version-four bot selection can bind its fresh
signal and independently collected plan. The caller still supplies explicit,
revocable RAM authority; a stored plan/reference/grant is never permission.
"""
from __future__ import annotations

from collections.abc import Callable
import secrets
import time

from eth_account import Account

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_journal import require_new_mainnet_bracket
from app.live_execution.grvt_mainnet_submit import _transmit_prepared
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient,fetch_mainnet_instrument,public_post as _public_post
from app.live_execution.grvt_perp_plan import PerpEntryTerms,PerpEntryPlan,plan_draft
from app.live_execution.grvt_perp_preflight import collect_perp_preflight
from app.live_execution.grvt_perp_bot_grant import PerpBotSelectionRequest,check_perp_candidate,select_perp_entry


def submit_perp_entry_once(vault,terms: PerpEntryTerms,*,parent_client_order_id: str,
        client_factory: Callable[[],GrvtMainnetClient],private_key: bytes,expected_signer: str,
        require_permission: Callable[[],None],confirmation: str,public_post: Callable=_public_post,
        clock_ns: Callable[[],int]=time.time_ns,bot_selection: PerpBotSelectionRequest|None=None) -> dict:
    generation=vault.session_generation
    if generation is None:raise GrvtMainnetError('Bóveda bloqueada')
    if confirmation!='ENVIAR_ENTRADA_PERPETUA_GRVT_MAINNET' or not callable(require_permission):
        raise GrvtMainnetError('Falta autorización específica del perfil MAINNET')
    try:
        terms=PerpEntryTerms.model_validate(terms.payload())
        if (type(private_key) is not bytes or len(private_key)!=32 or type(expected_signer) is not str
                or Account.from_key(private_key).address.lower()!=expected_signer.lower()):
            raise ValueError('signer')
    except Exception:
        raise GrvtMainnetError('Perfil, presupuesto o firma MAINNET inválidos') from None
    start=clock_ns();last=start
    def checked():
        nonlocal last
        require_permission();at=clock_ns()
        if (vault.session_generation!=generation or type(at) is not int or not 0<at<2**63
                or type(start) is not int or type(last) is not int or not last<=at<=start+120_000_000_000):
            raise GrvtMainnetError('Permiso del perfil MAINNET revocado, vencido o con reloj regresado')
        last=at;return at
    def public(url,body):
        checked();reply=public_post(url,body);checked();return reply
    checked()
    with vault.guard_session_generation(generation),vault.session() as db:
        if bot_selection is None:
            require_new_mainnet_bracket(db,terms.budget.sub_account_id,parent_client_order_id)
        elif isinstance(bot_selection,PerpBotSelectionRequest):
            check_perp_candidate(db,bot_selection,terms,parent_client_order_id,checked())
        else:
            raise GrvtMainnetError('Selección perpetua REAL inválida')
    nonces=tuple(secrets.randbits(32) for _ in range(3))
    while len(set(nonces))!=3:nonces=tuple(secrets.randbits(32) for _ in range(3))
    try:
        client=client_factory();checked()
        if (not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET'
                or client.sub_account_id!=terms.budget.sub_account_id):
            raise GrvtMainnetError('Sesión ajena a la subcuenta MAINNET autorizada')
        proof=collect_perp_preflight(client,terms,public_post=public,clock_ns=checked)
        plan=PerpEntryPlan.model_validate(proof['plan']);draft=plan_draft(plan)
        instrument=plan.source.instrument.instrument()
        if fetch_mainnet_instrument(terms.instrument,http_post=public)!=instrument:
            raise GrvtMainnetError('Instrumento MAINNET cambiado antes de firmar')
        if bot_selection is not None:
            select_perp_entry(vault,bot_selection,plan,parent_client_order_id,
                require_permission=lambda:checked(),clock_ns=checked)
        return _transmit_prepared(vault,draft,instrument,proof,client,generation=generation,
            parent_client_order_id=parent_client_order_id,nonces=nonces,private_key=private_key,
            expected_signer=expected_signer,checked=checked,clock_ns=clock_ns,
            bot_run_id=bot_selection.run_id if bot_selection is not None else None)
    except GrvtMainnetError:raise
    except Exception:
        raise GrvtMainnetError('Perfil MAINNET no confirmado; consulta el diario sin reenviar') from None
    finally:del private_key
