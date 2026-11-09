"""Real UI → LAB reserve → PAPER worker → chart/replay/export; owned market only."""
from decimal import Decimal
import io,json,os
from pathlib import Path

from playwright.sync_api import sync_playwright,expect

from app.derivatives.perp_observed_export import verify_observed_export
from app.derivatives.perp_gap_close_export import verify_gap_close_export
from tests.perp_observed_archive_fixture import LAB_KEY,PAPER_KEY

BASE=os.environ['ORBIS_TEST_BASE_URL'];directory=Path(os.environ['ORBIS_BASIC_DIRECTORY']).parent
reopen=os.environ['ORBIS_BASIC_PHASE']=='reopen';errors=[];server_errors=[];private_actions=[];activation_posts=[]
API='/paper/perp/observed'

def advance(page,action):
    response=page.request.post(BASE+'/__observed_fixture/advance',data={'fixture_id':directory.name,'action':action})
    assert response.ok,response.text()

def compare_reserve(page,paper,detail,validation_id):
    session=detail["session"]
    with page.expect_response(lambda r:"/reserve-comparison?" in r.url,timeout=15000) as compared:
        paper.locator("#observed-compare").click()
    reply=compared.value
    assert reply.status==200 and reply.headers["cache-control"]=="no-store",reply.text()
    value=reply.json()
    assert value["validation_id"]==validation_id and value["execution_authority"]=="NONE"
    assert value["paper"]["head_hash"]==session["head_hash"] and value["paper"]["sequence"]==session["sequence"]
    metrics=value["paper"]["metrics"]
    assert Decimal(metrics["net_profit"])==Decimal("74.997") and Decimal(metrics["return_pct"])==Decimal("7.4997")
    table=paper.locator("#observed-comparison-table")
    expect(table.locator("tbody tr")).to_have_count(2)
    expect(table.get_by_role("rowheader",name="PAPER observado Finalizado")).to_be_visible()
    expect(table.get_by_role("rowheader",name="LAB · Reserva 1",exact=False)).to_be_visible()
    expect(table.locator('[data-exact="'+metrics["net_profit"]+'"]')).to_be_visible()
    return value

def check_margin(paper, detail):
    history = detail['view']['margin_rules']
    change, = history['changes']
    assert history['change_total'] == 1 and change['changes'] == ['RISK_TIERS']
    assert change['rules']['risk_brackets'][0]['maintenance_margin_rate'] == '0.011'
    panel = paper.locator('#observed-margin')
    expect(panel).to_be_visible()
    panel.locator(':scope > summary').click()
    expect(panel).to_contain_text('1 cambio')
    item = panel.locator('[data-margin-sequence="'+str(change['sequence'])+'"]')
    item.locator(':scope > summary').click()
    expect(item.get_by_role('cell', name='0.011', exact=True)).to_be_visible()
    expect(panel.locator('[data-margin-sequence="0"]')).to_contain_text('Reglas iniciales')


def instrument_chart(page):
    page.evaluate('''()=>{const original=window.LightweightCharts;let current=false;
      Object.defineProperty(window,'LightweightCharts',{configurable:true,value:Object.assign({},original,{
        createChart(node,options){const chart=original.createChart(node,options);current=node.id==='observed-chart';
          if(current){window.__observedChart=chart;window.__observedMarkers=[];}return chart;},
        createSeriesMarkers(series,markers,options){if(current)window.__observedMarkers.push({series,markers});
          return original.createSeriesMarkers(series,markers,options);}
      })});}''')

