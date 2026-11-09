"""Browser authorization, lost reply, vault lock and bounded resume."""
import json
import os
from pathlib import Path
import time

from playwright.sync_api import expect, sync_playwright
from tests.browser_network import route_once
from tests.test_grvt_perp_limit_entry import CID
from tests.test_grvt_testnet_bracket_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY, SIGNER

BASE = os.environ['ORBIS_TEST_BASE_URL']
OUT = Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])


def wait_for_state(test, timeout):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if test():
            return
        time.sleep(.05)
    raise AssertionError('El servidor controlado no alcanzó el estado esperado')


with sync_playwright() as driver:
    browser = driver.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440,'height':1024})
    errors, starts, prepares, resumes = [],[],[],[]
    page.on('dialog',lambda dialog:dialog.accept())
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:starts.append(request.url) if request.url.endswith('/limit/start') else None)
    page.on('request',lambda request:resumes.append(request.post_data_json)
            if request.method=='POST' and request.url.endswith('/limit/resume/start') else None)
    page.on('request',lambda request:prepares.append(request.url) if request.method=='POST' and request.url.endswith('/limit/preview') else None)
    state_url = BASE+'/__test/state'
    fixture = page.request.get(state_url).json()
    assert fixture['fixture_id'] == OUT.name
    partial = fixture['partial']
    delayed_sources = []
    exact_query = '**/limit/sources?session_id='+fixture['session_id']

    def open_panel(*, handoff=False):
        page.locator('#real-passphrase').fill(PASSWORD)
        unlock = page.get_by_role('button',name='Desbloquear',exact=True)
        if handoff:
            route_once(page, exact_query, delayed_sources.append)
            with page.expect_request(exact_query):
                unlock.click()
        else:
            unlock.click()
        panel = page.locator('#real-grvt-mainnet')
        if not panel.evaluate('(el) => el.open'):
            panel.locator(':scope > summary').click()
        panel.locator('[name="sub_account_id"]').fill('123456')
        if handoff:
            assert len(delayed_sources) == 1
            delayed_sources.pop().continue_()
        panel.locator('[data-mainnet-refresh]').click()
        panel.locator('[data-mainnet-credentials] summary').click()
        for name,value in {'api_key':'synthetic-mainnet-api-key','dedicated_private_key':KEY.hex(),'expected_signer':SIGNER}.items():
            panel.locator('[name="'+name+'"]').fill(value)
        with page.expect_response('**/grvt/mainnet/connect') as connection:
            panel.locator('[data-mainnet-connect]').click()
        assert connection.value.status == 200,(connection.value.status,connection.value.text(),page.request.get(state_url).json())
        expect(panel.locator('[data-mainnet-connection]')).to_have_text('Firma conectada',timeout=25000)
        limit = panel.locator('[data-mainnet-limit]')
        if not limit.evaluate('(el) => el.open'):
            limit.locator(':scope > summary').click()
        return limit

    page.goto(BASE+'/ui/#/perpetuos')
    page.locator('#perp-observed-library > summary').click()
    paper = page.locator('[data-observed-session="'+fixture['session_id']+'"]')
    paper.locator('[data-observed-view]').click()
    page.get_by_role('button',name='Preparar GRVT REAL').click()
    page.wait_for_url('**/#/real')
    expect(page.get_by_text('Prueba PAPER seleccionada para GRVT.')).to_be_visible()
    assert fixture['session_id'] not in page.url and not starts and not prepares
    page.wait_for_load_state('networkidle')
    limit = open_panel(handoff=True)
    expect(limit.locator('[name="limit_session_id"]')).to_have_value(fixture['session_id'])
    expect(limit.locator('[data-limit-handoff]')).to_contain_text('PAPER → REAL · LIMIT')
    expect(limit.locator('[name="limit_dedicated_account_confirmed"]')).not_to_be_checked()
    expect(limit.locator('[data-limit-preview]')).to_be_disabled()
    assert not starts and not prepares
    limit.locator('[name="limit_dedicated_account_confirmed"]').check()
    limit.locator('[data-limit-form] summary').click()
    limit.locator('[name="limit_wait_seconds"]').fill('80')
    if partial:
        expect(limit.locator('[name="limit_session_id"] option:checked')).to_contain_text('2 parciales')
        expect(limit.locator('[name="limit_reserved_close_attempts"]')).to_have_value('2')
        limit.locator('[name="limit_max_reductions"]').fill('6')
    limit.locator('[data-limit-preview]').click()
    expect(limit.locator('[data-limit-review]')).to_be_visible(timeout=25000)
    expect(limit.locator('[data-limit-confirm]')).to_be_disabled()
    expect(limit.locator('[data-limit-values]')).to_contain_text('Nominal entrada / salida')
    expect(limit.locator('[data-limit-values]')).to_contain_text('Subcuenta')
    expect(limit.locator('[data-limit-values]')).to_contain_text('123456')
    evidence = limit.locator('[data-grvt-perp-review-evidence]')
    expect(evidence.locator('summary')).to_have_text('Origen LAB y PAPER · v1')
    evidence.locator('summary').click()
    expect(evidence).to_contain_text('Operaciones cerradas')
    expect(evidence).to_contain_text('Reserva 1')
    expect(evidence.locator('[title="'+fixture['session_id']+'"]')).to_be_visible()
    if partial:
        expect(limit.locator('[data-limit-values]')).to_contain_text('Objetivos parciales')
        expect(limit.locator('[data-limit-values]')).to_contain_text('Intentos para cierre')
        expect(limit.locator('[data-limit-values]')).to_contain_text('25 % de la posición')
    limit.locator('[data-limit-review]').screenshot(path=str(OUT/'limit-review.png'))
    limit.locator('[name="limit_entry_cash_limit"]').fill('24')
    expect(limit.locator('[data-limit-review]')).to_be_hidden()
    limit.locator('[data-limit-preview]').click()
    expect(limit.locator('[data-limit-review]')).to_be_visible(timeout=25000)
    limit.locator('[name="limit_authorize"]').check()

    def reject(route):
        body = {**route.request.post_data_json,'review_mac':'0'*64}
        rejected = route.fetch(post_data=body)
        assert rejected.status == 409,rejected.text()
        route.fulfill(response=rejected)

    route_once(page,'**/bots/perp/limit/start',reject)
    limit.locator('[data-limit-confirm]').click()
    expect(limit.locator('[data-limit-notice]')).to_contain_text('Bot LIMIT sin confirmar')
    expect(limit.locator('[data-limit-preview]')).to_be_enabled()
    expect(limit.locator('[data-limit-job]')).to_be_hidden()
    assert page.request.get(state_url).json()['entry_posts'] == 0 and len(starts) == 1

    limit.locator('[data-limit-preview]').click()
    expect(limit.locator('[data-limit-review]')).to_be_visible(timeout=25000)
    limit.locator('[name="limit_authorize"]').check()

    def lost(route):
        accepted = route.fetch()
        assert accepted.status == 202,accepted.text()
        route.abort('failed')

    route_once(page,'**/bots/perp/limit/start',lost)
    limit.locator('[data-limit-confirm]').click()
    expect(limit.locator('[data-limit-job]')).to_have_text('Esperando señal',timeout=25000)
    expect(limit.locator('[data-limit-notice]')).to_have_text('Autorización registrada · respuesta recuperada')
    assert page.request.get(state_url).json()['entry_posts'] == 0 and len(starts) == 2
    assert page.request.post(BASE+'/__test/emit').ok
    wait_for_state(lambda:page.request.get(state_url).json()['at_pending'],30)
    assert page.request.get(state_url).json()['entry_posts'] == 1
    page.locator('#real-lock').click()
    expect(page.locator('#real-passphrase')).to_be_visible()
    wait_for_state(lambda:not page.request.get(state_url).json()['running'],15)
    limit = open_panel()
    with page.expect_response('**/limit/attempts?sub_account_id=123456&offset=0') as checked_history:
        limit.locator('[data-limit-refresh]').click()
    history_response = checked_history.value
    assert history_response.status == 200,history_response.text()
    expect(limit.locator('[data-limit-items]')).to_contain_text('Interrumpido',timeout=25000)
    history_snapshots = []

    def read_interrupted_attempt():
        response = page.request.get(history_response.url, headers=history_response.request.headers)
        assert response.ok,response.text()
        snapshot = response.json()
        if snapshot['pending']:
            return False
        history_snapshots.append(snapshot)
        return True

    wait_for_state(read_interrupted_attempt,10)
    old_history, = history_snapshots
    (OUT/'resume-history.json').write_text(json.dumps(old_history))
    assert old_history['items'][0]['client_order_id'] == CID and old_history['job']['client_order_id'] == CID
    assert not old_history['job']['active']
    limit.get_by_role('button',name='Retomar intento').click()
    if partial:
        expect(limit.locator('[name="limit_resume_max"]')).to_have_value('6')
        expect(limit.locator('[name="limit_resume_max"]')).to_have_attribute('readonly','')
    limit.locator('[data-limit-recovery-preview]').click()
    expect(limit.locator('[data-limit-review-title]')).to_have_text('Retomar · REAL',timeout=25000)
    expect(limit.locator('[data-grvt-perp-review-evidence]')).to_have_count(0)
    expect(limit.locator('[data-limit-values]')).to_contain_text(CID)
    limit.locator('[name="limit_authorize"]').check()
    def lost_resume(route):
        accepted = route.fetch()
        assert accepted.status == 202,accepted.text()
        assert accepted.json()['review_id'] == route.request.post_data_json['review']['review_id']
        assert accepted.json()['review_id'] != old_history['job']['review_id']
        route_once(page,'**/limit/attempts?sub_account_id=123456&offset=0',lambda read:read.fulfill(json=old_history))
        route.abort('failed')

    route_once(page,'**/bots/perp/limit/resume/start',lost_resume)
    with page.expect_response('**/limit/attempts?sub_account_id=123456&offset=0') as stale:
        limit.locator('[data-limit-confirm]').click()
    assert stale.value.json() == old_history
    expect(limit.locator('[data-limit-job]')).to_have_text('Activación pendiente de confirmar')
    limit.locator('[data-limit-refresh]').click()
    expect(limit.locator('[data-limit-notice]')).to_have_text('Autorización registrada · respuesta recuperada')
    expect(limit.locator('[data-limit-items]')).to_contain_text('0.67 USDT' if partial else '0.025 USDT',timeout=60000)
    final = page.request.get(state_url).json()
    assert final['entry_posts'] == 1 and final['cancellations'] == 1 and final['exit_posts'] == (4 if partial else 2)
    assert final['order_history_pages'] >= 6
    assert not final['positions'] and len(starts) == 2 and len(resumes) == 1
    limit.get_by_role('button',name='Ver gráfico',exact=True).click()
    chart = page.locator('[data-mainnet-chart]')
    canvas = chart.locator('[data-real-chart-canvas]')
    expect(canvas).to_have_attribute('data-fill-count','6' if partial else '4',timeout=25000)
    expect(canvas).to_have_attribute('data-buy-count','2')
    expect(canvas).to_have_attribute('data-sell-count','4' if partial else '2')
    if partial:
        expect(canvas).to_have_attribute('data-partial-count','3')
        select = chart.locator('[data-real-chart-fill]')
        expect(select.locator('option').filter(has_text='Parcial 1')).to_have_count(2)
        expect(select.locator('option').filter(has_text='Parcial 2')).to_have_count(1)
        expect(select.locator('option').filter(has_text='Cierre final')).to_have_count(1)
    expect(chart.locator('[data-real-chart-title]')).to_contain_text('LIMIT')
    with page.expect_download() as downloaded:
        chart.get_by_role('button',name='Exportar JSONL',exact=True).click()
    archive=Path(downloaded.value.path()).read_bytes()
    rows=[json.loads(line) for line in archive.splitlines()]
    assert len(rows)==(6 if partial else 4) and CID not in archive.decode()
    assert all(row['entry_kind']=='LIMIT' and row['network']=='MAINNET' and
               row['provenance']['paper_session_id']==fixture['session_id'] for row in rows)
    if partial:
        assert [row['fill']['target_index'] for row in rows if row['fill']['role']=='REDUCTION']==[1,1,2,None]
    assert canvas.bounding_box()['height'] >= 420
    chart.screenshot(path=str(OUT/'limit-fills.png'))
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_function('document.documentElement.scrollWidth <= innerWidth+2',timeout=8000)
    chart.screenshot(path=str(OUT/'limit-fills-mobile.png'))
    limit.locator('.grvt-limit-attempt').screenshot(path=str(OUT/'limit-receipt-mobile.png'))
    storage = page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (KEY.hex(),SIGNER,'synthetic-mainnet-api-key'):
        assert secret not in storage
    assert not errors,errors
    (OUT/'limit-browser-proof.json').write_text(json.dumps({'ok':True,'state':final,'start_requests':len(starts),
        'page_errors':errors,'controlled_grvt':True,'resume_requests':len(resumes),'stale_resume_ignored':True,'research_export_fills':len(rows),'chart_fills':6 if partial else 4,
        'partial_fills':3 if partial else 0,'mobile_overflow':False},indent=2))
    browser.close()
