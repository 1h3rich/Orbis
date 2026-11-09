"""Componente real en Chromium; la frontera de API controla errores/latencia."""

from pathlib import Path
import re
from playwright.sync_api import expect,sync_playwright


MODULE = Path(__file__).resolve().parent.parent/"app/web/static/mexc-bots.js"
HELP = MODULE.with_name("help.js")
BOUNDARY = r"""() => {
  const copy = value => JSON.parse(JSON.stringify(value));
  const source = {session_id:'paper',version_id:'version',symbol:'BTC/USDT',source_kind:'STRATEGY',
    timeframe:'1h',name:'Prueba',version:1,ready:true,paper_ready:true,
    research_options:[{id:'reserve',created_at:'2025-01-01T00:00:00Z',test_bars:20,verification:'ON_ACTIVATION'}]};
  const draft = {id:'draft',strategy_version_id:'version',symbol:'BTCUSDT',quote_asset:'USDT',
    max_order_quote:'100',expires_at:new Date(Date.now()+7*86400000).toISOString()};
  const bot = {id:'bot',policy_id:'daily',strategy_version_id:'version',paper_session_id:'paper',
    symbol:'BTC/USDT',timeframe:'1h',execution_enabled:true,status:'RUNNING',stored_status:'ACTIVE',
    execution_mode:'CONTINUOUS',entry_budget_quote:'20',execution_permission:{expires_at:new Date(Date.now()+3*86400000).toISOString()},
    worker:{running:true,permission_remaining_seconds:3600},position:{verified:true,units:'0',cost_quote:'0',realized_pnl_quote:'0',fill_count:0}};
  const workflow = {view:'LINKED',trade_verified:true,pending_orders:false,withdrawal_pending:false,stopped:false,bot_active:false};
  window.boundary = {paperError:null,listError:null,workflow,actions:[],pauseDrafts:0,pending:[],posts:[],sources:[source],drafts:[draft],bots:[bot],account:'account',limitSource:null};
  const client = {
    getAccountId:()=> boundary.account,accountLabel:()=> boundary.account,format:String,uuid:()=> 'request',
    onChange:()=>{},onControlChange:()=>{},prepareLimits:item=>{boundary.limitSource=copy(item);},
    openAccountAction:action=>boundary.actions.push(action),amountsHidden:()=>false,
    request:(path,options)=>{
      const b=window.boundary;
      if(options && options.method==='POST'){b.posts.push(path);return Promise.reject(new Error('Unexpected POST'));}
      if(path.endsWith('/candidates')) return b.paperError ? Promise.reject(new Error(b.paperError)) : Promise.resolve(copy(b.sources));
      if(path.endsWith('/mexc-policy-drafts')){
        const result=copy(b.drafts);
        if(b.pauseDrafts>0){b.pauseDrafts--;return new Promise((resolve,reject)=>b.pending.push({resolve:()=>resolve(result),reject}));}
        return Promise.resolve(result);
      }
      if(path.endsWith('/mexc-workflow'))return Promise.resolve(copy(b.workflow));
      if(path.endsWith('/decisions'))return Promise.resolve([]);
      if(path.endsWith('/mexc-bots'))return b.listError ? Promise.reject(new Error(b.listError)) : Promise.resolve(copy(b.bots));
      return Promise.reject(new Error('Unexpected GET'));
    }
  };
  boundary.mount = prepared => window.OrbisMexcBots.mount(document.querySelector('#bots'),client,prepared);
  boundary.mount();
}"""


def ready_page(browser):
    page = browser.new_page()
    page.set_content('<main id="bots"></main>')
    page.add_script_tag(path=str(MODULE)); page.add_script_tag(path=str(HELP)); page.evaluate(BOUNDARY)
    page.locator("summary").click()
    expect(page.locator('[name="paper"] option')).to_have_count(2)
    page.locator('[name="paper"]').select_option("paper")
    page.locator('[name="budget"]').fill("20")
    expect(page.locator('[data-bots-start]')).to_be_enabled()
    return page


