"""Servidor temporal para verificar bloqueo Spot PAPER en el navegador."""

import os
from pathlib import Path

from fastapi import FastAPI

from app.automation.spot_paper_migration import encrypt_active_spot_paper
from app.automation.spot_strategy_paper import SpotStrategyPaper, create_spot_strategy_router
from app.web.mount import register_ui


paper = SpotStrategyPaper(Path(os.environ["ORBIS_SPOT_STRATEGY_PATH"]),
                          Path(os.environ["ORBIS_LAB_PATH"]))
paper.sessions()
encrypt_active_spot_paper(paper, "CIFRAR SPOT PAPER", "Spot paper browser secret 123")
app = FastAPI()
app.include_router(create_spot_strategy_router(paper.path, paper.lab.path, paper=paper))
register_ui(app)
