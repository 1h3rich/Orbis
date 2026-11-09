"""Stable local PAPER engine with explicit file mode and GUI access lifecycle.

No encrypted failure falls back to plaintext. Offline replacement is allowed
only after the hosting server drained its requests and disposed this pool.
The engine identity stays fixed for modules which already imported it.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import RLock
import os

import sqlcipher3
from sqlalchemy import create_engine,event
from sqlalchemy.engine import Engine

from app.database.legacy_lease import acquire_legacy_runtime_lease
from app.database.legacy_runtime import (
    LegacyDatabaseError,LegacyPaperAccess,_key,create_encrypted_legacy_engine,
    create_new_encrypted_legacy_database,legacy_database_format,
)


def enable_foreign_keys(dbapi_connection,_connection_record) -> None:
    cursor=dbapi_connection.cursor()
    try:
        cursor.execute('PRAGMA foreign_keys=ON')
        if cursor.execute('PRAGMA foreign_keys').fetchone()[0]!=1:
            raise LegacyDatabaseError('No se pudieron activar las claves foráneas PAPER')
    finally:cursor.close()


class LegacyPaperRuntime:
    def __init__(self,url: str,path: Path,*,initial_passphrase: str|None=None,allow_locked: bool=False):
        self.path=Path(path).expanduser().absolute()
        self._lock=RLock();self._paused=False;self._closed=False;self._creating=False
        self._format=legacy_database_format(self.path)
        if self._format not in ('plain','encrypted_or_invalid','missing'):
            raise LegacyDatabaseError('El archivo PAPER requiere revisión antes de abrirlo')
        if self._format=='missing' and not allow_locked:
            raise LegacyDatabaseError('El arranque directo no creará SQLite en claro')
        if self._format=='plain' and initial_passphrase is not None:
            raise LegacyDatabaseError('La contraseña PAPER no corresponde a una base cifrada')
        if self.path.parent.is_symlink():
            raise LegacyDatabaseError('La carpeta PAPER no puede ser un enlace')
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.access=LegacyPaperAccess(self.path)
        if self._format=='encrypted_or_invalid':
            if initial_passphrase is not None:self.access.unlock(initial_passphrase)
            elif not allow_locked:raise LegacyDatabaseError('PAPER cifrado requiere desbloqueo local')
        self._lease=acquire_legacy_runtime_lease(self.path)
        try:
            # SQLCipher handles the explicitly accepted existing plaintext mode
            # too, so the DB-API/dialect remains the same across a replacement.
            self.engine=create_engine(url,module=sqlcipher3,creator=self._connect)
            event.listen(self.engine,'connect',enable_foreign_keys)
            event.listen(self.engine,'connect',self._connected)
            event.listen(self.engine,'checkout',self._checkout)
        except Exception:
            os.close(self._lease);self._lease=None
            raise

    def require_open(self) -> None:
        with self._lock:
            if self._closed or self._paused or self._format=='missing':
                raise LegacyDatabaseError('PAPER manual no está disponible; revisa Ajustes')
            if self._format=='encrypted_or_invalid' and self.access.get() is None:
                raise LegacyDatabaseError('PAPER manual cifrado bloqueado')

    def _connect(self):
        with self._lock:
            self.require_open()
            if legacy_database_format(self.path)!=self._format:
                raise LegacyDatabaseError('El archivo PAPER cambió; no se abrirá con otro formato')
            key=self.access.get() if self._format=='encrypted_or_invalid' else None
            db=sqlcipher3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=30,check_same_thread=False)
            try:
                if self._format=='encrypted_or_invalid':
                    if key is None:raise LegacyDatabaseError('PAPER manual cifrado bloqueado')
                    _key(db,key)
                db.execute('SELECT count(*) FROM sqlite_master').fetchone()
                return db
            except Exception:
                db.close();raise

    def _connected(self,dbapi_connection,connection_record) -> None:
        info=self.path.stat()
        connection_record.info['orbis_legacy_inode']=(info.st_dev,info.st_ino)

    def _checkout(self,dbapi_connection,connection_record,proxy) -> None:
        # Also guards direct engine users and already pooled connections.
        with self._lock:
            self.require_open()
            info=self.path.stat()
            if (legacy_database_format(self.path)!=self._format
                    or connection_record.info.get('orbis_legacy_inode')!=(info.st_dev,info.st_ino)):
                raise LegacyDatabaseError('El archivo PAPER cambió; revisa el almacén antes de operar')

    def status(self) -> dict:
        with self._lock:
            if self._creating:
                return {'format':self._format,'unlocked':False,'remaining_seconds':0,
                    'maintenance':self._paused,'creating':True,'gui_access':True}
            current=legacy_database_format(self.path)
            if current!=self._format:
                self.access.lock();self.engine.dispose()
                return {'format':current,'unlocked':False,'remaining_seconds':0,
                    'maintenance':self._paused,'gui_access':True,'requires_review':True}
            available=not self._paused and not self._closed
            unlocked=available and (self._format=='plain' or self._format=='encrypted_or_invalid' and self.access.get() is not None)
            state=self.access.status() if self._format=='encrypted_or_invalid' else {
                'format':self._format,'unlocked':unlocked,'remaining_seconds':0}
            if not unlocked:self.engine.dispose()
            return {**state,'unlocked':bool(unlocked),'maintenance':self._paused,'creating':self._creating,'gui_access':True}

    def lock(self) -> dict:
        # Revocation does not wait for an in-flight schema/connection check.
        self.access.lock();self.engine.dispose()
        return self.status()

    def unlock(self,passphrase: str,*,initialize: Callable[[Engine],None]|None=None) -> dict:
        with self._lock:
            if self._paused or self._closed or self._format!='encrypted_or_invalid':
                raise LegacyDatabaseError('PAPER manual no está cifrado o está en mantenimiento')
        attempt=self.access.reserve_unlock()
        self.engine.dispose()
        self.access.unlock(passphrase,attempt=attempt)
        try:
            if initialize is not None:initialize(self.engine)
            self.access.require_attempt(attempt)
            self.require_open()
            return self.status()
        except Exception:
            self.access.discard_attempt(attempt);self.engine.dispose();raise

    def create(self,passphrase: str,*,initialize: Callable[[Engine],None]) -> dict:
        with self._lock:
            if self._paused or self._closed or self._creating or self._format!='missing':
                raise LegacyDatabaseError('PAPER ya existe; no se sobrescribirá')
            self._creating=True
        attempt=self.access.reserve_unlock()
        def staged(path: Path,key: str) -> None:
            engine=create_encrypted_legacy_engine('sqlite:///'+str(path),path,key)
            event.listen(engine,'connect',enable_foreign_keys)
            try:initialize(engine)
            finally:engine.dispose()
        try:
            create_new_encrypted_legacy_database(self.path,passphrase,initialize=staged)
        finally:
            with self._lock:
                # Creation never turns a pre-existing encrypted mode into
                # plaintext, including a disputed or uncertain publication.
                current=legacy_database_format(self.path)
                if current=='encrypted_or_invalid':self._format=current
                self._creating=False
        self.access.unlock(passphrase,attempt=attempt)
        self.engine.dispose()
        return self.status()

    def pause_offline(self) -> None:
        """Host must first drain HTTP; no live request is cut off here."""
        self.access.lock()
        with self._lock:
            if self._closed or self._paused:raise LegacyDatabaseError('PAPER no admite otra pausa')
            if getattr(self.engine.pool,'checkedout',lambda:0)()!=0:
                raise LegacyDatabaseError('PAPER conserva conexiones en uso; espera a que terminen')
            self._paused=True;self.engine.dispose()
            if self._lease is not None:os.close(self._lease);self._lease=None

    def resume_offline(self) -> None:
        with self._lock:
            if self._closed or not self._paused:raise LegacyDatabaseError('PAPER no estaba pausado')
            current=legacy_database_format(self.path)
            if current not in ('plain','encrypted_or_invalid') or self._format=='encrypted_or_invalid' and current!='encrypted_or_invalid':
                raise LegacyDatabaseError('El formato PAPER cambió de forma incompatible')
            lease=acquire_legacy_runtime_lease(self.path)
            self._format=current;self._lease=lease;self._paused=False
            self.access.lock();self.engine.dispose()

    def close(self) -> None:
        self.access.lock()
        with self._lock:
            self._closed=True;self.engine.dispose()
            if self._lease is not None:os.close(self._lease);self._lease=None
