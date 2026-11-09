"""Límite original insuficiente → revisión del nuevo tope → cierre conciliado."""
from tests.browser_network import route_once
import os,json,time
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.test_grvt_mainnet_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY,SIGNER
BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801');output=Path(os.environ['ORBIS_TEST_GRVT_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1100});errors=[];renewals=[];session_token=[None]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:renewals.append(request.url) if request.url.endswith('/renew') else None)
    # Sólo rutas y tiempos: permite distinguir una consulta pendiente de un
    # resultado ignorado por la vista, sin registrar credenciales ni cuerpos.
    def network(event,request):
        if '/mainnet' in request.url:
            if event=='request':session_token[0]=request.headers.get('x-orbis-vault-token') or session_token[0]
            with (output/'network.jsonl').open('a') as log:
                log.write(json.dumps({'at':time.monotonic(),'event':event,'method':request.method,'url':request.url})+'\n')
    for event in ('request','requestfinished','requestfailed'):
        page.on(event,lambda request,event=event:network(event,request))
    assert page.request.get(BASE+'/__test/state').json()['fixture_id']==output.name
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    expect(panel.locator('[name="sub_account_id"]')).to_have_value('123456')
    panel.locator('[data-mainnet-credentials] summary').click()
    for name,value in {'api_key':'synthetic-mainnet-api-key','dedicated_private_key':KEY.hex(),'expected_signer':SIGNER}.items():panel.locator('[name="'+name+'"]').fill(value)
    panel.locator('[data-mainnet-connect]').click()
    group=panel.locator('[data-mainnet-group]').first;expect(group.locator('[data-mainnet-close-position]')).to_be_enabled()
    group.locator('[data-mainnet-close-position]').click();panel.locator('[name="authorize_close"]').check();panel.locator('[data-mainnet-confirm-close]').click()
    expect(group).to_contain_text('Revisión necesaria',timeout=30000)
    assert page.request.get(BASE+'/__test/state').json()['reductions']==0
    group.get_by_role('button',name='Límites de cierre',exact=True).click()
    form=panel.locator('[data-mainnet-renew]');expect(form.locator('[name="renew_exit_cap"]')).to_have_value('100')
    form.locator('[name="renew_exit_cap"]').fill('150');form.locator('[name="renew_ttl"]').fill('120');form.locator('[name="renew_attempts"]').fill('1')
    form.locator('[data-mainnet-renew-prepare]').click();expect(form.locator('[data-mainnet-renew-review]')).to_be_visible()
    expect(form.locator('[data-mainnet-renew-terms]')).to_contain_text('tope 150 USDT')
    expect(form.locator('[data-mainnet-renew-terms]')).to_contain_text('tope anterior 100 USDT')
    expect(form.locator('[data-mainnet-renew-send]')).to_be_disabled()
    form.locator('[name="renew_exit_cap"]').fill('160');expect(form.locator('[data-mainnet-renew-review]')).to_be_hidden()
    form.locator('[name="renew_exit_cap"]').fill('150');form.locator('[data-mainnet-renew-prepare]').click()
    expect(form.locator('[data-mainnet-renew-review]')).to_be_visible();form.locator('[name="authorize_renewal"]').check()
    form.screenshot(path=str(output/'exit-limit-review-desktop.png'))
    def lost(route):
        result=route.fetch();assert result.status==200,result.text();route.abort('failed')
    stalled=[];failed_reads=[]
    if os.environ.get('ORBIS_TEST_STALLED_READ')=='1':
        parent=page.request.get(BASE+'/__test/state').json()['parent']
        page.route('**/mainnet/groups/'+parent,lambda route:stalled.append((route,time.monotonic())),times=1)
        page.on('requestfailed',lambda request:failed_reads.append(time.monotonic()) if request.url.endswith('/groups/'+parent) else None)
    route_once(page,'**/mainnet/groups/*/renew',lost);form.locator('[data-mainnet-renew-send]').click()
    try:
        expect(group).to_contain_text('Neto conciliado · 91.96 USDT',timeout=45000)
    except Exception:
        page.screenshot(path=str(output/'exit-limit-failure.png'),full_page=True)
        try:
            parent=page.request.get(BASE+'/__test/state').json()['parent']
            direct=page.request.get(BASE+'/real/exchanges/grvt/mainnet/groups/'+parent,
                headers={'X-Orbis-Vault-Token':session_token[0]},timeout=5000)
            probe={'status':direct.status,'body':direct.json()}
        except Exception as error:probe={'error':type(error).__name__}
        (output/'failure-state.json').write_text(json.dumps({'errors':errors,'panel':panel.inner_text(),
            'backend':page.request.get(BASE+'/__test/state').json(),'direct_group_probe':probe},indent=2))
        raise
    expect(group).to_contain_text('Tope de cierre revisado · 150 USDT')
    assert len(renewals)==1
    if os.environ.get('ORBIS_TEST_STALLED_READ')=='1':
        assert len(stalled)==len(failed_reads)==1
        assert failed_reads[0]-stalled[0][1]<25
        # El navegador ya canceló; libera también el manejador del fixture.
        stalled[0][0].abort('aborted')
    group.screenshot(path=str(output/'exit-limit-complete-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1');group.screenshot(path=str(output/'exit-limit-complete-mobile.png'))
    page.locator('#real-lock').click();page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    panel=page.locator('#real-grvt-mainnet');panel.locator(':scope > summary').click()
    expect(panel.locator('[data-mainnet-group]').first).to_contain_text('Neto conciliado · 91.96 USDT',timeout=20000)
    expect(panel.locator('[data-mainnet-preview]')).to_be_disabled()
    final=page.request.get(BASE+'/__test/state').json();assert final['reductions']==1 and not final['positions'] and final['balance']=='10091.96'
    storage=page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
    for secret in (KEY.hex(),SIGNER,'synthetic-mainnet-api-key'):assert secret not in storage
    assert not errors,errors
    (output/'browser-result.json').write_text(json.dumps({'ok':True,'state':final,'page_errors':errors,
        'renewal_requests':len(renewals),'stalled_reads':len(stalled),'cancelled_stalled_reads':len(failed_reads)},indent=2))
    browser.close();print('MAINNET exit cap review browser passed',output)
