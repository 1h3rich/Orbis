"""Real GUI-first launcher, own files and real SQLCipher; no owner data."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import os,sqlite3,subprocess,sys,time

import httpx

from app.database.legacy_runtime import create_new_encrypted_legacy_database
from tests.test_storage_protection import prepared

ROOT=Path(__file__).resolve().parents[1]
PASSWORD='owned manual PAPER graphical password'


@contextmanager
def launched_manual(directory,*,initial='plain'):
    directory.mkdir(parents=True,exist_ok=True)
    paths={name:directory/(name.lower()+'.db') for name in ('LAB','DCA','SPOT','GRVT')}
    if not any(path.exists() for path in paths.values()):
        _,paths,_=prepared(directory)
    manual=directory/'manual.db'
    if initial=='plain':
        with sqlite3.connect(manual) as db:
            db.execute('CREATE TABLE owned_preserved (quantity TEXT)')
            db.execute("INSERT INTO owned_preserved VALUES ('0.000000000000000001')")
    elif initial=='cipher':create_new_encrypted_legacy_database(manual,PASSWORD)
    elif initial not in ('missing','existing'):raise AssertionError('Unknown owned fixture mode')
    env={name:value for name,value in os.environ.items() if not name.startswith('ORBIS_')}
    env.update({'ORBIS_DATABASE_URL':'sqlite:///'+str(manual),'ORBIS_LAB_PATH':str(paths['LAB']),
        'ORBIS_AUTOMATION_PATH':str(paths['DCA']),'ORBIS_SPOT_STRATEGY_PATH':str(paths['SPOT']),
        'ORBIS_PAPER_PERP_PATH':str(paths['GRVT']),'ORBIS_REAL_VAULT_PATH':str(directory/'real.db'),
        'ORBIS_PRICE_PROVIDER':'manual'})
    logfile=directory/('launch-'+initial+'.log');output=logfile.open('w')
    process=subprocess.Popen([sys.executable,str(ROOT/'scripts/run_orbis_local.py'),'--port','0','--no-browser'],
        cwd=directory,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT)
    client=None
    try:
        deadline=time.monotonic()+20;url=None
        while time.monotonic()<deadline:
            assert process.poll() is None,logfile.read_text()[-3000:]
            announced=next((line for line in logfile.read_text().splitlines() if line.startswith('ORBIS local: ')),None)
            if announced:
                url=announced.removeprefix('ORBIS local: ').removesuffix('/ui/');break
            time.sleep(.05)
        assert url,'Owned GUI launcher did not announce a URL'
        client=httpx.Client(base_url=url,trust_env=False,timeout=3)
        while time.monotonic()<deadline:
            assert process.poll() is None,logfile.read_text()[-3000:]
            try:
                if client.get('/paper/legacy/status').status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.05)
        else:raise AssertionError('Owned launcher was not ready')
        yield SimpleNamespace(process=process,client=client,path=manual,paths=paths,url=url,log=logfile)
    finally:
        if client is not None:client.close()
        if process.poll() is None:process.terminate()
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        output.close()
        assert PASSWORD not in logfile.read_text()


def paper_roundtrip(client):
    from uuid import uuid4
    response=client.post('/paper/scenarios',json={'quote_currency':'EUR','initial_balance':'1000',
        'max_order_amount':'500','max_fee_percentage':'2'})
    assert response.status_code==201,response.text
    buy={'request_id':str(uuid4()),'asset':'BTC','quote_currency':'EUR',
        'order_amount':'300','price':'100000','estimated_fee':'1'}
    bought=client.post('/paper/buy',json=buy)
    assert bought.status_code==200 and bought.json()['executed'],bought.text
    sell={'request_id':str(uuid4()),'asset':'BTC','quote_currency':'EUR',
        'quantity':'0.001','price':'110000','fee':'1'}
    sold=client.post('/paper/sell',json=sell)
    assert sold.status_code==200 and sold.json()['executed'],sold.text
    operations=client.get('/paper/operations').json()
    scenario=client.get('/paper/scenarios/EUR').json()
    assert len(operations)==2
    return buy,bought.json(),sell,sold.json(),operations,scenario
