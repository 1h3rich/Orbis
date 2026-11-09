"""Revisión → activación única → vista protegida → señal → salida real auditada."""
from tests.browser_network import route_once
import os,json
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.test_grvt_mainnet_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER
BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1100});errors=[];starts=[]
    network=[]
    page.on('response',lambda response:network.append((response.url,response.status)) if '/mainnet' in response.url else None)
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:starts.append(request.url) if request.url.endswith('/mainnet/bots/start') else None)
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    panel.locator('[name="sub_account_id"]').fill('123456');panel.locator('[data-mainnet-refresh]').click()
    panel.locator('[data-mainnet-credentials] summary').click()
    for name,value in {'api_key':'synthetic-mainnet-api-key','dedicated_private_key':KEY.hex(),'expected_signer':SIGNER}.items():panel.locator('[name="'+name+'"]').fill(value)
    panel.locator('[data-mainnet-connect]').click();expect(panel.locator('[data-mainnet-connection]')).to_have_text('Firma conectada',timeout=30000)
    bot=panel.locator('[data-mainnet-bots]');bot.locator(':scope > summary').click()
    bot.locator('[data-bot-sources]').click();expect(bot.locator('[name="bot_session_id"] option')).to_have_count(2)
    bot.locator('[name="bot_session_id"]').select_option(fixture['session_id'])
    for name,value in {'entry_cap':'50','exit_cap':'100','total_entry_cap':'50','loss_cap':'10','max_entries':'1','wait_minutes':'2','exit_minutes':'2'}.items():bot.locator('[name="bot_'+name+'"]').fill(value)
    expect(panel.locator('[name="entry_cap"]')).to_have_value('150')
    bot.locator('[data-bot-prepare]').click();expect(bot.locator('[data-bot-review]')).to_be_visible()
    expect(bot.locator('[data-bot-review]')).to_contain_text('Continúa')
    expect(bot.locator('[data-bot-activate]')).to_be_disabled()
    assert page.request.get(BASE+'/__test/state').json()['sends']==0
    bot.locator('[name="bot_authorize_bot"]').check()
    bot.screenshot(path=str(output/'bot-mainnet-review-desktop.png'))
    # Se pierde sólo la respuesta local; el UUID revisado permite recuperar.
    def lost(route):
        response=route.fetch();assert response.status==202,response.text();route.abort('failed')
    route_once(page,'**/mainnet/bots/start',lost);bot.locator('[data-bot-activate]').click()
    card=bot.locator('[data-mainnet-bot-run]').first
    try:expect(card).to_contain_text('Esperando señal',timeout=30000)
    except Exception:
        print('Bot start diagnostics',errors,network,page.request.get(BASE+'/__test/state').json(),flush=True);raise
    assert len(starts)==1 and page.request.get(BASE+'/__test/state').json()['sends']==0
    page.locator('#real-protect').click();expect(page.locator('[data-operational-status]')).to_contain_text('Operativa activa')
    assert page.request.post(BASE+'/__test/emit').ok
    page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    expect(panel.locator('[name="sub_account_id"]')).to_have_value('123456')
    bot=panel.locator('[data-mainnet-bots]');bot.locator(':scope > summary').click()
    card=bot.locator('[data-mainnet-bot-run]').first
    expect(card).to_contain_text('Siguiendo posición',timeout=45000)
    expect(card).to_contain_text('Entrada media · 100')
    card.get_by_role('button',name='Ver operación',exact=True).click()
    group=panel.locator('[data-mainnet-group]').first
    expect(group).to_contain_text('Siguiendo posición',timeout=15000)
    expect(group.locator('[data-mainnet-close-position]')).to_be_disabled()
    page.evaluate("""() => {
      const original=window.LightweightCharts;window.mainnetMarkers=[];window.mainnetRemoved=0;
      window.LightweightCharts=Object.assign({},original,{
        createChart(host,options) {const chart=original.createChart(host,options);const remove=chart.remove;
          chart.remove=function(){window.mainnetRemoved++;return remove.call(chart);};return chart;},
        createSeriesMarkers(series,markers){window.mainnetMarkers.push(markers);return original.createSeriesMarkers(series,markers);}
      });
    }""")
    group.get_by_role('button',name='Ver gráfico',exact=True).click()
    graph=panel.locator('[data-mainnet-chart]');canvas=graph.locator('[data-real-chart-canvas]')
    expect(canvas).to_have_attribute('data-fill-count','1',timeout=20000)
    expect(graph.locator('[data-real-chart-state]')).to_contain_text('Fills observados')
    expect(graph.locator('[data-real-chart-inspect]')).to_contain_text('100 USDT')
    assert page.evaluate('window.mainnetMarkers.flat().length')==1
    assert canvas.bounding_box()['height']>=420
    graph.locator('[data-real-chart-timeframe]').select_option('4h')
    expect(graph.locator('[data-real-chart-canvas]')).to_have_attribute('data-fill-count','1')
    graph.screenshot(path=str(output/'real-entry-chart-desktop.png'))
    assert page.request.post(BASE+'/__test/trigger').ok
    expect(card).to_contain_text('Límite de entradas',timeout=45000);expect(card).to_contain_text('-0.03 USDT')
    page.evaluate('window.mainnetMarkers=[]')
    graph.locator('[data-real-chart-refresh]').click()
    expect(canvas).to_have_attribute('data-fill-count','2',timeout=20000)
    expect(canvas).to_have_attribute('data-sell-count','1')
    expect(graph.locator('[data-real-chart-state]')).to_contain_text('Conciliado')
    assert page.evaluate('window.mainnetMarkers.flat().length')==2
    count=int(canvas.get_attribute('data-candle-count'))
    graph.locator('[data-real-chart-older]').click()
    expect(canvas).to_have_attribute('data-candle-count',str(count+6))
    graph.locator('[data-real-chart-fill]').select_option(index=1)
    expect(graph.locator('[data-real-chart-inspect]')).to_contain_text('ENTRY')
    graph.screenshot(path=str(output/'real-roundtrip-chart-desktop.png'))
    card.screenshot(path=str(output/'bot-mainnet-closed-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
    card.screenshot(path=str(output/'bot-mainnet-closed-mobile.png'))
    final=page.request.get(BASE+'/__test/state').json()
    assert final['sends']==1 and final['reductions']==1 and not final['positions'] and len(starts)==1
    graph.screenshot(path=str(output/'real-roundtrip-chart-mobile.png'))
    # Una respuesta de otro proveedor no se transforma en velas GRVT.
    page.route('**/market/candles?provider=grvt&*',lambda route:route.fulfill(status=200,json={
        'available':True,'resolved_provider':'kraken','symbol':'BTC/USDT:PERP','timeframe':'1h','candles':[]}),times=1)
    graph.locator('[data-real-chart-timeframe]').select_option('1h')
    expect(graph.locator('[data-real-chart-state]')).to_contain_text('Fuente de velas distinta',timeout=20000)
    expect(canvas).to_have_attribute('data-candle-count','0');expect(canvas).to_have_attribute('data-fill-count','2')
    graph.locator('[data-real-chart-refresh]').click()
    expect(canvas).to_have_attribute('data-candle-count','13',timeout=20000)
    removed=page.evaluate('window.mainnetRemoved')
    page.locator('#real-lock').click();expect(page.locator('#real-passphrase')).to_be_visible()
    assert page.evaluate('window.mainnetRemoved')>removed
    expect(page.locator('[data-real-chart-canvas]')).to_have_count(0)
    page.locator('#real-passphrase').fill(PASSWORD)
    page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    bot=panel.locator('[data-mainnet-bots]');bot.locator(':scope > summary').click()
    expect(bot.locator('[data-mainnet-bot-run]').first).to_contain_text('Límite de entradas',timeout=15000)
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (KEY.hex(),SIGNER,'synthetic-mainnet-api-key'):assert secret not in storage
    assert not errors,errors
    (output/'browser-result.json').write_text(json.dumps({'ok':True,'state':final,'page_errors':errors,'activation_requests':len(starts)},indent=2))
    browser.close();print('MAINNET bot and REAL chart browser passed',output)
