from fastapi.testclient import TestClient
from app.database.database import Base, get_db
from app.main import app
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

TEST_DATABASE_URL = "sqlite://"

test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autoflush=False,
    autocommit=False,
)

Base.metadata.create_all(bind=test_engine)

def override_get_db():
    """
    Sustituye la base de datos real por la base de datos de pruebas.
    """
    db = TestingSessionLocal()

    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)


def test_root():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["name"] == "Orbis"

def test_create_strategy():
    response = client.post(
        "/strategies",
        json={
            "name": "DCA Test",
            "strategy_type": "DCA",
            "enabled": True,
            "config": {
                "asset": "BTC",
                "quote_currency": "EUR",
                "amount": 50,
                "frequency": "weekly"
            }
        }
    )

    assert response.status_code == 200

    data = response.json()

    assert data["name"] == "DCA Test"
    assert data["config"]["amount"] == 50

def test_get_strategy():
    # Primero creamos una estrategia
    create_response = client.post(
        "/strategies",
        json={
            "name": "DCA GET Test",
            "strategy_type": "DCA",
            "enabled": True,
            "config": {
                "asset": "BTC",
                "quote_currency": "EUR",
                "amount": 50,
                "frequency": "weekly"
            }
        }
    )

    strategy_id = create_response.json()["id"]

    # Después la buscamos por su ID
    response = client.get(f"/strategies/{strategy_id}")

    assert response.status_code == 200
    assert response.json()["id"] == strategy_id
    assert response.json()["name"] == "DCA GET Test"

def test_get_strategy_not_found():
    """
    Comprueba que una estrategia inexistente devuelve 404.
    """
    response = client.get("/strategies/999999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Strategy not found"

def test_update_strategy():
    """
    Comprueba que podemos modificar una estrategia existente.
    """
    create_response = client.post(
        "/strategies",
        json={
            "name": "DCA antes",
            "strategy_type": "DCA",
            "enabled": True,
            "config": {
                "asset": "BTC",
                "amount": 50,
                "frequency": "weekly"
            }
        }
    )

    strategy_id = create_response.json()["id"]

    response = client.put(
        f"/strategies/{strategy_id}",
        json={
            "name": "DCA actualizado",
            "strategy_type": "DCA",
            "enabled": True,
            "config": {
                "asset": "BTC",
                "amount": 100,
                "frequency": "weekly"
            }
        }
    )

    assert response.status_code == 200
    assert response.json()["name"] == "DCA actualizado"
    assert response.json()["config"]["amount"] == 100

def test_delete_strategy():
    """
    Comprueba que podemos eliminar una estrategia.
    """
    create_response = client.post(
        "/strategies",
        json={
            "name": "DCA para borrar",
            "strategy_type": "DCA",
            "enabled": True,
            "config": {
                "asset": "BTC",
                "amount": 50,
                "frequency": "weekly"
            }
        }
    )

    strategy_id = create_response.json()["id"]

    response = client.delete(
        f"/strategies/{strategy_id}"
    )

    assert response.status_code == 200

    # Comprobamos que realmente ha desaparecido
    get_response = client.get(
        f"/strategies/{strategy_id}"
    )

    assert get_response.status_code == 404
