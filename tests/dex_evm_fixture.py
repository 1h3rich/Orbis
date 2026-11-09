"""Real local EVM, isolated ports/accounts; no user wallet or mainnet writes."""
from contextlib import contextmanager
import json,os,subprocess,time
from pathlib import Path
from threading import Lock
import httpx

ROOT=Path(__file__).resolve().parents[1]


def wait_receipt(rpc,tx_hash,*,timeout=5):
    """Await a fixture's receipt without resending or forcing its mining state."""
    deadline=time.monotonic()+timeout
    while True:
        receipt=rpc('eth_getTransactionReceipt',[tx_hash])
        if receipt is not None:return receipt
        remaining=deadline-time.monotonic()
        assert remaining>0,'Own EVM preparation transaction did not mine before the deadline'
        time.sleep(min(.01,remaining))


@contextmanager
def evm(directory,*,backend=None):
    directory.mkdir(exist_ok=True,parents=True);env=os.environ.copy()
    env['ORBIS_DEX_FIXTURE_DIRECTORY']=str(directory);env['UWS_USE_FALLBACK']='1'
    if backend is not None:
        assert backend in {'ganache','anvil'}
        env['ORBIS_DEX_FIXTURE_BACKEND']=backend
    with (directory/'evm.log').open('w') as output:
        process=subprocess.Popen(['node',str(ROOT/'contracts/support/deployment-fixture.mjs')],cwd=ROOT/'contracts',env=env,
            stdout=output,stderr=subprocess.STDOUT)
        try:
            ready=directory/'ready.json';deadline=time.monotonic()+20
            while not ready.exists():
                assert process.poll() is None,(directory/'evm.log').read_text()[-2500:]
                assert time.monotonic()<deadline,'EVM fixture startup timed out'
                time.sleep(.05)
            meta=json.loads(ready.read_text());assert meta['rpc'].startswith('http://127.0.0.1:')
            with httpx.Client(base_url=meta['rpc'],timeout=10,trust_env=False) as client:
                sequence=0;sequence_lock=Lock()
                def rpc(method,params=None):
                    nonlocal sequence
                    with sequence_lock:sequence+=1;request_id=sequence
                    response=client.post('/',json={'jsonrpc':'2.0','id':request_id,'method':method,'params':params or []})
                    response.raise_for_status();data=response.json();assert 'error' not in data,data.get('error')
                    assert data['id']==request_id;return data['result']
                assert rpc('orbis_fixture_identity')=={'id':meta['fixtureId'],'chain':56}
                assert int(rpc('eth_chainId'),16)==56
                yield meta,rpc
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
