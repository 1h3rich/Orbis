"""LAB y UI aislados para comprobar la reproducción sin datos del usuario."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.lab.api import create_lab_router
from app.lab.backup import lab_runtime_lease
from app.web.mount import register_ui


_lab_path = Path(os.environ["ORBIS_LAB_PATH"])


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with lab_runtime_lease(_lab_path):
        yield


app = FastAPI(lifespan=lifespan)
app.include_router(create_lab_router(_lab_path))
register_ui(app)
