"""Compare the historical holdout with one observed PAPER DCA day in the UI."""

from decimal import Decimal, localcontext
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


BASE = os.environ['ORBIS_TEST_BASE_URL']
OUT = Path(os.environ['ORBIS_AUTOMATION_PATH']).parent.parent
START, DAY = 1_735_689_600, 86400


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440, 'height':900}, timezone_id='UTC')
    errors, writes = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' else None)
    page.route('**/market/**', lambda route: route.fulfill(status=503, json={}))
    page.goto(BASE + '/ui/#/dca')
    plan = page.locator('#dca-rule')
    plan.select_option(index=1)
    expect(page.locator('#dca-version')).not_to_have_value('')
    page.locator('#dca-validation > summary').click()
    with page.expect_response(lambda response: response.url.endswith('/validations')
                              and response.request.method == 'POST') as validation:
        page.locator('#dca-validation-start').click()
    assert validation.value.ok, validation.value.text()
    artifact = validation.value.json()
    expect(page.locator('#dca-validation-report')).to_have_attribute('data-state','verified')
    page.locator('#dca-paper-dataset').select_option(index=1)
    with page.expect_response(lambda response: response.url.endswith('/paper/sessions')
                              and response.request.method == 'POST') as activated:
        page.locator('#dca-paper-start').click()
    assert activated.value.ok, activated.value.text()
    session = activated.value.json()
    endpoint = BASE + '/automation/dca/paper/sessions/' + session['id'] + '/reserve-comparison'
    writes.clear()
    panel = page.locator('#dca-paper-reserve')
    choice = panel.locator('select')
    report = panel.locator('#dca-paper-reserve-result')
    table = report.get_by_role('table', name='Reserva LAB y PAPER DCA')

    def compare():
        with page.expect_response(lambda response: response.url.startswith(endpoint + '?')) as reply:
            panel.locator('#dca-paper-reserve-compare').click()
        assert reply.value.ok, reply.value.text()
        expect(table).to_be_visible()
        return reply.value.json()

    panel.locator('summary').click()
    choice.select_option(artifact['id'])
    expect(choice.locator('option')).to_have_count(2)
    empty = compare()
    expect(report).to_contain_text('Sin observaciones')
    assert empty['paper']['mark_bid'] is None
    assert page.request.post(BASE + '/__test/advance_paper_dca', data={'now':START + 101 * DAY + 1}).ok
    expect(page.locator('#dca-paper-report').get_by_text('BUY',exact=True)).to_have_count(1, timeout=25000)
    expect(table).to_have_count(0)
    observed = compare()
    assert observed['execution_authority'] == 'NONE'
    assert observed['paper']['observed_days'] == 1 and observed['paper']['mark_bid'] == '70'
    assert observed['reserve']['days'] == 30 and observed['reserve']['result_hash'] == artifact['result_hash']
    paper = observed['paper']['metrics']
    with localcontext() as context:
        context.prec = 300
        assert Decimal(paper['final_equity']) == Decimal(paper['remaining_cash']) + 70 * Decimal(paper['units'])
    expect(table.get_by_role('row', name='Días 30 1')).to_be_visible()
    expect(table.get_by_role('row', name='Compras', exact=False)).to_be_visible()
    assert not writes and not errors, (writes,errors)
    page.reload()
    page.locator('#dca-paper-session').select_option(session['id'])
    panel.locator('summary').click()
    choice.select_option(artifact['id'])
    assert compare() == observed
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    assert page.request.post(BASE + '/lab/lock').ok
    expect(report).to_have_text('Desbloquea LAB',timeout=25000)
    assert page.request.get(endpoint, params={'validation_id':artifact['id']}).status == 503
    (OUT/'paper-reserve-result.json').write_text(json.dumps({
        'verified_reserve':True,'observed_days':1,'mark_bid':'70','restored_after_reload':True,
        'lab_lock_clears_report':True,'no_browser_writes_during_comparison':True,
    },indent=2))
    print('DCA LAB reserve and PAPER bid valuation verified, including reload and LAB lock.')
    browser.close()