def source_unavailable_is_scoped_to_preparation(browser):
    page = ready_page(browser)
    try:
        page.evaluate("() => {boundary.paperError='Desbloquea Spot PAPER cifrado antes de operar con un bot'; OrbisMexcBots.refresh();}")
        expect(page.locator('[name="paper"] option')).to_have_count(1)
        expect(page.locator('[data-bots-form-state]')).to_have_text("PAPER · no disponible")
        expect(page.locator('[data-bots-form-state]')).to_have_attribute("title",re.compile("Desbloquea Spot PAPER"))
        expect(page.locator('[data-bots-form-state]')).to_have_attribute("aria-description",re.compile("Desbloquea Spot PAPER"))
        expect(page.locator('[data-bots-state].error')).not_to_be_visible()
        expect(page.locator('[data-bots-badge]')).to_contain_text("activo")
        expect(page.locator('[data-bots-start]')).to_be_disabled()
        expect(page.locator('[data-bot-stop]')).to_be_enabled()
        assert not page.evaluate("boundary.posts")
    finally:
        page.evaluate("OrbisMexcBots.clear()"); page.close()


def unverifiable_controls_block_new_authorization_until_refreshed(browser):
    page = ready_page(browser)
    dialogs = []
    def accept(dialog): dialogs.append(dialog.message); dialog.accept()
    page.on("dialog",accept)
    try:
        page.evaluate("() => {boundary.pauseDrafts=1; OrbisMexcBots.refresh();}")
        page.wait_for_function("boundary.pending.length === 1")
        expect(page.locator('[data-bots-start]')).to_be_disabled()
        expect(page.locator('[data-bot-stop]')).to_be_enabled()
        page.evaluate("boundary.pending.shift().reject(new Error('Fallo controlado de lectura'))")
        expect(page.locator('[data-bots-form-state]')).to_have_text("Preparación · no disponible")
        expect(page.locator('[data-bots-start]')).to_be_disabled()
        page.locator('[data-bots-form]').evaluate("form => form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))")
        assert not dialogs and not page.evaluate("boundary.posts")
        page.evaluate("OrbisMexcBots.refresh()")
        expect(page.locator('[data-bots-start]')).to_be_enabled()
        expect(page.locator('[data-bots-form-state]')).to_have_text("Preparado para activar")
        assert page.locator('[name="budget"]').input_value() == "20"
    finally:
        page.evaluate("OrbisMexcBots.clear()"); page.close()


def slow_preparation_does_not_suspend_bot_status_polling(browser):
    page = ready_page(browser)
    try:
        page.evaluate("() => {boundary.pauseDrafts=1; OrbisMexcBots.refresh();}")
        page.wait_for_function("boundary.pending.length === 1")
        page.evaluate("() => {const b=boundary.bots[0]; b.execution_enabled=false; b.status=b.stored_status='PAUSED'; b.worker.running=false;}")
        expect(page.locator('[data-bots-list] .statusbadge')).to_have_text("Pausado",timeout=7500)
        expect(page.locator('[data-bots-start]')).to_be_disabled()
        expect(page.locator('[data-bots-form-state]')).to_have_text("Comprobando…")
        assert not page.evaluate("boundary.posts")
    finally:
        page.evaluate("OrbisMexcBots.clear()"); page.close()


def old_controls_cannot_overwrite_newer_unavailable_sources(browser):
    page = ready_page(browser)
    try:
        page.evaluate("() => {boundary.pauseDrafts=1; OrbisMexcBots.refresh();}")
        page.wait_for_function("boundary.pending.length === 1")
        page.evaluate("() => {boundary.paperError='PAPER cerrado'; OrbisMexcBots.refresh();}")
        expect(page.locator('[data-bots-form-state]')).to_have_text("PAPER · no disponible")
        page.evaluate("boundary.pending.shift().resolve()")
        expect(page.locator('[name="paper"] option')).to_have_count(1)
        expect(page.locator('[data-bots-form-state]')).to_have_text("PAPER · no disponible")
        expect(page.locator('[data-bots-start]')).to_be_disabled()
    finally:
        page.evaluate("OrbisMexcBots.clear()"); page.close()


def unverifiable_bot_list_blocks_authorization_even_if_controls_arrive_later(browser):
    page = ready_page(browser)
    try:
        page.evaluate("() => {boundary.pauseDrafts=1; boundary.listError='Estado no verificable'; OrbisMexcBots.refresh();}")
        page.wait_for_function("boundary.pending.length === 1")
        expect(page.locator('[data-bots-state]')).to_have_text("Estado no verificable")
        expect(page.locator('[data-bots-list] .statusbadge')).to_have_text("Sin verificar")
        expect(page.locator('[data-bots-badge]')).to_contain_text("sin verificar")
        expect(page.locator('[data-bot-stop]')).to_be_enabled()
        page.evaluate("boundary.pending.shift().resolve()")
        expect(page.locator('[data-bots-form-state]')).to_have_text("Bots · sin verificar")
        expect(page.locator('[data-bots-start]')).to_be_disabled()
        assert not page.evaluate("boundary.posts")
        page.evaluate("() => {boundary.listError=null; OrbisMexcBots.refresh();}")
        expect(page.locator('[data-bots-start]')).to_be_enabled()
        expect(page.locator('[data-bots-list] .statusbadge')).to_have_text("Activo")
        expect(page.locator('[data-bots-state]')).not_to_be_visible()
    finally:
        page.evaluate("OrbisMexcBots.clear()"); page.close()


