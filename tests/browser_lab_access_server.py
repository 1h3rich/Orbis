"""LAB cifrado aislado para comprobar Ajustes sin datos del usuario."""

from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI

from app.lab.api import create_lab_router
from app.lab.backup import lab_runtime_lease
from app.lab.store import LabStore
from app.web.mount import register_ui


path = Path(os.environ["ORBIS_LAB_PATH"])
if not path.exists():
    with LabStore(path, passphrase="browser LAB access password").connect():
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with lab_runtime_lease(path):
        yield


app = FastAPI(lifespan=lifespan)
app.include_router(create_lab_router(path))
register_ui(app)
