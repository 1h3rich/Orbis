"""Activate a verified LIMIT reserve and follow its quote-based fill in the UI."""
from decimal import Decimal
import io
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from app.derivatives.perp_observed_export import verify_observed_export
from tests.perp_observed_archive_fixture import LAB_KEY, PAPER_KEY


BASE = os.environ['ORBIS_TEST_BASE_URL']
API = BASE + '/paper/perp/observed'
directory = Path(os.environ['ORBIS_BASIC_DIRECTORY']).parent
reopen = os.environ['ORBIS_BASIC_PHASE'] == 'reopen'
partial = os.environ.get('ORBIS_TEST_PERP_OBSERVED_PARTIAL') == '1'


def advance(page, action):
    response = page.request.post(BASE+'/__observed_fixture/advance',
                                 data={'fixture_id':directory.name, 'action':action})
    assert response.ok, response.text()


def capture_chart(page, name):
    chart = page.locator('#observed-chart')
    chart.evaluate('(node) => node.scrollIntoView({block:"center"})')
    chart.screenshot(path=str(directory/name))


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440, 'height':1000})
    errors = []
    private_writes = []
    page.on('pageerror', lambda error:errors.append(str(error)))
    page.on('response', lambda response:errors.append(response.url) if response.status>=500 else None)
    page.on('request', lambda request:private_writes.append(request.url)
            if '/real/' in request.url and request.method not in ('GET', 'HEAD', 'OPTIONS') else None)
    state = page.request.get(BASE+'/__observed_fixture/state').json()
    assert state['lab_locked'] and state['paper_locked'] and not state['real_exists']
    assert state['coordinator']['running_workers'] == 0 and state['calls'] == []
    page.goto(BASE+'/ui/#/config')
    page.wait_for_load_state('networkidle')
    stores = [('perp', PAPER_KEY)] if reopen else [('lab', LAB_KEY), ('perp', PAPER_KEY)]
    for name, password in stores:
        form = page.locator(f'#cfg-{name}-unlock-form')
        form.evaluate('(form) => form.closest("details").open = true')
        form.locator('[name=passphrase]').fill(password)
        form.locator('button[type=submit]').click()
        expect(page.locator(f'#cfg-{name}-status')).to_contain_text('min restantes')
    page.goto(BASE+'/ui/#/laboratorio')
    page.wait_for_load_state('networkidle')
    paper = page.locator('#lab-perp-observed')
    if reopen:
        saved = json.loads((directory/'owned-limit-session.json').read_text())
        identifier = saved['session']['id']
        paper.locator(':scope > summary').click()
    else:
        panel = page.locator('#lab-perp-validations')
        panel.locator(':scope > summary').click()
        panel.locator('[name=profile_id]').select_option(state['profile_id'])
        panel.locator('[name=dataset_id]').select_option(state['dataset_id'])
        panel.locator('#perp-validation-form button[type=submit]').click()
        panel.locator('#perp-validation-paper').click(timeout=15000)
        expect(paper.locator('#observed-review-values')).to_contain_text('Límite')
        expect(paper.locator('#observed-review-values')).to_contain_text('1 vela')
        if partial:
            expect(paper.locator('#observed-review-values')).to_contain_text('Objetivos parciales')
        with page.expect_response(lambda r:r.url==API+'/activate') as activated:
            paper.locator('#observed-activate').click()
        assert activated.value.status == 202, activated.value.text()
        identifier = activated.value.json()['session_id']
    row = paper.locator(f'[data-observed-session="{identifier}"]')
    expect(row).to_contain_text('Límite', timeout=15000)
    detail_url = API+'/sessions/'+identifier+'/view'
    if not reopen:
        advance(page, 'ENTRY')
        expect(row).to_contain_text('Límite pendiente', timeout=15000)
        row.locator('[data-observed-view]').click()
        expect(paper.locator('#observed-metrics')).to_contain_text('Vencimiento UTC')
        pending = page.request.get(detail_url).json()['session']['checkpoint']
        assert pending['resting'] and not pending['account']['positions']
        assert Decimal(pending['order_reservation']['available']) < Decimal('1000')
        expect(page.locator('#observed-chart')).to_have_attribute('data-entry-count', '0')
        paper.locator('#observed-metrics').screenshot(path=str(directory/'pending-limit.png'))
        advance(page, 'LIMIT_FILL')
        expect(row).to_contain_text('24.999', timeout=15000)
        row.locator('[data-observed-view]').click()
        expect(page.locator('#observed-chart')).to_have_attribute('data-entry-count', '1')
        if partial:
            advance(page, 'PARTIAL_FILL')
            expect(row).to_contain_text('10.001',timeout=15000)
            row.locator('[data-observed-view]').click()
            expect(page.locator('#observed-chart')).to_have_attribute('data-partial-count','2')
            expect(paper.locator('#observed-metrics')).to_contain_text('Cantidad abierta')
            expect(paper.locator('#observed-metrics')).to_contain_text('10.001')
            expect(paper.locator('#observed-trades tbody tr')).to_have_count(0)
            live=page.request.get(detail_url).json()
            assert [event['target_index'] for event in live['session']['checkpoint']['opened']['partial_exits']]==[1,2]
            capture_chart(page,'partial-chart-open.png')
        with page.expect_response(lambda r:r.url.endswith('/'+identifier+'/close')) as closed:
            row.locator('[data-observed-close]').click()
        assert closed.value.status == 200, closed.value.text()
        advance(page, 'EXIT')
    expect(row).to_contain_text('Finalizado', timeout=15000)
    row.locator('[data-observed-view]').click()
    expect(page.locator('#observed-chart')).to_have_attribute('data-entry-count', '1')
    expect(page.locator('#observed-chart')).to_have_attribute('data-exit-count', '3' if partial else '1')
    expect(paper.locator('#observed-trades tbody tr')).to_have_count(1)
    detail = page.request.get(detail_url).json()
    assert detail['session']['engine'] == ('perp-observed-3.0' if partial else 'perp-observed-2.0')
    assert detail['session']['checkpoint']['resting'] is None
    assert [Decimal(point['price']) for point in detail['view']['markers']] == ([99,107,107,103] if partial else [99,103])
    assert Decimal(detail['view']['metrics']['equity']) == Decimal('1159.988' if partial else '1099.996')
    if partial:
        expect(page.locator('#observed-chart')).to_have_attribute('data-partial-count','2')
        assert [point['partial_index'] for point in detail['view']['markers'] if 'partial_index' in point]==[1,2]
        assert detail['view']['markers'][1]['time_ns']==detail['view']['markers'][2]['time_ns']
        paper.locator('#observed-trades summary').click()
        expect(paper.locator('#observed-trades [data-partial-target="2"]')).to_contain_text('10.001')
        capture_chart(page,'partial-chart-reopened.png' if reopen else 'partial-chart-final.png')
    with page.expect_download() as exported:
        paper.locator('#observed-export').click()
    output = directory/('limit-reopened.jsonl' if reopen else 'limit-created.jsonl')
    exported.value.save_as(output)
    verified = verify_observed_export(io.BytesIO(output.read_bytes()))
    assert verified['checkpoint'] == detail['session']['checkpoint']
    if reopen:
        assert detail['session']['checkpoint'] == saved['session']['checkpoint']
        assert output.read_bytes() == (directory/'limit-created.jsonl').read_bytes()
        state = page.request.get(BASE+'/__observed_fixture/state').json()
        assert state['lab_locked'] and state['coordinator']['running_workers'] == 0 and state['calls'] == []
    else:
        (directory/'owned-limit-session.json').write_text(json.dumps(detail, indent=2))
    page.set_viewport_size({'width':390, 'height':844})
    expect(paper.locator('#observed-inspection')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    assert not errors and not private_writes, (errors, private_writes)
    browser.close()
