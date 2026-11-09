"""Browser concurrency/compatibility checks; transport fixtures never sign/send.

The real HTTP + signed disposable-chain journey is browser_dex_booking_flow.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


SCRIPT = Path(__file__).resolve().parents[1] / 'app/web/static/dex-receipt-booking.js'
HARNESS = """() => {
  window.calls=[]; window.pending=[];
  window.state={accountId:'own-account',wallet:'0x1111111111111111111111111111111111111111',
    chain:'0x38',generation:1,verified:true,hidden:false};
  window.row={source_kind:'OPERATOR',source_id:'own-source',action:'BUY',booked:false,
    status:'FINALIZED',occurred_at:'2026-11-06T12:00:00Z',gas_amount:'0.0001'};
  window.answer={saved:true,source_kind:'OPERATOR',source_id:'own-source',
    proof:{transaction_index:'1',block_number:'1234'}};
  window.client={snapshot:()=>window.state,request:(url,options,passive)=>{
    calls.push({url,body:options&&JSON.parse(options.body),passive});
    if(url==='/vault/status')return window.capabilityError?Promise.reject(new Error('offline')):
      Promise.resolve(window.capabilityStatus);
    if(url.endsWith('/query'))return Promise.resolve({offset:0,next_offset:null,total:1,items:[row]});
    if(url.endsWith('/order-proof'))return new Promise((resolve,reject)=>pending.push({resolve,reject}));
    throw new Error('Unexpected request');
  }};
  OrbisDexReceiptBooking.mount(document.querySelector('#host'),client);
}"""


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    page.set_content('<main><div id="host"></div></main>')
    page.add_script_tag(path=str(SCRIPT.with_name('dex-receipt-order.js')))
    page.add_script_tag(path=str(SCRIPT))
    yield page
    page.close()


def mount(page, status=None, *, failure=False):
    page.evaluate('(s)=>{window.capabilityStatus=s;}', status)
    page.evaluate('(e)=>{window.capabilityError=e;}', failure)
    page.evaluate(HARNESS)
    page.locator('summary').click()
    expect(page.locator('[data-book-history]')).to_contain_text('Compra · Pendiente')


@pytest.mark.parametrize('status,failure', [({},False),({'capabilities':{'dex_receipt_order_proof':False}},False),
    ({'capabilities':{'dex_receipt_order_proof':'true'}},False),({},True)])
def test_old_or_unavailable_backend_keeps_existing_booking_without_false_control(page, status, failure):
    mount(page,status,failure=failure)
    expect(page.get_by_role('button',name='Revisar',exact=True)).to_be_enabled()
    expect(page.get_by_role('button',name='Comprobar bloque',exact=True)).to_have_count(0)
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/order-proof"))')
    assert page.evaluate('calls.find(c=>c.url==="/vault/status").passive') is True


def supported(page):
    mount(page,{'capabilities':{'dex_receipt_order_proof':True}})
    page.get_by_role('button',name='Comprobar bloque',exact=True).click()
    expect(page.get_by_role('button',name='Comprobar bloque',exact=True)).to_be_disabled()
    assert page.evaluate('pending.length') == 1


def test_shared_proof_client_preserves_explicit_passive_collection(page):
    page.evaluate('''() => {
      window.collected=[];
      window.promise=OrbisDexReceiptOrder.check({request:(url,options,passive)=>{
        collected.push({passive,body:JSON.parse(options.body)});
        return Promise.resolve({saved:true,source_kind:'OPERATOR',source_id:'source',
          proof:{transaction_index:'0',block_number:'42'}});
      }},{accountId:'context',wallet:'0x1111111111111111111111111111111111111111'},'OPERATOR','source',true);
    }''')
    assert page.evaluate('promise.then(value=>value.block_number)')=='42'
    assert page.evaluate('collected')==[{'passive':True,'body':{'address':'0x1111111111111111111111111111111111111111'}}]


def test_lost_response_requires_explicit_retry_and_redacts_saved_block_when_hidden(page):
    supported(page)
    page.evaluate('pending.shift().reject(new Error("connection lost"))')
    expect(page.locator('[data-book-status]')).to_have_text('Bloque sin confirmar · Reintenta.')
    expect(page.locator('[data-book-order-saved]')).to_have_count(0)
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/order-proof")).length') == 1
    page.get_by_role('button',name='Comprobar bloque',exact=True).click()
    page.evaluate('pending.shift().resolve(answer)')
    badge=page.locator('[data-book-order-saved]')
    expect(badge).to_have_text('Bloque ✓')
    expect(badge).to_have_attribute('title','Bloque 1234 · Posición 2')
    page.evaluate('state.hidden=true;OrbisDexReceiptBooking.update()')
    expect(badge).to_have_attribute('title','Bloque verificado')
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/order-proof")).map(c=>c.body)') == [
        {'address':'0x1111111111111111111111111111111111111111'}] * 2


@pytest.mark.parametrize('field,value', [('wallet','0x2222222222222222222222222222222222222222'),
    ('accountId','other-account'),('chain','0x1'),('generation',2),('verified',False)])
def test_late_proof_cannot_restore_cleared_wallet_account_chain_or_session(page,field,value):
    supported(page)
    page.evaluate('([field,value])=>{state[field]=value;OrbisDexReceiptBooking.update();}',[field,value])
    page.evaluate('pending.shift().resolve(answer)')
    expect(page.locator('[data-book-order-saved]')).to_have_count(0)
    expect(page.locator('[data-book-status]')).to_have_text('Verifica tu wallet para consultar recibos.')


def test_late_reply_cannot_restore_a_remounted_component(page):
    supported(page)
    page.evaluate("document.querySelector('#host').remove();let el=document.createElement('div');el.id='host';document.querySelector('main').append(el);OrbisDexReceiptBooking.mount(el,client)")
    page.evaluate('pending.shift().resolve(answer)')
    expect(page.locator('[data-book-order-saved]')).to_have_count(0)
    expect(page.locator('[data-book-status]')).to_have_text('Verifica tu wallet para consultar recibos.')


@pytest.mark.parametrize('change',[{'source_id':'wrong-source'},{'saved':False},
    {'proof':{'transaction_index':'100000','block_number':'1234'}}])
def test_incomplete_or_different_receipt_response_is_not_displayed_as_verified(page,change):
    supported(page)
    page.evaluate('(change)=>pending.shift().resolve({...answer,...change})',change)
    expect(page.locator('[data-book-status]')).to_have_text('Bloque sin confirmar · Reintenta.')
    expect(page.locator('[data-book-order-saved]')).to_have_count(0)
