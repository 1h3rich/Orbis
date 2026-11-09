"""Create an optimized LAB reserve, confirm fictitious execution and reopen it."""
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.perp_observed_archive_fixture import LAB_KEY, PAPER_KEY

BASE = os.environ['ORBIS_TEST_BASE_URL']
ROOT = Path(os.environ['ORBIS_BASIC_DIRECTORY']).parent
REOPEN = os.environ['ORBIS_BASIC_PHASE'] == 'reopen'
API = BASE + '/paper/perp/observed'
GRID = BASE + '/lab/perp/optimizations'
OUTPUT = ROOT / 'optimized-paper.json'


def advance(page, action):
    reply = page.request.post(BASE+'/__observed_fixture/advance', data={'fixture_id':ROOT.name, 'action':action})
    assert reply.ok, reply.text()


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440, 'height':1000})
    errors, activated, real_writes = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('response', lambda response: errors.append((response.url, response.status)) if response.status >= 500 else None)
    page.on('request', lambda request: activated.append(request.url) if request.url == API+'/activate'
            else real_writes.append(request.url) if '/real/' in request.url and request.method != 'GET' else None)
    initial = page.request.get(BASE+'/__observed_fixture/state').json()
    assert initial['lab_locked'] and initial['paper_locked']
    assert initial['calls'] == [] and initial['coordinator']['running_workers'] == 0
    page.goto(BASE+'/ui/#/config')
    page.wait_for_load_state('networkidle')
    for name, password in [('lab', LAB_KEY), ('perp', PAPER_KEY)]:
        form = page.locator('#cfg-'+name+'-unlock-form')
        form.evaluate('(e) => e.closest("details").open = true')
        form.locator('[name=passphrase]').fill(password)
        form.locator('button[type=submit]').click()
        expect(page.locator('#cfg-'+name+'-status')).to_contain_text('min restantes')
    page.goto(BASE+'/ui/#/laboratorio')
    page.wait_for_load_state('networkidle')
    optimizer = page.locator('#lab-perp-optimization')
    optimizer.locator(':scope > summary').click()
    paper = page.locator('#lab-perp-observed')
    review = paper.locator('#observed-review')
    if not REOPEN:
        form = optimizer.locator('#perp-optimization-form')
        form.locator('[name=profile_id]').select_option(initial['profile_id'])
        form.locator('[name=dataset_id]').select_option(initial['dataset_id'])
        form.locator('[name=stops]').fill('1,25 2')
        form.locator('[name=targets]').fill('1 3')
        form.locator('[name=fraction]').fill('50')
        with page.expect_response(lambda response: response.url == GRID and response.request.method == 'POST') as made:
            form.locator('button[type=submit]').click()
        assert made.value.status == 201, made.value.text()
        grid = made.value.json()
        record = grid['optimization']
        assert grid['result']['out_of_sample']['metrics']['trades'] >= 10
        with page.expect_response(lambda response: response.url == GRID+'/'+record['id']+'/variant') as saved:
            optimizer.get_by_role('button', name='Guardar variante').click()
        assert saved.value.status == 201, saved.value.text()
        variant = saved.value.json()

        # A late preparation response cannot put confirmation back after the
        # user closes its originating optimizer panel.
        held = []
        pattern = '**/paper/perp/observed/prepare'
        page.route(pattern, lambda route: held.append(route))
        prepare = optimizer.get_by_role('button', name='Preparar PAPER')
        prepare.click()
        expect(paper.locator('#observed-status')).to_have_text('Verificando reserva…')
        assert len(held) == 1
        optimizer.locator(':scope > summary').click()
        response = held[0].fetch()
        assert response.ok, response.text()
        held[0].fulfill(response=response)
        page.unroute(pattern)
        expect(review).to_be_hidden()
        assert page.request.get(API+'/sessions').json()['total'] == 0
        optimizer.locator(':scope > summary').click()
        optimizer.locator('#perp-optimization-selection').select_option(record['id'])
        with page.expect_response(lambda response: response.url == API+'/prepare') as prepared:
            prepare.click()
        assert prepared.value.ok, prepared.value.text()
        value = prepared.value.json()
        validation = value['validation_id']
        assert value['phase'] == 'READY' and value['session_created'] is False
        assert value['reference']['profile']['strategy_version_id'] == variant['version_id']
        assert value['reference']['windows'][0]['result_hash'] == grid['result']['out_of_sample']['result_hash']
        assert value['optimization_origin']['reserve_run_id'] == variant['reserve_run_id']
        expect(review).to_be_visible()
        expect(review).to_contain_text('Reserva optimizada')
        expect(review).to_contain_text('60 velas')
        expect(review).to_contain_text('Stop %')
        expect(review).to_contain_text('Objetivo %')
        expect(review).to_contain_text('Capital ficticio')
        assert not activated
        preflight = page.request.get(BASE+'/__observed_fixture/state').json()
        assert preflight['calls'] == [] and preflight['observed_sources'] == 0
        assert page.request.get(API+'/sessions').json()['total'] == 0
        review.screenshot(path=str(ROOT/'optimized-paper-review.png'))
        with page.expect_response(lambda response: response.url == API+'/activate') as started:
            paper.get_by_role('button', name='Activar ficticio').click()
        assert started.value.status == 202, started.value.text()
        session_id = started.value.json()['session_id']
        assert len(activated) == 1
        row = paper.locator('[data-observed-session="'+session_id+'"]')
        advance(page, 'ENTRY')
        expect(row).to_contain_text('· LONG', timeout=15000)
        with page.expect_response(lambda response: response.url == API+'/sessions/'+session_id+'/close') as closed:
            row.locator('[data-observed-close]').click()
        assert closed.value.ok, closed.value.text()
        advance(page, 'EXIT')
        expect(row).to_contain_text('Finalizado', timeout=15000)
        assert page.request.get(API+'/sessions').json()['total'] == 1
    else:
        original = json.loads(OUTPUT.read_text())
        validation, session_id, variant = (original[k] for k in ('validation', 'session_id', 'variant'))
        optimizer.locator('#perp-optimization-selection').select_option(variant['optimization_id'])
        expect(optimizer.get_by_role('button', name='Preparar PAPER')).to_be_visible()
        paper.locator(':scope > summary').click()
        row = paper.locator('[data-observed-session="'+session_id+'"]')
        expect(row).to_contain_text('Finalizado')
        assert not activated
    row.locator('[data-observed-view]').click()
    expect(paper.locator('#observed-trades tbody tr')).to_have_count(1)
    view = page.request.get(API+'/sessions/'+session_id+'/view').json()
    params = {'snapshot_sequence':view['session']['sequence'], 'snapshot_hash':view['session']['head_hash']}
    compared = page.request.get(API+'/sessions/'+session_id+'/reserve-comparison', params=params)
    assert compared.ok, compared.text()
    comparison = compared.json()
    assert comparison['validation_id'] == validation and comparison['execution_authority'] == 'NONE'
    linked = page.request.get(BASE+'/lab/perp/validations/'+validation).json()
    assert linked['validation']['optimization_origin']['optimization_id'] == variant['optimization_id']
    proof = {'validation':validation, 'session_id':session_id, 'variant':variant, 'snapshot':params,
             'comparison':comparison}
    if REOPEN:
        assert proof == original
    else:
        OUTPUT.write_text(json.dumps(proof))
    (ROOT/('paper-proof-reopen.json' if REOPEN else 'paper-proof.json')).write_text(json.dumps({
        'validation':validation, 'session_id':session_id, 'linked_optimization':variant['optimization_id'],
        'closed_trades':comparison['paper']['metrics']['closed_trades'], 'activation_posts':len(activated)}))
    state = page.request.get(BASE+'/__observed_fixture/state').json()
    assert not state['real_exists'] and state['coordinator']['running_workers'] == 0
    if REOPEN:
        assert not state['calls'] and state['observed_sources'] == 0
    assert not errors, errors
    assert not real_writes, real_writes
    browser.close()
