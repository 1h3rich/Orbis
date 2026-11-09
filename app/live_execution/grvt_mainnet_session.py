"""Sesión MAINNET revocable sólo en RAM y revisiones de orden de un solo uso."""

from dataclasses import dataclass,field,asdict
from decimal import Decimal
import hashlib
import hmac
import json
import secrets
from threading import RLock
import time

from eth_account import Account
from sqlalchemy import select,func

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError,MainnetBracketDraft,unsigned_mainnet_bracket
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient,fetch_mainnet_instrument,public_post
from app.live_execution.grvt_mainnet_preflight import collect_mainnet_preflight,canonical
from app.live_execution.grvt_mainnet_journal import (
    _control, encode, read_mainnet_bracket, require_new_mainnet_bracket, stop_mainnet_subaccount,
)
from app.live_execution.grvt_mainnet_submit import submit_mainnet_bracket_once
from app.portfolio.models import GrvtMainnetBracket,GrvtMainnetControl


@dataclass(repr=False)
class _Connection:
    owner: str
    generation: int
    sub: str
    api_key: str=field(repr=False)
    private_key: bytes|None=field(repr=False)
    signer: str=field(repr=False)
    account_ref: str
    deadline: float
    review: dict|None=None
    review_capability: object|None=field(default=None,repr=False)


