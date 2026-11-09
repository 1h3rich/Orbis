"""Navegador → una activación → señal posterior → entrada/salida y recuperación."""
from tests.browser_network import route_once

import json
import os
import re
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

from tests.grvt_bracket_ui_fixture import PASSWORD
from tests.test_grvt_testnet_signer import KEY, SIGNER

BASE = os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
OUTPUT = Path(os.environ.get('ORBIS_TEST_GRVT_DIRECTORY','/tmp'))
lost = os.environ.get('ORBIS_TEST_BOT_LOST_START') == '1'
delayed = os.environ.get('ORBIS_TEST_BOT_DELAYED_START') == '1'
bounded = os.environ.get('ORBIS_TEST_BOUNDED_BOT') == '1'
partial = os.environ.get('ORBIS_TEST_NATIVE_PARTIAL') == '1'
provider_minimum = os.environ.get('ORBIS_TEST_PROVIDER_MINIMUM') == '100'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width':1440,'height':1100})
    errors = []; starts = []; dialogs = []; archived_sources = {}
    page.on('pageerror',lambda error:errors.append(str(error)))
    def accept_dialog(dialog):
        dialogs.append(dialog.message)
        dialog.accept()
    page.on('dialog',accept_dialog)
    page.on('request',lambda request:starts.append(request.url) if request.url.endswith('/bots/start') else None)
    def capture_sources(response):
        if response.ok and '/testnet/bots?sub_account_id=' in response.url:
            for item in response.json()['items']:
                archived_sources[item['id']] = item['source']
    page.on('response',capture_sources)
    source = page.request.get(BASE+'/__test/state').json()['source']
    key = 'fake-testnet-key-'+source['sub_account_id']
    page.goto(BASE+'/ui/#/real'); page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel = page.locator('#real-grvt-bot'); panel.locator(':scope > summary').click()
    expect(panel.locator('[name="session_id"] option').nth(1)).to_be_attached()
    panel.locator('[name="sub_account_id"]').fill(source['sub_account_id'])
    panel.locator('[data-bot-refresh]').click()
    panel.locator('[name="session_id"]').select_option(source['session_id'])
    if bounded:
        panel.locator('[data-bot-form] details > summary').click()
        panel.locator('[name="max_entries"]').fill('2')
        expect(panel.locator('[name="total_entry_cap"]')).to_be_visible()
        panel.locator('[name="total_entry_cap"]').fill('100')
        panel.locator('[name="loss_cap"]').fill('10')
        panel.locator('[name="wait_seconds"]').fill('10800')
        expect(panel.locator('[data-bot-confirm-text]')).to_contain_text('hasta 2 entradas')
    for name,value in {'api_key':key,'dedicated_private_key':KEY.hex(),'expected_signer':SIGNER}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    panel.locator('input[type="checkbox"]').check()
    expect(panel.locator('[data-bot-start]')).to_be_enabled()
    if provider_minimum:
        panel.locator('[data-bot-start]').click()
        expect(panel.locator('[data-bot-notice]')).to_contain_text('Mínimos de GRVT',timeout=10000)
        expect(panel.locator('[data-bot-records]')).to_be_empty()
        expect(panel.locator('[data-bot-fields]')).to_be_enabled()
        assert page.request.get(BASE+'/__test/state').json()['bulk_sends'] == 0
        for name,value in {'api_key':key,'dedicated_private_key':KEY.hex(),'expected_signer':SIGNER}.items():
            panel.locator('[name="'+name+'"]').fill(value)
        panel.locator('[name="entry_cap"]').fill('150')
        panel.locator('[name="exit_cap"]').fill('200')
        panel.locator('input[type="checkbox"]').check()
        expect(panel.locator('[data-bot-start]')).to_be_enabled()
    pending = []
    if lost:
        def lose(route):
            reply = route.fetch(); assert reply.status == 202,reply.text(); route.abort('failed')
        route_once(page,'**/bots/start',lose)
    if delayed: page.route('**/bots/start',lambda route:pending.append(route),times=1)
    panel.locator('[data-bot-start]').click()
    expect(panel.locator('[name="api_key"]')).to_have_value('')
    expect(panel.locator('[name="dedicated_private_key"]')).to_have_value('')
    if delayed:
        expect(panel.locator('[data-bot-notice]')).to_contain_text('Activación sin confirmar',timeout=8000)
        assert pending
        with page.expect_response('**/bots/stop') as stopped:
            panel.locator('[data-bot-stop-all]').click()
        assert stopped.value.status == 200
        pending.pop().continue_()
        page.wait_for_timeout(1800)
        expect(panel.locator('[data-bot-records]')).to_be_empty()
        assert page.request.post(BASE+'/__test/bot-signal').ok
        assert page.request.get(BASE+'/__test/state').json()['bulk_sends'] == 0
    else:
        record = panel.locator('[data-grvt-bot-run]').first
        expect(record).to_contain_text('Esperando señal',timeout=10000)
        assert len(starts) == (2 if provider_minimum else 1)
        stop = record.get_by_role('button',name='Detener ciclo',exact=True)
        stop.focus(); page.wait_for_timeout(1800); expect(stop).to_be_focused()
        assert page.request.get(BASE+'/__test/state').json()['bulk_sends'] == 0
        assert page.request.post(BASE+'/__test/bot-signal').ok
        expect(record).to_contain_text('Observando posición',timeout=15000)
        before = page.request.get(BASE+'/__test/state').json()
        assert before['bulk_sends'] == 1 and before['watch_connected']
        assert before['cancel_sends'] == before['close_sends'] == 0
        record.get_by_role('button',name='Gráfico de fills').click()
        chart = panel.locator('[data-bot-fill-panel]')
        expect(chart.locator('[data-bot-fill-state]')).to_have_text('Sin fills auditados.')
        expect(chart.locator('[data-bot-fill-canvas]')).to_be_hidden()
        chart.get_by_role('button',name='Cerrar gráfico').click()
        assert page.request.post(BASE+'/__test/trigger').ok
        if bounded:
            expect(record).to_contain_text('Esperando señal',timeout=60000 if partial else 25000)
            expect(record).to_contain_text('1/2 entradas')
            assert page.request.post(BASE+'/__test/bot-next').ok
            expect(record).to_contain_text('Observando posición',timeout=15000)
            assert page.request.get(BASE+'/__test/state').json()['bulk_sends'] == 2
            assert page.request.post(BASE+'/__test/trigger').ok
        label = 'Entradas completadas' if bounded else 'Ciclo completado'
        expect(record).to_contain_text(label,timeout=60000 if partial else 25000)
        expect(record).not_to_contain_text('Registro final pendiente')
        durable = page.request.get(BASE+'/__test/bot-records').json()['items']
        assert len(durable) == 1 and durable[0]['status'] == ('ENTRY_LIMIT' if bounded else 'COMPLETE') and durable[0]['terminal']
        if bounded: assert durable[0]['entry_count'] == durable[0]['completed_entries'] == 2
        final = page.request.get(BASE+'/__test/state').json()
        assert final['bulk_sends'] == (2 if bounded else 1) and Decimal(final['position']) == 0
        assert final['cancel_sends'] == final['close_sends'] == ((2 if bounded else 1) if partial else 0)
        assert len(starts) == (2 if provider_minimum else 1)
        expected_roles = ['ENTRY','TAKE_PROFIT','IOC'] if partial else ['ENTRY','TAKE_PROFIT']
        if bounded:
            expected_roles *= 2
        route_once(page,'**/executions?sub_account_id=*',lambda route:route.fulfill(
            status=503,json={'detail':'Archivo TESTNET en uso. Actualiza el gráfico.'}))
        def chart_response(response):
            return '/executions?sub_account_id=' in response.url
        with page.expect_response(lambda response: chart_response(response) and response.status==503) as busy_chart, \
                page.expect_response(lambda response: chart_response(response) and response.ok) as opened_chart:
            record.get_by_role('button',name='Gráfico de fills').click()
        assert 'Archivo TESTNET en uso' in busy_chart.value.json()['detail']
        reply = opened_chart.value
        assert reply.ok and reply.headers['cache-control'] == 'no-store', reply.text()
        assert reply.request.headers['x-orbis-passive'] == '1'
        chart_payload = reply.json()
        assert chart_payload['network']=='TESTNET' and chart_payload['sub_account_id']==source['sub_account_id']
        assert [fill['role'] for fill in chart_payload['executions']] == expected_roles
        assert all(fill['event_time_ns'].isascii() and fill['event_time_ns'].isdecimal() for fill in chart_payload['executions'])
        expect(chart.locator('[data-bot-fill-canvas]')).to_have_attribute('data-fill-count',str(len(expected_roles)))
        expect(chart.locator('canvas').first).to_be_visible()
        assert len({int(fill['event_time_ns']) // 10**9 for fill in chart_payload['executions']}) > 1
        fills_range = chart.get_by_label('Rango visible del gráfico')
        expect(fills_range).to_have_value('all')
        fills_range.select_option('custom')
        chart.get_by_role('button',name='Aplicar',exact=True).click()
        expect(fills_range).to_have_value('custom')
        expect(chart.locator('[data-range-coverage]')).to_contain_text('Cobertura de ejecuciones')
        chart.get_by_role('combobox',name='Ejecución TESTNET').select_option('0')
        first_fill = chart_payload['executions'][0]
        expect(chart.locator('[data-bot-fill-detail]')).to_contain_text(first_fill['size']+' @ '+first_fill['price'])
        expect(chart.locator('[data-bot-fill-detail]')).to_contain_text('Conciliado')

        def research_file():
            with page.expect_download() as download, page.expect_response(
                    lambda response: response.url.endswith('/research-export') and response.ok) as exported:
                chart.get_by_role('button',name='Exportar JSONL').click()
            result = exported.value
            assert result.headers['cache-control']=='no-store' and result.request.headers['x-orbis-passive']=='1'
            assert result.request.post_data_json=={'sub_account_id':source['sub_account_id'],
                                                   'confirmation':'EXPORTAR_JSONL_TESTNET'}
            bundle = result.json()
            content = download.value.path().read_bytes()
            assert content==bundle['jsonl'].encode() and sha256(content).hexdigest()==bundle['sha256']
            assert download.value.suggested_filename=='orbis-grvt-testnet-'+bundle['sha256'][:12]+'.jsonl'
            assert 'sin cifrar' in dialogs[-1]
            return content

        research = research_file()
        samples = list(map(json.loads,research.splitlines()))
        archived_source = archived_sources[durable[0]['id']]
        assert len(samples)==(2 if bounded else 1)
        assert all(sample['environment']=='TESTNET' for sample in samples)
        assert all(sample['provenance']['dataset_sha256']==archived_source['dataset_hash'] for sample in samples)
        assert all(sample['provenance']['strategy_version_id']==archived_source['version_id'] for sample in samples)
        assert [fill['role'] for sample in samples for fill in sample['outcome']['fills']]==expected_roles
        assert durable[0]['id'].encode() not in research and key.encode() not in research
        if partial:
            # La tarjeta del grupo distingue el motivo sin volcar evidencia
            # técnica. El mismo backend conserva petición y recibos cifrados.
            recovery = page.locator('#real-grvt-bracket')
            recovery.locator(':scope > summary').click()
            recovery.locator('[name="sub_account_id"]').fill(source['sub_account_id'])
            recovery.locator('[data-bracket-refresh]').click()
            expect(recovery.locator('[data-bracket-group]').first).to_contain_text('Objetivo parcial')
            recovery.locator(':scope > summary').click()
        chart.locator('[data-bot-fill-canvas]').screenshot(path=str(OUTPUT/'orbis-grvt-bot-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
        chart.locator('[data-bot-fill-canvas]').screenshot(path=str(OUTPUT/'orbis-grvt-bot-mobile.png'))
        page.locator('#real-lock').click()
        expect(page.locator('[data-bot-fill-canvas]')).to_have_count(0)
        page.locator('#real-passphrase').fill(PASSWORD)
        page.get_by_role('button',name='Desbloquear',exact=True).click()
        panel = page.locator('#real-grvt-bot'); panel.locator(':scope > summary').click()
        # El registro se recupera por subcuenta; nunca se rellena una credencial.
        panel.locator('[name="sub_account_id"]').fill(source['sub_account_id'])
        panel.locator('[data-bot-refresh]').click()
        expect(panel.locator('[data-grvt-bot-run]')).to_contain_text(label,timeout=10000)
        assert page.request.get(BASE+'/__test/state').json()['bulk_sends'] == (2 if bounded else 1)
        with page.expect_response(lambda response: '/executions?sub_account_id=' in response.url and response.ok) as reopened_chart:
            panel.locator('[data-grvt-bot-run]').first.get_by_role('button',name='Gráfico de fills').click()
        assert reopened_chart.value.ok and reopened_chart.value.json() == chart_payload
        expect(panel.locator('[data-bot-fill-canvas]')).to_have_attribute('data-fill-count',str(len(expected_roles)))
        chart = panel.locator('[data-bot-fill-panel]')
        assert research_file() == research
        panel.locator('[name="sub_account_id"]').fill('99999999')
        expect(panel.locator('[data-bot-fill-panel]')).to_be_hidden()
        panel.locator('[name="sub_account_id"]').fill(source['sub_account_id'])
        panel.locator('[data-bot-refresh]').click()
        if bounded:
            record = panel.locator('[data-grvt-bot-run]').first
            expect(panel.locator('[data-bot-start]')).to_be_disabled()
            record.get_by_role('button',name='Rearmar subcuenta',exact=True).click()
            expect(panel.locator('[data-bot-notice]')).to_contain_text('requiere nueva activación')
            expect(panel.locator('[data-bot-start]')).to_be_enabled()
            assert len(starts) == 1 and page.request.get(BASE+'/__test/state').json()['bulk_sends'] == 2
    storage = page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (key,KEY.hex(),SIGNER): assert secret not in storage
    assert not errors,errors
    if not delayed:
        (OUTPUT/'testnet-bot-chart-result.json').write_text(json.dumps({
            'passed':True,'fills':len(expected_roles),'roles':expected_roles,
            'reopened':True,'account_switch_cleared':True,'network':'TESTNET',
            'research_jsonl_rows':len(samples),'research_jsonl_stable_after_reopen':True,
            'research_jsonl_sha256':sha256(research).hexdigest(),
            'external_financial_transactions':False,
        },indent=2)+'\n')
    browser.close()