def chart_points(page,count):
    expect(page.locator('#observed-chart')).to_have_attribute('data-entry-count','1')
    expect(page.locator('#observed-chart')).to_have_attribute('data-exit-count',str(count-1))
    page.wait_for_timeout(150)
    points=page.evaluate('''()=>window.__observedMarkers.flatMap(({series,markers})=>markers.map(m=>{
      const data=series.data().find(d=>d.time===m.time);return {x:window.__observedChart.timeScale().timeToCoordinate(m.time),
        y:series.priceToCoordinate(data.value),price:data.value,time:m.time,label:m.text};}))''')
    assert len(points)==count and all(p['x'] is not None and p['y'] is not None for p in points),points
    assert all(p['x']>=0 and p['y']>=0 for p in points),points
    painted=page.evaluate('''()=>window.__observedMarkers.flatMap(({series,markers})=>markers.map(m=>{
      const data=series.data().find(d=>d.time===m.time),x=window.__observedChart.timeScale().timeToCoordinate(m.time),y=series.priceToCoordinate(data.value),
        color=m.text==='×'?[255,123,120]:[233,183,100];
      return [...document.querySelectorAll('#observed-chart canvas')].some(c=>{
        if(c.clientWidth<200||c.clientHeight<300)return false;const dpr=c.width/c.clientWidth,ctx=c.getContext('2d');
        const pixels=ctx.getImageData(Math.max(0,Math.round(x*dpr)-5),Math.max(0,Math.round(y*dpr)-5),11,11).data;
        for(let i=0;i<pixels.length;i+=4)if(color.every((v,k)=>Math.abs(pixels[i+k]-v)<20))return true;return false;
      });}))''')
    assert all(painted),{'points':points,'painted':painted}
    return points


