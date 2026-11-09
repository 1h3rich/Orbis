"""Owned files and actual GUI-first launcher, without financial transports."""
import os

os.environ['ORBIS_DATABASE_GUI_ACCESS']='1'
from tests.browser_storage_protection_launcher import main  # deterministic public reads only
from app.main import app


@app.get('/__test/manual-process')
def owned_process():
    return {'pid':os.getpid()}


if __name__=='__main__':raise SystemExit(main())