def malformed_preparation_response_does_not_poison_the_form(browser):
    page = ready_page(browser)
    errors = []
    page.on("pageerror",lambda error:errors.append(str(error)))
    try:
        page.evaluate("() => {boundary.savedDrafts=boundary.drafts; boundary.drafts=null; OrbisMexcBots.refresh();}")
        expect(page.locator('[data-bots-form-state]')).to_have_text("Preparación · no disponible")
        expect(page.locator('[data-bots-start]')).to_be_disabled()
        assert not errors,errors
        page.evaluate("() => {boundary.drafts=boundary.savedDrafts; OrbisMexcBots.refresh();}")
        expect(page.locator('[data-bots-start]')).to_be_enabled()
        assert not errors and not page.evaluate("boundary.posts")
    finally:
        page.evaluate("OrbisMexcBots.clear()"); page.close()


def malformed_entries_are_not_treated_as_verified_preparation(browser):
    for field,value in (("sources",[None]),("drafts",[None]),("sources",[{}]),("drafts",[{}])):
        page = ready_page(browser)
        errors = []
        page.on("pageerror",lambda error:errors.append(str(error)))
        try:
            page.evaluate("([field,value]) => {boundary[field]=value; OrbisMexcBots.refresh();}",[field,value])
            expect(page.locator('[data-bots-form-state]')).to_have_text("Preparación · no disponible")
            expect(page.locator('[data-bots-start]')).to_be_disabled()
            assert not errors and not page.evaluate("boundary.posts"),(field,errors)
        finally:
            page.evaluate("OrbisMexcBots.clear()"); page.close()


def handoff_never_substitutes_another_session_or_reuses_the_previous_account(browser):
    page = ready_page(browser)
    try:
        form = page.locator('[data-bots-form]')
        status = page.locator('[data-bots-form-state]')
        page.evaluate("boundary.mount({id:'exact',kind:'DCA'})")
        expect(status).to_have_text("Sesión PAPER · no disponible")
        expect(form.locator('[name=paper]')).to_have_value("")
        expect(form.locator('[name=budget]')).to_have_value("")
        expect(form.locator('[data-bots-start]')).to_be_disabled()
        page.evaluate("""() => {
          const base=boundary.sources[0];
          boundary.sources.push({...base,session_id:'exact',source_kind:'STRATEGY'});
          OrbisMexcBots.refresh();
        }""")
        expect(status).to_have_text("Sesión PAPER · no disponible")
        expect(form.locator('[name=paper]')).to_have_value("")
        page.evaluate("""() => {
          Object.assign(boundary.sources[1],{source_kind:'DCA',version_id:'dca-v2',name:'Plan diario',version:2,
            symbol:'ETH/USDT',timeframe:'1d',ready:false,paper_ready:false,reason:'Falta observación'});
          OrbisMexcBots.refresh();
        }""")
        expect(status).to_have_text("Observación PAPER · pendiente")
        expect(form.locator('[name=paper]')).to_have_value("")
        page.evaluate("() => {Object.assign(boundary.sources[1],{ready:true,paper_ready:true}); OrbisMexcBots.refresh();}")
        expect(form.locator('[name=paper]')).to_have_value("exact")
        expect(form.locator('[name=research]')).to_have_value("reserve")
        expect(form.locator('[name=draft]')).to_have_value("")
        expect(status).to_have_text("Límites · seleccionar")
        expect(form.locator('[name=dca_total]')).to_be_visible()
        form.get_by_role('button',name='Crear límites').click()
        assert page.evaluate("[boundary.limitSource.version_id,boundary.limitSource.symbol]")==["dca-v2","ETH/USDT"]
        form.locator('[name=budget]').fill('30')
        page.evaluate("""() => {boundary.pauseDrafts=1;OrbisMexcBots.refresh();}""")
        page.wait_for_function("boundary.pending.length===1")
        page.evaluate("""() => {
          boundary.account='other'; boundary.drafts=[]; boundary.bots=[];
          OrbisMexcBots.accountChanged(); boundary.pending.shift().resolve();
        }""")
        expect(page.locator('[data-bots-handoff-account]')).to_have_text("other")
        expect(form.locator('[name=budget]')).to_have_value("")
        expect(form.locator('[name=paper]')).to_have_value("exact")
        expect(form.locator('[data-bots-start]')).to_be_disabled()
        expect(page.locator('[data-bots-list]')).to_contain_text("Sin bots reales")
        page.locator('[data-bots-discard-handoff]').click()
        expect(page.locator('[data-bots-handoff]')).not_to_be_visible()
        expect(form.locator('[name=paper]')).to_have_value("")
        assert not page.evaluate("boundary.posts")
    finally:
        page.evaluate("OrbisMexcBots.clear()"); page.close()


