"""Diario MAINNET cifrado, separado de simulación y sin autoridad persistente."""

from dataclasses import asdict
from decimal import Decimal
import hashlib
import hmac
import json
import re
import secrets

from sqlalchemy import select

from app.live_execution.grvt_mainnet_bracket import (GrvtMainnetError,MainnetBracketDraft,
    MainnetInstrument,ROLES,unsigned_mainnet_bracket,client_ids)
from app.live_execution.grvt_mainnet_preflight import verify_mainnet_preflight
from app.portfolio.models import GrvtMainnetBracket,GrvtMainnetLeg,GrvtMainnetControl


_HASH=re.compile(r'[a-f0-9]{64}\Z')
_AMOUNTS={'size','entry_price','stop_loss','take_profit','entry_cap','exit_cap'}
_TERMS={'tick_size','min_size','min_notional'}


def _safe(value):
    if isinstance(value,Decimal): return format(value,'f')
    if isinstance(value,dict): return {key:_safe(item) for key,item in value.items()}
    if isinstance(value,(tuple,list)): return [_safe(item) for item in value]
    if value is None or type(value) in (str,int,bool): return value
    raise ValueError('noncanonical mainnet payload')


def encode(value) -> tuple[str,str]:
    text=json.dumps(_safe(value),sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
    return text,hashlib.sha256(text.encode('ascii')).hexdigest()


def signer_ref(salt: str,address: str) -> str:
    return hmac.new(bytes.fromhex(salt),('GRVT_MAINNET_SIGNER:'+address.lower()).encode('ascii'),hashlib.sha256).hexdigest()


def require_mainnet_schema(db) -> None:
    count=db.connection().exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('grvt_mainnet_brackets','grvt_mainnet_legs','grvt_mainnet_controls','grvt_mainnet_observations')").scalar_one()
    if count!=4 or db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<50:
        raise GrvtMainnetError('Esquema MAINNET incompleto o anterior')


def _control(db,sub: str,*,create: bool=False):
    require_mainnet_schema(db)
    row=db.get(GrvtMainnetControl,sub)
    if row is None and create:
        row=GrvtMainnetControl(sub_account_id=sub,identity_salt=secrets.token_hex(32),account_ref=None,
            stopped=False,revision=0,pending_parent_id=None)
        db.add(row); db.flush()
    if row is not None:
        if (not isinstance(sub,str) or not re.fullmatch(r'[0-9]{1,20}',sub) or not 0<int(sub)<2**64
                or str(int(sub))!=sub or not _HASH.fullmatch(row.identity_salt) or type(row.stopped) is not bool
                or type(row.revision) is not int or not 0<=row.revision<2**53-1
                or row.account_ref is not None and not _HASH.fullmatch(row.account_ref)):
            raise GrvtMainnetError('Control MAINNET no verificable')
        if row.pending_parent_id is not None:
            group=db.get(GrvtMainnetBracket,row.pending_parent_id)
            if group is None or group.sub_account_id!=sub: raise GrvtMainnetError('Ancla MAINNET truncada')
    return row


def require_new_mainnet_bracket(db,sub: str,parent: str,*,bot_run_id: str|None=None) -> None:
    client_ids(parent)
    from app.live_execution.grvt_perp_limit_journal import require_no_limit_conflict
    limit_history = require_no_limit_conflict(db,sub,client_ids(parent))
    from app.live_execution.grvt_mainnet_bot_entry import require_bot_submission
    require_bot_submission(db,sub,parent,bot_run_id)
    control=_control(db,sub)
    if (control is not None and (control.stopped or control.pending_parent_id is not None)
            or db.get(GrvtMainnetBracket,parent) is not None
            or any(db.get(GrvtMainnetLeg,cid) is not None for cid in client_ids(parent))
            or db.scalar(select(GrvtMainnetBracket.parent_client_order_id).where(
                GrvtMainnetBracket.active_slot==sub).limit(1)) is not None):
        raise GrvtMainnetError('Subcuenta MAINNET detenida o con intento pendiente; consulta el diario')
    previous=list(db.scalars(select(GrvtMainnetBracket).where(GrvtMainnetBracket.sub_account_id==sub)))
    if control is not None and control.account_ref is not None and not previous and not limit_history:
        raise GrvtMainnetError('Historial MAINNET truncado')
    for group in previous:verified_mainnet_bracket(db,group)
    from app.live_execution.grvt_mainnet_finance import verified_settlement_chain
    verified_settlement_chain(db,sub)
    from app.live_execution.grvt_mainnet_recovery import recovery_schema
    from app.portfolio.models import GrvtMainnetReduction
    if recovery_schema(db) and any(db.get(GrvtMainnetReduction,cid) is not None for cid in client_ids(parent)):
        raise GrvtMainnetError('Identificador MAINNET ya reservado por una reducción')


def _draft(raw: dict) -> MainnetBracketDraft:
    expected=set(MainnetBracketDraft.__dataclass_fields__)
    if set(raw)!=expected: raise ValueError('draft fields')
    return MainnetBracketDraft(**{key:Decimal(value) if key in _AMOUNTS and type(value) is str else value for key,value in raw.items()})


def _instrument(raw: dict) -> MainnetInstrument:
    if set(raw)!=set(MainnetInstrument.__dataclass_fields__): raise ValueError('instrument fields')
    return MainnetInstrument(**{key:Decimal(value) if key in _TERMS and type(value) is str else value for key,value in raw.items()})


def verified_mainnet_bracket(db,row) -> dict:
    try:
        from app.live_execution.grvt_mainnet_finance import finance_schema,verified_financial_evidence
        from app.portfolio.models import GrvtMainnetReconciliation
        reconciled=finance_schema(db) and db.get(GrvtMainnetReconciliation,row.parent_client_order_id) is not None
        payload=json.loads(row.payload_json)
        perp_bot=payload.get('schema')=='orbis.grvt.mainnet.bracket.claim.v3'
        profiled=payload.get('schema') in ('orbis.grvt.mainnet.bracket.claim.v2','orbis.grvt.mainnet.bracket.claim.v3')
        if (set(payload)!={'schema','network','source','parent_client_order_id','draft','instrument','issued_at_ns',
                'claimed_at_ns','nonces','unsigned_digests','wire_digests','signer_ref','account_ref','preflight','control_revision'}
                or payload['schema'] not in ('orbis.grvt.mainnet.bracket.claim.v1','orbis.grvt.mainnet.bracket.claim.v2','orbis.grvt.mainnet.bracket.claim.v3') or payload['network']!='MAINNET'
                or (payload['source']!=('PERP_BOT_NATIVE_PROTECTED_ENTRY' if perp_bot else 'PERP_PROFILE_NATIVE_PROTECTED_ENTRY') if profiled else
                    payload['source'] not in ('MANUAL_NATIVE_PROTECTED_ENTRY','BOT_NATIVE_PROTECTED_ENTRY'))
                or payload['preflight'].get('schema')!=('orbis.grvt.mainnet.perp.preflight.v1' if profiled else 'orbis.grvt.mainnet.preflight.v1')
                or payload['parent_client_order_id']!=row.parent_client_order_id
                or encode(payload)!=(row.payload_json,row.payload_digest) or row.status not in ('POST_CLAIMED','ACKNOWLEDGED','OBSERVED')):
            raise ValueError('mainnet claim shape')
        if payload['source'] in ('BOT_NATIVE_PROTECTED_ENTRY','PERP_BOT_NATIVE_PROTECTED_ENTRY'):
            from app.live_execution.grvt_mainnet_bot_entry import verify_bot_claim
            verify_bot_claim(db,payload)
        else:
            from app.live_execution.grvt_mainnet_bot_entry import bot_schema
            from app.portfolio.models import GrvtMainnetBotSelection
            if bot_schema(db) and db.get(GrvtMainnetBotSelection,row.parent_client_order_id) is not None:
                raise ValueError('manual claim relabeled from a bot selection')
        draft=_draft(payload['draft']); instrument=_instrument(payload['instrument'])
        if (row.sub_account_id!=draft.sub_account_id or row.active_slot!=(None if reconciled else row.sub_account_id)
                or type(payload['issued_at_ns']) is not int or type(payload['claimed_at_ns']) is not int
                or not 0<=payload['claimed_at_ns']-payload['issued_at_ns']<20_000_000_000
                or payload['preflight']['checked_at_ns']>payload['issued_at_ns']
                or type(payload['control_revision']) is not int or payload['control_revision']<1
                or not isinstance(payload['nonces'],list)):
            raise ValueError('mainnet claim scope')
        orders=unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=row.parent_client_order_id,
            nonces=tuple(payload['nonces']),issued_at_ns=payload['issued_at_ns'])
        if (payload['unsigned_digests']!=[encode(order)[1] for order in orders]
                or not isinstance(payload['wire_digests'],list) or len(payload['wire_digests'])!=3
                or any(not isinstance(value,str) or not _HASH.fullmatch(value) for value in payload['wire_digests'])
                or len(set(payload['wire_digests']))!=3 or not _HASH.fullmatch(payload['signer_ref'])
                or not _HASH.fullmatch(payload['account_ref'])):
            raise ValueError('mainnet contract digests')
        verify_mainnet_preflight(payload['preflight'],draft,now_ns=payload['claimed_at_ns'])
        control=_control(db,row.sub_account_id)
        if (control is None or (control.pending_parent_id==row.parent_client_order_id if reconciled
                else control.pending_parent_id!=row.parent_client_order_id) or control.account_ref!=payload['account_ref']
                or control.revision<payload['control_revision']): raise ValueError('mainnet control changed')
        legs=list(db.scalars(select(GrvtMainnetLeg).where(GrvtMainnetLeg.parent_client_order_id==row.parent_client_order_id)))
        if sorted((leg.role,leg.client_order_id) for leg in legs)!=sorted(zip(ROLES,client_ids(row.parent_client_order_id))):
            raise ValueError('mainnet leg reservation truncated')
        acknowledgement=None
        if row.ack_json is not None or row.ack_digest is not None:
            ack=json.loads(row.ack_json)
            if (set(ack)!={'claim_digest','received_at_ns','orders','execution_unverified','position_verified'}
                    or encode(ack)!=(row.ack_json,row.ack_digest) or ack['claim_digest']!=row.payload_digest
                    or type(ack['received_at_ns']) is not int or not payload['claimed_at_ns']<=ack['received_at_ns']<2**63
                    or ack['execution_unverified'] is not True or ack['position_verified'] is not False
                    or not isinstance(ack['orders'],list) or len(ack['orders'])!=3): raise ValueError('mainnet acknowledgement')
            ids=[]
            for actual,role,cid in zip(ack['orders'],ROLES,client_ids(row.parent_client_order_id)):
                if (set(actual)!={'role','client_order_id','order_id'} or actual['role']!=role or actual['client_order_id']!=cid
                        or not isinstance(actual['order_id'],str) or not re.fullmatch(r'[A-Za-z0-9x]{1,80}',actual['order_id'])):
                    raise ValueError('mainnet acknowledged identities')
                ids.append(actual['order_id'])
            if len(set(ids))!=3: raise ValueError('duplicate mainnet exchange identity')
            acknowledgement=ack
        if (row.status=='ACKNOWLEDGED')!=(acknowledgement is not None): raise ValueError('mainnet acknowledgement projection')
        from app.live_execution.grvt_mainnet_observation import verified_mainnet_observations
        observations=verified_mainnet_observations(db,row,payload,acknowledgement)
        if row.status=='OBSERVED' and not observations: raise ValueError('observed projection without evidence')
        audits,receipt=verified_financial_evidence(db,row,payload,observations)
        from app.live_execution.grvt_mainnet_recovery import verified_recovery
        recovery=verified_recovery(db,row,payload,observations)
        return {'network':'MAINNET','authorization':False,'parent_client_order_id':row.parent_client_order_id,
            'sub_account_id':row.sub_account_id,'status':'RECONCILED' if receipt is not None else row.status,'claim':payload,'acknowledgement':acknowledgement,
            'execution_unverified':receipt is None,'position_verified':receipt is not None,'financially_reconciled':receipt is not None,
            'audits':audits,'reconciliation':receipt,'recovery':recovery,
            'last_observation':observations[-1] if observations else None}
    except Exception:
        raise GrvtMainnetError('Diario de entrada MAINNET no verificable') from None


