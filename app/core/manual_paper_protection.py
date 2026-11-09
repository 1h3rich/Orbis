"""User-requested offline protection of the stable manual PAPER engine."""
from __future__ import annotations

from dataclasses import dataclass,field
from datetime import datetime,timezone
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from uuid import UUID
import hashlib,hmac,json,secrets

from app.core.storage_protection import StorageProtection,ProtectionError
from app.database.legacy_controller import LegacyPaperRuntime
from app.database.legacy_migration import encrypt_active_legacy_database
from app.database.legacy_runtime import legacy_database_format


SCHEMA='orbis.manual.paper.protection.v1'
def now() -> str:return datetime.now(timezone.utc).isoformat()


@dataclass(repr=False)
class _Pending:
    identifier: str
    identity: tuple[int,int]
    owner: object
    passphrase: str|None=field(repr=False)


class ManualPaperProtection:
    def __init__(self,runtime: LegacyPaperRuntime|None,shared: StorageProtection,*,
                 readiness: Callable[[],str|None],forbidden_paths: tuple[Path,...]=(),migrate=None):
        self.runtime=runtime;self.shared=shared;self.readiness=readiness
        self.forbidden=tuple(Path(path).expanduser().absolute() for path in forbidden_paths)
        self.migrate=migrate or encrypt_active_legacy_database
        self._lock=RLock();self._pending=None;self._online=False;self._shutdown=None
        self._jobs={};self._digests={};self._secret=secrets.token_bytes(32)

    def attach(self,shutdown: Callable[[],None]) -> None:
        with self._lock:self._shutdown=shutdown;self._online=True

    def server_stopped(self) -> None:
        with self._lock:self._shutdown=None;self._online=False

    def _reason(self) -> str|None:
        if self.runtime is None:return 'PAPER manual no utiliza un archivo local compatible'
        if self.runtime.status().get('requires_review'):return 'Revisa el archivo PAPER antes de protegerlo'
        if legacy_database_format(self.runtime.path)!='plain':return 'PAPER manual no es un legado plano elegible'
        return self.readiness()

    def status(self) -> dict:
        with self._lock:
            try:reason=self._reason()
            except Exception:reason='No se pudo comprobar el estado de PAPER'
            return {'schema':SCHEMA,'scope':'MANUAL_PAPER_ONLY','supported':self.runtime is not None and self._shutdown is not None,
                'busy':self._pending is not None,'ready':self._shutdown is not None and reason is None and not self.shared.frozen,
                'reason':reason,'latest_job':self._public(next(reversed(self._jobs))) if self._jobs else None}

    def _public(self,identifier: str) -> dict:
        return json.loads(json.dumps(self._jobs[identifier]))

    def job(self,identifier: str) -> dict:
        try:identifier=str(UUID(identifier))
        except (ValueError,TypeError,AttributeError):raise ProtectionError('Trabajo PAPER no disponible') from None
        with self._lock:
            if identifier not in self._jobs:raise ProtectionError('Trabajo PAPER no disponible')
            return self._public(identifier)

    def queue(self,identifier: str,passphrase: str,repeated: str) -> dict:
        try:identifier=str(UUID(identifier))
        except (ValueError,TypeError,AttributeError):raise ProtectionError('Identificador PAPER no válido') from None
        if (not isinstance(passphrase,str) or not isinstance(repeated,str) or not 12<=len(passphrase)<=1024
                or passphrase!=repeated or any(char in passphrase for char in ('\0','\r','\n'))):
            raise ProtectionError('Las contraseñas PAPER deben coincidir y tener entre 12 y 1024 caracteres')
        digest=hmac.new(self._secret,json.dumps([identifier,passphrase],ensure_ascii=False).encode(),'sha256').hexdigest()
        with self._lock:
            if identifier in self._jobs:
                if not hmac.compare_digest(self._digests[identifier],digest):raise ProtectionError('El ID PAPER tiene otro contenido')
                return self._public(identifier)
            if self._shutdown is None:raise ProtectionError('Abre ORBIS con el lanzador para proteger PAPER')
            reason=self._reason()
            if reason is not None:raise ProtectionError(reason)
            owner=self.shared.reserve_external()
            try:
                path=self.runtime.path
                info=path.stat();identity=(info.st_dev,info.st_ino)
                for other in self.forbidden:
                    if other.resolve()==path.resolve() or other.exists() and (other.stat().st_dev,other.stat().st_ino)==identity:
                        raise ProtectionError('PAPER comparte el archivo de un almacén excluido')
                self._pending=_Pending(identifier,identity,owner,passphrase)
                self._digests[identifier]=digest
                self._jobs[identifier]={'schema':SCHEMA,'id':identifier,'scope':'MANUAL_PAPER_ONLY',
                    'state':'QUEUED','created_at':now(),'active_database_encrypted':False}
                return self._public(identifier)
            except Exception:
                self.shared.release_external(owner);raise

    def request_pause(self) -> None:
        with self._lock:shutdown=self._shutdown if self._pending is not None else None
        if shutdown is not None:shutdown()

    def perform_offline(self) -> bool:
        with self._lock:
            if self._pending is None:return False
            if self._online:raise ProtectionError('El servidor debe terminar antes de proteger PAPER')
            pending=self._pending;job=self._jobs[pending.identifier];job['state']='RUNNING'
        paused=False
        try:
            reason=self._reason()
            if reason is not None:raise ProtectionError(reason)
            path=self.runtime.path;info=path.stat()
            if (info.st_dev,info.st_ino)!=pending.identity:raise ProtectionError('PAPER cambió antes de cifrar')
            self.runtime.pause_offline();paused=True
            result=self.migrate(path,'CIFRAR PAPER',pending.passphrase)
            if result.get('encrypted') is not True or legacy_database_format(path)!='encrypted_or_invalid':
                raise ProtectionError('No se pudo acreditar el cifrado PAPER')
            with self._lock:
                job.update(state='SUCCEEDED',active_database_encrypted=True,backup=result['backup'])
        except Exception:
            try:encrypted=legacy_database_format(self.runtime.path)=='encrypted_or_invalid'
            except Exception:encrypted=False
            with self._lock:
                job.update(state='ENCRYPTED_REVIEW_REQUIRED' if encrypted else 'FAILED',
                    active_database_encrypted=encrypted,
                    error='No se completó la verificación PAPER. Consulta el estado y las copias antes de reintentar.')
        finally:
            pending.passphrase=None
            if paused:
                try:self.runtime.resume_offline()
                except Exception:
                    with self._lock:
                        job.update(state='REOPEN_REVIEW_REQUIRED',error='PAPER conserva una pausa pendiente. Revisa el archivo y reinicia cuando esté disponible.')
            with self._lock:
                self._pending=None;job['completed_at']=now()
                while len(self._jobs)>10:
                    first=next(iter(self._jobs));self._jobs.pop(first);self._digests.pop(first,None)
            self.shared.release_external(pending.owner)
        return True
