"""Create and reopen a saved derivative grid through the private LAB UI."""
import json
import os
from decimal import Decimal
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from app.lab.store import content_hash
from tests.perp_dataset_fixture import PASSWORD


base = os.environ['ORBIS_TEST_BASE_URL']
directory = Path(os.environ['ORBIS_BASIC_DIRECTORY']).parent
reopen = os.environ.get('ORBIS_BASIC_PHASE') == 'reopen'
output = directory / 'created-optimization.json'
with_variant = os.environ.get('ORBIS_TEST_PERP_OPTIMIZATION_VARIANT') == '1'
errors, mutations = [], []

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: mutations.append(request.url)
            if '/real/' in request.url and request.method not in ('GET', 'HEAD', 'OPTIONS') else None)
    assert page.request.get(base+'/__perp_validation_fixture/state').json()['locked']
    assert page.request.get(base+'/lab/perp/optimizations').status == 423
    page.goto(base+'/ui/#/config')
    page.wait_for_load_state('networkidle')
    form = page.locator('#cfg-lab-unlock-form' if reopen else '#cfg-lab-create-form')
    form.evaluate('(e) => e.closest("details").open = true')
    form.locator('[name=passphrase]').fill(PASSWORD)
    form.locator('button[type=submit]').click()
    expect(page.locator('#cfg-lab-status')).to_contain_text('desbloqueado')
    page.goto(base+'/ui/#/laboratorio')
    page.wait_for_load_state('networkidle')
    endpoint = base+'/lab/perp/optimizations'
    if not reopen:
        page.locator('#lab-perp-data > summary').click()
        form = page.locator('#perp-data-form')
        form.locator('[name=start]').fill('2025-01-01T00:00')
        form.locator('[name=end]').fill('2025-01-06T00:00')
        with page.expect_response(lambda response: response.url.endswith('/lab/perp/datasets/download')) as fetched:
            form.locator('button[type=submit]').click()
        assert fetched.value.ok, fetched.value.text()
        data = fetched.value.json()
        form = page.locator('#lab-strategy-form')
        form.locator('[name=name]').fill('Cuadrícula perpetua de prueba')
        rule = form.locator('.lab-rule').first
        rule.locator('[name=kind]').select_option('BULLISH_STREAK')
        rule.locator('[name=period]').fill('2')
        for key, value in [('initial_capital','1000'), ('allocation','0.5'), ('fee_rate','0'),
                           ('slippage_rate','0'), ('stop_loss','0.5'), ('take_profit','0.5')]:
            form.locator('[name='+key+']').fill(value)
        with page.expect_response(lambda response: response.url.endswith('/lab/strategies')
                                  and response.request.method == 'POST') as made:
            page.locator('#lab-save-strategy').click()
        assert made.value.ok, made.value.text()
        version = made.value.json()
        created = page.request.post(base+'/lab/perp/profiles', data={
            'version_id': version['version_id'], 'margin_mode':'ISOLATED', 'leverage':'2',
            'trade_path':'O_H_L_C', 'mark_path':'O_H_L_C'})
        assert created.ok, created.text()
        profile = created.json()
    page.locator('#lab-perp-optimization > summary').click()
    panel = page.locator('#lab-perp-optimization')
    form = panel.locator('form')
    status = panel.locator('#perp-optimization-status')
    selection = panel.locator('#perp-optimization-selection')
    if reopen:
        original = json.loads(output.read_text())
        identifier = original['optimization']['id']
        expect(selection.locator('option')).to_have_count(2)
        selection.select_option(identifier)
    else:
        expect(form.locator('[name=profile_id] option')).to_have_count(2)
        form.locator('[name=profile_id]').select_option(profile['id'])
        form.locator('[name=dataset_id]').select_option(data['id'])
        form.locator('[name=stops]').fill('1,25 2')
        form.locator('[name=targets]').fill('1 3')
        form.locator('[name=fraction]').fill('50')
        with page.expect_response(lambda response: response.url == endpoint
                                  and response.request.method == 'POST') as made:
            form.locator('button[type=submit]').click()
        assert made.value.status == 201, made.value.text()
        value = made.value.json()
        identifier = value['optimization']['id']
        assert value['result']['plan']['stop_losses'] == ['0.0125','0.02']
        winner = value['result']['candidates'][value['result']['selected_index']]
        assert winner['take_profit'] == '0.01'
        assert Decimal(value['result']['out_of_sample']['metrics']['net_equity_change']) > 0
    expect(panel.locator('#perp-optimization-result')).to_be_visible()
    expect(panel.locator('#perp-optimization-grid tbody tr')).to_have_count(4)
    expect(panel.locator('#perp-optimization-grid tbody')).to_contain_text('Elegida')
    expect(panel.locator('#perp-optimization-grid tbody')).to_contain_text('1,25')
    heatmap = panel.locator('#perp-optimization-map')
    expect(heatmap.get_by_role('table', name='Mapa de calor · Neto de desarrollo')).to_be_visible()
    expect(heatmap.locator('tbody th')).to_have_text(['1,25 %', '2 %'])
    expect(heatmap.locator('[data-selected]')).to_have_count(1)
    results = original['result'] if reopen else value['result']
    assert heatmap.locator('td').evaluate_all('cells => cells.map(cell => cell.title)') == [
        'Neto exacto · ' + candidate['development']['metrics']['net_equity_change']
        for candidate in results['candidates']
    ]
    expect(form.locator('[name=stops]')).to_have_value('1,25 2')
    expect(form.locator('[name=fraction]')).to_have_value('50')
    assert form.locator('[name=profile_id]').input_value()
    assert form.locator('[name=dataset_id]').input_value()
    expect(panel.locator('#perp-optimization-reserve tbody tr')).to_have_count(1)
    expect(status).to_contain_text('guardado en LAB')
    assert panel.get_by_role('button', name='Probar en PAPER').count() == 0
    if with_variant:
        if reopen:
            variant = original['variant']
            result = original['result']
        else:
            result = value['result']
            with page.expect_response(lambda response: response.url == endpoint+'/'+identifier+'/variant'
                                      and response.request.method == 'POST') as persisted:
                panel.get_by_role('button', name='Guardar variante').click()
            assert persisted.value.status == 201, persisted.value.text()
            variant = persisted.value.json()
            retry = page.request.post(endpoint+'/'+identifier+'/variant',
                                     data={'expected_hash':value['optimization']['hash']})
            assert retry.ok and retry.json() == variant
            assert variant['version'] == 2 and variant['strategy_id'] == version['id']
            assert variant['execution_authority'] == 'NONE'
            # A newer manual edit must not be loaded in place of the selected variant.
            definition = {**version['definition'], 'take_profit':'0.4'}
            edited = page.request.post(base+'/lab/strategies/'+version['id']+'/versions',
                                      data={'definition':definition})
            assert edited.ok, edited.text()
            assert edited.json()['version'] == 3
        expect(panel.locator('#perp-optimization-version-badge')).to_have_text('v2 · LAB')
        expect(panel.get_by_role('button', name='Guardar variante')).not_to_be_visible()
        chosen = result['candidates'][result['selected_index']]
        checked = {}
        for label, key, score in (
            ('Ver desarrollo','development_run_id', chosen['development']),
            ('Ver reserva','reserve_run_id', result['out_of_sample']),
        ):
            with page.expect_response(lambda response: response.url.endswith('/lab/perp/runs/'+variant[key])) as opened:
                panel.get_by_role('button', name=label, exact=True).click()
            assert opened.value.ok, opened.value.text()
            report = opened.value.json()['execution']['result']
            assert content_hash(report) == score['result_hash']
            chart = page.locator('#perp-run-chart')
            expect(chart).to_have_attribute('data-run-id', variant[key])
            expect(chart).to_have_attribute('data-entry-count', str(score['metrics']['trades']))
            expect(chart).to_have_attribute('data-exit-count', str(score['metrics']['trades']))
            expect(chart.locator('canvas').first).to_be_visible()
            checked[key] = {'trades':score['metrics']['trades'], 'result_hash':content_hash(report)}
        page.locator('#perp-run-chart').screenshot(path=str(directory/('variant-chart-reopened.png' if reopen else 'variant-chart.png')))
        panel.get_by_role('button', name='Abrir versión').click()
        expect(status).to_have_text('Versión abierta · v2')
        expect(page.locator('#lab-version')).to_have_value(variant['version_id'])
        expect(page.locator('#lab-strategy-form [name=take_profit]')).to_have_value(chosen['take_profit'])
        (directory/('variant-proof-reopened.json' if reopen else 'variant-proof.json')).write_text(
            json.dumps({'checked':checked, 'variant':variant, 'newer_version_excluded':True}))
    button = panel.get_by_role('button', name='Exportar JSON')
    page.once('dialog', lambda dialog: dialog.dismiss())
    button.click()
    expect(panel.locator('#perp-optimization-result')).to_be_visible()
    page.once('dialog', lambda dialog: dialog.accept())
    with page.expect_download() as exported:
        button.click()
    assert exported.value.suggested_filename == f'orbis-perp-optimization-{identifier}.json'
    saved = json.loads(exported.value.path().read_text())
    assert saved['result']['execution_authority'] == 'NONE'
    if reopen:
        assert saved == original
    else:
        output.write_text(json.dumps(saved))
    page.set_viewport_size({'width':390, 'height':844})
    panel.screenshot(path=str(directory/('optimization-reopened.png' if reopen else 'optimization-created.png')))
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    if reopen:
        pending = []
        url = '**/lab/perp/optimizations/' + identifier
        page.route(url, lambda route: pending.append(route))
        selection.select_option('')
        selection.select_option(identifier)
        expect(status).to_have_text('Reproduciendo…')
        assert pending
        page.locator('#lab-perp-optimization > summary').click()
        pending[0].fulfill(status=200, json={'optimization':saved['optimization'],'result':saved['result']})
        page.unroute(url)
        expect(panel.locator('#perp-optimization-grid')).to_be_empty()
        expect(panel.locator('#perp-optimization-map')).to_be_empty()
        page.goto(base+'/ui/#/config')
        page.locator('#cfg-lab-lock').evaluate('(e) => e.closest("details").open = true')
        page.locator('#cfg-lab-lock').click()
        expect(page.locator('#cfg-lab-status')).not_to_contain_text('desbloqueado')
        assert page.request.get(endpoint+'/'+identifier).status == 423
    assert not errors, errors
    assert not mutations, mutations
    state = page.request.get(base+'/__perp_validation_fixture/state').json()
    assert not state['real_exists']
    assert state['calls'] == (0 if reopen else 5)
    browser.close()