def account_workflow_blocks_submit_and_opens_the_matching_control(browser):
    page = ready_page(browser)
    try:
        workflow = page.evaluate("boundary.workflow")
        form = page.locator('[data-bots-form]')
        state = page.locator('[data-bots-form-state]')
        action = form.locator('[data-bots-account-action]')
        cases = [
            ({"view":"DISCONNECTED"}, "View · conectar", "Conectar View", "view"),
            ({"view":"OTHER_ACCOUNT"}, "View · otra cuenta", "Elegir cuenta", "account"),
            ({"view":"DIFFERENT_CONNECTION"}, "View · revisar vínculo", "Revisar vínculo", "link"),
            ({"view":"UNLINKED","book_empty":True}, "View · vincular", "Registrar saldo", "opening"),
            ({"view":"UNLINKED","book_empty":False}, "View · vincular", "Vincular View", "link"),
            ({"pending_orders":True}, "Orden · pendiente", "Revisar órdenes", "orders"),
            ({"withdrawal_pending":True}, "Retirada · pendiente", "Revisar retirada", "withdrawal"),
            ({"stopped":True}, "STOP activo", "Abrir Orden Spot", "spot"),
            ({"trade_verified":False}, "Trade · verificar", "Configurar Trade", "trade"),
        ]
        for patch, label, button, destination in cases:
            page.evaluate("value => { boundary.workflow=value; OrbisMexcBots.refresh(); }", {**workflow,**patch})
            expect(state).to_have_text(label)
            expect(action).to_have_text(button)
            expect(form.locator('[data-bots-start]')).to_be_disabled()
            form.evaluate("form => form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))")
            action.click()
            assert page.evaluate("boundary.actions.at(-1)") == destination
        page.evaluate("value => { boundary.workflow=value; OrbisMexcBots.refresh(); }", {**workflow,"bot_active":True})
        expect(state).to_have_text("Cuenta · bot activo")
        expect(action).to_be_hidden()
        expect(form.locator('[data-bots-start]')).to_be_disabled()
        page.evaluate("""() => {
          const bot=boundary.bots[0]; bot.execution_enabled=false; bot.worker.running=false; OrbisMexcBots.refresh();
        }""")
        page.locator('[data-bot-rearm]').click()
        expect(form.locator('[data-bots-start]')).to_be_enabled()
        page.evaluate("value => { boundary.workflow=value; OrbisMexcBots.refresh(); }", {**workflow,"stopped":True})
        expect(form.locator('[data-bots-start]')).to_be_enabled()
        page.evaluate("() => { boundary.workflow={view:'LINKED'}; OrbisMexcBots.refresh(); }")
        expect(state).to_have_text("Cuenta · sin verificar")
        expect(form.locator('[data-bots-start]')).to_be_disabled()
        assert not page.evaluate("boundary.posts")
    finally:
        page.evaluate("OrbisMexcBots.clear()"); page.close()


if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        source_unavailable_is_scoped_to_preparation(browser)
        unverifiable_controls_block_new_authorization_until_refreshed(browser)
        slow_preparation_does_not_suspend_bot_status_polling(browser)
        old_controls_cannot_overwrite_newer_unavailable_sources(browser)
        unverifiable_bot_list_blocks_authorization_even_if_controls_arrive_later(browser)
        malformed_preparation_response_does_not_poison_the_form(browser)
        malformed_entries_are_not_treated_as_verified_preparation(browser)
        handoff_never_substitutes_another_session_or_reuses_the_previous_account(browser)
        account_workflow_blocks_submit_and_opens_the_matching_control(browser)
        browser.close()
    print("Formulario: disponibilidad PAPER separada del estado del bot · Chromium real")
