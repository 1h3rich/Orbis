"""Create and inspect a reproducible DCA reserve through actual UI and API."""

import json
import os
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from app.lab.store import content_hash

BASE = os.environ['ORBIS_TEST_BASE_URL']
DATA = Path(os.environ['ORBIS_BASIC_DIRECTORY'])
OUT = DATA.parent
PASSWORD = 'owned dca validation browser password'
assert os.environ.get('ORBIS_BASIC_FIXTURE') == '1'
assert os.environ['ORBIS_BASIC_PHASE'] == 'create'
for key in ('ORBIS_LAB_PATH', 'ORBIS_AUTOMATION_PATH', 'ORBIS_REAL_VAULT_PATH'):
    assert Path(os.environ[key]).resolve().is_relative_to(DATA.resolve())
    assert not Path(os.environ[key]).exists()


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, timezone_id='UTC')
    errors, writes = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' else None)
    page.route('**/market/**', lambda route: route.fulfill(status=503, json={}))
    try:
        page.goto(BASE + '/ui/#/config')
        page.wait_for_load_state('networkidle')
        for selector, endpoint in [('#cfg-lab-create-form', '/lab/create'),
                                   ('#cfg-automation-create-form', '/automation/dca/create')]:
            form = page.locator(selector)
            form.evaluate("el=>{for(let p=el.parentElement;p;p=p.parentElement){if(p.tagName==='DETAILS')p.open=true;}}")
            form.locator('[name="passphrase"]').fill(PASSWORD)
            with page.expect_response(BASE + endpoint) as response:
                form.locator('button[type="submit"]').click()
            assert response.value.status == 200, response.value.text()
        page.goto(BASE + '/ui/#/laboratorio')
        page.wait_for_load_state('networkidle')
        # A user CSV is honestly labelled as imported data, not public MEXC.
        csv = 'time,open,high,low,close,volume\n' + '\n'.join(
            f'{1735689600 + i*86400},{100+i},{102+i},{99+i},{101+i},10' for i in range(100))
        upload = page.locator('#lab-csv')
        upload.evaluate("el=>{for(let p=el.parentElement;p;p=p.parentElement){if(p.tagName==='DETAILS')p.open=true;}}")
        upload.locator('[name="timeframe"]').select_option('1d')
        upload.locator('input[type="file"]').set_input_files({'name': 'daily.csv',
            'mimeType': 'text/csv', 'buffer': csv.encode()})
        with page.expect_response(lambda r: r.request.method == 'POST' and '/lab/datasets' in r.url) as response:
            upload.locator('button[type="submit"]').click()
        assert response.value.status == 200, response.value.text()
        dataset = response.value.json()
        assert dataset['count'] == 100 and not dataset['verified_public']

        # Reference export uses the real application router and encrypted LAB.
        # Optimizer winners are not certificates for the saved base definition.
        def post(path, body):
            result = page.request.post(BASE + path, data=body)
            assert result.status == 200, result.text()
            return result.json()

        def reveal(locator):
            locator.evaluate("el=>{for(let p=el.parentElement;p;p=p.parentElement){if(p.tagName==='DETAILS')p.open=true;}}")

        def export_reference(button, family, artifact_id):
            expect(button).to_be_enabled()
            with page.expect_download() as event:
                button.click()
            download = event.value
            path = OUT / download.suggested_filename
            download.save_as(path)
            reference = json.loads(path.read_text())
            assert reference['schema'] == 'orbis.research.reference.v1'
            assert reference['execution_authority'] == 'NONE'
            assert reference['family'] == family and reference['artifact_id'] == artifact_id
            assert reference['sha256'] == content_hash({k: v for k, v in reference.items() if k != 'sha256'})
            return reference

        definition = {'direction': 'LONG', 'initial_capital': '1000', 'allocation': '0.2',
            'fee_rate': '0.001', 'slippage_rate': '0.001', 'stop_loss': '0.03',
            'take_profit': '0.08', 'entry': {'kind': 'CLOSE_ABOVE_EMA', 'period': 2}}
        lab_artifacts = []
        for direction in ('LONG', 'SHORT'):
            strategy = post('/lab/strategies', {'name': direction + ' referencia',
                'definition': {**definition, 'direction': direction}})
            body = {'dataset_id': dataset['id'], 'version_id': strategy['version_id']}
            validation = post('/lab/validations', body)
            lab_artifacts.append(validation)
            if direction == 'LONG':
                lab_artifacts.append(post('/lab/rolling-validations', {**body, 'folds': 2}))
                optimization = post('/lab/optimizations', {**body,
                    'stop_losses': ['0.02', '0.04'], 'take_profits': ['0.05']})
            post('/lab/strategies/' + strategy['id'] + '/versions', {
                'definition': {**strategy['definition'], 'take_profit': '0.1'}})
        page.reload()
        page.wait_for_load_state('networkidle')
        selection = page.locator('#lab-validation-select')
        reveal(selection)
        lab_references = []
        for validation in lab_artifacts:
            selection.select_option(validation['id'])
            report = page.locator('#lab-validation-report')
            reference_button = report.get_by_role('button', name='Referencia', exact=True)
            expect(reference_button).to_be_disabled()
            report.get_by_role('button', name='Verificar', exact=True).click()
            expect(report).to_have_attribute('data-integrity', 'verified')
            reference = export_reference(reference_button, 'STRATEGY', validation['id'])
            assert reference['version_id'] == validation['version_id']
            assert len(reference['windows']) == (2 if validation['method'] == 'ROLLING_FIXED_VERSION' else 1)
            lab_references.append(reference)
        opt = page.locator('#lab-opt-select')
        reveal(opt)
        opt.select_option(optimization['id'])
        expect(page.locator('#lab-opt-report .lab-verify')).to_be_visible()
        assert page.locator('#lab-opt-report .research-reference').count() == 0

        # A finished replay may arrive after the user deselects the artifact.
        # It must not download or resurrect its previous report.
        held, downloads = [], []
        page.on('download', lambda value: downloads.append(value))
        late_url = BASE + '/lab/validations/' + lab_artifacts[-1]['id'] + '/reference'
        page.route(late_url, lambda route: held.append(route))
        report.get_by_role('button', name='Referencia', exact=True).click()
        deadline = time.monotonic() + 5
        while not held:
            assert time.monotonic() < deadline
            page.wait_for_timeout(20)
        selection.select_option('')
        held[0].fulfill(response=held[0].fetch())
        page.wait_for_load_state('networkidle')
        expect(report).to_have_attribute('data-integrity', 'empty')
        assert not downloads and report.locator('.research-reference').count() == 0
        page.unroute(late_url)

        page.goto(BASE + '/ui/#/dca')
        page.wait_for_load_state('networkidle')
        form = page.locator('#dca-form')
        form.locator('.dca-options summary').click()
        form.locator('[name="frequency"]').select_option('daily')
        for name, value in {'name': 'DCA reserva', 'amount': '100', 'budget_total': '1000',
            'monthly_limit': '1500', 'max_exposure': '1', 'fee_rate': '0', 'slippage_rate': '0',
            'drop1': '', 'extra1': '', 'drop2': '', 'extra2': '', 'max_extra_activations': '0'}.items():
            form.locator('[name="' + name + '"]').fill(value)
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url == BASE + '/automation/dca/rules') as response:
            page.locator('#dca-save').click()
        assert response.value.status == 200, response.value.text()
        rule = response.value.json()
        expect(page.locator('#dca-version')).to_have_value(rule['version_id'])
        page.locator('#dca-validation > summary').click()
        expect(page.locator('#dca-validation-start')).to_be_enabled()
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url == BASE + '/automation/dca/validations') as response:
            page.locator('#dca-validation-start').click()
        assert response.value.status == 200, response.value.text()
        saved = response.value.json()
        report = page.locator('#dca-validation-report')
        expect(report).to_have_attribute('data-state', 'verified')
        expect(report).to_have_attribute('data-run-id', saved['id'])
        expect(report.locator('tbody tr')).to_have_count(3)
        assert saved['train_bars'] == 70 and saved['test_bars'] == 30
        assert saved['out_of_sample']['results']['lump']['events'][0]['execution_time'] == saved['test_start_t']
        assert saved['out_of_sample']['results']['lump']['events'][0]['spent'] == '1000'
        dca_reference = export_reference(report.get_by_role('button', name='Referencia', exact=True), 'DCA', saved['id'])
        page.locator('#dca-validation-fraction').select_option('0.8')
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url == BASE + '/automation/dca/validations') as response:
            page.locator('#dca-validation-start').click()
        assert response.value.status == 422
        expect(report).to_have_attribute('data-state', 'error')
        assert report.locator('tbody tr').count() == 0
        assert len(page.request.get(BASE + '/automation/dca/validations').json()) == 1
        form.locator('[name="amount"]').fill('80')
        with page.expect_response(lambda r: r.request.method == 'POST' and '/versions' in r.url) as response:
            page.locator('#dca-save').click()
        assert response.value.status == 200, response.value.text()
        updated = response.value.json()
        assert updated['version_id'] != saved['version_id']
        page.locator('#dca-validation-select').select_option(saved['id'])
        expect(report).to_have_attribute('data-state', 'verified')
        assert saved['version_id'][:8] in report.inner_text()
        for close in page.get_by_role('button', name='Cerrar aviso', exact=True).all():
            if close.is_visible():
                close.click()
        page.locator('#dca-validation').screenshot(path=str(OUT / 'reserve-desktop.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.locator('#dca-validation').screenshot(path=str(OUT / 'reserve-mobile.png'))
        assert page.request.get(BASE + '/automation/dca/paper/sessions').json() == []
        assert not page.request.get(BASE + '/real/vault/status').json()['exists']
        assert not any('/real/' in url or '/sessions' in url for url in writes), writes
        assert not errors, errors
        for filename in ('lab.db', 'automation.db', 'legacy.db'):
            assert (DATA / filename).read_bytes()[:16] != b'SQLite format 3\0'
        (OUT / 'result.json').write_text(json.dumps({'passed': True, 'saved': saved,
            'lab_references': lab_references, 'dca_reference': dca_reference,
            'late_reference_rejected': True,
            'latest_version': updated['version_id'], 'errors': errors}, indent=2))
    except Exception:
        page.screenshot(path=str(OUT / 'failure.png'), full_page=True)
        (OUT / 'failure-dom.yml').write_text(page.locator('body').aria_snapshot())
        raise
    finally:
        browser.close()

print('DCA reserve: imported dataset, independent capital, exact old version, invalid split and responsive UI passed')
