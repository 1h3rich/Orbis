"""User-requested protection of four legacy stores, while HTTP is stopped.

The REAL vault and the manual PAPER engine are deliberately outside this job.
No password, plaintext digest, database path or account value is public output.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
from threading import RLock
from typing import Literal
from uuid import UUID

from app.lab.access import lab_format


Target = Literal['LAB','DCA','SPOT','GRVT']
LABELS = {'LAB':'Laboratorio','DCA':'DCA','SPOT':'Spot PAPER','GRVT':'GRVT PAPER'}


class ProtectionError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def active_plain_sessions(paths: Mapping[str,Path]) -> bool:
    """Read only; never creates/migrates a file or opens encrypted storage."""
    for name,table in (('DCA','paper_dca_sessions'),('SPOT','sessions'),('GRVT','paper_signal_sessions')):
        path=paths[name]
        if lab_format(path)!='plain':
            continue
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                if db.execute(f"SELECT 1 FROM {table} WHERE status='ACTIVE' LIMIT 1").fetchone():
                    return True
    return False


@dataclass
class _Pending:
    identifier: str
    targets: tuple[str,...]
    identities: dict[str,tuple[int,int]]
    passphrase: str | None = field(repr=False)


class StorageProtection:
    def __init__(self, paths: Mapping[str,Path], *, forbidden_paths: tuple[Path,...]=(),
                 readiness: Callable[[],str|None] | None=None,
                 migrate: Callable[[str,Path,str],dict] | None=None):
        if set(paths)!=set(LABELS):
            raise ProtectionError('Configuración de almacenes no válida')
        self.paths={name:Path(path).expanduser().absolute() for name,path in paths.items()}
        self.forbidden_paths=tuple(Path(p).expanduser().absolute() for p in forbidden_paths)
        self._readiness=readiness or (lambda:'La pausa del servicio aún no está configurada')
        self._migrate=migrate or self._migrate_store
        self._lock=RLock()
        self._secret=secrets.token_bytes(32)
        self._shutdown: Callable[[],None] | None=None
        self._online=False
        self._pending: _Pending | None=None
        self._jobs: dict[str,dict]={}
        self._digests: dict[str,str]={}
        self._frozen=False
        self._external_owner: object|None=None

    def set_readiness(self, callback: Callable[[],str|None]) -> None:
        self._readiness=callback

    def attach(self, shutdown: Callable[[],None]) -> None:
        with self._lock:
            self._shutdown=shutdown;self._online=True

    def server_stopped(self) -> None:
        with self._lock:
            self._shutdown=None;self._online=False

    @property
    def frozen(self) -> bool:
        with self._lock:return self._frozen or self._external_owner is not None

    def reserve_external(self) -> object:
        """Share only the maintenance slot; no extra store enters this batch."""
        with self._lock:
            if self._frozen or self._external_owner is not None:
                raise ProtectionError('Ya hay una protección en curso')
            owner=object();self._external_owner=owner
            return owner

    def release_external(self,owner: object) -> None:
        with self._lock:
            if self._external_owner is owner:self._external_owner=None

    def _files(self) -> list[dict]:
        result=[]
        for name,path in self.paths.items():
            kind=lab_format(path)
            result.append({'target':name,'label':LABELS[name],'format':kind,'eligible':kind=='plain'})
        return result

    def status(self) -> dict:
        with self._lock:
            reason=None
            try:
                files=self._files()
                if not self._frozen:reason=self._readiness()
            except (OSError,ValueError,sqlite3.Error):
                files=[];reason='No se pudieron comprobar los almacenes'
            busy=self._frozen or self._external_owner is not None
            return {'supported':self._shutdown is not None,'busy':busy,'stores':files,
                'ready':self._shutdown is not None and not busy and reason is None,
                'reason':reason,'latest_job':self._public(next(reversed(self._jobs))) if self._jobs else None,
                'scope':'LAB_DCA_SPOT_GRVT_ONLY'}

    def _public(self, identifier: str) -> dict:
        return json.loads(json.dumps(self._jobs[identifier]))

    def job(self, identifier: str) -> dict:
        try:identifier=str(UUID(identifier))
        except (ValueError,TypeError,AttributeError):raise ProtectionError('Trabajo no disponible') from None
        with self._lock:
            if identifier not in self._jobs:raise ProtectionError('Trabajo no disponible')
            return self._public(identifier)

    def queue(self, identifier: str, targets: list[str], passphrase: str, repeated: str) -> dict:
        try:identifier=str(UUID(identifier))
        except (ValueError,TypeError,AttributeError):raise ProtectionError('Identificador no válido') from None
        if (type(targets) is not list or not 1<=len(targets)<=4
                or any(type(t) is not str or t not in LABELS for t in targets)
                or len(set(targets))!=len(targets)):
            raise ProtectionError('Elige almacenes compatibles sin duplicarlos')
        if (not isinstance(passphrase,str) or not isinstance(repeated,str)
                or not 12<=len(passphrase)<=1024 or passphrase!=repeated
                or any(c in passphrase for c in ('\0','\r','\n'))):
            raise ProtectionError('Las contraseñas deben coincidir y tener entre 12 y 1024 caracteres')
        ordered=tuple(name for name in LABELS if name in targets)
        material=json.dumps({'id':identifier,'targets':ordered,'passphrase':passphrase},ensure_ascii=False).encode()
        digest=hmac.new(self._secret,material,hashlib.sha256).hexdigest()
        with self._lock:
            if identifier in self._jobs:
                if not hmac.compare_digest(self._digests[identifier],digest):
                    raise ProtectionError('El ID del trabajo ya tiene otro contenido')
                return self._public(identifier)
            if self._frozen or self._external_owner is not None:raise ProtectionError('Ya hay una protección en curso')
            if self._shutdown is None:raise ProtectionError('Abre ORBIS con el lanzador para proteger los datos')
            reason=self._readiness()
            if reason is not None:raise ProtectionError(reason)
            identities={};seen=set()
            forbidden={p.resolve() for p in self.forbidden_paths}
            forbidden_ids={(p.stat().st_dev,p.stat().st_ino) for p in self.forbidden_paths if p.exists()}
            for name in ordered:
                path=self.paths[name]
                if path.resolve() in forbidden or lab_format(path)!='plain':
                    raise ProtectionError('El almacén elegido no es un legado plano elegible')
                info=path.stat();identity=(info.st_dev,info.st_ino)
                if identity in forbidden_ids:raise ProtectionError('El archivo pertenece a un almacén excluido')
                if identity in seen:raise ProtectionError('Los almacenes elegidos comparten un archivo')
                seen.add(identity);identities[name]=identity
            # Also reject aliasing a different configured store not selected.
            for name in ordered:
                for other,path in self.paths.items():
                    if name!=other and path.exists():
                        info=path.stat()
                        if identities[name]==(info.st_dev,info.st_ino):
                            raise ProtectionError('Las rutas de los almacenes coinciden')
            self._pending=_Pending(identifier,ordered,identities,passphrase)
            self._frozen=True
            self._jobs[identifier]={'id':identifier,'state':'QUEUED','created_at':_now(),
                'targets':list(ordered),'results':[],'scope':'LAB_DCA_SPOT_GRVT_ONLY'}
            self._digests[identifier]=digest
            return self._public(identifier)

    def request_pause(self) -> None:
        with self._lock:
            callback=self._shutdown if self._pending is not None else None
        if callback is not None:callback()

    def perform_offline(self) -> bool:
        """Called ONLY after uvicorn drained lifespan and released runtime leases."""
        with self._lock:
            if self._pending is None:return False
            if self._online:raise ProtectionError('El servidor debe terminar antes de cifrar')
            pending=self._pending;self._pending=None
            result=self._jobs[pending.identifier];result['state']='RUNNING'
        try:
            # Includes real idle, ephemeral locks, and fictitious active sessions.
            reason=self._readiness()
            if reason is not None:raise ProtectionError(reason)
            for name in pending.targets:
                path=self.paths[name]
                try:
                    info=path.stat()
                    if (info.st_dev,info.st_ino)!=pending.identities[name] or lab_format(path)!='plain':
                        raise ProtectionError('El archivo cambió antes de cifrar')
                    migrated=self._migrate(name,path,pending.passphrase)
                    if migrated.get('encrypted') is not True or lab_format(path)!='encrypted_or_invalid':
                        raise ProtectionError('No se pudo acreditar el cifrado')
                    entry={'target':name,'state':'ENCRYPTED','backup':migrated['backup']}
                except Exception:
                    # Never serialize raw exception text: SQL/key errors may
                    # contain request material. No optimistic batch rollback.
                    try:kind=lab_format(path)
                    except (OSError,ValueError):kind='unavailable'
                    entry={'target':name,'state':'ENCRYPTED_REVIEW_REQUIRED' if kind=='encrypted_or_invalid' else 'FAILED',
                        'error':'No se completó la verificación. Revisa el estado y las copias antes de reintentar.'}
                    with self._lock:result['results'].append(entry)
                    break
                with self._lock:result['results'].append(entry)
            with self._lock:
                attempted={entry['target'] for entry in result['results']}
                result['results'].extend({'target':name,'state':'NOT_ATTEMPTED'} for name in pending.targets if name not in attempted)
                states=[row['state'] for row in result['results']]
                result['state']=('SUCCEEDED' if len(states)==len(pending.targets) and all(s=='ENCRYPTED' for s in states)
                    else 'PARTIAL' if any(s.startswith('ENCRYPTED') for s in states) else 'FAILED')
        except Exception:
            with self._lock:
                result['state']='FAILED';result['error']='El estado cambió antes de proteger los archivos; no se inició el lote.'
        finally:
            pending.passphrase=None
            with self._lock:
                result['completed_at']=_now();self._frozen=False
                while len(self._jobs)>10:
                    first=next(iter(self._jobs));self._jobs.pop(first);self._digests.pop(first,None)
        return True

    def _migrate_store(self, name: str, path: Path, passphrase: str) -> dict:
        if name=='LAB':
            from app.lab.migration import encrypt_active_lab
            from app.lab.store import LabStore
            return encrypt_active_lab(LabStore(path),'CIFRAR LAB',passphrase)
        if name=='DCA':
            from app.automation.migration import encrypt_active_automation
            from app.automation.store import AutomationStore
            return encrypt_active_automation(AutomationStore(path),'CIFRAR DCA',passphrase)
        if name=='SPOT':
            from app.automation.spot_paper_migration import encrypt_active_spot_paper
            from app.automation.spot_strategy_paper import SpotStrategyPaper
            return encrypt_active_spot_paper(SpotStrategyPaper(path,self.paths['LAB']),'CIFRAR SPOT PAPER',passphrase)
        if name=='GRVT':
            from app.derivatives.paper_migration import encrypt_active_paper_perp
            from app.derivatives.paper_store import PaperPerpStore
            return encrypt_active_paper_perp(PaperPerpStore(path),'CIFRAR GRVT PAPER',passphrase)
        raise ProtectionError('Almacén no compatible')
