"""One live provider request: recovery, late replies and real API/SQLCipher scopes."""
import json,os
from datetime import datetime,timedelta
from decimal import Decimal
from pathlib import Path

from eth_account import Account
from playwright.sync_api import sync_playwright,expect
from tests.browser_wallet_swap_common import install_provider

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
OUTPUT=Path(os.environ['ORBIS_TEST_SWAP_DIRECTORY'])
CASE=os.environ['ORBIS_TEST_WALLET_PENDING_CASE']
assert CASE in {'timeout','clear','remount','late_reject','wallet_change','lock_unlock'}
PASSWORD='isolated browser pending wallet password'


with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    wallet=Account.create();install_provider(page,wallet)
    # Record actual mount arguments only in this owned test. Product bytes
    # remain untouched, and the wrapper always invokes the real component.
    page.add_init_script("""Object.defineProperty(window,'OrbisWalletSwap',{
      configurable:true,set(value){const mount=value.mount;
        value.mount=function(root,api){window.fixtureSwapRoot=root;
          window.fixtureSwapApi=api;return mount(root,api);};
        Object.defineProperty(window,'OrbisWalletSwap',{value,writable:true,configurable:true});
      }});""")
    errors=[];writes=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    page.on('request',lambda req:writes.append(req.url) if req.method=='POST' and
        any(part in req.url for part in ('/wallet-swaps/','/dex-call-resolutions/','/dex-receipts/')) else None)
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    page.locator('#real-passphrase').fill(PASSWORD)
    page.locator('#real-passphrase-confirm').fill(PASSWORD)
    with page.expect_response('**/real/vault/create') as created:
        page.get_by_role('button',name='Crear bóveda',exact=True).click()
    assert created.value.status==200,created.value.text()
    token=created.value.json()['token']
    page.locator('#real-new-account [name="name"]').fill('Wallet BNB')
    page.get_by_role('button',name='Crear cuenta',exact=True).click()
    expect(page.locator('#real-account')).not_to_have_value('')
    account=page.locator('#real-account').input_value()
    if CASE=='timeout':
        for currency,amount in [('USDT','1000'),('BNB','1')]:
            form=page.locator('#real-event-form')
            form.locator('[name="kind"]').select_option('CONTRIBUTION')
            form.locator('[name="quote_currency"]').fill(currency)
            form.locator('[name="amount"]').fill(amount)
            form.locator('[name="occurred_at"]').fill((datetime.now()-timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'))
            with page.expect_response('**/events') as funded:
                form.locator('button[type="submit"]').click()
            assert funded.value.status==200,funded.value.text()
            expect(form.locator('[name="amount"]')).to_have_value('')
    page.get_by_role('button',name='Conectar wallet',exact=True).click()
    expect(page.locator('#real-wallet-state')).to_contain_text('Saldo actualizado')
    page.get_by_role('button',name='Verificar control',exact=True).click()
    expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
    if CASE=='timeout':
        page.locator('#real-wallet-links > summary').click()
        page.locator('#real-wallet-link-alias').fill('Wallet BNB')
        page.get_by_role('button',name='Guardar vínculo',exact=True).click()
        expect(page.locator('#real-wallet-proof-state')).to_contain_text('corresponde a esta cuenta REAL')
    panel=page.locator('#real-wallet-swap')
    panel.locator('details > summary').first.click()
    panel.locator('[name="amount"]').fill('12.345678901234567891')
    panel.get_by_role('button',name='Cotizar',exact=True).click()
    expect(panel.locator('[data-swap-state]')).to_contain_text('Cotización lista')
    panel.get_by_role('button',name='Preparar permiso',exact=True).click()
    expect(panel.locator('[data-swap-state]')).to_contain_text('Plan listo')
    page.evaluate('mode=>window.testWallet.mode=mode','hold_reject' if CASE=='late_reject' else 'hold')
    panel.get_by_role('button',name='Confirmar en wallet',exact=True).click()
    page.wait_for_function("() => window.testWallet.sends.length===1 && (typeof window.testWallet.releaseSend==='function'||typeof window.testWallet.rejectSend==='function')")

    def history():
        if not panel.locator('details details').evaluate('el=>el.open'):
            panel.get_by_text('Historial DEX',exact=True).click()
        with page.expect_response('**/wallet-swaps'):
            panel.locator('[data-swap-history-refresh]').click()
        expect(panel.locator('[data-swap-hash]')).to_be_enabled()
        expect(panel.locator('[data-swap-resolve]')).to_be_enabled()
        expect(panel.get_by_role('button',name='Cotizar',exact=True)).to_be_disabled()

    def check_receipt():
        panel.locator('[data-swap-hash]').fill(page.evaluate('Object.keys(window.testWallet.receipts)[0]'))
        with page.expect_response('**/dex-call-resolutions/*/*/check-original') as checked:
            panel.get_by_role('button',name='Comprobar recibo',exact=True).click()
        assert checked.value.status==200,checked.value.text()
        expect(panel.locator('[data-swap-history]')).to_contain_text('Confirmada')

    if CASE in {'wallet_change','lock_unlock'}:
        if CASE=='wallet_change':
            page.evaluate("address=>{window.testWallet.address=address;window.testWallet.emit('accountsChanged',[address]);}",Account.create().address)
            expect(page.locator('#real-wallet-proof-state')).not_to_contain_text('Control EOA comprobado')
        else:
            page.locator('#real-lock').click()
            expect(page.locator('#real-passphrase')).to_be_visible()
            page.locator('#real-passphrase').fill(PASSWORD)
            with page.expect_response('**/real/vault/unlock') as reopened:
                page.get_by_role('button',name='Desbloquear',exact=True).click()
            assert reopened.value.status==200,reopened.value.text()
            token=reopened.value.json()['token']
        page.evaluate('window.testWallet.releaseSend()')
        page.wait_for_load_state('networkidle')
        page.evaluate('async()=>{await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);}')
        assert not any(url.endswith('/hash') or url.endswith('/rejected') for url in writes),writes
        rows=page.request.get(BASE+'/real/accounts/'+account+'/wallet-swaps',headers={'X-Orbis-Vault-Token':token}).json()
        assert len(rows)==1 and rows[0]['status']=='WALLET_REQUESTED' and not rows[0].get('resolution'),rows
    else:
        if CASE in {'timeout','late_reject'}:
            expect(panel.locator('[data-swap-state]')).to_contain_text('Wallet sin respuesta',timeout=36000)
        elif CASE=='clear':
            page.evaluate('()=>{window.OrbisWalletSwap.clear();window.OrbisWalletSwap.update();}')
        elif CASE=='remount':
            page.evaluate('()=>window.OrbisWalletSwap.mount(window.fixtureSwapRoot,window.fixtureSwapApi)')
            panel.locator('details > summary').first.click()
        history()
        if CASE=='timeout':
            check_receipt()
            # Ledger writes are non-financial: confirmed gas can be recorded
            # while the original wallet reply still has not settled.
            panel.get_by_role('button',name='Revisar registro',exact=True).click()
            booking=panel.locator('[data-swap-booking]')
            with page.expect_response('**/dex-receipts/CALL_RESOLUTION/*/book') as booked:
                booking.get_by_role('button',name='Registrar',exact=True).click()
            assert booked.value.status==200 and booked.value.json()['booked'],booked.value.text()
            expect(panel.locator('[data-swap-history]')).to_contain_text('contabilizado')
            expect(panel.get_by_role('button',name='Cotizar',exact=True)).to_be_disabled()
            page.evaluate('window.testWallet.releaseSend()')
            expect(panel.get_by_role('button',name='Cotizar',exact=True)).to_be_enabled()
            snapshot=page.request.get(BASE+'/real/accounts/'+account+'/snapshot',headers={'X-Orbis-Vault-Token':token}).json()
            assert Decimal(snapshot['cash']['BNB'])==Decimal('0.9999')
            assert Decimal(snapshot['cash']['USDT'])==Decimal('1000') and not snapshot['positions']
            assert sum(url.endswith('/hash') for url in writes)==1
            assert sum(url.endswith('/book') for url in writes)==1
        elif CASE=='late_reject':
            page.evaluate('window.testWallet.rejectSend()')
            expect(panel.locator('[data-swap-state]')).to_contain_text('Rechazo comunicado')
            expect(panel.locator('[data-swap-history]')).to_contain_text('Rechazo comunicado')
            assert sum(url.endswith('/rejected') for url in writes)==1
            assert not any(url.endswith('/hash') for url in writes)
        else:
            # A late reply from the old mount must not clear the new read's busy.
            pending=[]
            check_pattern='**/dex-call-resolutions/*/*/check-original'
            page.route(check_pattern,lambda route:pending.append(route),times=1)
            panel.locator('[data-swap-hash]').fill(page.evaluate('Object.keys(window.testWallet.receipts)[0]'))
            with page.expect_request(check_pattern):
                panel.get_by_role('button',name='Comprobar recibo',exact=True).click()
            page.evaluate('window.testWallet.releaseSend()')
            page.wait_for_function('async()=>{await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);return true;}')
            expect(panel.get_by_role('button',name='Cotizar',exact=True)).to_be_disabled()
            expect(panel.locator('[data-swap-check]')).to_be_disabled()
            with page.expect_response(check_pattern) as checked:
                pending[0].continue_()
            assert checked.value.status==200,checked.value.text()
            expect(panel.locator('[data-swap-history]')).to_contain_text('Confirmada')
            assert sum(url.endswith('/hash') for url in writes)==1
            assert not any(url.endswith('/rejected') for url in writes)
        expect(panel.get_by_role('button',name='Cotizar',exact=True)).to_be_enabled()
        panel.screenshot(path=str(OUTPUT/'pending-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        page.wait_for_function('()=>document.documentElement.scrollWidth<=innerWidth+1',timeout=3000)
        panel.screenshot(path=str(OUTPUT/'pending-mobile.png'))
    assert sum(url.endswith('/claim') for url in writes)==1
    assert page.evaluate('window.testWallet.sends.length')==1
    assert not errors,errors
    assert wallet.address.lower() not in page.evaluate('JSON.stringify({...localStorage})').lower()
    (OUTPUT/'result.json').write_text(json.dumps({'passed':True,'case':CASE,'wallet_sends':1,'claims':1,
        'hash_reports':sum(url.endswith('/hash') for url in writes),'rejection_reports':sum(url.endswith('/rejected') for url in writes),
        'bookings':sum(url.endswith('/book') for url in writes),'errors':errors},indent=2))
    browser.close()
print('Pending wallet scope verified:',CASE)
