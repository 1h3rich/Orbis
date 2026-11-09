"""Create/reopen fixed-profile reserve, rolling windows, path scenarios and charts."""
import json
import os
from decimal import Decimal
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

from tests.perp_dataset_fixture import PASSWORD


BASE=os.environ['ORBIS_TEST_BASE_URL'];directory=Path(os.environ['ORBIS_BASIC_DIRECTORY']).parent
reopen=os.environ.get('ORBIS_BASIC_PHASE')=='reopen'
limited=os.environ.get('ORBIS_TEST_PERP_ENTRY')=='LIMIT'
errors=[];actions=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:actions.append(request.url) if '/real/' in request.url and request.method not in ('GET','HEAD','OPTIONS') else None)
    assert page.request.get(BASE+'/__perp_validation_fixture/state').json()['locked']
    page.goto(BASE+'/ui/#/config');page.wait_for_load_state('networkidle')
    form=page.locator('#cfg-lab-unlock-form' if reopen else '#cfg-lab-create-form');form.evaluate('(e)=>e.closest("details").open=true')
    form.locator('[name=passphrase]').fill(PASSWORD);form.locator('button[type=submit]').click()
    expect(page.locator('#cfg-lab-status')).to_contain_text('desbloqueado')
    page.goto(BASE+'/ui/#/laboratorio');page.wait_for_load_state('networkidle')
    if not reopen:
        page.locator('#lab-perp-data > summary').click();form=page.locator('#perp-data-form')
        form.locator('[name=start]').fill('2025-01-01T00:00');form.locator('[name=end]').fill('2025-01-06T00:00')
        with page.expect_response(lambda r:r.url.endswith('/lab/perp/datasets/download')) as downloaded:form.locator('button[type=submit]').click()
        assert downloaded.value.status==200,downloaded.value.text();data=downloaded.value.json()
        form=page.locator('#lab-strategy-form');form.locator('[name=name]').fill('Reserva perpetua propia')
        rule=page.locator('#lab-rule-list .lab-rule').first;rule.locator('[name=kind]').select_option('BULLISH_STREAK');rule.locator('[name=period]').fill('2')
        for key,value in [('initial_capital','1000'),('allocation','0.5'),('fee_rate','0'),('slippage_rate','0'),('stop_loss','0.5'),('take_profit','0.5')]:form.locator('[name='+key+']').fill(value)
        with page.expect_response(lambda r:r.url.endswith('/lab/strategies') and r.request.method=='POST') as created:page.locator('#lab-save-strategy').click()
        assert created.value.status==200,created.value.text();version=created.value.json()
        page.locator('#lab-perp-runs > summary').click();form=page.locator('#perp-run-form')
        expect(form.locator('[name=version_id] option')).to_have_count(2)
        form.locator('[name=version_id]').select_option(version['version_id']);form.locator('[name=dataset_id]').select_option(data['id'])
        if limited:
            form.locator('[name=entry_kind]').select_option('LIMIT')
            form.locator('[name=limit_offset]').fill('1')
            form.locator('[name=limit_bars]').fill('3')
        with page.expect_response(lambda r:r.url.endswith('/lab/perp/runs') and r.request.method=='POST') as tested:form.locator('button[type=submit]').click()
        assert tested.value.status==201,tested.value.text();profile_id=tested.value.json()['profile_id']
        expect(page.locator('#perp-run-verification')).to_contain_text('Reproducible')
        originals=[]
    else:
        previous=json.loads((directory/'owned-validations.json').read_text());data=previous['data'];version=previous['version'];profile_id=previous['profile_id'];originals=previous['validations']
    panel=page.locator('#lab-perp-validations');panel.locator(':scope > summary').click();form=panel.locator('#perp-validation-form')
    expect(form.locator('[name=profile_id] option')).to_have_count(3 if reopen else 2)
    for index,method in enumerate(['CHRONOLOGICAL_HOLDOUT','ROLLING_FIXED_VERSION','PATH_SENSITIVITY']):
        if not reopen:
            form.locator('[name=profile_id]').select_option(profile_id);form.locator('[name=dataset_id]').select_option(data['id']);form.locator('[name=method]').select_option(method)
            if method=='CHRONOLOGICAL_HOLDOUT':form.locator('[name=fraction]').fill('70')
            if method=='ROLLING_FIXED_VERSION':form.locator('[name=folds]').fill('3')
            with page.expect_response(lambda r:r.url.endswith('/lab/perp/validations') and r.request.method=='POST') as made:form.locator('button[type=submit]').click()
            assert made.value.status==201,made.value.text();record=made.value.json()
        else:
            record=originals[index]['record'];panel.locator('#perp-validation-selection').select_option(record['id'])
        expect(panel.locator('#perp-validation-result')).to_be_visible()
        expect(panel.locator('#perp-validation-windows tbody tr')).to_have_count([2,6,4][index])
        with page.expect_download() as exported:panel.locator('#perp-validation-export').click()
        output=directory/(('reopened-' if reopen else '')+method+'.json');exported.value.save_as(output);bundle=json.loads(output.read_text())
        assert bundle['validation']['verification']=='REPLAY_VERIFIED' and bundle['execution']['result']['execution_authority']=='NONE'
        assert bundle['schema']=='orbis.lab.perp.validation.bundle.v'+('2' if limited else '1')
        assert all(Decimal(w['metrics']['final_equity'])==Decimal('1025.24899' if limited else '1024.999') for w in bundle['execution']['result']['windows'])
        if limited:
            assert bundle['execution']['simulation_engine']=='perp-ohlc-2.0'
            assert all(w['profile']['entry_order']['offset']=='0.01' for w in bundle['execution']['result']['windows'])
            if method!='PATH_SENSITIVITY':expect(panel.locator('#perp-validation-paper')).to_be_visible()
            else:expect(panel.locator('#perp-validation-paper')).not_to_be_visible()
        if reopen:assert bundle==originals[index]['bundle']
        if method!='PATH_SENSITIVITY':
            with page.expect_download() as reference:panel.locator('#perp-validation-reference').click()
            reference_output=directory/(('reopened-' if reopen else '')+'reference-'+str(index)+'.json');reference.value.save_as(reference_output)
            ref=json.loads(reference_output.read_text());assert ref['execution_authority']=='NONE' and ref['sample_bars']==[36,90][index]
            assert ref['schema']=='orbis.research.perp.reference.v'+('2' if limited else '1')
        else:expect(panel.locator('#perp-validation-reference')).not_to_be_visible()
        child_index=1 if index!=1 else 3
        panel.locator('[data-window="'+str(child_index)+'"]').click()
        expect(panel.locator('#perp-validation-status')).to_have_text('Tramo abierto en Pruebas de derivados.')
        expect(page.locator('#perp-run-chart')).to_have_attribute('data-entry-count','1')
        expect(page.locator('#perp-run-chart')).to_have_attribute('data-exit-count','1')
        expect(page.locator('#perp-run-trades tbody tr')).to_have_count(1)
        if not reopen:originals.append({'record':record,'bundle':bundle})
    if not reopen:
        copy=page.request.post(BASE+'/lab/backup/encrypted',data={'passphrase':'owned reserve encrypted copy key'})
        assert copy.ok,copy.text();assert copy.json()['verified']['counts']['perp_validations']==3
        form=page.locator('#lab-strategy-form');form.locator('[name=direction]').select_option('SHORT')
        with page.expect_response(lambda r:'/versions' in r.url and r.request.method=='POST') as changed:page.locator('#lab-save-strategy').click()
        assert changed.value.status==200
        (directory/'owned-validations.json').write_text(json.dumps({'data':data,'version':version,'profile_id':profile_id,'validations':originals},indent=2))
    panel.screenshot(path=str(directory/('validation-reopened.png' if reopen else 'validation-created.png')))
    page.set_viewport_size({'width':390,'height':844});panel.scroll_into_view_if_needed()
    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
    page.screenshot(path=str(directory/('validation-mobile-reopened.png' if reopen else 'validation-mobile.png')),full_page=True)
    if reopen:
        page.set_viewport_size({'width':1440,'height':1000});held=[];id=originals[0]['record']['id'];pattern='**/lab/perp/validations/'+id
        page.route(pattern,lambda route:held.append(route));panel.locator('#perp-validation-selection').select_option('');panel.locator('#perp-validation-selection').select_option(id)
        expect(panel.locator('#perp-validation-status')).to_have_text('Reproduciendo originales…')
        panel.locator('#perp-validation-selection').select_option('');assert held
        held[0].fulfill(status=200,json=originals[0]['bundle']);page.unroute(pattern)
        expect(panel.locator('#perp-validation-result')).not_to_be_visible()
        corrupt=page.request.post(BASE+'/__perp_validation_fixture/corrupt').json();assert corrupt['id']==id
        panel.locator('#perp-validation-selection').select_option(id)
        expect(panel.locator('#perp-validation-status')).to_contain_text('no se reproduce')
        expect(panel.locator('#perp-validation-result')).not_to_be_visible()
    state=page.request.get(BASE+'/__perp_validation_fixture/state').json();assert state['calls']==(0 if reopen else 5) and not state['real_exists']
    assert page.request.post(BASE+'/lab/lock').ok
    page.reload();page.wait_for_load_state('networkidle')
    # Force strategy-versions to reject first. Previously that Promise.all arm
    # hid the valid 423 as generic unavailability, depending on response order.
    held_locked=[]
    for pattern in ('**/lab/perp/profiles','**/lab/perp/datasets'):
        page.route(pattern,lambda route:held_locked.append(route))
    page.locator('#lab-perp-validations > summary').click()
    expect(page.locator('#perp-validation-status')).to_contain_text('bloqueado')
    for route in held_locked:route.fulfill(status=423,json={'detail':'LAB bloqueado'})
    for pattern in ('**/lab/perp/profiles','**/lab/perp/datasets'):page.unroute(pattern)
    expect(page.locator('#perp-validation-form button')).to_be_disabled()
    assert not errors and not actions,(errors,actions)
    browser.close()
(directory/('result-reopened.json' if reopen else 'result.json')).write_text(json.dumps({'ok':True,'validations':3,'entry_kind':'LIMIT'if limited else'MARKET','js_errors':errors,'private_actions':actions,'calls':state['calls']}))
