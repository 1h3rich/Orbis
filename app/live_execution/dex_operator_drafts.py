"""Ephemeral, newly generated DEX operators and opt-in encrypted recovery files.

Never accepts an owner key or signs/sends a transaction. Expensive key-file work
holds neither vault nor draft locks. All results revalidate the original session.
"""
from contextlib import contextmanager
from dataclasses import dataclass,field
from datetime import datetime,timedelta
import hashlib,hmac,json,re,time
from threading import RLock,BoundedSemaphore,Timer
from uuid import UUID
from weakref import ref
from typing import Any,Callable,Iterator

from eth_account import Account

from app.live_execution.dex_artifact import DexExecutorError,_owner
from app.live_execution.dex_permission import MAX_PERMISSION_SECONDS
from app.portfolio.models import RealAccount
from app.portfolio.vault import RealVault,VaultError
from app.portfolio.wallet_funding import _time

SCHEMA='orbis.dex.operator.backup.v1'
KDF_N=262144


@dataclass(slots=True,repr=False)
class _Draft:
    identifier:str
    account_id:str
    owner:str=field(repr=False)
    address:str=field(repr=False)
    key:bytearray=field(repr=False)
    generation:int
    expires_at:datetime
    deadline:float
    ttl_seconds:int
    backup_verified:bool=False
    restore_digest:str|None=None


