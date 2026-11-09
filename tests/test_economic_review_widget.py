"""Accounting-review state machine in Chromium; controlled transport, no funds.

The separate browser_economic_review journey uses actual HTTP, encrypted SQL
and signed disposable-chain receipts; these cases exercise races and failures.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect,sync_playwright

STATIC=Path(__file__).resolve().parents[1]/'app/web/static'
HARNESS="""() => {
  window.calls=[];window.pending=[];window.walletCalls=[];window.booked=0;
  window.account='11111111-1111-4111-8111-111111111111';
  window.source='22222222-2222-4222-8222-222222222222';
  window.acceptance='33333333-3333-4333-8333-333333333333';
  window.state={accountId:account,wallet:'0x1111111111111111111111111111111111111111',chain:'0x38',
    generation:1,verified:true,hidden:false,provider:{request:({method,params})=>{
      walletCalls.push({method,params});return Promise.resolve('0x'+'11'.repeat(65));}}};
  window.row={source_kind:'OPERATOR',source_id:source,context_account_id:account,context_name:'Principal',
    context_verified:true,action:'BUY',occurred_at:'2026-11-06T12:00:00Z',quantity:'1',gross_amount:'2',gas_amount:'0.0001',
    booked:false,order_checked:true,trade_candidates:[],gas_candidates:[]};
  window.catalog={root_account_id:account,root_account_name:'Principal',sources:[row]};
  window.actual={account_id:account,cash:{USDT:'100',BNB:'0.01'},contributions:{USDT:'100',BNB:'0.01'},withdrawals:{},
    realized_pnl:{},cash_fees:{},positions:[]};
  window.network={...actual,cash:{USDT:'98',BNB:'0.0099'},positions:[{asset:'WBNB',quote_currency:'USDT',quantity:'1',cost_basis:'2'}]};
  window.preview={id:acceptance,preview_digest:'a'.repeat(64),mode:'BATCH',batch_id:source,
    recorded_at:'2026-11-06T12:05:00+00:00',root_account_id:account,saved:false,
    review:{checked_receipts:1,events_to_create:2,accounts:[{account_id:account,name:'Principal',
      current:actual,current_error:null,registration:network,network,changed:true}]},
    operating:{can_accept:true,blockers:[]}};
  window.result={id:acceptance,saved:true,is_current:true};
  window.capability={capabilities:{economic_review:true}};
  window.client={snapshot:()=>state,onBooked:()=>{booked++;},request:(url,options,passive)=>{
    let body=options&&JSON.parse(options.body);calls.push({url,body,passive});
    if(url==='/vault/status')return Promise.resolve(capability);
    if(url.endsWith('/sources'))return Promise.resolve(catalog);
    if(url.endsWith('/order-proof'))return Promise.resolve({saved:true,source_kind:body.kind||'OPERATOR',
      source_id:url.split('/').at(-2),proof:{block_number:'42',transaction_index:'0'}});
    if(url.endsWith('/preview'))return window.deferPreview?new Promise((resolve,reject)=>pending.push({resolve,reject})):Promise.resolve(preview);
    if(url.endsWith('/result'))return Promise.resolve(result);
    if(url.endsWith('/wallet-proof/start'))return Promise.resolve({purpose:'SESSION_EOA_CONTROL_ONLY',nonce:'a'.repeat(64),message:'Verify local context'});
    if(url.endsWith('/wallet-proof/confirm'))return Promise.resolve({verified:true,spending_authorized:false});
    if(url.endsWith('/confirm'))return new Promise((resolve,reject)=>pending.push({resolve,reject}));
    throw new Error('Unexpected request '+url);
  }};
  OrbisEconomicReview.mount(document.querySelector('#host'),client);
}"""

@pytest.fixture(scope='module')
def browser():
    with sync_playwright()as p:
        browser=p.chromium.launch(headless=True)
        yield browser
        browser.close()

@pytest.fixture
def page(browser):
    page=browser.new_page(viewport={'width':1200,'height':900})
    page.set_content('<main><div id="host"></div></main>')
    page.add_style_tag(path=str(STATIC/'app.css'))
    # Real central formatter, without starting the full application.
    formatter=(STATIC/'app.js').read_text().split('  function fmtExactDecimal(',1)[1].split('\n  function fmtEUR(',1)[0]
    page.add_script_tag(content='window.OrbisUI={fmtDecimal:function fmtExactDecimal('+formatter+'};')
    page.add_script_tag(path=str(STATIC/'dex-receipt-order.js'))
    page.add_script_tag(path=str(STATIC/'economic-review.js'))
    page.evaluate(HARNESS)
    yield page
    page.close()

def opened(page):
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    expect(page.locator('[data-er-source]')).to_have_count(1)

def reviewed(page):
    opened(page)
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()

@pytest.mark.parametrize('capability',[{}, {'capabilities':{'economic_review':False}}, {'capabilities':{'economic_review':'true'}}])
def test_old_backend_hides_new_controls(page,capability):
    page.evaluate('(cap)=>{capability=cap;OrbisEconomicReview.mount(document.querySelector("#host"),client);}',capability)
    expect(page.get_by_role('button',name='Revisar cuenta',exact=True)).not_to_be_visible()
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/sources"))')

def test_actual_not_proposed_registration_and_hidden_values(page):
    reviewed(page)
    row=page.locator('tr').filter(has=page.get_by_role('rowheader',name='Efectivo',exact=True)).filter(has_text='USDT')
    expect(row.locator('td').nth(0)).to_have_text('100 USDT')
    expect(row.locator('td').nth(1)).to_have_text('98 USDT')
    page.evaluate('state.hidden=true;OrbisEconomicReview.update()')
    expect(row.locator('td').nth(0)).to_have_text('•••• USDT')
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_disabled()
    assert '100'not in page.locator('dialog').inner_text()
    assert not page.locator('[data-er-body] [data-help]').count()

def test_tiny_scientific_and_large_amounts_keep_exact_digits_in_help(page):
    page.evaluate("actual.cash.BNB='-1E-18';network.cash.BNB='2E-18';actual.cash.USDT='123456789012345678.120000000001'")
    reviewed(page)
    expect(page.locator('[data-er-body] [data-help="-1E-18"]')).to_have_text('−<0,00000001')
    expect(page.locator('[data-er-body] [data-help="2E-18"]')).to_have_text('<0,00000001')
    expect(page.locator('[data-er-body] [data-help="123456789012345678.120000000001"]')).to_have_text('≈ 123.456.789.012.345.678,12')

@pytest.mark.parametrize('change',["preview.root_account_id=source", "preview.review.accounts[0].network.cash.USDT=98"])
def test_foreign_or_non_decimal_review_is_not_accepted(page,change):
    page.evaluate(change);opened(page)
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.locator('[data-er-message]')).to_have_text('Revisión sin confirmar.')
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_have_count(0)
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/confirm"))')

def test_manual_candidates_require_explicit_selection_and_exact_ids(page):
    page.evaluate("row.trade_candidates=[{id:acceptance,occurred_at:row.occurred_at}];row.gas_candidates=[{id:account,occurred_at:row.occurred_at,amount:'0.00005'}]")
    opened(page)
    expect(page.get_by_role('button',name='Revisar',exact=True)).to_be_disabled()
    page.locator('[data-er-trade]').select_option('33333333-3333-4333-8333-333333333333')
    expect(page.get_by_role('button',name='Revisar',exact=True)).to_be_disabled()
    page.locator('[data-er-gas]').check()
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
    payload=page.evaluate('calls.find(c=>c.url.endsWith("/preview")).body.receipts[0]')
    assert payload['trade_event_id']=='33333333-3333-4333-8333-333333333333'
    assert payload['gas_event_ids']==['11111111-1111-4111-8111-111111111111']
    assert not payload['confirm_new_trade']and not payload['confirm_new_gas']
    assert page.evaluate('walletCalls')==[]

def test_missing_block_proofs_are_collected_once_then_passively(page):
    page.evaluate("row.order_checked=false;catalog.sources.push({...row,source_id:acceptance})")
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    expect(page.locator('[data-er-source]')).to_have_count(2)
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/order-proof")).map(c=>c.passive)')==[False,True]
    assert page.evaluate('walletCalls')==[]

@pytest.mark.parametrize('field,value',[('wallet','0x2222222222222222222222222222222222222222'),
    ('accountId','44444444-4444-4444-8444-444444444444'),('chain','0x1'),('generation',2),('verified',False)])
def test_late_review_never_restores_changed_context(page,field,value):
    page.evaluate('deferPreview=true');opened(page)
    page.get_by_role('button',name='Revisar',exact=True).click()
    page.wait_for_function('pending.length===1')
    page.evaluate('([field,value])=>{state[field]=value;OrbisEconomicReview.update();pending.shift().resolve(preview);}',[field,value])
    expect(page.locator('dialog')).not_to_be_visible()
    expect(page.locator('[data-er-body]')).to_be_empty()
    assert page.evaluate('booked')==0
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/confirm"))')

def test_pending_worker_is_visible_and_never_stopped_implicitly(page):
    page.evaluate("preview.operating={can_accept:false,blockers:[{code:'BOT_ACTIVE',kind:'DEX',id:source}]}")
    reviewed(page)
    expect(page.locator('.economic-warning')).to_contain_text('Bot activo · DEX')
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_disabled()
    assert not page.evaluate('calls.some(c=>/stop|cancel/.test(c.url))')

def test_lost_confirmation_survives_close_and_recovers_passively_without_retry(page):
    reviewed(page)
    page.get_by_role('button',name='Aceptar criterio',exact=True).click()
    expect(page.get_by_role('button',name='Cerrar revisión')).to_be_disabled()
    page.keyboard.press('Escape');expect(page.locator('dialog')).to_be_visible()
    page.evaluate('pending.shift().reject(new Error("lost response"))')
    expect(page.get_by_role('button',name='Comprobar estado',exact=True)).to_be_visible()
    page.get_by_role('button',name='Cerrar revisión').click()
    expect(page.locator('[data-er-body]')).to_be_empty()
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    page.get_by_role('button',name='Comprobar estado',exact=True).click()
    expect(page.locator('.economic-saved')).to_have_text('Criterio guardado ✓')
    assert page.evaluate('booked')==1
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/confirm")).length')==1
    assert page.evaluate('calls.find(c=>c.url.endsWith("/result")).passive')is True
    payload=page.evaluate('calls.find(c=>c.url.endsWith("/confirm")).body')
    assert payload['confirmation']=='ACEPTAR_CRITERIO_CONTABLE'and 'acceptance_id'not in payload

def test_unrecorded_reply_allows_only_an_explicit_fresh_review(page):
    reviewed(page);page.get_by_role('button',name='Aceptar criterio',exact=True).click()
    page.evaluate('pending.shift().reject(new Error("lost"));result={id:acceptance,saved:false}')
    page.get_by_role('button',name='Comprobar estado',exact=True).click()
    expect(page.get_by_role('button',name='Volver a revisar',exact=True)).to_be_visible()
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/confirm")).length')==1
    page.get_by_role('button',name='Volver a revisar',exact=True).click()
    expect(page.locator('[data-er-source]')).to_have_count(1)
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/confirm")).length')==1

def test_extra_context_requests_only_personal_control_signature(page):
    page.evaluate('row.context_account_id=acceptance;row.context_verified=false')
    opened(page)
    expect(page.get_by_role('button',name='Revisar',exact=True)).to_be_disabled()
    page.on('dialog',lambda d:d.accept())
    page.get_by_role('button',name='Verificar contexto · Principal',exact=True).click()
    expect(page.get_by_role('button',name='Revisar',exact=True)).to_be_enabled()
    assert page.evaluate('walletCalls.map(c=>c.method)')==['personal_sign']
    assert page.evaluate('calls.filter(c=>c.url.includes("/wallet-proof/")).length')==2
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/confirm")&&!c.url.includes("wallet-proof"))')

def test_pagination_bounds_dom_without_losing_offscreen_selection(page):
    page.evaluate("catalog.sources=Array.from({length:51},(_,i)=>({...row,source_id:'22222222-2222-4222-8222-'+String(i).padStart(12,'0')}))")
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    expect(page.locator('[data-er-source]')).to_have_count(50)
    page.locator('[data-er-selected]').first.uncheck()
    page.get_by_role('button',name='Siguiente',exact=True).click()
    expect(page.locator('[data-er-source]')).to_have_count(1)
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
    assert page.evaluate('calls.find(c=>c.url.endsWith("/preview")).body.receipts.length')==50


def gas_mode(page,*,booked=False):
    page.evaluate("""(booked)=>{
      capability={capabilities:{economic_review:true,economic_review_gas:true,dex_gas_order_proof:true}};
      window.financeCatalog=catalog;
      window.gasRow={...row,source_kind:'CALL_RESOLUTION',gas_only:true,gas_action:'APPROVE',action:'GAS',
        quantity:null,gross_amount:null,gas_amount:'1E-18',order_checked:false,booked};
      window.gasCatalog={...catalog,sources:[gasRow]};
      network={...actual,cash:{...actual.cash,BNB:'0.009999999999999999'},positions:[]};
      preview={...preview,mode:booked?'EXISTING':'BATCH',batch_id:booked?null:source,
        review:{...preview.review,events_to_create:booked?0:1,accounts:[{...preview.review.accounts[0],network}]}};
      const original=client.request;
      client.request=(url,options,passive)=>{
        const body=options&&JSON.parse(options.body);
        if(url.endsWith('/sources')){calls.push({url,body,passive});return Promise.resolve(body.include_gas?gasCatalog:financeCatalog);}
        if(url.endsWith('/gas-order-proof')){calls.push({url,body,passive});return Promise.resolve({saved:true,
          source_kind:url.split('/').at(-3),source_id:url.split('/').at(-2),
          proof:{schema:'orbis.dex.gas.order.proof.v1',purpose:'NATIVE_GAS_ONLY',block_number:'43',transaction_index:'0'}});}
        return original(url,options,passive);
      };
      OrbisEconomicReview.mount(document.querySelector('#host'),client);
    }""",booked)
    opened(page)
    page.locator('[data-er-include-gas]').check()
    expect(page.locator('[data-er-source] strong')).to_have_text('Gas · Permiso')


@pytest.mark.parametrize('capability',[
    {'capabilities':{'economic_review':True,'dex_gas_order_proof':True}},
    {'capabilities':{'economic_review':True,'economic_review_gas':'true','dex_gas_order_proof':True}},
    {'capabilities':{'economic_review':True,'economic_review_gas':True,'dex_gas_order_proof':False}},
])
def test_gas_control_requires_both_exact_backend_capabilities(page,capability):
    page.evaluate('(cap)=>{capability=cap;OrbisEconomicReview.mount(document.querySelector("#host"),client);}',capability)
    opened(page)
    expect(page.locator('[data-er-include-gas]')).to_have_count(0)


def test_gas_row_has_no_phantom_trade_uses_gas_proof_and_explicit_request(page):
    gas_mode(page)
    expect(page.locator('[data-er-source]')).not_to_contain_text('WBNB')
    expect(page.locator('[data-er-source]')).not_to_contain_text('Venta')
    expect(page.locator('[data-er-trade]')).to_have_count(0)
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
    body=page.evaluate('calls.find(c=>c.url.endsWith("/preview")).body')
    assert body['include_gas'] is True and body['receipts'][0]['gas_only'] is True
    assert body['receipts'][0]['trade_event_id'] is None and not body['receipts'][0]['confirm_new_trade']
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/gas-order-proof")).length')==1
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/order-proof"))')
    assert page.evaluate('walletCalls')==[]


def test_booked_gas_opt_in_uses_existing_review_with_no_new_receipt(page):
    gas_mode(page,booked=True)
    expect(page.locator('[data-er-selected]')).to_have_count(0)
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
    body=page.evaluate('calls.find(c=>c.url.endsWith("/preview")).body')
    assert body['receipts']==[] and body['include_gas'] is True


def test_deselected_new_gas_does_not_require_another_context_or_proof(page):
    gas_mode(page)
    page.evaluate("gasCatalog.sources.push({...gasRow,source_id:acceptance,context_account_id:acceptance,context_name:'Other',context_verified:false})")
    page.get_by_role('button',name='Actualizar',exact=True).click()
    expect(page.locator('[data-er-source]')).to_have_count(2)
    page.locator('[data-er-source]').last.locator('[data-er-selected]').uncheck()
    expect(page.get_by_role('button',name='Revisar',exact=True)).to_be_enabled()
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/gas-order-proof")).length')==1


def test_gas_confirmation_lost_reply_preserves_selection_and_recovers_without_resubmission(page):
    gas_mode(page)
    page.get_by_role('button',name='Revisar',exact=True).click()
    page.get_by_role('button',name='Aceptar criterio',exact=True).click()
    page.evaluate('pending.shift().reject(new Error("lost gas reply"))')
    expect(page.get_by_role('button',name='Comprobar estado',exact=True)).to_be_visible()
    page.get_by_role('button',name='Cerrar revisión',exact=True).click()
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    page.get_by_role('button',name='Comprobar estado',exact=True).click()
    expect(page.locator('.economic-saved')).to_be_visible()
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/confirm")).length')==1
    assert page.evaluate('calls.find(c=>c.url.endsWith("/confirm")).body.include_gas') is True
    assert page.evaluate('walletCalls')==[]


def test_gas_catalogue_cannot_describe_a_trade_with_fee_only_profile(page):
    gas_mode(page)
    page.evaluate("gasRow.quantity='1'")
    page.get_by_role('button',name='Actualizar',exact=True).click()
    expect(page.locator('[data-er-message]')).to_have_text('Catálogo de recibos inválido.')
    expect(page.get_by_role('button',name='Revisar',exact=True)).to_be_disabled()


def test_gas_privacy_and_wallet_change_clear_old_opt_in_and_visible_values(page):
    gas_mode(page)
    page.evaluate('state.hidden=true;OrbisEconomicReview.update()')
    assert '1E-18' not in page.locator('[data-er-source]').inner_html()
    page.evaluate("state.hidden=false;state.wallet='0x'+'22'.repeat(20);state.generation++;OrbisEconomicReview.update()")
    expect(page.get_by_role('dialog')).not_to_be_visible()
    page.get_by_role('button',name='Revisar cuenta',exact=True).click()
    expect(page.locator('[data-er-include-gas]')).not_to_be_checked()
    expect(page.locator('[data-er-source] strong')).to_have_text('Compra · WBNB')


def order_mode(page,*,nodes=3,groups=1,deferred=False):
    page.evaluate("""({nodes,groups,deferred})=>{
      capability={capabilities:{economic_review:true,economic_review_order:true}};
      const id=i=>'44444444-4444-4444-8444-'+String(i).padStart(12,'0');
      window.orderData={schema:'orbis.portfolio.economic.order.options.v1',revision:'c'.repeat(64),root_account_id:account,
        moves_funds:false,trading_authority:false,saved:false,groups:Array.from({length:groups},(_,g)=>{
          const manual=Array.from({length:nodes-2},(_,i)=>({id:'event:'+id(g*1000+i),type:'MANUAL',kind:'CONTRIBUTION',
            asset:'USDT',quantity:null,amount:'1.000000000000000001',quote_currency:'USDT',gas_amount:null}));
          const chain=Array.from({length:2},(_,i)=>({id:'chain:'+id(g*1000+500+i),type:'CHAIN',kind:'BUY',asset:'WBNB',
            quantity:'1',amount:'2',quote_currency:'USDT',gas_amount:'0.0001',block_number:'42',transaction_index:String(i)}));
          return {account_id:account,name:'Principal '+g,at:'2026-11-06T12:00:'+String(g).padStart(2,'0')+'Z',accepted:false,
            nodes:[...manual,...chain],chain_order:chain.map(n=>n.id)};
        })};
      const original=client.request;
      client.request=(url,options,passive)=>{
        if(url.endsWith('/order-options')){calls.push({url,body:JSON.parse(options.body),passive});
          return deferred?new Promise((resolve,reject)=>{window.orderPending={resolve,reject};}):Promise.resolve(orderData);}
        return original(url,options,passive);
      };
      OrbisEconomicReview.mount(document.querySelector('#host'),client);
    }""",{'nodes':nodes,'groups':groups,'deferred':deferred})
    opened(page)
    page.get_by_role('button',name='Revisar',exact=True).click()
    if not deferred:expect(page.locator('[data-er-fix-order]')).to_be_visible()


def test_order_step_is_explicit_keeps_network_subsequence_and_recovers_exact_request(page):
    order_mode(page)
    expect(page.locator('[data-er-fix-order]')).not_to_be_checked()
    expect(page.locator('[data-er-action="order-down"]').first).to_be_disabled()
    page.locator('[data-er-fix-order]').check()
    page.locator('[data-er-action="order-down"]').first.click()
    assert page.locator('[data-er-order-node]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.erOrderNode)')==page.evaluate(
        '[orderData.groups[0].nodes[1].id,orderData.groups[0].nodes[0].id,orderData.groups[0].nodes[2].id]')
    page.locator('[data-er-action="order-down"]').nth(1).click()
    expect(page.locator('[data-er-action="order-down"]').first).to_be_disabled()
    expect(page.locator('[data-er-action="order-up"]').nth(1)).to_be_disabled()
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
    body=page.evaluate('calls.find(c=>c.url.endsWith("/preview")).body')
    assert body['order_options_digest']=='c'*64
    assert body['tie_orders'][0]['nodes']==page.evaluate('[orderData.groups[0].nodes[1].id,orderData.groups[0].nodes[2].id,orderData.groups[0].nodes[0].id]')
    page.get_by_role('button',name='Aceptar criterio',exact=True).click()
    page.evaluate('pending.shift().reject(new Error("lost"))')
    page.get_by_role('button',name='Comprobar estado',exact=True).click()
    expect(page.locator('.economic-saved')).to_have_text('Criterio guardado ✓')
    submitted=page.evaluate('calls.find(c=>c.url.endsWith("/confirm")).body')
    assert submitted['tie_orders']==body['tie_orders'] and submitted['order_options_digest']==body['order_options_digest']
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/confirm")).length')==1
    assert page.evaluate('walletCalls')==[]


def test_unfixed_order_does_not_silently_send_manual_choices_and_empty_groups_skip_step(page):
    order_mode(page)
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
    assert page.evaluate('calls.find(c=>c.url.endsWith("/preview")).body.tie_orders')==[]
    page.get_by_role('button',name='Volver',exact=True).click()
    page.get_by_role('button',name='Recibos',exact=True).click()
    page.evaluate('orderData.groups=[]')
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/order-options")).length')==2


def test_order_pagination_keeps_offscreen_groups_and_focus_after_crossing_node_page(page):
    order_mode(page,nodes=53,groups=2)
    expect(page.locator('[data-er-order-node]')).to_have_count(50)
    page.locator('[data-er-fix-order]').check()
    page.get_by_role('navigation',name='Movimientos del grupo').get_by_role('button',name='Siguiente',exact=True).click()
    expect(page.locator('[data-er-order-node]')).to_have_count(3)
    moved=page.locator('[data-er-order-node]').first.get_attribute('data-er-order-node')
    page.locator('[data-er-action="order-up"]').first.click()
    expect(page.locator('[data-er-order-node]')).to_have_count(50)
    assert page.evaluate('document.activeElement.closest("[data-er-order-node]").dataset.erOrderNode')==moved
    page.get_by_role('button',name='Grupo siguiente',exact=True).click()
    page.locator('[data-er-fix-order]').check()
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
    selected=page.evaluate('calls.find(c=>c.url.endsWith("/preview")).body.tie_orders')
    assert len(selected)==2 and all(len(g['nodes'])==53 for g in selected)
    assert selected[0]['nodes'][49]==moved


def test_changed_selection_discards_order_options_and_refresh_does_not_preserve_old_choices(page):
    order_mode(page)
    page.locator('[data-er-fix-order]').check()
    page.get_by_role('button',name='Actualizar orden',exact=True).click()
    expect(page.locator('[data-er-fix-order]')).not_to_be_checked()
    request=page.evaluate('calls.filter(c=>c.url.endsWith("/order-options")).at(-1).body')
    assert 'tie_orders'not in request and 'order_options_digest'not in request
    page.get_by_role('button',name='Recibos',exact=True).click()
    page.locator('[data-er-selected]').uncheck()
    page.locator('[data-er-selected]').check()
    page.get_by_role('button',name='Revisar',exact=True).click()
    expect(page.locator('[data-er-fix-order]')).not_to_be_checked()
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/order-options")).length')==3


@pytest.mark.parametrize('change',['wallet','generation','close'])
def test_late_order_options_never_repopulate_another_wallet_session_or_closed_dialog(page,change):
    order_mode(page,deferred=True)
    if change=='close':page.get_by_role('button',name='Cerrar revisión').click()
    else:page.evaluate("(field)=>{if(field==='wallet')state.wallet='0x'+'22'.repeat(20);else state.generation++;OrbisEconomicReview.update();}",change)
    page.evaluate('orderPending.resolve(orderData)')
    expect(page.locator('dialog')).not_to_be_visible()
    expect(page.locator('[data-er-body]')).to_be_empty()
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/preview"))')


def test_order_options_reject_contradictory_network_nodes_and_mask_exact_values(page):
    order_mode(page)
    page.evaluate('state.hidden=true;OrbisEconomicReview.update()')
    assert '1.000000000000000001'not in page.locator('[data-er-body]').inner_html()
    page.evaluate('state.hidden=false;orderData.groups[0].chain_order.reverse();OrbisEconomicReview.update()')
    page.get_by_role('button',name='Actualizar orden',exact=True).click()
    expect(page.locator('[data-er-message]')).to_have_text('Opciones de orden inválidas.')
    expect(page.locator('[data-er-fix-order]')).to_have_count(0)
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/preview"))')


def test_nonboolean_order_capability_retains_the_old_review_request(page):
    page.evaluate("capability={capabilities:{economic_review:true,economic_review_order:'true'}};OrbisEconomicReview.mount(document.querySelector('#host'),client)")
    reviewed(page)
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/order-options"))')
    assert 'order_options_digest'not in page.evaluate('calls.find(c=>c.url.endsWith("/preview")).body')
