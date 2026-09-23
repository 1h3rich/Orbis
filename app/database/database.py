from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


DATABASE_URL = "sqlite:///./orbis.db"


class Base(DeclarativeBase):
    """Clase base compartida por los modelos de tablas de SQLAlchemy."""
    pass


engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False
)

def get_db():
    """Cede una sesión por petición de FastAPI y la cierra al terminar."""
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()
