"""Create via UI, download real adapter inputs, exact JSON, archive and reopen."""
import json
import os
from pathlib import Path

from playwright.sync_api import expect,sync_playwright
from tests.perp_dataset_fixture import PASSWORD,NOW

BASE=os.environ['ORBIS_TEST_BASE_URL'];directory=Path(os.environ['ORBIS_BASIC_DIRECTORY']).parent
reopen=os.environ.get('ORBIS_BASIC_PHASE')=='reopen'
errors=[];actions=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:actions.append(request.url) if '/real/' in request.url and request.method=='POST' else None)
    initial=page.request.get(BASE+'/__perp_fixture/state').json()
    assert initial['locked'] is True and not initial['real_exists']
    page.goto(BASE+'/ui/#/config');page.wait_for_load_state('networkidle')
    page.locator('#cfg-privacidad').get_by_text('LAB activo',exact=True).click()
    form=page.locator('#cfg-lab-unlock-form' if reopen else '#cfg-lab-create-form')
    expect(form).to_be_visible();form.locator('input[name=passphrase]').fill(PASSWORD)
    with page.expect_response(lambda r:r.url.endswith('/lab/unlock' if reopen else '/lab/create')) as response:
        form.locator('button[type=submit]').click()
    assert response.value.status==200
    expect(page.locator('#cfg-lab-status')).to_contain_text('desbloqueado')
    page.goto(BASE+'/ui/#/laboratorio');page.wait_for_load_state('networkidle')
    panel=page.locator('#lab-perp-data');panel.locator(':scope > summary').click()
    expect(panel.locator('#perp-data-selection')).to_be_visible()
    if not reopen:
        form=panel.locator('#perp-data-form')
        form.locator('[name=start]').fill('2025-01-01T00:00')
        form.locator('[name=end]').fill('2025-01-01T04:00')
        with page.expect_response(lambda r:r.url.endswith('/lab/perp/datasets/download')) as response:
            form.locator('button[type=submit]').click()
        assert response.value.status==200,(response.value.status,response.value.text())
        saved=response.value.json()
        (directory/'perp-saved.json').write_text(json.dumps(saved,indent=2))
    else:
        saved=json.loads((directory/'perp-saved.json').read_text())
        expect(panel.locator('#perp-data-selection option')).to_have_count(2)
        panel.locator('#perp-data-selection').select_option(saved['id'])
    report=panel.locator('#perp-data-report')
    expect(report).to_contain_text('4 velas');expect(report).to_contain_text('4 liquidaciones + ancla')
    expect(report).to_contain_text('Captura actual');expect(panel).to_contain_text('reglas actuales')
    rules=panel.locator('#perp-rules-panel')
    rules.locator(':scope > summary').click()
    if not reopen:
        expect(rules.locator('#perp-rules-list details')).to_have_count(1)
        assert page.request.post(BASE+'/__perp_fixture/rules-next').ok
        with page.expect_response('**/lab/perp/rules/capture') as captured:
            rules.get_by_role('button',name='Capturar reglas',exact=True).click()
        assert captured.value.ok,captured.value.text()
    expect(rules.locator('#perp-rules-list details')).to_have_count(2)
    expect(rules.locator('#perp-rules-list details').first).to_contain_text('Margen · cambio')
    rules.locator('#perp-rules-list details').first.locator('summary').click()
    expect(rules.locator('#perp-rules-list details').first).to_contain_text('Mantenimiento 0.012')
    with page.expect_download() as rules_file:rules.get_by_role('button',name='Exportar capturas',exact=True).click()
    archive=directory/('rules-reopened.json' if reopen else 'rules.json')
    rules_file.value.save_as(archive)
    rules_export=json.loads(archive.read_text())
    assert rules_export['scope']=='OBSERVED_AT_CAPTURE_ONLY' and len(rules_export['items'])==2
    if reopen:assert rules_export==json.loads((directory/'rules.json').read_text())
    assert len(page.request.get(BASE+'/lab/datasets').json())==0
    with page.expect_download() as downloaded:panel.locator('#perp-data-export').click()
    output=directory/('perp-export-reopened.json' if reopen else 'perp-export.json')
    downloaded.value.save_as(output);bundle=json.loads(output.read_text())
    assert bundle['hash']==saved['hash']
    assert bundle['dataset']['captured_start_ns']==str(NOW)
    assert bundle['dataset']['rules']['fetched_at_ns']==str(NOW)
    assert bundle['dataset']['trade']['bars'][0]['c']=='101'
    assert bundle['dataset']['mark']['bars'][0]['c']=='108'
    if reopen:assert bundle==json.loads((directory/'perp-export.json').read_text())
    if not reopen:
        result=page.request.post(BASE+'/lab/backup/encrypted',data={'passphrase':'owned perp browser backup password'})
        assert result.ok,result.text()
        assert result.json()['verified']['counts']['perp_datasets']==1
        assert result.json()['verified']['counts']['perp_rule_observations']==2
    page.screenshot(path=str(directory/('perp-reopened.png' if reopen else 'perp-created.png')),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    panel.scroll_into_view_if_needed()
    assert panel.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
    page.screenshot(path=str(directory/('perp-mobile-reopened.png' if reopen else 'perp-mobile.png')),full_page=True)
    final=page.request.get(BASE+'/__perp_fixture/state').json()
    assert final['calls']==(0 if reopen else 7),final
    assert not final['real_exists'] and not actions
    if reopen:
        # A late response cannot restore an older selection/report.
        page.set_viewport_size({'width':1440,'height':1000})
        held=[]
        page.route('**/lab/perp/datasets/'+saved['id'],lambda route:held.append(route))
        panel.locator('#perp-data-selection').select_option('')
        panel.locator('#perp-data-selection').select_option(saved['id'])
        page.wait_for_function("window.performance.getEntriesByType('resource').length>0")
        # A DOM action yields to the intercepted fetch; it is still unresolved.
        expect(report).to_have_text('Sin archivo seleccionado.')
        assert held
        panel.locator('#perp-data-selection').select_option('')
        with page.expect_response(lambda r:r.url.endswith('/lab/perp/datasets/'+saved['id'])):
            held[0].fulfill(status=200,json=saved)
        expect(report).to_have_text('Sin archivo seleccionado.')
        expect(panel.locator('#perp-data-export')).to_be_disabled()
        assert page.request.post(BASE+'/__perp_fixture/corrupt').ok
        panel.locator(':scope > summary').click();panel.locator(':scope > summary').click()
        expect(panel.locator('#perp-data-status')).to_contain_text('no verificable')
        expect(panel.locator('#perp-data-export')).to_be_disabled()
        expect(report).not_to_contain_text('4 velas')
    assert page.request.post(BASE+'/lab/lock').ok
    page.reload();page.wait_for_load_state('networkidle')
    page.locator('#lab-perp-data > summary').click()
    expect(page.locator('#perp-data-status')).to_contain_text('bloqueado')
    expect(page.locator('#perp-data-export')).to_be_disabled()
    assert not errors,errors
    browser.close()
(directory/('reopened.json' if reopen else 'created.json')).write_text(json.dumps({'ok':True,'saved':saved,'calls':final['calls'],'errors':errors,'real_actions':len(actions)},indent=2))
print('Perpetual inputs: UI, source separation, exact JSON, encrypted archive and locked/reopen boundaries OK')