def gap_point(page,expected):
    expect(page.locator('#observed-chart')).to_have_attribute('data-entry-count','1')
    expect(page.locator('#observed-chart')).to_have_attribute('data-exit-count','0')
    expect(page.locator('#observed-chart')).to_have_attribute('data-estimated-close-count','1')
    page.wait_for_timeout(150)
    points=page.evaluate('''()=>window.__observedMarkers.flatMap(({series,markers})=>markers.filter(m=>m.text.startsWith('≈')).map(m=>{
      const data=series.data().find(d=>d.time===m.time),x=window.__observedChart.timeScale().timeToCoordinate(m.time),y=series.priceToCoordinate(data.value);
      const painted=[...document.querySelectorAll('#observed-chart canvas')].some(c=>{
        if(c.clientWidth<200||c.clientHeight<300)return false;const dpr=c.width/c.clientWidth;
        const pixels=c.getContext('2d').getImageData(Math.max(0,Math.round(x*dpr)-6),Math.max(0,Math.round(y*dpr)-6),13,13).data;
        for(let i=0;i<pixels.length;i+=4)if([167,139,250].every((v,k)=>Math.abs(pixels[i+k]-v)<20))return true;return false;
      });return {x,y,price:data.value,time:m.time,painted};}))''')
    assert len(points)==1 and points[0]['price']==103 and points[0]['painted'],points
    assert points[0]['time']==int(expected['result']['exit_time_ns'])//10**9

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('response',lambda r:server_errors.append([r.status,r.url]) if r.status>=500 else None)
    page.on('request',lambda r:private_actions.append(r.url) if '/real/' in r.url and r.method not in ('GET','HEAD','OPTIONS') else None)
    page.on('request',lambda r:activation_posts.append(r.post_data_json) if r.url.endswith(API+'/activate') and r.method=='POST' else None)
    state=page.request.get(BASE+'/__observed_fixture/state').json()
    assert state['lab_locked'] and state['paper_locked'] and not state['real_exists']
    assert state['coordinator']['running_workers']==0 and state['calls']==[]
    page.goto(BASE+'/ui/#/config');page.wait_for_load_state('networkidle')
    for name,password in [('lab',LAB_KEY),('perp',PAPER_KEY)]:
        form=page.locator('#cfg-'+name+'-unlock-form');form.evaluate('(e)=>e.closest("details").open=true')
        if name=='perp':
            expect(form.locator('[name=session_seconds]')).to_have_value('900')
            if not reopen:form.locator('[name=session_seconds]').select_option('604800')
        form.locator('[name=passphrase]').fill(password);form.locator('button[type=submit]').click()
        expect(page.locator('#cfg-'+name+'-status')).to_contain_text('h restantes' if name=='perp' and not reopen else 'min restantes')
        expect(form.locator('[name=passphrase]')).to_have_value('')
    selected=page.request.get(BASE+'/paper/perp/status').json()
    assert selected['session_seconds']==(900 if reopen else 604800)
    if not reopen:
        page.set_viewport_size({'width':390,'height':844})
        page.locator('#cfg-perp-status').locator('..').screenshot(path=str(directory/'paper-duration-mobile.png'))
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.set_viewport_size({'width':1440,'height':1000})
    page.goto(BASE+'/ui/#/laboratorio');page.wait_for_load_state('networkidle');instrument_chart(page)
    paper=page.locator('#lab-perp-observed')
    if not reopen:
        panel=page.locator('#lab-perp-validations');panel.locator(':scope > summary').click()
        form=panel.locator('#perp-validation-form')
        expect(form.locator('[name=profile_id] option')).to_have_count(2)
        form.locator('[name=profile_id]').select_option(state['profile_id']);form.locator('[name=dataset_id]').select_option(state['dataset_id'])
        with page.expect_response(lambda r:r.url.endswith('/lab/perp/validations') and r.request.method=='POST') as made:form.locator('button[type=submit]').click()
        assert made.value.status==201,made.value.text();validation=made.value.json()['id']
        expect(panel.locator('#perp-validation-paper')).to_be_visible()
        panel.locator('#perp-validation-paper').click()
        expect(paper.locator('#observed-review')).to_be_visible(timeout=10000)
        assert page.request.get(BASE+API+'/sessions').json()['total']==0
        with page.expect_response(lambda r:r.url.endswith(API+'/activate')) as activated:paper.locator('#observed-activate').click()
        assert activated.value.status==202,activated.value.text();id=activated.value.json()['session_id']
        expect(paper.locator('[data-observed-session]')).to_have_count(1,timeout=10000)
        advance(page,'ENTRY');row=paper.locator('[data-observed-session="'+id+'"]')
        expect(row).to_contain_text('24.999',timeout=15000)
        row.locator('[data-observed-view]').click();points=chart_points(page,1)
        assert points[0]['price']==100
        expect(paper.locator('#observed-older')).to_be_enabled()
        paper.locator('#observed-older').click()
        expect(paper.locator('#observed-older')).to_be_disabled(timeout=15000)
        assert int(paper.locator('#observed-chart-coverage').inner_text().split(' / ')[0])>1000
        points=chart_points(page,1)
        paper.locator('#observed-inspection').screenshot(path=str(directory/'observed-open.png'))
        with page.expect_response(lambda r:r.url.endswith('/'+id+'/close')) as closed:row.locator('[data-observed-close]').click()
        assert closed.value.status==200,closed.value.text();advance(page,'EXIT')
        expect(row).to_contain_text('Finalizado',timeout=15000)
        row.locator('[data-observed-view]').click();points=chart_points(page,2)
        assert sorted(p['price'] for p in points)==[100,103]
        expect(paper.locator('#observed-trades tbody tr')).to_have_count(1)
        detail=page.request.get(BASE+API+'/sessions/'+id+'/view').json()
        check_margin(paper, detail)
        paper.locator('#observed-margin').screenshot(path=str(directory/'observed-margin.png'))
        assert Decimal(detail['view']['metrics']['equity'])==Decimal('1074.997')
        comparison=compare_reserve(page,paper,detail,validation)
        paper.locator("#observed-comparison").screenshot(path=str(directory/"observed-comparison.png"))
        with page.expect_download() as exported:paper.locator('#observed-export').click()
        output=directory/'observed-session.jsonl';exported.value.save_as(output)
        verified=verify_observed_export(io.BytesIO(output.read_bytes()))
        assert verified['checkpoint']==detail['session']['checkpoint']
        page.set_viewport_size({'width':390,'height':844})
        paper.locator('#observed-inspection').screenshot(path=str(directory/'observed-mobile.png'))
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.set_viewport_size({'width':1440,'height':1000})
        # A response lost after publication is recovered with GET, without
        # sending another activation or resetting the first session's capital.
        panel.locator('#perp-validation-paper').click();expect(paper.locator('#observed-review')).to_be_visible()
        lost=[];pattern='**/paper/perp/observed/activate'
        def lose_response(route):
            response=route.fetch();lost.append(response.json());route.abort('failed')
        page.route(pattern,lose_response)
        with page.expect_event('requestfailed',predicate=lambda request:request.url.endswith(API+'/activate'),timeout=15000) as lost_request:
            paper.locator('#observed-activate').click()
        assert lost_request.value.failure is not None and len(lost)==1
        page.unroute(pattern)
        if paper.locator('#observed-check').is_visible():paper.locator('#observed-check').click()
        expect(paper.locator('[data-observed-session]')).to_have_count(2,timeout=10000)
        assert len(lost)==1 and len(activation_posts)==2
        second_id=lost[0]['session_id'];second=paper.locator('[data-observed-session="'+second_id+'"]')
        second.locator('[data-observed-stop]').click();expect(second).to_contain_text('Finalizado',timeout=10000)
        # Cancel accepted before the held activation POST prevents late start.
        panel.locator('#perp-validation-paper').click();expect(paper.locator('#observed-review')).to_be_visible()
        held=[];page.route(pattern,lambda route:held.append(route));paper.locator('#observed-activate').click()
        expect(paper.locator('#observed-cancel')).to_be_visible();assert held
        with page.expect_response(lambda r:r.url.endswith('/cancel')) as cancelled:paper.locator('#observed-cancel').click()
        assert cancelled.value.ok,cancelled.value.text();held[0].continue_();page.unroute(pattern)
        expect(paper.locator('#observed-status')).to_contain_text('cancelada',timeout=10000)
        assert page.request.get(BASE+API+'/sessions').json()['total']==2 and len(activation_posts)==3
        # A real worker opens another owned PAPER position, then loses a point.
        panel.locator('#perp-validation-paper').click();expect(paper.locator('#observed-review')).to_be_visible()
        with page.expect_response(lambda r:r.url.endswith(API+'/activate')) as gap_started:paper.locator('#observed-activate').click()
        assert gap_started.value.status==202,gap_started.value.text();gap_id=gap_started.value.json()['session_id']
        advance(page,'GAP_ENTRY');gap_row=paper.locator('[data-observed-session="'+gap_id+'"]')
        expect(gap_row).to_contain_text('24.999',timeout=15000)
        advance(page,'GAP');expect(gap_row).to_contain_text('Interrumpido',timeout=15000)
        before_gap=page.request.get(BASE+API+'/sessions/'+gap_id).json()
        gap_row.locator('[data-observed-gap]').click()
        review=paper.locator('#gap-close-review');expect(review).to_be_visible()
        confirm=review.locator('[data-gap-action=confirm]');expect(confirm).to_be_disabled()
        paper.locator('#gap-close-ack').check();advance(page,'GAP_EXIT')
        lost_close=[];gap_pattern='**/sessions/'+gap_id+'/gap-close'
        def lose_close(route):
            response=route.fetch();assert response.ok,response.text();lost_close.append(response.json());route.abort('failed')
        page.route(gap_pattern,lose_close);confirm.click()
        expect(paper.locator('#gap-close-status')).to_contain_text('Respuesta pendiente',timeout=15000)
        saved_pending=page.evaluate("JSON.parse(sessionStorage.getItem('orbis.paper.perp.gapClose.pending'))")
        assert set(saved_pending)=={'session_id','expected_head','request_id'} and saved_pending['session_id']==gap_id
        page.unroute(gap_pattern)
        assert len(lost_close)==1
        paper.locator('[data-gap-action=check]').click()
        expect(paper.locator('#gap-close-result')).to_be_visible(timeout=15000)
        gap_value=lost_close[0]
        assert Decimal(gap_value['result']['metrics']['equity'])==Decimal('1074.997')
        expect(paper.locator('#gap-close-metrics [data-exact="'+gap_value['result']['metrics']['equity']+'"]')).to_have_count(1)
        gap_point(page,gap_value)
        assert page.request.get(BASE+API+'/sessions/'+gap_id).json()==before_gap
        assert gap_value['quality']=='ESTIMATED_GAP_CLOSE' and gap_value['execution_authority']=='NONE'
        with page.expect_download() as exported:paper.locator('#gap-close-export').click()
        output=directory/'estimated-gap-close.jsonl';exported.value.save_as(output)
        assert verify_gap_close_export(io.BytesIO(output.read_bytes()))['hash']==gap_value['hash']
        paper.locator('#gap-close-result').screenshot(path=str(directory/'estimated-gap-close.png'))
        page.set_viewport_size({'width':390,'height':844})
        paper.locator('#gap-close-result').screenshot(path=str(directory/'estimated-gap-close-mobile.png'))
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.set_viewport_size({'width':1440,'height':1000})
        assert page.request.get(BASE+API+'/sessions').json()['total']==3
        (directory/'owned-observed.json').write_text(json.dumps({'id':id,'other_id':second_id,'gap_id':gap_id,'gap_close':gap_value,
            'validation_id':validation,'checkpoint':verified['checkpoint'],'comparison':comparison,
            'margin_rules':detail['view']['margin_rules']}))
    else:
        saved=json.loads((directory/'owned-observed.json').read_text());id=saved['id']
        paper.locator(':scope > summary').click()
        expect(paper.locator('[data-observed-session]')).to_have_count(3)
        row=paper.locator('[data-observed-session="'+id+'"]');row.locator('[data-observed-view]').click();chart_points(page,2)
        report=page.request.get(BASE+API+'/sessions/'+id+'/view').json()
        assert report['session']['checkpoint']==saved['checkpoint']
        assert report['view']['margin_rules']==saved['margin_rules']
        check_margin(paper, report)
        assert not report['session']['active_in_this_process'] and report['session']['status']=='STOPPED'
        assert page.request.get(BASE+'/__observed_fixture/state').json()['calls']==[]
        with page.expect_download() as exported:paper.locator('#observed-export').click()
        output=directory/'reopened-observed.jsonl';exported.value.save_as(output)
        assert verify_observed_export(io.BytesIO(output.read_bytes()))['checkpoint']==saved['checkpoint']
        page.goto(BASE+'/ui/#/perpetuos');page.wait_for_load_state('networkidle')
        paper=page.locator('#perp-observed-library');paper.locator(':scope > summary').click()
        expect(paper.locator('[data-observed-session]')).to_have_count(3)
        paper.locator('[data-observed-view="'+id+'"]').click();chart_points(page,2)
        assert page.request.get(BASE+'/__observed_fixture/state').json()['observed_sources']==0
        assert compare_reserve(page,paper,report,saved["validation_id"])==saved["comparison"]
        assert page.request.post(BASE+'/lab/lock').ok
        expect(paper.locator("#observed-comparison-table")).to_be_empty(timeout=9000)
        paper.locator("#observed-compare").click()
        expect(paper.locator("#observed-comparison-status")).to_contain_text("Desbloquea LAB")
        expect(paper.locator("#observed-inspection")).to_be_visible()
        paper.locator('[data-observed-gap-view="'+saved['gap_id']+'"]').click()
        expect(paper.locator('#gap-close-result')).to_be_visible(timeout=15000)
        gap_point(page,saved['gap_close'])
        recovered=page.request.get(BASE+API+'/sessions/'+saved['gap_id']+'/gap-close').json()
        assert recovered['hash']==saved['gap_close']['hash'] and recovered['result']==saved['gap_close']['result']
        assert page.request.get(BASE+'/__observed_fixture/state').json()['observed_sources']==0
        assert page.request.post(BASE+'/paper/perp/lock').ok
        paper.locator('#observed-refresh').click()
        expect(paper.locator('#observed-status')).to_contain_text('bloqueado')
        expect(paper.locator('#observed-inspection')).not_to_be_visible()
        expect(paper.locator('#gap-close-result')).not_to_be_visible()
        assert paper.locator('#gap-close-metrics').inner_text()==''
        expect(paper.locator('#observed-margin-history')).to_be_empty()
    assert not errors and not server_errors and not private_actions,(errors,server_errors,private_actions)
    assert not page.request.get(BASE+'/__observed_fixture/state').json()['real_exists']
    browser.close()
(directory/('result-reopened.json' if reopen else 'result.json')).write_text(json.dumps({'ok':True,'js_errors':errors,
    'server_errors':server_errors,'real_actions':private_actions,'reopened':reopen}))
