"""Servidor aislado para comprobar el informe DCA en navegador."""

import os
from pathlib import Path

from fastapi import FastAPI

from app.automation.api import create_automation_router
from app.automation.paper_dca import create_paper_dca_router
from app.automation.store import AutomationStore
from app.lab.api import create_lab_router
from app.lab.store import LabStore
from app.web.mount import register_ui


lab_path = Path(os.environ["ORBIS_LAB_PATH"])
automation_path = Path(os.environ["ORBIS_AUTOMATION_PATH"])
with LabStore(lab_path).connect():
    pass
with AutomationStore(automation_path).connect():
    pass
app = FastAPI()
app.include_router(create_lab_router(lab_path))
app.include_router(create_automation_router(automation_path, lab_path))
app.include_router(create_paper_dca_router(automation_path, lab_path))
register_ui(app)
