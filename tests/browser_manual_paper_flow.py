"""Actual Settings -> SQLCipher -> exact ledger, including lost reply/reentry."""
import hashlib
import json
import os
from pathlib import Path

import httpx
from playwright.sync_api import expect,sync_playwright

from tests.manual_paper_gui_fixture import PASSWORD,paper_roundtrip

BASE=os.environ['ORBIS_TEST_BASE_URL']
directory=Path(os.environ['ORBIS_BASIC_DIRECTORY']).parent
reopen=os.environ['ORBIS_BASIC_PHASE']=='reopen'
mode=os.environ['ORBIS_TEST_MANUAL_MODE']
paper=Path(os.environ['ORBIS_DATABASE_URL'].removeprefix('sqlite:///'))
other_paths=[Path(os.environ[name]) for name in ('ORBIS_LAB_PATH','ORBIS_AUTOMATION_PATH','ORBIS_SPOT_STRATEGY_PATH','ORBIS_PAPER_PERP_PATH')]
original_path=directory/'manual-ledger.json'


def hashes():
    return {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in other_paths}


with httpx.Client(base_url=BASE,trust_env=False,timeout=5) as client,sync_playwright() as p:
    before_others=hashes();pid=client.get('/__test/manual-process').json()['pid']
    if not reopen and mode=='protect':
        original=json.loads(json.dumps(paper_roundtrip(client)));original_path.write_text(json.dumps(original))
    elif reopen:original=json.loads(original_path.read_text())
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[];real_actions=[];writes=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda req:real_actions.append(req.url) if '/real/' in req.url and req.method not in ('GET','HEAD','OPTIONS') else None)
    page.goto(BASE+'/ui/#/config');page.wait_for_load_state('networkidle')
    panel=page.locator('#cfg-legacy-panel');panel.locator(':scope > summary').click()
    status=page.locator('#cfg-legacy-status')
    if not reopen and mode=='create':
        expect(status).to_contain_text('no inicializado')
        assert not paper.exists()
        form=page.locator('#cfg-legacy-create-form')
        expect(form).to_be_visible()
        form.locator('[name=passphrase]').fill(PASSWORD)
        form.locator('[name=repeated]').fill(PASSWORD)
        with page.expect_response('**/paper/legacy/create') as created:
            form.locator('button').click()
        assert created.value.status==200,created.value.text()
        expect(form.locator('[name=passphrase]')).to_have_value('')
        expect(form.locator('[name=repeated]')).to_have_value('')
        expect(status).to_contain_text('min restantes')
        assert paper.read_bytes()[:16]!=b'SQLite format 3\x00'
        original=json.loads(json.dumps(paper_roundtrip(client)));original_path.write_text(json.dumps(original))
        page.locator('#cfg-legacy-lock').click()
        expect(status).to_contain_text('bloqueado')
    elif not reopen:
        expect(status).to_contain_text('sin cifrar')
        form=page.locator('#cfg-legacy-protect-form')
        expect(form.locator('button')).to_be_enabled()
        # Secret inputs clear when leaving Settings, even before submission.
        form.locator('[name=passphrase]').fill(PASSWORD)
        form.locator('[name=passphrase]').evaluate('(el)=>window.__ownedManualInput=el')
        page.goto(BASE+'/ui/#/laboratorio');page.wait_for_load_state('networkidle')
        assert page.evaluate('window.__ownedManualInput.value')==''
        page.goto(BASE+'/ui/#/config');page.wait_for_load_state('networkidle')
        panel=page.locator('#cfg-legacy-panel');panel.locator(':scope > summary').click()
        form=page.locator('#cfg-legacy-protect-form')

        def lost_reply(route):
            body=route.request.post_data_json
            writes.append(body['id'])
            response=route.fetch()
            assert response.status==202,response.text()
            route.abort('failed')  # exactly one real request; the browser never gets its reply

        page.route('**/paper/legacy/protection/jobs',lost_reply)
        form.locator('[name=passphrase]').fill(PASSWORD)
        form.locator('[name=repeated]').fill(PASSWORD)
        form.locator('button').click()
        expect(form.locator('[name=passphrase]')).to_have_value('')
        expect(form.locator('[name=repeated]')).to_have_value('')
        expect(page.locator('#cfg-legacy-protect-result')).to_contain_text('PAPER protegido',timeout=45000)
        assert len(writes)==1
        job=client.get('/paper/legacy/protection/jobs/'+writes[0]).json()
        assert job['state']=='SUCCEEDED' and job['active_database_encrypted']
        backup=Path(job['backup']);assert backup.is_file() and backup.read_bytes()[:16]!=b'SQLite format 3\x00'
        assert client.get('/__test/manual-process').json()['pid']==pid
        panel=page.locator('#cfg-legacy-panel')
        expect(panel).to_have_attribute('open','')
        expect(status).to_contain_text('bloqueado')
    else:
        expect(status).to_contain_text('bloqueado')
        assert client.get('/paper/legacy/protection').json()['latest_job'] is None
    assert client.get('/paper/operations').status_code==423
    assert paper.read_bytes()[:16]!=b'SQLite format 3\x00'
    unlock=page.locator('#cfg-legacy-unlock-form')
    expect(unlock).to_be_visible()
    unlock.locator('[name=passphrase]').fill('owned incorrect manual password')
    with page.expect_response('**/paper/legacy/unlock') as wrong:
        unlock.locator('button').click()
    assert wrong.value.status==400
    expect(status).to_contain_text('No se pudo desbloquear')
    expect(unlock.locator('[name=passphrase]')).to_have_value('')
    assert client.get('/paper/operations').status_code==423
    unlock.locator('[name=passphrase]').fill(PASSWORD)
    with page.expect_response('**/paper/legacy/unlock') as opened:
        unlock.locator('button').click()
    assert opened.value.status==200,opened.value.text()
    expect(status).to_contain_text('min restantes')
    assert client.get('/paper/operations').json()==original[4]
    assert client.get('/paper/scenarios/EUR').json()==original[5]
    assert client.post('/paper/buy',json=original[0]).json()==original[1]
    assert client.post('/paper/sell',json=original[2]).json()==original[3]
    assert len(client.get('/paper/operations').json())==2
    expect(page.locator('#cfg-legacy-protect-state')).to_be_hidden()
    ack=page.locator('#cfg-legacy-backup-form [name=acknowledge]').bounding_box()
    assert ack is not None and ack['height']==16 and ack['width']==16
    assert PASSWORD not in page.evaluate('JSON.stringify([localStorage,sessionStorage])')
    panel.screenshot(path=str(directory/('manual-reopened.png' if reopen else 'manual-completed.png')))
    page.set_viewport_size({'width':390,'height':844});panel.scroll_into_view_if_needed()
    assert panel.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    panel.screenshot(path=str(directory/('manual-mobile-reopened.png' if reopen else 'manual-mobile.png')))
    page.locator('#cfg-legacy-lock').click();expect(status).to_contain_text('bloqueado')
    assert client.get('/paper/operations').status_code==423
    assert not errors and not real_actions,(errors,real_actions)
    assert hashes()==before_others and not Path(os.environ['ORBIS_REAL_VAULT_PATH']).exists()
    browser.close()
    result={'ok':True,'mode':mode,'reopen':reopen,'pid':pid,'protection_post_count':len(writes),
        'operations':2,'cipher_locked':True,'other_stores_unchanged':True,'errors':errors,'real_actions':real_actions}
    (directory/('manual-reentry.json' if reopen else 'manual-result.json')).write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
