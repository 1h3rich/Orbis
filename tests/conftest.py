"""Fixtures compartidos de la suite de pruebas de Orbis.

Garantías que establece este fichero (B2):

* Ningún test escribe en las bases locales del usuario: antes de importar
  ``app.main`` se fija la base SQLAlchemy en memoria y las rutas LAB,
  automatización, PAPER perpetuos y bóveda REAL a un directorio temporal.
* Cada test trabaja con un esquema recién creado (``drop_all`` + ``create_all``).
* El override de ``get_db`` se instala y se retira **por test**: no se comparte
  estado entre pruebas ni a nivel de módulo.

Las pruebas que necesitan un fichero temporal (p. ej. concurrencia con dos
conexiones) usan el fixture ``file_db_engine``.
"""

import os
from tempfile import TemporaryDirectory

# Debe fijarse ANTES de importar app.*: app.database.database lee esta variable
# en el momento de la importación.
os.environ["ORBIS_DATABASE_URL"] = "sqlite://"
_isolated_storage = TemporaryDirectory(prefix="orbis-pytest-")
for _name, _file in (("ORBIS_LAB_PATH", "lab.db"),
                     ("ORBIS_AUTOMATION_PATH", "automation.db"),
                     ("ORBIS_PAPER_PERP_PATH", "paper-perp.db"),
                     ("ORBIS_REAL_VAULT_PATH", "real-vault.db")):
    os.environ[_name] = os.path.join(_isolated_storage.name, _file)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database.database import (  # noqa: E402
    DATABASE_URL,
    Base,
    engine as app_engine,
    enforce_sqlite_foreign_keys,
    get_db,
)
from app.database.schema_check import schema_report  # noqa: E402
from app.main import app  # noqa: E402


# Comprobación de esquema AL INICIO de la suite: si los modelos y lo que crea
# `Base.metadata.create_all` no coinciden, la sesión falla listando las
# ausencias (mismo criterio que `scripts/check_schema.py`, que se puede lanzar
# contra orbis.db para ver los cambios pendientes de esa base).
_PENDING_SCHEMA, _SCHEMA_NOTES = schema_report(app_engine)
if _PENDING_SCHEMA:
    raise RuntimeError(
        "Esquema incoherente al inicio de la suite:\n"
        + "\n".join(f"  - {entry}" for entry in _PENDING_SCHEMA)
    )


def _memory_engine():
    """Motor SQLite en memoria compartido por hilos (una sola conexión)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine, "connect", enforce_sqlite_foreign_keys)
    return engine


@pytest.fixture(scope="session", autouse=True)
def _never_touch_orbis_db():
    """Falla de forma explícita si la suite llegara a apuntar a ``orbis.db``."""
    assert DATABASE_URL == "sqlite://", (
        "La suite nunca debe escribir en orbis.db; "
        f"ORBIS_DATABASE_URL apunta a {DATABASE_URL!r}"
    )
    yield


@pytest.fixture()
def db_engine():
    """Motor en memoria con el esquema completo recién creado."""
    engine = _memory_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def file_db_engine(tmp_path):
    """Motor sobre fichero temporal (para pruebas de concurrencia real)."""
    path = tmp_path / "orbis-test.db"
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine, "connect", enforce_sqlite_foreign_keys)
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def session_factory(db_engine):
    """Fábrica de sesiones ligada al motor aislado del test."""
    return sessionmaker(bind=db_engine, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _app_db_override(session_factory):
    """Instala el override de ``get_db`` por test y lo retira al terminar."""
    previous = dict(app.dependency_overrides)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture()
def db(session_factory):
    """Sesión de SQLAlchemy aislada para pruebas de bajo nivel."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(_app_db_override):
    """Cliente HTTP con la base aislada del test inyectada."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def create_scenario(client):
    """Fábrica de sesiones PAPER para las pruebas de la API."""

    def _create(**overrides):
        body = {
            "quote_currency": "EUR",
            "initial_balance": "1000",
            "max_order_amount": "500",
            "max_fee_percentage": "2",
        }
        body.update(overrides)
        response = client.post("/paper/scenarios", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    return _create
