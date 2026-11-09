"""Browser → sealed budget → independent future signal → actual close/receipt."""
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_network import route_once
from tests.test_grvt_mainnet_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER

BASE=os.environ['ORBIS_TEST_BASE_URL'];OUT=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1100})
    errors=[];starts=[];prepares=[]
    page.on('dialog',lambda dialog:dialog.accept())
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:starts.append(request.url) if request.url.endswith('/bots/perp/start') else None)
    page.on('request',lambda request:prepares.append(request.url) if request.method=='POST' and request.url.endswith('/bots/perp/preview') else None)
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==OUT.name
    page.goto(BASE+'/ui/#/perpetuos')
    page.locator('#perp-observed-library > summary').click()
    page.locator('[data-observed-session="'+fixture['session_id']+'"] [data-observed-view]').click()
    page.get_by_role('button',name='Preparar GRVT REAL').click()
    page.wait_for_url('**/#/real')
    expect(page.get_by_text('Prueba PAPER seleccionada para GRVT.')).to_be_visible()
    assert fixture['session_id'] not in page.url and not starts and not prepares
    page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet')
    bot=panel.locator('[data-mainnet-bots]')
    expect(bot.locator('[data-bot-handoff]')).to_contain_text('PAPER → REAL · MARKET')
    expect(bot.locator('[name="bot_model"]')).to_have_value('PERPETUAL')
    expect(bot.locator('[name="bot_session_id"]')).to_have_value(fixture['session_id'])
    expect(bot.locator('[data-bot-prepare]')).to_be_disabled()
    route_once(page,'**/bots/perp/sources?session_id='+fixture['session_id'],lambda route:route.fulfill(json={
        'execution_authority':'NONE','items':[{'session_id':'00000000-0000-4000-8000-000000000001'}]}))
    bot.get_by_role('button',name='Verificar sesión seleccionada').click()
    expect(bot.locator('[data-bot-notice]')).to_have_text('No se pudo verificar la sesión seleccionada')
    expect(bot.locator('[name="bot_session_id"]')).to_have_value('')
    expect(bot.locator('[data-bot-prepare]')).to_be_disabled()
    bot.get_by_role('button',name='Verificar sesión seleccionada').click()
    expect(bot.locator('[name="bot_session_id"]')).to_have_value(fixture['session_id'])
    panel.locator('[name="sub_account_id"]').fill('123456');panel.locator('[data-mainnet-refresh]').click()
    panel.locator('[data-mainnet-credentials] summary').click()
    for name,value in {'api_key':'synthetic-mainnet-api-key','dedicated_private_key':KEY.hex(),'expected_signer':SIGNER}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    panel.locator('[data-mainnet-connect]').click()
    expect(panel.locator('[data-mainnet-connection]')).to_have_text('Firma conectada',timeout=30000)
    expect(bot.locator('[name="bot_session_id"]')).to_have_value(fixture['session_id'])
    assert not starts and not prepares
    expect(bot.locator('[name="bot_research"]')).to_be_hidden()
    for name,value in {'collateral_limit':'100','entry_cash_limit':'50','entry_cap':'150','exit_cap':'200',
        'total_entry_cap':'150','loss_cap':'15','max_entries':'1','wait_minutes':'60','exit_minutes':'60'}.items():
        bot.locator('[name="bot_'+name+'"]').fill(value)
    expect(bot.locator('[data-bot-prepare]')).to_be_disabled()
    bot.locator('[name="bot_dedicated"]').check()
    with page.expect_response('**/bots/perp/preview') as preview:
        bot.locator('[data-bot-prepare]').click()
    assert preview.value.status==200,preview.value.text()
    review=preview.value.json()['review'];run=review['run_id']
    expect(bot.locator('[data-bot-review]')).to_be_visible()
    expect(bot.locator('[data-bot-review]')).to_contain_text('subcuenta completa')
    expect(bot.locator('[data-bot-review]')).to_contain_text('Bid LONG / ask SHORT')
    expect(bot.locator('[data-bot-review]')).to_contain_text('LAST')
    expect(bot.locator('[data-bot-activate]')).to_be_disabled()
    assert page.request.get(BASE+'/__test/state').json()['sends']==0
    evidence = bot.locator('[data-grvt-perp-review-evidence]')
    optimized = os.environ.get('ORBIS_TEST_GRVT_PERP_OPTIMIZED') == '1'
    expect(evidence.locator('summary')).to_have_text('Origen LAB y PAPER · v' + ('2' if optimized else '1'))
    evidence.locator('summary').click()
    expect(evidence).to_contain_text(fixture['session_id'][:8])
    expect(evidence).to_contain_text('Operaciones cerradas')
    expect(evidence).to_contain_text('Reserva 1')
    expect(evidence.locator('[title="'+review['source']['paper']['head_hash']+'"]')).to_be_visible()
    if optimized:
        original = review['lab_origin']['optimization']
        assert original['version_id'] == review['source']['version_id']
        expect(evidence).to_contain_text('Optimización')
        expect(evidence.locator('[title="'+original['reserve_run_id']+'"]')).to_be_visible()
        bot.locator('[data-bot-review]').screenshot(path=str(OUT/'perp-optimized-review.png'))
        bot.locator('[name="bot_entry_cash_limit"]').fill('49')
        expect(evidence).to_have_count(0)
        # A mismatched lineage reply cannot open the review or enable Start.
        def corrupt(route):
            response = route.fetch()
            assert response.ok, response.text()
            value = response.json()
            value['review']['lab_origin']['optimization']['version_id'] = '00000000-0000-4000-8000-000000000001'
            route.fulfill(response=response, json=value)
        route_once(page, '**/bots/perp/preview', corrupt)
        bot.locator('[data-bot-prepare]').click()
        expect(bot.locator('[data-bot-notice]')).to_contain_text('La optimización no corresponde')
        expect(bot.locator('[data-bot-review]')).to_be_hidden()
        expect(bot.locator('[data-bot-activate]')).to_be_disabled()
        final = page.request.get(BASE+'/__test/state').json()
        assert not starts and not final['sends'] and not final['jobs'] and not errors
        (OUT/'optimized-real-proof.json').write_text(json.dumps({
            'version':review['lab_origin']['version'], 'optimization_id':original['optimization_id'],
            'paper_session_id':fixture['session_id'], 'paper_head':review['source']['paper']['head_hash'],
            'start_requests':len(starts), 'sends':final['sends'], 'mismatch_rejected':True}))
        browser.close()
        raise SystemExit(0)
    bot.screenshot(path=str(OUT/'perp-review-desktop.png'))
    # Changing a reviewed budget removes the old activation authority in UI.
    bot.locator('[name="bot_entry_cash_limit"]').fill('49')
    expect(bot.locator('[data-bot-review]')).to_be_hidden()
    bot.locator('[data-bot-prepare]').click()
    expect(bot.locator('[data-bot-review]')).to_be_visible()
    bot.locator('[name="bot_authorize_bot"]').check()
    def lost(route):
        response=route.fetch();assert response.status==202,response.text();route.abort('failed')
    route_once(page,'**/bots/perp/start',lost)
    bot.locator('[data-bot-activate]').click()
    card=bot.locator('[data-mainnet-bot-run]').first
    expect(card).to_contain_text('Esperando señal',timeout=30000)
    assert len(starts)==1 and page.request.get(BASE+'/__test/state').json()['sends']==0
    page.locator('#real-protect').click()
    expect(page.locator('[data-operational-status]')).to_contain_text('Operativa activa')
    assert page.request.post(BASE+'/__test/emit').ok
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    expect(panel.locator('[name="sub_account_id"]')).to_have_value('123456')
    bot=panel.locator('[data-mainnet-bots]');bot.locator(':scope > summary').click()
    card=bot.locator('[data-mainnet-bot-run]').first
    expect(card).to_contain_text('Siguiendo posición',timeout=45000)
    expect(card).to_contain_text('ISOLATED · 5×')
    assert 'undefined' not in card.inner_text()
    card.get_by_role('button',name='Ver operación',exact=True).click()
    group=panel.locator('[data-mainnet-group]').first
    expect(group).to_contain_text('Siguiendo posición')
    expect(group.locator('[data-mainnet-close-position]')).to_be_disabled()
    group.get_by_role('button',name='Ver gráfico',exact=True).click()
    graph=panel.locator('[data-mainnet-chart]');canvas=graph.locator('[data-real-chart-canvas]')
    expect(canvas).to_have_attribute('data-fill-count','1',timeout=20000)
    export_button=graph.get_by_role('button',name='Exportar JSONL',exact=True)
    expect(export_button).to_be_disabled()
    assert canvas.bounding_box()['height']>=420
    graph.screenshot(path=str(OUT/'perp-entry-chart.png'))
    assert page.request.post(BASE+'/__test/trigger').ok
    expect(card).to_contain_text('Límite de entradas',timeout=45000)
    expect(card).to_contain_text('-0.03 USDT')
    graph.locator('[data-real-chart-refresh]').click()
    expect(canvas).to_have_attribute('data-fill-count','2',timeout=20000)
    expect(canvas).to_have_attribute('data-sell-count','1')
    expect(graph.locator('[data-real-chart-state]')).to_contain_text('Conciliado')
    with page.expect_download() as downloaded:
        export_button.click()
    records=[json.loads(line) for line in Path(downloaded.value.path()).read_bytes().splitlines()]
    assert [row['fill']['side'] for row in records]==['BUY','SELL']
    assert all(row['provenance']['kind']=='PAPER_OBSERVED' and
               row['provenance']['paper_session_id']==fixture['session_id'] for row in records)
    graph.screenshot(path=str(OUT/'perp-receipt-chart.png'))
    page.set_viewport_size({'width':390,'height':844})
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+2')
    card.screenshot(path=str(OUT/'perp-closed-mobile.png'))
    final=page.request.get(BASE+'/__test/state').json()
    assert final['sends']==1 and final['reductions']==1 and not final['positions'] and len(starts)==1
    page.locator('#real-lock').click();page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    bot=panel.locator('[data-mainnet-bots]');bot.locator(':scope > summary').click()
    expect(bot.locator('[data-mainnet-bot-run]').first).to_contain_text('Límite de entradas',timeout=15000)
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (KEY.hex(),SIGNER,'synthetic-mainnet-api-key'):assert secret not in storage
    assert not errors,errors
    (OUT/'perp-activation-proof.json').write_text(json.dumps({'ok':True,'review':review,'state':final,
        'page_errors':errors,'start_requests':len(starts),'owned_provider_only':True,'research_export_fills':len(records)},indent=2))
    browser.close()
