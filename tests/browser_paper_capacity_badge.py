"""Comprueba el aviso de capacidad PAPER sin tocar las bases del usuario."""

from pathlib import Path

from playwright.sync_api import sync_playwright


STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content('<div id="mount"></div>')
    page.wait_for_load_state("networkidle")
    page.add_script_tag(path=str(STATIC / "spot-paper.js"))
    page.evaluate("""() => {
      const session = {id:'session-1', symbol:'BTC/USDT', timeframe:'1h', status:'ACTIVE'};
      const detail = {...session, engine_version:'lab-test', capacity_remaining:2,
        last_t:1735689600, status_reason:null, events:[], coverage:null,
        state:{cash:'1000', units:'0', realized_pnl:'0', closed_trades:0},
        valuation:{basis:'UNAVAILABLE_NO_QUOTE', equity_if_closed:null,
          unrealized_pnl_if_closed:null}, last_quote:null};
      const responses = {
        '/lab/datasets':[{id:'dataset-1',symbol:'BTC/USDT',timeframe:'1h',count:4998,
          source:'mexc-spot-public',verified_public:true,gaps:[]}],
        '/lab/strategy-versions':[{id:'version-1',strategy_id:'strategy-1',name:'Prueba',
          version:1,definition:{direction:'LONG'}}],
        '/paper/spot/strategies':[session],
        '/paper/spot/strategies/worker':{running:false},
        '/paper/spot/strategies/readiness?dataset_id=dataset-1':{ready:false,reason:'prueba'},
        '/lab/datasets/dataset-1/lineage':[],
        '/lab/strategies/strategy-1/versions':[{id:'version-1',version:1,definition:{direction:'LONG'}}],
        '/paper/spot/strategies/session-1':detail
      };
      window.fetch = async path => ({ok:Object.hasOwn(responses,path),
        json:async () => responses[path] ?? {detail:'no disponible en la prueba'}});
      window.OrbisSpotPaper.render(document.getElementById('mount'));
    }""")
    badge = page.locator(".paper-capacity-warning")
    badge.wait_for()
    assert badge.inner_text() == "Restan 2 velas"
    assert "5000 velas" in badge.get_attribute("data-help")
    page.evaluate("window.OrbisSpotPaper.cleanup()")
    browser.close()
