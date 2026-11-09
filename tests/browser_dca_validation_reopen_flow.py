"""Recover a reserve in a new process, then reject stale/tampered displays."""

import json
import os
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
import sqlcipher3

BASE = os.environ['ORBIS_TEST_BASE_URL']
DATA = Path(os.environ['ORBIS_BASIC_DIRECTORY'])
OUT = DATA.parent
PASSWORD = 'owned dca validation browser password'
assert os.environ.get('ORBIS_BASIC_FIXTURE') == '1'
assert os.environ['ORBIS_BASIC_PHASE'] == 'reopen'
original = json.loads((OUT / 'result.json').read_text())
saved = original['saved']
assert original['passed']

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    errors, writes = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' else None)
    page.route('**/market/**', lambda route: route.fulfill(status=503, json={}))
    try:
        for endpoint in ('/lab/status', '/automation/dca/status'):
            assert page.request.get(BASE + endpoint).json()['unlocked'] is False
        assert page.request.get(BASE + '/automation/dca/validations').status == 423
        assert page.request.get(BASE + '/automation/dca/validations/' + saved['id'] + '/reference').status == 423
        assert page.request.get(BASE + '/lab/validations/' + original['lab_references'][0]['artifact_id'] + '/reference').status == 423
        page.goto(BASE + '/ui/#/config')
        page.wait_for_load_state('networkidle')
        for selector, endpoint in [('#cfg-lab-unlock-form', '/lab/unlock'),
                                   ('#cfg-automation-unlock-form', '/automation/dca/unlock')]:
            form = page.locator(selector)
            form.evaluate("el=>{for(let p=el.parentElement;p;p=p.parentElement){if(p.tagName==='DETAILS')p.open=true;}}")
            form.locator('[name="passphrase"]').fill(PASSWORD)
            with page.expect_response(BASE + endpoint) as response:
                form.locator('button[type="submit"]').click()
            assert response.value.status == 200, response.value.text()

        def export_reference(button):
            with page.expect_download() as event:
                button.click()
            path = OUT / ('reopened-' + event.value.suggested_filename)
            event.value.save_as(path)
            return json.loads(path.read_text())

        page.goto(BASE + '/ui/#/laboratorio')
        page.wait_for_load_state('networkidle')
        selection = page.locator('#lab-validation-select')
        selection.evaluate("el=>{for(let p=el.parentElement;p;p=p.parentElement){if(p.tagName==='DETAILS')p.open=true;}}")
        for reference in original['lab_references']:
            selection.select_option(reference['artifact_id'])
            report = page.locator('#lab-validation-report')
            report.get_by_role('button', name='Verificar', exact=True).click()
            expect(report).to_have_attribute('data-integrity', 'verified')
            assert export_reference(report.get_by_role('button', name='Referencia', exact=True)) == reference
        # A previously green report cannot export after its stored result changes.
        artifact_id = original['lab_references'][-1]['artifact_id']
        with sqlcipher3.connect(str(DATA / 'lab.db')) as db:
            db.execute("PRAGMA key = '" + PASSWORD + "'")
            payload = json.loads(db.execute('SELECT result_json FROM validation_runs WHERE id=?', (artifact_id,)).fetchone()[0])
            payload['out_of_sample']['metrics']['net_profit'] = '999999'
            db.execute('UPDATE validation_runs SET result_json=? WHERE id=?', (json.dumps(payload), artifact_id))
        downloads = []
        page.on('download', lambda value: downloads.append(value))
        with page.expect_response(BASE + '/lab/validations/' + artifact_id + '/reference') as response:
            report.get_by_role('button', name='Referencia', exact=True).click()
        assert response.value.status == 409
        expect(report).to_have_attribute('data-integrity', 'error')
        expect(report.get_by_role('button', name='Referencia', exact=True)).to_be_disabled()
        assert not downloads
        prefix = BASE + '/automation/dca/validations/' + saved['id']
        assert page.request.get(prefix).json() == saved
        assert page.request.get(prefix + '/verify').json()['verified']
        page.goto(BASE + '/ui/#/dca')
        page.wait_for_load_state('networkidle')
        page.locator('#dca-validation > summary').click()
        selection = page.locator('#dca-validation-select')
        selection.select_option(saved['id'])
        report = page.locator('#dca-validation-report')
        expect(report).to_have_attribute('data-state', 'verified')
        assert saved['version_id'][:8] in report.inner_text()
        assert export_reference(report.get_by_role('button', name='Referencia', exact=True)) == original['dca_reference']
        downloads.clear()
        late_reference = []
        reference_url = prefix + '/reference'
        page.route(reference_url, lambda route: late_reference.append(route))
        report.get_by_role('button', name='Referencia', exact=True).click()
        deadline = time.monotonic() + 5
        while not late_reference:
            assert time.monotonic() < deadline
            page.wait_for_timeout(20)
        page.goto(BASE + '/ui/#/config')
        page.wait_for_load_state('networkidle')
        late_reference[0].fulfill(response=late_reference[0].fetch())
        page.wait_for_load_state('networkidle')
        assert not downloads
        page.unroute(reference_url)
        page.goto(BASE + '/ui/#/dca')
        page.wait_for_load_state('networkidle')
        page.locator('#dca-validation > summary').click()
        selection = page.locator('#dca-validation-select')
        selection.select_option(saved['id'])
        report = page.locator('#dca-validation-report')
        expect(report).to_have_attribute('data-state', 'verified')
        held = []
        url = prefix + '?require_verified=true&view=summary'
        page.route(url, lambda route: held.append(route))
        selection.select_option('')
        selection.select_option(saved['id'])
        deadline = time.monotonic() + 5
        while not held:
            assert time.monotonic() < deadline
            page.wait_for_timeout(20)
        selection.select_option('')
        with page.expect_response(url):
            held[0].fulfill(response=held[0].fetch())
        page.wait_for_load_state('networkidle')
        expect(report).to_have_attribute('data-state', 'empty')
        assert report.locator('tbody tr').count() == 0
        page.unroute(url)
        selection.select_option(saved['id'])
        expect(report).to_have_attribute('data-state', 'verified')
        assert Path(os.environ['ORBIS_AUTOMATION_PATH']).resolve().is_relative_to(DATA.resolve())
        with sqlcipher3.connect(str(DATA / 'automation.db')) as db:
            db.execute("PRAGMA key = '" + PASSWORD + "'")
            payload = json.loads(db.execute('SELECT result_json FROM dca_validation_runs WHERE id=?', (saved['id'],)).fetchone()[0])
            payload['out_of_sample']['results']['conditional']['metrics']['net_profit'] = '999999'
            db.execute('UPDATE dca_validation_runs SET result_json=? WHERE id=?', (json.dumps(payload), saved['id']))
        with page.expect_response(reference_url) as response:
            report.get_by_role('button', name='Referencia', exact=True).click()
        assert response.value.status == 409
        expect(report).to_have_attribute('data-state', 'error')
        assert report.locator('tbody tr').count() == 0 and not downloads
        selection.select_option('')
        selection.select_option(saved['id'])
        expect(report).to_have_attribute('data-state', 'error')
        assert report.locator('tbody tr').count() == 0
        assert page.request.get(prefix + '?require_verified=true').status == 409
        assert page.request.get(BASE + '/automation/dca/paper/sessions').json() == []
        assert not page.request.get(BASE + '/real/vault/status').json()['exists']
        assert not any('/real/' in url or '/sessions' in url or url.endswith('/validations') for url in writes), writes
        assert not errors, errors
        (OUT / 'reopened.json').write_text(json.dumps({'passed': True,
            'run_id': saved['id'], 'exact_recovery': True, 'stale_response_rejected': True,
            'references_exact_after_reopen': True, 'late_reference_rejected': True,
            'tampered_lab_and_dca_exports_rejected': True,
            'tamper_rejected': True, 'errors': errors}, indent=2))
    except Exception:
        page.screenshot(path=str(OUT / 'reopen-failure.png'), full_page=True)
        (OUT / 'reopen-failure-dom.yml').write_text(page.locator('body').aria_snapshot())
        raise
    finally:
        browser.close()

print('DCA reserve restart: locked, exact recovery, old version, stale response and tamper rejection passed')