class GrvtMainnetSession:
    def __init__(self,vault,*,client_factory=None,public=None,clock_ns=None,waiter=None):
        self.vault=vault;self.factory=client_factory or GrvtMainnetClient
        self.public=public or public_post;self.clock=clock_ns or time.time_ns
        self._lock=RLock();self._salt=secrets.token_hex(32);self._mac_key=secrets.token_bytes(32)
        self._epoch=0;self._revisions={};self._connections={};self._jobs={}
        self.waiter=waiter or (lambda stop,seconds:stop.wait(seconds))
        self.external_status=lambda sub:None

    def _owner(self,token):return hmac.new(self._mac_key,token.encode('ascii'),hashlib.sha256).hexdigest()

    def ticket(self,sub):
        with self._lock:return self._epoch,self._revisions.get(sub,0)

    def _require_ticket(self,sub,ticket):
        if self.ticket(sub)!=tuple(ticket):raise GrvtMainnetError('Permiso MAINNET revocado')

    def revoke_all(self):
        with self._lock:
            self._epoch+=1
            for connection in self._connections.values():
                connection.private_key=None;connection.api_key='';connection.review=None;connection.review_capability=None
            self._connections.clear()
            for job in self._jobs.values():job['stop'].set()

    def disconnect(self,sub,*,keep_read=False):
        with self._lock:
            self._revisions[sub]=self._revisions.get(sub,0)+1
            connection=self._connections.get(sub)
            if connection:
                connection.private_key=None;connection.review=None;connection.review_capability=None
                if not keep_read:connection.api_key='';self._connections.pop(sub,None)
            for job in self._jobs.values():
                if job['sub']==sub:job['stop'].set()

    def status(self,token,sub,generation):
        with self._lock:
            connection=self._connections.get(sub)
            if connection and time.monotonic()>=connection.deadline:
                self.disconnect(sub);connection=None
            connected=bool(connection and connection.owner==self._owner(token) and connection.generation==generation)
            return {'network':'MAINNET','authorization':False,'connected':connected,
                'can_sign':bool(connected and connection.private_key is not None),
                'permission_revision':list(self.ticket(sub)),
                'connection_remaining_seconds':max(0,int(connection.deadline-time.monotonic())) if connected else 0}

    def capture(self,token,sub,revision,generation,*,sign=False):
        with self._lock:
            self._require_ticket(sub,revision);connection=self._connections.get(sub)
            if (connection is None or connection.owner!=self._owner(token) or connection.generation!=generation
                    or time.monotonic()>=connection.deadline or sign and connection.private_key is None):
                raise GrvtMainnetError('Conecta de nuevo la sesión MAINNET')
        def checked():
            with self._lock:
                self._require_ticket(sub,revision)
                if (self._connections.get(sub) is not connection or not connection.api_key
                        or time.monotonic()>=connection.deadline or sign and connection.private_key is None):
                    raise GrvtMainnetError('Sesión MAINNET retirada o vencida')
            if self.vault.session_generation!=generation:raise GrvtMainnetError('Bóveda MAINNET bloqueada')
        checked()
        def client():
            checked();result=self.factory(connection.api_key);checked()
            if (not isinstance(result,GrvtMainnetClient) or result.network!='MAINNET' or result.sub_account_id!=sub
                    or result.identity_ref(self._salt)!=connection.account_ref):raise GrvtMainnetError('Identidad MAINNET cambiada')
            return result
        return connection,checked,client

    def connect(self,token,sub,revision,generation,*,api_key,private_key,signer):
        self._require_ticket(sub,revision)
        try:
            key=bytes.fromhex(private_key.removeprefix('0x'))
            if len(key)!=32 or Account.from_key(key).address.lower()!=signer.lower():raise ValueError('signer')
            client=self.factory(api_key)
            if not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET' or client.sub_account_id!=sub:
                raise ValueError('subaccount')
            reference=client.identity_ref(self._salt)
            if self.vault.session_generation!=generation:raise ValueError('generation')
            with self._lock:
                self._require_ticket(sub,revision)
                self.disconnect(sub)
                self._connections[sub]=_Connection(self._owner(token),generation,sub,api_key,key,signer,reference,time.monotonic()+900)
            return self.status(token,sub,generation)
        except Exception:
            raise GrvtMainnetError('GRVT no confirmó la subcuenta y su firma dedicada MAINNET') from None
        finally:del private_key

    def connect_view(self, token, sub, revision, generation, *, api_key):
        """Reattach a read capability to an account already bound in the journal."""
        try:
            self._require_ticket(sub, revision)
            with self.vault.guard_session_generation(generation), self.vault.session() as db:
                control = _control(db, sub)
                if control is None or control.account_ref is None:
                    raise ValueError('no known account')
                salt, expected = control.identity_salt, control.account_ref

            client = self.factory(api_key)
            if (not isinstance(client, GrvtMainnetClient) or client.network != 'MAINNET'
                    or client.sub_account_id != sub or client.identity_ref(salt) != expected):
                raise ValueError('unexpected read account')
            summary = client.read_account('account_summary', sub)['result']
            if not isinstance(summary, dict) or summary.get('sub_account_id') != sub:
                raise ValueError('unexpected summary')

            with self.vault.guard_session_generation(generation), self.vault.session() as db:
                control = _control(db, sub)
                if control is None or (control.identity_salt, control.account_ref) != (salt, expected):
                    raise ValueError('journal binding changed')
            external = self.external_status(sub)
            if external and external['active']:
                raise ValueError('bot active')
            reference = client.identity_ref(self._salt)
            with self._lock:
                self._require_ticket(sub, revision)
                now = time.monotonic()
                current = self._connections.get(sub)
                if ((current and current.private_key is not None and current.deadline > now)
                        or any(job['sub'] == sub and job['active'] for job in self._jobs.values())):
                    raise ValueError('signing session or recovery active')
                self.disconnect(sub)
                self._connections[sub] = _Connection(
                    self._owner(token), generation, sub, api_key, None, '', reference, now + 900)
            return self.status(token, sub, generation)
        except Exception:
            raise GrvtMainnetError('GRVT no confirmó acceso View a la subcuenta registrada') from None

    def preview(self,token,draft,revision,generation):
        external=self.external_status(draft.sub_account_id)
        if external and external['active']:raise GrvtMainnetError('El bot gestiona esta subcuenta')
        connection,checked,client_factory=self.capture(token,draft.sub_account_id,revision,generation,sign=True)
        parent=str(2**63|secrets.randbits(63))
        with self.vault.guard_session_generation(generation),self.vault.session() as db:
            require_new_mainnet_bracket(db,draft.sub_account_id,parent)
        instrument=fetch_mainnet_instrument(draft.instrument,http_post=self.public);checked()
        unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=parent,nonces=(1,2,3),issued_at_ns=self.clock())
        proof=collect_mainnet_preflight(client_factory(),draft,public_post=self.public,clock_ns=lambda:(checked() or self.clock()))
        checked();now=self.clock()
        review={'schema':'orbis.grvt.mainnet.review.v1','network':'MAINNET','authorization':False,
            'parent_client_order_id':parent,'review_id':secrets.token_urlsafe(24),
            'draft':{key:format(value,'f') if isinstance(value,Decimal) else value for key,value in asdict(draft).items()},
            'instrument_digest':encode(asdict(instrument))[1],'created_at_ns':str(now),'expires_at_ns':str(now+120_000_000_000),
            'quote':{key:value for key,value in proof['quote'].items() if key not in ('observed_at_ns','event_time_ns')}}
        mac=hmac.new(self._mac_key,(connection.owner+encode(review)[0]).encode('ascii'),hashlib.sha256).hexdigest()
        with self._lock:
            self._require_ticket(draft.sub_account_id,revision)
            if self._connections.get(draft.sub_account_id) is not connection:raise GrvtMainnetError('Sesión MAINNET cambiada')
            connection.review=review
            connection.review_capability=None
        return {'review':review,'review_mac':mac}

    def send(self,token,review,mac,revision,generation):
        sub=review['draft']['sub_account_id']
        external=self.external_status(sub)
        if external and external['active']:raise GrvtMainnetError('El bot gestiona esta subcuenta')
        connection,checked,factory=self.capture(token,sub,revision,generation,sign=True)
        with self._lock:
            expected=hmac.new(self._mac_key,(connection.owner+encode(review)[0]).encode('ascii'),hashlib.sha256).hexdigest()
            if (connection.review!=review or not isinstance(mac,str) or not secrets.compare_digest(expected,mac)
                    or review.get('schema')!='orbis.grvt.mainnet.review.v1' or review.get('network')!='MAINNET'
                    or not int(review['created_at_ns'])<=self.clock()<int(review['expires_at_ns'])):
                raise GrvtMainnetError('Revisión MAINNET alterada, usada o vencida')
            connection.review=None
            connection.review_capability=None
            key=connection.private_key;signer=connection.signer
        from app.live_execution.grvt_mainnet_journal import _draft
        draft=_draft(review['draft'])
        instrument=fetch_mainnet_instrument(draft.instrument,http_post=self.public);checked()
        if encode(asdict(instrument))[1]!=review['instrument_digest']:raise GrvtMainnetError('Metadatos MAINNET cambiados; revisa de nuevo')
        return submit_mainnet_bracket_once(self.vault,draft,parent_client_order_id=review['parent_client_order_id'],
            client_factory=factory,private_key=key,expected_signer=signer,require_permission=checked,
            confirmation='ENVIAR_ENTRADA_PROTEGIDA_GRVT_MAINNET',public_post=self.public,clock_ns=self.clock)

    def history(self,token,sub,generation,*,offset=0):
        status=self.status(token,sub,generation)
        with self.vault.try_guard_session_generation(generation) as available:
            if not available:return {**status,'groups':[],'history_pending':True,'has_more':False,'stop':None}
            with self.vault.session() as db:
                parents=list(db.scalars(select(GrvtMainnetBracket.parent_client_order_id).where(GrvtMainnetBracket.sub_account_id==sub)
                    .order_by(func.json_extract(GrvtMainnetBracket.payload_json,'$.claimed_at_ns').desc(),
                        GrvtMainnetBracket.parent_client_order_id.desc()).offset(offset).limit(26)))
                control=db.get(GrvtMainnetControl,sub)
                stop={'stopped':control.stopped,'revision':control.revision} if control else {'stopped':False,'revision':0}
            groups=[self.group(parent) for parent in parents[:25]]
        return {**status,'groups':groups,'history_pending':False,'has_more':len(parents)>25,'stop':stop}

    def group(self,parent):
        value=read_mainnet_bracket(self.vault,parent);job=self.job(parent)
        external=self.external_status(value['sub_account_id']) if value['claim']['source'] in (
            'BOT_NATIVE_PROTECTED_ENTRY','PERP_BOT_NATIVE_PROTECTED_ENTRY') else None
        if external and external['active'] and external.get('parent_client_order_id')==parent and not job['active']:
            job={'active':True,'status':external['status']}
        from app.live_execution.grvt_mainnet_view import mainnet_group_view
        return mainnet_group_view(value,job)

    def job(self,parent):
        with self._lock:
            item=self._jobs.get(parent)
            return {'active':False,'status':'IDLE'} if item is None else {key:item[key] for key in ('active','status')}

    def stop(self,sub):
        self.disconnect(sub,keep_read=True)
        return stop_mainnet_subaccount(self.vault,sub)

    def sizing(self,token,sub,revision,generation,*,instrument,direction,entry_cap,exit_cap,stop_percent,take_percent):
        from decimal import localcontext,ROUND_CEILING,ROUND_FLOOR
        from app.live_execution.grvt_mainnet_transport import TICKER_URL,_result
        _,checked,_=self.capture(token,sub,revision,generation)
        meta=fetch_mainnet_instrument(instrument,http_post=self.public);checked()
        ticker=_result(self.public(TICKER_URL,{'instrument':instrument}))['result'];at=self.clock()
        if (ticker.get('instrument')!=instrument or not isinstance(ticker.get('event_time'),str)
                or not 0<=at-int(ticker['event_time'])<=5_000_000_000):raise GrvtMainnetError('Cotización MAINNET antigua')
        with localcontext() as context:
            context.prec=100
            cap=Decimal(entry_cap);stop=Decimal(stop_percent)/100;target=Decimal(take_percent)/100
            if not 0<stop<1 or not 0<target<1:raise GrvtMainnetError('Porcentajes de protección inválidos')
            price=Decimal(ticker['best_ask_price' if direction=='LONG' else 'best_bid_price'])
            size=(cap/price).quantize(Decimal(1).scaleb(-meta.base_decimals),rounding=ROUND_FLOOR)
            sign=1 if direction=='LONG' else -1
            sl=(price*(1-sign*stop)/meta.tick_size).to_integral_value(rounding=ROUND_CEILING if sign==1 else ROUND_FLOOR)*meta.tick_size
            tp=(price*(1+sign*target)/meta.tick_size).to_integral_value(rounding=ROUND_CEILING if sign==1 else ROUND_FLOOR)*meta.tick_size
        draft=MainnetBracketDraft(sub,instrument,direction,size,price,sl,tp,cap,Decimal(exit_cap))
        unsigned_mainnet_bracket(draft,meta,parent_client_order_id=str(2**63+10),nonces=(1,2,3),issued_at_ns=at)
        checked()
        return {'network':'MAINNET','authorization':False,'draft':{key:format(value,'f') if isinstance(value,Decimal) else value
            for key,value in asdict(draft).items()},'quote_time_ns':str(at)}

    def live(self,token,parent,sub,revision,generation):
        from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
        from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
        from app.live_execution.grvt_mainnet_reduction import observe_recovery_reductions
        from app.live_execution.grvt_mainnet_view import TERMINAL_ORDER_STATES
        _,checked,factory=self.capture(token,sub,revision,generation)
        group=self.group(parent)
        if group['sub_account_id']!=sub:raise GrvtMainnetError('Orden de otra subcuenta')
        if group['result'] is not None or group['job']['active']:return group
        observed=observe_mainnet_bracket(self.vault,parent,client_factory=factory,require_permission=checked,clock_ns=self.clock)
        state=observed['state']
        if any(order['status'] not in TERMINAL_ORDER_STATES for order in state['orders']):
            return self.group(parent)
        if group['reductions']:
            if any(item['state'] is None or item['state']['status'] not in TERMINAL_ORDER_STATES
                   for item in group['reductions']):
                observe_recovery_reductions(self.vault,parent,client_factory=factory,require_permission=checked,clock_ns=self.clock)
            group=self.group(parent)
            if group['recovery']['remaining_size_from_orders'] != '0':
                return group
        elif state['needs_review'] or Decimal(state['residual_size_from_orders']) != 0:
            return self.group(parent)
        audit_mainnet_bracket(self.vault,parent,client_factory=factory,require_permission=checked,clock_ns=self.clock)
        if self.group(parent)['financial_audit_count']>=2:
            try:
                with self.vault.guard_session_generation(generation):
                    checked()
                    reconcile_mainnet_bracket(self.vault,parent,now_ns=self.clock())
            except GrvtMainnetError:
                pass
        return self.group(parent)

    def start_recovery(self,token,parent,sub,revision,generation,*,ttl_seconds,max_attempts):
        from threading import Event,Thread
        from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery,read_mainnet_recovery
        from app.live_execution.grvt_mainnet_journal import signer_ref,_control
        connection,checked,factory=self.capture(token,sub,revision,generation,sign=True)
        external=self.external_status(sub)
        if external and external['active']:raise GrvtMainnetError('Detén el bot antes de recuperar manualmente')
        saved=read_mainnet_bracket(self.vault,parent)
        if saved['sub_account_id']!=sub or saved['reconciliation'] is not None:raise GrvtMainnetError('No hay una posición pendiente de recuperar')
        with self.vault.session() as db:salt=_control(db,sub).identity_salt
        if signer_ref(salt,connection.signer)!=saved['claim']['signer_ref']:raise GrvtMainnetError('La firma no corresponde a esta entrada')
        checked()
        with self._lock:
            if self._jobs.get(parent,{}).get('active'):raise GrvtMainnetError('Cierre MAINNET ya activo')
            job={'sub':sub,'active':True,'status':'PREPARING','stop':Event()};self._jobs[parent]=job
        try:
            if saved['recovery'] is None:
                begin_mainnet_recovery(self.vault,parent,ttl_seconds=ttl_seconds,max_attempts=max_attempts,
                    confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=self.clock())
            view=read_mainnet_recovery(self.vault,parent)
            if self.clock()>=view['request']['deadline_ns']:raise GrvtMainnetError('Permiso de recuperación vencido')
            checked();key=connection.private_key;signer=connection.signer
            thread=Thread(target=self._recover,args=(parent,job,checked,factory,key,signer),
                name='orbis-mainnet-recovery-'+parent[-5:],daemon=True)
            thread.start();return self.job(parent)
        except Exception:
            with self._lock:job['active']=False;job['status']='REVIEW'
            raise

    def _recover(self,parent,job,checked,factory,key,signer):
        from app.live_execution.grvt_mainnet_recovery_runner import run_mainnet_recovery
        def status(value):
            with self._lock:job['status']=value
        try:
            run_mainnet_recovery(self.vault,parent,checked=checked,factory=factory,key=key,signer=signer,
                public=self.public,clock=self.clock,waiter=self.waiter,stop=job['stop'],on_status=status)
        finally:
            key=None
            with self._lock:job['active']=False

    def rearm(self,token,sub,revision,generation,*,stop_revision):
        from app.live_execution.grvt_mainnet_journal import _control,verified_mainnet_bracket
        _,checked,_=self.capture(token,sub,revision,generation,sign=True)
        external=self.external_status(sub)
        if external and external['active']:raise GrvtMainnetError('El bot gestiona esta subcuenta')
        with self.vault.guard_session_generation(generation),self.vault.session() as db:
            try:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE');control=_control(db,sub)
                if (control is None or not control.stopped or control.revision!=stop_revision or control.pending_parent_id is not None):
                    raise GrvtMainnetError('STOP MAINNET o recibo pendiente de revisión')
                for group in db.scalars(select(GrvtMainnetBracket).where(GrvtMainnetBracket.sub_account_id==sub)):
                    if not verified_mainnet_bracket(db,group)['financially_reconciled']:raise GrvtMainnetError('Orden MAINNET sin conciliar')
                checked();control.stopped=False;control.revision+=1
                from app.live_execution.grvt_mainnet_stop_origin import record_release
                record_release(db,control);db.commit()
            except Exception:db.rollback();raise
        with self._lock:
            self._revisions[sub]=self._revisions.get(sub,0)+1
            connection=self._connections.get(sub)
            if connection:connection.review=None;connection.review_capability=None
        return self.status(token,sub,generation)|{'stopped':False}

    def seal_bot_review(self,token,sub,revision,generation,proposal):
        return self._seal_review(token,sub,revision,generation,proposal,kind='bot')

    def seal_researched_bot_review(self,token,sub,revision,generation,proposal):
        return self._seal_review(token,sub,revision,generation,proposal,kind='bot',version=2)

    def seal_perp_bot_review(self,token,sub,revision,generation,proposal,origin):
        from app.live_execution.grvt_perp_bot_origin import VerifiedPerpBotOrigin
        if not isinstance(origin,VerifiedPerpBotOrigin):raise GrvtMainnetError('Falta el original perpetuo verificado')
        return self._seal_review(token,sub,revision,generation,proposal,kind='perp.bot',capability=origin)

    def seal_limit_review(self,token,sub,revision,generation,proposal,origin):
        from app.live_execution.grvt_perp_limit_origin import VerifiedLimitPaperOrigin, VerifiedPartialPaperOrigin
        if not isinstance(origin,(VerifiedLimitPaperOrigin,VerifiedPartialPaperOrigin)):
            raise GrvtMainnetError('Falta la prueba PAPER LIMIT')
        version = 2 if isinstance(origin,VerifiedPartialPaperOrigin) else 1
        return self._seal_review(token,sub,revision,generation,proposal,kind='perp.limit',version=version,capability=origin)

    def seal_limit_resume(self,token,sub,revision,generation,proposal,*,partial=False):
        return self._seal_review(token,sub,revision,generation,proposal,kind='perp.limit.resume',version=2 if partial else 1)

    def _seal_review(self,token,sub,revision,generation,proposal,*,kind,version=1,capability=None):
        connection,checked,_=self.capture(token,sub,revision,generation,sign=True);checked();now=self.clock()
        review={**proposal,'schema':'orbis.grvt.mainnet.'+kind+'.review.v'+str(version),'network':'MAINNET','sub_account_id':sub,
            'created_at_ns':str(now),'expires_at_ns':str(now+120_000_000_000),'review_id':secrets.token_urlsafe(24),'authorization':False}
        review=json.loads(encode(review)[0])
        mac=hmac.new(self._mac_key,(connection.owner+encode(review)[0]).encode('ascii'),hashlib.sha256).hexdigest()
        with self._lock:
            self._require_ticket(sub,revision)
            if self._connections.get(sub) is not connection:raise GrvtMainnetError('Sesión cambiada')
            connection.review=review
            connection.review_capability=capability
        return {'review':review,'review_mac':mac}

    def consume_bot_review(self,token,review,mac,revision,generation):
        return self._consume_review(token,review,mac,revision,generation,kind='bot')

    def consume_researched_bot_review(self,token,review,mac,revision,generation):
        return self._consume_review(token,review,mac,revision,generation,kind='bot',version=2)

    def consume_perp_bot_review(self,token,review,mac,revision,generation):
        from app.live_execution.grvt_perp_bot_origin import VerifiedPerpBotOrigin
        credentials,checked,origin=self._consume_review(token,review,mac,revision,generation,
            kind='perp.bot',with_capability=True)
        if not isinstance(origin,VerifiedPerpBotOrigin):raise GrvtMainnetError('Original perpetuo retirado')
        return credentials,checked,origin

    def consume_limit_review(self,token,review,mac,revision,generation,*,partial=False):
        return self._consume_review(token,review,mac,revision,generation,kind='perp.limit',
            version=2 if partial else 1,with_capability=True)

    def consume_limit_resume(self,token,review,mac,revision,generation,*,partial=False):
        return self._consume_review(token,review,mac,revision,generation,kind='perp.limit.resume',version=2 if partial else 1)

    def _consume_review(self,token,review,mac,revision,generation,*,kind,version=1,with_capability=False):
        sub=review['sub_account_id'];connection,checked,_=self.capture(token,sub,revision,generation,sign=True)
        with self._lock:
            expected=hmac.new(self._mac_key,(connection.owner+encode(review)[0]).encode('ascii'),hashlib.sha256).hexdigest()
            if (connection.review!=review or not isinstance(mac,str) or not secrets.compare_digest(expected,mac)
                    or review.get('schema')!='orbis.grvt.mainnet.'+kind+'.review.v'+str(version) or review.get('network')!='MAINNET'
                    or not int(review['created_at_ns'])<=self.clock()<int(review['expires_at_ns'])):
                raise GrvtMainnetError('Revisión usada, alterada o vencida')
            connection.review=None
            capability=connection.review_capability;connection.review_capability=None
            credentials=(connection.api_key,connection.private_key,connection.signer)
        checked()
        return (credentials,checked,capability) if with_capability else (credentials,checked)

    def preview_renewal(self,token,parent,sub,revision,generation,*,ttl_seconds,max_attempts,exit_cap=None):
        from app.live_execution.grvt_mainnet_recovery import read_mainnet_recovery
        from app.live_execution.grvt_mainnet_recovery_grants import exhausted,reviewed_exit_cap
        from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
        from app.live_execution.grvt_mainnet_reduction import observe_recovery_reductions
        from app.live_execution.grvt_mainnet_journal import signer_ref,_control
        connection,checked,factory=self.capture(token,sub,revision,generation,sign=True)
        external=self.external_status(sub)
        if self.job(parent)['active'] or external and external['active']:raise GrvtMainnetError('Detén el seguimiento antes de renovar el cierre')
        saved=read_mainnet_bracket(self.vault,parent)
        if saved['sub_account_id']!=sub or saved['reconciliation'] is not None or saved['recovery'] is None:raise GrvtMainnetError('No hay una recuperación pendiente')
        with self.vault.session() as db:salt=_control(db,sub).identity_salt
        if signer_ref(salt,connection.signer)!=saved['claim']['signer_ref']:raise GrvtMainnetError('Firma ajena a la entrada')
        observe_mainnet_bracket(self.vault,parent,client_factory=factory,require_permission=checked,clock_ns=self.clock)
        observe_recovery_reductions(self.vault,parent,client_factory=factory,require_permission=checked,clock_ns=self.clock)
        view=read_mainnet_recovery(self.vault,parent)
        selected_cap=reviewed_exit_cap(view['request'],exit_cap)
        if (not exhausted(view['request'],view['reductions'],self.clock()) and selected_cap==view['request']['exit_cap']
                or any(item['state'] is None or item['state']['status'] not in ('FILLED','CANCELLED','REJECTED') for item in view['reductions'])):
            raise GrvtMainnetError('El cierre anterior sigue vigente o tiene una IOC pendiente')
        return self._seal_review(token,sub,revision,generation,{'parent_client_order_id':parent,'request_digest':encode(view['request'])[1],
            'event_count':len(view['events']),'ttl_seconds':ttl_seconds,'max_attempts':max_attempts,
            'exit_cap':selected_cap,'previous_exit_cap':view['request']['exit_cap'],'max_spread_bps':view['request']['max_spread_bps'],
            'max_slippage_bps':view['request']['max_slippage_bps']},kind='recovery')

    def renew_recovery(self,token,parent,review,mac,revision,generation):
        from app.live_execution.grvt_mainnet_recovery_grants import renew_mainnet_recovery
        if review.get('parent_client_order_id')!=parent:raise GrvtMainnetError('Revisión de otro cierre')
        _,checked=self._consume_review(token,review,mac,revision,generation,kind='recovery')
        sub=review['sub_account_id'];external=self.external_status(sub)
        if self.job(parent)['active'] or external and external['active']:raise GrvtMainnetError('Cierre ya activo')
        renew_mainnet_recovery(self.vault,parent,expected_request_digest=review['request_digest'],expected_event_count=review['event_count'],
            ttl_seconds=review['ttl_seconds'],max_attempts=review['max_attempts'],review_id=review['review_id'],require_permission=checked,now_ns=self.clock(),
            exit_cap=review['exit_cap'] if review['exit_cap']!=review['previous_exit_cap'] else None)
        return self.start_recovery(token,parent,sub,revision,generation,ttl_seconds=review['ttl_seconds'],max_attempts=review['max_attempts'])
