"""Collect actual perpetual entry fills without the legacy MARK exit model."""
from __future__ import annotations

import time

from app.live_execution.grvt_mainnet_bot_entry import read_bot_grant
from app.live_execution.grvt_mainnet_bot_exit import entry_from_fills
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_entry_fills import save_verified_entry_fills
from app.live_execution.grvt_mainnet_finance import _fill
from app.live_execution.grvt_mainnet_journal import _control, encode, read_mainnet_bracket
from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient


def collect_perp_entry_fills(vault,run_id,parent,*,client_factory,require_permission,clock_ns=time.time_ns):
    generation=vault.session_generation
    last=clock_ns();start=last
    def checked():
        nonlocal last
        require_permission()
        at=clock_ns()
        if (generation is None or vault.session_generation!=generation or type(at) is not int
                or not 0<last<=at<=start+120_000_000_000):
            raise GrvtMainnetError('Confirmación de fills perpetuos revocada o vencida')
        last=at
        return at
    checked()
    grant=read_bot_grant(vault,run_id)
    if (grant['source'].get('schema')!='orbis.grvt.perp.bot.source.v1' or grant['terminal'] is not None
            or not grant['selections'] or grant['selections'][-1]['parent_client_order_id']!=parent
            or grant['selections'][-1]['settlement'] is not None):
        raise GrvtMainnetError('La entrada no pertenece al bot perpetuo activo')
    observed=observe_mainnet_bracket(vault,parent,client_factory=client_factory,
        require_permission=checked,clock_ns=checked)
    saved=read_mainnet_bracket(vault,parent)
    with vault.session() as db:
        salt=_control(db,saved['sub_account_id']).identity_salt
    client=client_factory();checked()
    if (not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET'
            or client.sub_account_id!=saved['sub_account_id']
            or client.identity_ref(salt)!=saved['claim']['account_ref']):
        raise GrvtMainnetError('La identidad de los fills perpetuos cambió')
    cutoff=checked();rows=[];cursor='';seen=set()
    for page in range(1,21):
        checked()
        batch=client.history_page('fill_history',saved['sub_account_id'],
            start_ns=saved['claim']['preflight']['states'][-1]['observed_at_ns'],end_ns=cutoff,cursor=cursor)
        checked()
        if (type(batch.get('result')) is not list or len(batch['result'])>500
                or type(batch.get('next')) is not str or len(batch['next'])>1024
                or not batch['next'].isascii() or any(ord(c)<32 for c in batch['next'])):
            raise GrvtMainnetError('Paginación de fills perpetuos incompleta')
        rows.extend(_fill(row,saved['sub_account_id']) for row in batch['result'])
        following=batch['next']
        if not following:
            break
        if following in seen:
            raise GrvtMainnetError('Paginación de fills perpetuos repetida')
        seen.add(following);cursor=following
    else:
        raise GrvtMainnetError('Fills perpetuos fuera de la capacidad de consulta')
    section={'pages':page,'drained':True,'rows':rows}
    size,notional,at=entry_from_fills(saved['claim'],observed,section,cutoff_ns=cutoff)
    value={'schema':'orbis.grvt.mainnet.entry.fills.v1','network':'MAINNET','parent_client_order_id':parent,
        'claim_digest':encode(saved['claim'])[1],'observation_sequence':observed['sequence'],
        'observation_digest':encode(observed)[1],'cutoff_ns':cutoff,'fills':section,
        'size':format(size,'f'),'notional':format(notional,'f'),'last_fill_ns':at}
    checked()
    return save_verified_entry_fills(vault,value,require_permission=checked)