def read_mainnet_bracket(vault,parent: str) -> dict:
    with vault.session() as db:
        require_mainnet_schema(db)
        row=db.get(GrvtMainnetBracket,parent)
        if row is None: raise GrvtMainnetError('Intento MAINNET no encontrado')
        return verified_mainnet_bracket(db,row)


def claim_mainnet_bracket(vault,draft,instrument,orders,preflight,*,issued_at_ns,claimed_at_ns,client,expected_signer,bot_run_id=None):
    parent=orders[0]['metadata']['client_order_id']
    profiled=preflight.get('schema')=='orbis.grvt.mainnet.perp.preflight.v1'
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            require_new_mainnet_bracket(db,draft.sub_account_id,parent,bot_run_id=bot_run_id)
            from app.live_execution.grvt_mainnet_bot_entry import require_bot_submission
            require_bot_submission(db,draft.sub_account_id,parent,bot_run_id,now_ns=claimed_at_ns)
            control=_control(db,draft.sub_account_id,create=True)
            reference=client.identity_ref(control.identity_salt)
            if control.account_ref is not None and control.account_ref!=reference: raise ValueError('changed main account')
            control.account_ref=reference; control.revision+=1
            unsigned=unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=parent,
                nonces=tuple(order['signature']['nonce'] for order in orders),issued_at_ns=issued_at_ns)
            payload={'schema':('orbis.grvt.mainnet.bracket.claim.v3' if bot_run_id is not None else 'orbis.grvt.mainnet.bracket.claim.v2') if profiled else 'orbis.grvt.mainnet.bracket.claim.v1','network':'MAINNET',
                'source':('PERP_BOT_NATIVE_PROTECTED_ENTRY' if bot_run_id is not None else 'PERP_PROFILE_NATIVE_PROTECTED_ENTRY') if profiled else 'BOT_NATIVE_PROTECTED_ENTRY' if bot_run_id is not None else 'MANUAL_NATIVE_PROTECTED_ENTRY',
                'parent_client_order_id':parent,'draft':asdict(draft),'instrument':asdict(instrument),'issued_at_ns':issued_at_ns,
                'claimed_at_ns':claimed_at_ns,'nonces':[order['signature']['nonce'] for order in orders],
                'unsigned_digests':[encode(order)[1] for order in unsigned],'wire_digests':[encode(order)[1] for order in orders],
                'signer_ref':signer_ref(control.identity_salt,expected_signer),'account_ref':reference,
                'preflight':preflight,'control_revision':control.revision}
            text,digest=encode(payload)
            row=GrvtMainnetBracket(parent_client_order_id=parent,sub_account_id=draft.sub_account_id,
                active_slot=draft.sub_account_id,status='POST_CLAIMED',payload_json=text,payload_digest=digest)
            db.add(row); db.flush(); control.pending_parent_id=parent
            for role,cid in zip(ROLES,client_ids(parent)):
                db.add(GrvtMainnetLeg(client_order_id=cid,parent_client_order_id=parent,role=role))
            db.flush(); result=verified_mainnet_bracket(db,row); db.commit(); return result
        except Exception:
            db.rollback()
            raise GrvtMainnetError('No se pudo reservar el intento MAINNET') from None