class DexOperatorDrafts:
    def __init__(self,vault:RealVault,*,monotonic:Callable[[],float]=time.monotonic)->None:
        self.vault=vault;self._clock=monotonic;self._lock=RLock()
        self._kdf=BoundedSemaphore(1);self._draft:_Draft|None=None;self._timer:Timer|None=None;self._revision=0;self._used_ids:set[str]=set()
        self._attached_signer=None

    def revoke(self)->None:
        with self._lock:
            self._revision+=1
            if self._timer is not None:self._timer.cancel();self._timer=None
            if self._draft is not None:self._draft.key[:]=bytes(len(self._draft.key))
            self._draft=None
            attached=self._attached_signer
            signer=attached[0]() if attached is not None else None
            self._attached_signer=None
            if signer is not None:signer.revoke_revision(attached[1])

    def _remember_signer(self,draft:_Draft,session)->None:
        """Called with the draft lock held by the checked binding integration."""
        from app.live_execution.dex_operator import DexOperatorSession
        if (self._draft is not draft or not draft.backup_verified or type(session) is not DexOperatorSession
                or session.vault is not self.vault):raise DexExecutorError('La firma no corresponde a esta preparación')
        prior=self._attached_signer[0]() if self._attached_signer is not None else None
        if prior is not None and prior is not session and prior.status()['connected']:
            raise DexExecutorError('Esta preparación ya tiene otra sesión de firma')
        self._attached_signer=(ref(session),session.permission_revision)

    def _revoke_generation(self,generation:int|None)->None:
        with self._lock:
            if self._draft is not None and self._draft.generation==generation:self.revoke()

    def _expire(self,draft:_Draft)->None:
        with self._lock:
            if self._draft is draft:self.revoke()

    @staticmethod
    def _empty()->dict[str,Any]:
        return {'exists':False,'credential_storage':'NONE','backup_verified':False,'automatic_execution':False}

    def _public(self,draft:_Draft)->dict[str,Any]:
        return {'exists':True,'id':draft.identifier,'account_id':draft.account_id,
            'operator_address':draft.address,'expires_at':draft.expires_at.isoformat(),
            'remaining_seconds':max(0,int(draft.deadline-self._clock())),
            'credential_storage':'MEMORY_ONLY','backup_verified':draft.backup_verified,'automatic_execution':False}

    def _live(self,generation:int|None,now:datetime)->_Draft|None:
        draft=self._draft
        if draft is not None and (draft.generation!=generation or now>=draft.expires_at or self._clock()>=draft.deadline):
            self.revoke();draft=None
        return draft

    def _match(self,identifier:str,account:str,wallet:str,generation:int|None,now:datetime)->_Draft:
        draft=self._live(generation,now)
        if draft is None or (draft.identifier,draft.account_id,draft.owner)!=(str(UUID(identifier)),account,wallet):
            raise DexExecutorError('La preparación de la operadora cambió o caducó')
        return draft

    def create(self,*,identifier:str,account_id:str,wallet_address:str,now:datetime,ttl_seconds:int=900)->dict[str,Any]:
        now=_time(now);identifier,account_id=str(UUID(identifier)),str(UUID(account_id));owner=_owner(wallet_address)
        if type(ttl_seconds) is not int or not 1<=ttl_seconds<=MAX_PERMISSION_SECONDS:raise DexExecutorError('Vigencia de operadora no válida')
        generation=self.vault.session_generation
        try:
            with self.vault.guard_session_generation(generation),self._lock:
                draft=self._live(generation,now)
                if draft is not None:
                    if (draft.identifier,draft.account_id,draft.owner,draft.ttl_seconds)!=(identifier,account_id,owner,ttl_seconds):
                        raise DexExecutorError('Conserva o retira la operadora en preparación antes de crear otra')
                    return self._public(draft)
                with self.vault.session() as db:
                    if db.get(RealAccount,account_id) is None:raise DexExecutorError('Cuenta REAL no encontrada')
                if identifier in self._used_ids:raise DexExecutorError('Este intento ya caducó o se retiró; usa uno nuevo')
                if len(self._used_ids)>=1000:raise DexExecutorError('Límite de preparaciones de esta sesión alcanzado')
                generated=Account.create()
                address=_owner(generated.address)
                if address==owner:raise DexExecutorError('La operadora debe ser distinta de la wallet propietaria')
                draft=_Draft(identifier,account_id,owner,address,bytearray(generated.key),generation,
                    now+timedelta(seconds=ttl_seconds),self._clock()+ttl_seconds,ttl_seconds)
                self._used_ids.add(identifier);self._revision+=1;self._draft=draft;self._timer=Timer(ttl_seconds,self._expire,args=(draft,));self._timer.daemon=True
                try:self._timer.start()
                except Exception:self.revoke();raise DexExecutorError('No se pudo iniciar la caducidad de la operadora') from None
                return self._public(draft)
        except VaultError:
            self._revoke_generation(generation);raise DexExecutorError('La bóveda debe permanecer desbloqueada') from None

    def status(self,*,account_id:str,wallet_address:str,now:datetime)->dict[str,Any]:
        now=_time(now);account_id=str(UUID(account_id));owner=_owner(wallet_address)
        with self._lock:prior=self._draft
        generation=self.vault.session_generation
        if generation is None:
            with self._lock:
                if prior is not None and self._draft is prior:self.revoke()
            return self._empty()
        try:
            with self.vault.guard_session_generation(generation),self._lock:
                draft=self._live(generation,now)
                if draft is None or (draft.account_id,draft.owner)!=(account_id,owner):return self._empty()
                return self._public(draft)
        except VaultError:self._revoke_generation(generation);return self._empty()

    @contextmanager
    def _copy(self,identifier:str,account_id:str,wallet_address:str,now:datetime)->Iterator[tuple[_Draft,bytearray]]:
        account_id=str(UUID(account_id));owner=_owner(wallet_address);generation=self.vault.session_generation
        try:
            with self.vault.guard_session_generation(generation),self._lock:
                draft=self._match(identifier,account_id,owner,generation,_time(now));key=bytearray(draft.key)
            try:yield draft,key
            finally:key[:]=bytes(len(key))
        except VaultError:
            self._revoke_generation(generation);raise DexExecutorError('La sesión REAL cambió; operadora retirada') from None

    def require_verified(self,*,identifier:str,account_id:str,wallet_address:str,now:datetime)->str:
        """Resolve a funding recipient from this live preparation, without keys.

        This is a server-side check, not a client-supplied backup confirmation.
        Call again immediately before granting the owner-wallet request.
        """
        generation=self.vault.session_generation
        try:
            with self.vault.guard_session_generation(generation),self._lock:
                draft=self._match(identifier,str(UUID(account_id)),_owner(wallet_address),generation,_time(now))
                if not draft.backup_verified:raise DexExecutorError('Comprueba la copia cifrada antes de financiar la operadora')
                return draft.address
        except VaultError:
            self._revoke_generation(generation);raise DexExecutorError('La sesión REAL cambió; operadora retirada') from None

    @contextmanager
    def _crypto(self,password:str)->Iterator[None]:
        if type(password) is not str or not 12<=len(password)<=256 or password.isspace():
            raise DexExecutorError('Usa una contraseña de copia de 12 a 256 caracteres')
        if not self._kdf.acquire(blocking=False):raise DexExecutorError('Hay otra copia cifrada en proceso')
        try:yield
        finally:self._kdf.release()

    def _finish(self,draft:_Draft,*,verified:bool=False,now:datetime)->dict[str,Any]:
        generation=self.vault.session_generation
        try:
            with self.vault.guard_session_generation(generation),self._lock:
                if self._draft is not draft or self._live(generation,_time(now)) is not draft:
                    raise DexExecutorError('La preparación de la operadora cambió durante la copia')
                if verified:draft.backup_verified=True
                return self._public(draft)
        except VaultError:
            self._revoke_generation(generation);raise DexExecutorError('La sesión REAL cambió; copia descartada') from None

    def export_backup(self,*,identifier:str,account_id:str,wallet_address:str,password:str,now:datetime)->dict[str,Any]:
        with self._copy(identifier,account_id,wallet_address,now) as (draft,key),self._crypto(password):
            keyfile=Account.encrypt(bytes(key),password,kdf='scrypt',iterations=KDF_N)
            self._finish(draft,now=datetime.now(now.tzinfo))
            return {'schema':SCHEMA,'chain_id':'0x38','draft_id':draft.identifier,'keyfile':keyfile,
                'wallet_binding':self._binding(key,draft.owner)}

    @staticmethod
    def _validate_backup(backup:object)->dict[str,Any]:
        try:
            if not isinstance(backup,dict) or set(backup)!={'schema','chain_id','draft_id','keyfile','wallet_binding'}:
                raise ValueError()
            if backup['schema']!=SCHEMA or backup['chain_id']!='0x38':raise ValueError()
            UUID(backup['draft_id'])
            if not isinstance(backup['wallet_binding'],str) or not re.fullmatch('[0-9a-f]{64}',backup['wallet_binding']):raise ValueError()
            f=backup['keyfile']
            if not isinstance(f,dict) or set(f)!={'address','crypto','id','version'} or type(f['version']) is not int or f['version']!=3:raise ValueError()
            UUID(f['id']);_owner('0x'+f['address']);c=f['crypto']
            if set(c)!={'cipher','cipherparams','ciphertext','kdf','kdfparams','mac'} or c['cipher']!='aes-128-ctr' or c['kdf']!='scrypt':raise ValueError()
            if set(c['cipherparams'])!={'iv'}:raise ValueError()
            k=c['kdfparams']
            if set(k)!={'dklen','n','p','r','salt'} or any(type(k[name]) is not int or k[name]!=value for name,value in [('dklen',32),('n',KDF_N),('p',1),('r',8)]):raise ValueError()
            for value,length in [(c['cipherparams']['iv'],32),(c['ciphertext'],64),(c['mac'],64),(k['salt'],32)]:
                if not isinstance(value,str) or not re.fullmatch('[0-9a-fA-F]{'+str(length)+'}',value):raise ValueError()
            return f
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
            raise DexExecutorError('El archivo no es una copia de operadora Orbis compatible') from None

    @staticmethod
    def _binding(key:bytes|bytearray,owner:str)->str:
        return hmac.new(key,('orbis-dex-operator-owner-v1:0x38:'+owner).encode('ascii'),hashlib.sha256).hexdigest()

    def verify_backup(self,*,identifier:str,account_id:str,wallet_address:str,backup:object,password:str,now:datetime)->dict[str,Any]:
        keyfile=self._validate_backup(backup)
        with self._copy(identifier,account_id,wallet_address,now) as (draft,key),self._crypto(password):
            if backup['draft_id']!=draft.identifier or _owner('0x'+keyfile['address'])!=draft.address:
                raise DexExecutorError('La copia pertenece a otra operadora')
            recovered=None
            try:
                recovered=bytearray(Account.decrypt(keyfile,password))
                if not hmac.compare_digest(recovered,key) or not hmac.compare_digest(self._binding(recovered,draft.owner),backup['wallet_binding']):raise ValueError()
                return self._finish(draft,verified=True,now=datetime.now(now.tzinfo))
            except ValueError:raise DexExecutorError('No se pudo comprobar la contraseña o la copia de la operadora') from None
            finally:
                if recovered is not None:recovered[:]=bytes(len(recovered))


    def restore_backup(self,*,identifier:str,account_id:str,wallet_address:str,backup:object,password:str,now:datetime,ttl_seconds:int=900)->dict[str,Any]:
        now=_time(now);identifier,account_id=str(UUID(identifier)),str(UUID(account_id));owner=_owner(wallet_address)
        if type(ttl_seconds) is not int or not 1<=ttl_seconds<=MAX_PERMISSION_SECONDS:raise DexExecutorError('Vigencia de operadora no válida')
        keyfile=self._validate_backup(backup);address=_owner('0x'+keyfile['address'])
        if address==owner:raise DexExecutorError('La copia no puede corresponder a la wallet propietaria')
        digest=hashlib.sha256(json.dumps(backup,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        generation=self.vault.session_generation;key=None
        try:
            with self.vault.guard_session_generation(generation),self._lock:
                existing=self._live(generation,now);revision=self._revision
                if existing is not None and (existing.identifier,existing.account_id,existing.owner,existing.ttl_seconds,existing.restore_digest)!=(identifier,account_id,owner,ttl_seconds,digest):
                    raise DexExecutorError('Retira la preparación existente antes de recuperar otra operadora')
                if existing is None and (identifier in self._used_ids or len(self._used_ids)>=1000):
                    raise DexExecutorError('Este intento ya terminó; recupera con un identificador nuevo')
                with self.vault.session() as db:
                    if db.get(RealAccount,account_id) is None:raise DexExecutorError('Cuenta REAL no encontrada')
            with self._crypto(password):
                try:key=bytearray(Account.decrypt(keyfile,password))
                except ValueError:raise DexExecutorError('No se pudo descifrar la copia de la operadora') from None
                if (_owner(Account.from_key(bytes(key)).address)!=address
                        or not hmac.compare_digest(self._binding(key,owner),backup['wallet_binding'])):
                    raise DexExecutorError('La copia no corresponde a esta wallet propietaria')
                current=datetime.now(now.tzinfo)
                with self.vault.guard_session_generation(generation),self._lock:
                    if self._revision!=revision or self._live(generation,current) is not existing:
                        raise DexExecutorError('La preparación cambió durante la recuperación')
                    if existing is not None:return self._public(existing)
                    draft=_Draft(identifier,account_id,owner,address,bytearray(key),generation,
                        current+timedelta(seconds=ttl_seconds),self._clock()+ttl_seconds,ttl_seconds,True,digest)
                    self._used_ids.add(identifier);self._revision+=1;self._draft=draft;self._timer=Timer(ttl_seconds,self._expire,args=(draft,));self._timer.daemon=True
                    try:self._timer.start()
                    except Exception:self.revoke();raise DexExecutorError('No se pudo iniciar la caducidad de la operadora') from None
                    return self._public(draft)
        except VaultError:
            self._revoke_generation(generation);raise DexExecutorError('La sesión REAL cambió durante la recuperación') from None
        finally:
            if key is not None:key[:]=bytes(len(key))


    def discard(self,*,identifier:str,account_id:str,wallet_address:str,now:datetime)->dict[str,Any]:
        generation=self.vault.session_generation;account_id=str(UUID(account_id));owner=_owner(wallet_address)
        try:
            with self.vault.guard_session_generation(generation),self._lock:
                draft=self._live(generation,_time(now))
                if draft is not None:self._match(identifier,account_id,owner,generation,_time(now))
                self.revoke();return self._empty()
        except VaultError:
            self._revoke_generation(generation);raise DexExecutorError('La sesión REAL cambió') from None
