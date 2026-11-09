"""Settings password -> drained server -> ciphers/backups -> explicit unlock/data."""
import json
import os
from pathlib import Path

from playwright.sync_api import expect,sync_playwright
from tests.test_storage_protection import PWD,rows

BASE=os.environ['ORBIS_TEST_BASE_URL'];directory=Path(os.environ['ORBIS_BASIC_DIRECTORY']).parent
reopen=os.environ.get('ORBIS_BASIC_PHASE')=='reopen'
paths={name:Path(os.environ[env]) for name,env in (('LAB','ORBIS_LAB_PATH'),('DCA','ORBIS_AUTOMATION_PATH'),('SPOT','ORBIS_SPOT_STRATEGY_PATH'),('GRVT','ORBIS_PAPER_PERP_PATH'))}
errors=[];real_actions=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:real_actions.append(r.url) if '/real/' in r.url and r.method not in ('GET','HEAD','OPTIONS') else None)
    before=page.request.get(BASE+'/system/storage-protection').json()
    assert before['supported'] and before['ready'],before
    if reopen:
        previous=json.loads((directory/'protected.json').read_text())
        assert before['latest_job'] is None
    else:
        original={name:rows(path) for name,path in paths.items()}
        (directory/'original-books.json').write_text(json.dumps(original,ensure_ascii=False))
        assert all(s['format']=='plain' for s in before['stores'])
    if reopen:
        page.goto(BASE+'/ui/');page.wait_for_load_state('networkidle')
        page.evaluate('(id)=>sessionStorage.setItem("orbis.storage.protection.job",id)',previous['job']['id'])
    page.goto(BASE+'/ui/#/config');page.wait_for_load_state('networkidle')
    expect(page.locator('#cfg-privacy-summary')).not_to_contain_text('La migración requiere cerrar ORBIS')
    panel=page.locator('#cfg-storage-protection')
    if not reopen:
        page.locator('#cfg-lab-status').evaluate('(el)=>el.closest("details").open=true')
        page.locator('#cfg-lab-status [data-open-storage-protection]').click()
        expect(panel).to_have_attribute('open','')
    else:panel.locator('summary').click()
    form=panel.locator('form');button=form.locator('button[type=submit]')
    if not reopen:
        expect(button).to_be_enabled()
        form.locator('[name=passphrase]').fill(PWD)
        form.locator('[name=passphrase]').evaluate('(el)=>window.__ownedOldProtectionInput=el')
        page.goto(BASE+'/ui/#/laboratorio');page.wait_for_load_state('networkidle')
        assert page.evaluate('window.__ownedOldProtectionInput.value')==''
        page.goto(BASE+'/ui/#/config');page.wait_for_load_state('networkidle')
        panel=page.locator('#cfg-storage-protection');panel.locator('summary').click()
        form=panel.locator('form');button=form.locator('button[type=submit]')
        expect(button).to_be_enabled()
        focused=form.locator('input[value=LAB]');focused.focus()
        with page.expect_response(lambda r:r.url.endswith('/system/storage-protection')):
            expect(focused).to_be_focused(timeout=7000)
            page.wait_for_timeout(5200)
        expect(focused).to_be_focused()
        form.locator('[name=passphrase]').fill(PWD);form.locator('[name=repeated]').fill(PWD)
        with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/system/storage-protection/jobs')) as accepted:
            button.click()
        assert accepted.value.status==202,accepted.value.text()
        job=accepted.value.json()
        expect(form.locator('[name=passphrase]')).to_have_value('')
        expect(form.locator('[name=repeated]')).to_have_value('')
        expect(page.locator('#storage-protection-result')).to_contain_text('Protección completada',timeout=45000)
        result=page.request.get(BASE+'/system/storage-protection/jobs/'+job['id']).json()
        assert result['state']=='SUCCEEDED'
        assert all(e['state']=='ENCRYPTED' for e in result['results'])
        actual={name:rows(path,PWD) for name,path in paths.items()}
        assert json.loads(json.dumps(actual,ensure_ascii=False))==json.loads((directory/'original-books.json').read_text())
    else:
        expect(page.locator('#storage-protection-status')).to_contain_text('Trabajo no disponible')
        expect(button).to_be_disabled()
        assert all(s['format']=='encrypted_or_invalid' for s in before['stores'])
        result=previous['job']
    assert PWD not in str(page.evaluate('Object.values(localStorage).concat(Object.values(sessionStorage))'))
    page.locator('#cfg-storage-protection').screenshot(path=str(directory/('protection-reopened.png' if reopen else 'protection-completed.png')))
    page.set_viewport_size({'width':390,'height':844})
    panel=page.locator('#cfg-storage-protection');panel.scroll_into_view_if_needed()
    assert panel.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    panel.screenshot(path=str(directory/('protection-mobile-reopened.png' if reopen else 'protection-mobile.png')))
    page.set_viewport_size({'width':1440,'height':1000})
    for selector,route in (('lab','/lab/unlock'),('automation','/automation/dca/unlock'),('spot','/paper/spot/strategies/unlock'),('perp','/paper/perp/unlock')):
        form=page.locator('#cfg-'+selector+'-unlock-form')
        form.evaluate('(el)=>{let parent=el.parentElement;while(parent){if(parent.tagName==="DETAILS")parent.open=true;parent=parent.parentElement;}}')
        expect(form).to_be_visible();form.locator('input').fill(PWD)
        with page.expect_response(lambda r:r.url.endswith(route) and r.request.method=='POST') as unlocked:
            form.locator('button[type=submit]').click()
        assert unlocked.value.status==200,unlocked.value.text()
    assert page.request.get(BASE+'/lab/datasets').json()[0]['symbol']=='BTC/USDT'
    assert page.request.get(BASE+'/automation/dca/rules').json()[0]['name']=='Owned DCA'
    assert page.request.get(BASE+'/paper/perp/accounts').json()[0]['name']=='Owned futures'
    assert page.request.get(BASE+'/paper/spot/strategies').json()==[]
    page.goto(BASE+'/ui/#/laboratorio');page.wait_for_load_state('networkidle')
    panel=page.locator('#lab-perp-data');panel.locator(':scope > summary').click()
    if not reopen:
        form=panel.locator('#perp-data-form')
        form.locator('[name=start]').fill('2025-01-01T00:00');form.locator('[name=end]').fill('2025-01-01T04:00')
        with page.expect_response(lambda r:r.url.endswith('/lab/perp/datasets/download')) as downloaded:
            form.locator('button[type=submit]').click()
        assert downloaded.value.status==200,downloaded.value.text()
        archive=downloaded.value.json()
    else:
        archive=previous['archive']
        expect(panel.locator('#perp-data-selection option')).to_have_count(2)
        panel.locator('#perp-data-selection').select_option(archive['id'])
    expect(panel.locator('#perp-data-report')).to_contain_text('4 velas')
    with page.expect_download() as download:panel.locator('#perp-data-export').click()
    target=directory/('protected-perp-reopened.json' if reopen else 'protected-perp.json')
    download.value.save_as(target);bundle=json.loads(target.read_text())
    assert bundle['hash']==archive['hash']
    if reopen:assert bundle==json.loads((directory/'protected-perp.json').read_text())
    for route in ('/lab/lock','/automation/dca/lock','/paper/spot/strategies/lock','/paper/perp/lock'):
        assert page.request.post(BASE+route).ok
    assert not Path(os.environ['ORBIS_REAL_VAULT_PATH']).exists()
    assert not errors and not real_actions,(errors,real_actions)
    browser.close()
(directory/('protection-reentry.json' if reopen else 'protected.json')).write_text(json.dumps({'ok':True,'job':result,'archive':archive,'errors':errors,'real_actions':real_actions},indent=2))
print('Protection UI -> real pause/cipher/reopen -> explicit unlock -> perp archive roundtrip passed')