def stop_mainnet_subaccount(vault,sub: str) -> dict:
    if (not isinstance(sub,str) or not re.fullmatch(r'[0-9]{1,20}',sub)
            or not 0<int(sub)<2**64 or str(int(sub))!=sub):
        raise GrvtMainnetError('Subcuenta MAINNET inválida')
    with vault.session() as db:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        control=_control(db,sub,create=True); control.stopped=True; control.revision+=1
        from app.live_execution.grvt_mainnet_stop_origin import record_stop
        record_stop(db,control,'MANUAL')
        db.commit()
        return {'network':'MAINNET','stopped':True,'revision':control.revision,'authorization':False}


def verify_all_mainnet_brackets(db) -> None:
    require_mainnet_schema(db)
    from app.live_execution.grvt_mainnet_finance import finance_schema,verified_settlement_chain
    from app.portfolio.models import GrvtMainnetSettlementControl
    financial=finance_schema(db)
    from app.live_execution.grvt_mainnet_recovery import recovery_schema
    recovery_schema(db)
    for control in db.scalars(select(GrvtMainnetControl)): _control(db,control.sub_account_id)
    for row in db.scalars(select(GrvtMainnetBracket)): verified_mainnet_bracket(db,row)
    for leg in db.scalars(select(GrvtMainnetLeg)):
        if db.get(GrvtMainnetBracket,leg.parent_client_order_id) is None: raise GrvtMainnetError('Reserva MAINNET huérfana')
    if financial:
        for control in db.scalars(select(GrvtMainnetSettlementControl)):
            verified_settlement_chain(db,control.sub_account_id)
