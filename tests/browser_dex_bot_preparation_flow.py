"""Browser → tested source → exact real limits → privacy/invalidation; no sends."""
import json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.dex_bot_source_fixture import PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
    errors=[];private_posts=[]
    page.on('pageerror',lambda exc:errors.append(str(exc)))
    page.on('request',lambda request:private_posts.append(request.url) if request.method=='POST' and '/real/' in request.url else None)
    fixture=page.request.get(BASE+'/__test/state').json()
    assert fixture['fixture_id']==output.name
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD);page.locator('#real-passphrase-confirm').fill(PASSWORD)
    page.get_by_role('button',name='Crear bóveda',exact=True).click()
    page.locator('#real-new-account [name="name"]').fill('Wallet de pruebas')
    page.get_by_role('button',name='Crear cuenta',exact=True).click()
    panel=page.locator('#real-wallet-dex-bot')
    expect(panel).to_be_visible()
    panel.locator(':scope > details > summary').click()
    panel.get_by_role('button',name='Consultar PAPER',exact=True).click()
    expect(panel.locator('[name="source"] option')).to_have_count(2)
    expect(panel.locator('[name="source"] option').first).to_contain_text('BNB probado')
    for name,value in {'entry_usdt':'12,000000000000000001','budget_usdt':'60','exposure_usdt':'25','loss_usdt':'5','wbnb_quota':'1'}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    with page.expect_response(lambda r:r.url.endswith('/dex-bots/preview')) as reply:
        panel.get_by_role('button',name='Comprobar preparación',exact=True).click()
    assert reply.value.status==200,reply.value.text()
    assert reply.value.json()['amounts']['entry_units']=='12000000000000000001'
    result=panel.locator('[data-dex-bot-preview]')
    expect(result).to_contain_text('12,000000000000000001 USDT')
    expect(panel.locator('[data-dex-bot-status]')).to_contain_text('Listo para vincular')
    expect(result).to_contain_text('MEXC · BNB/USDT → Uniswap v3')
    assert not any('send' in url or 'sign' in url or 'claim' in url for url in private_posts)
    panel.screenshot(path=str(output/'preparation-desktop.png'))
    # Privacy toggle already used by the real application updates this panel too.
    hide=page.locator('#real-hide')
    if hide.count():hide.click()
    else:
        controls=page.get_by_role('button',name='Ocultar importes',exact=True)
        assert controls.count()==1
        controls.click()
    # The application's existing privacy toggle remounts the whole REAL view;
    # preparation and entered amounts are deliberately discarded on remount.
    expect(result).to_be_empty()
    page.evaluate("location.hash='#/dca'")
    page.locator('#dca-paper-session').select_option(fixture['dca_session'])
    previous_posts=list(private_posts)
    page.get_by_role('button',name='Preparar DEX',exact=True).click()
    page.wait_for_url('**/#/real')
    expect(panel.locator('[data-dex-bot-handoff]')).to_contain_text('PAPER → Uniswap v3')
    expect(panel.locator('[name="source"]')).to_have_value('DCA:'+fixture['dca_session'])
    expect(panel.locator('[name="research_validation_id"]')).to_have_value(fixture['dca_reserve'])
    expect(panel.locator('[name="entry_usdt"]')).to_have_attribute('type','password')
    expect(panel.locator('[name="monthly_usdt"]')).to_have_attribute('type','password')
    expect(panel.locator('[name="gas_budget_bnb"]')).to_have_attribute('type','password')
    assert private_posts==previous_posts
    for name,value in {'entry_usdt':'13','budget_usdt':'60','exposure_usdt':'25','loss_usdt':'5','wbnb_quota':'1'}.items():
        panel.locator('[name="'+name+'"]').fill(value)
    panel.locator('[name="monthly_usdt"]').fill('40')
    with page.expect_response(lambda r:r.url.endswith('/dex-bots/preview')) as reply:
        panel.get_by_role('button',name='Comprobar preparación',exact=True).click()
    assert reply.value.status==200,reply.value.text()
    assert reply.value.json()['source']['session_id']==fixture['dca_session']
    assert reply.value.json()['allow_sell'] is False
    expect(result).to_contain_text('Límite mensual efectivo')
    expect(result).to_contain_text('•••• USDT')
    expect(result).not_to_contain_text('13 USDT')
    panel.locator('[name="entry_usdt"]').fill('14')
    expect(result).to_be_empty()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    panel.screenshot(path=str(output/'preparation-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1050})
    page.locator('#real-lock').click();expect(page.locator('#real-passphrase')).to_be_visible()
    expect(page.locator('[data-dex-bot-preview]')).to_have_count(0)
    assert not errors,errors
    (output/'browser-result.json').write_text(json.dumps({'passed':True,'private_posts':private_posts,'errors':errors}))
    print('DEX preparation: tested LONG and DCA, exact units, privacy, changes, mobile and lock; no send')
    browser.close()
