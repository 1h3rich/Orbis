import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.local_sqlite import ensure_private_sqlite
from app.database.legacy_runtime import (
    LegacyDatabaseError,
    consume_passphrase_fd,
    legacy_database_format,
)
from app.database.legacy_controller import LegacyPaperRuntime
from fastapi import HTTPException


DATABASE_URL = os.environ.get("ORBIS_DATABASE_URL", "sqlite:///./orbis.db")
_database_target = make_url(DATABASE_URL)
_require_encrypted = os.environ.get("ORBIS_DATABASE_REQUIRE_ENCRYPTED") == "1"
_gui_access = os.environ.get('ORBIS_DATABASE_GUI_ACCESS') == '1'
if _require_encrypted:
    if (_database_target.get_backend_name() != "sqlite" or not _database_target.database
            or _database_target.database == ":memory:" or _database_target.query.get("uri")
            or legacy_database_format(_database_target.database) not in
                (('encrypted_or_invalid','missing') if _gui_access else ('encrypted_or_invalid',))):
        raise LegacyDatabaseError(
            "Se exige PAPER cifrado: la ruta indicada debe ser una base SQLCipher existente")
if (_database_target.get_backend_name() == "sqlite" and _database_target.database
        and _database_target.database != ":memory:" and not _database_target.query.get("uri")
        and legacy_database_format(_database_target.database) == "missing" and not _gui_access):
    raise LegacyDatabaseError(
        "La nueva base PAPER debe crearse cifrada con scripts.run_orbis_local; "
        "el arranque directo no creará SQLite en claro")
if (_database_target.get_backend_name() == "sqlite" and _database_target.database
        and _database_target.database != ":memory:" and not _database_target.query.get("uri")):
    path=Path(_database_target.database).expanduser().absolute()
    others=[Path(os.environ.get(name,fallback)).expanduser().absolute() for name,fallback in (
        ('ORBIS_REAL_VAULT_PATH','~/.local/share/orbis/real-vault.db'),
        ('ORBIS_LAB_PATH','~/.local/share/orbis/lab.db'),('ORBIS_AUTOMATION_PATH','~/.local/share/orbis/automation.db'),
        ('ORBIS_SPOT_STRATEGY_PATH','~/.local/share/orbis/spot-strategy-paper.db'),
        ('ORBIS_PAPER_PERP_PATH','~/.local/share/orbis/paper-perp.db'))]
    for other in others:
        if (path.resolve()==other.resolve() or path.exists() and other.exists()
                and (path.stat().st_dev,path.stat().st_ino)==(other.stat().st_dev,other.stat().st_ino)):
            raise LegacyDatabaseError('PAPER manual no puede compartir el archivo de otro almacén')
    if legacy_database_format(path)!='missing':ensure_private_sqlite(path)


class Base(DeclarativeBase):
    """Clase base compartida por los modelos de tablas de SQLAlchemy."""
    pass


_legacy_passphrase = consume_passphrase_fd()
legacy_paper_access = None
legacy_paper_runtime = None
if (_database_target.get_backend_name() == "sqlite" and _database_target.database
        and _database_target.database != ":memory:" and not _database_target.query.get("uri")):
    if legacy_database_format(_database_target.database)=='encrypted_or_invalid' and _legacy_passphrase is None and not _gui_access:
        raise LegacyDatabaseError(
            "La base PAPER heredada está cifrada. Inicia ORBIS con scripts.run_orbis_local "
            "para introducir su contraseña sin exponerla en la línea de comandos")
    legacy_paper_runtime=LegacyPaperRuntime(DATABASE_URL,_database_target.database,
        initial_passphrase=_legacy_passphrase,allow_locked=_gui_access)
    legacy_paper_access=legacy_paper_runtime.access
    engine=legacy_paper_runtime.engine
else:
    if _legacy_passphrase is not None:
        raise LegacyDatabaseError("Se recibió una contraseña PAPER, pero la base no está cifrada")
    engine = create_engine(DATABASE_URL)
del _legacy_passphrase

def enforce_sqlite_foreign_keys(dbapi_connection, _connection_record):
    """Impide nuevas referencias huérfanas en cada conexión SQLite/SQLCipher."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone()[0] != 1:
            raise LegacyDatabaseError("SQLite no pudo activar las claves foráneas PAPER")
    finally:
        cursor.close()


if engine.dialect.name == "sqlite" and legacy_paper_runtime is None:
    # La conexión SQLCipher ya está desbloqueada por su creator antes de este
    # evento. El PRAGMA es por conexión y debe ejecutarse fuera de transacción.
    event.listen(engine, "connect", enforce_sqlite_foreign_keys)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False
)

def get_db():
    """Cede una sesión por petición de FastAPI y la cierra al terminar."""
    if legacy_paper_runtime is not None:
        try:legacy_paper_runtime.require_open()
        except LegacyDatabaseError:
            engine.dispose()
            raise HTTPException(status_code=423,detail='PAPER manual cerrado; revisa Ajustes') from None
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()
