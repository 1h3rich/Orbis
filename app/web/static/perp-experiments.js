/* Encrypted research only. Financial strings are retained for tables/export. */
(function () {
  "use strict";
  function esc(v) { return String(v == null ? "" : v).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];}); }
  function seconds(ns) { return Number(BigInt(ns)/1000000000n); }
  function utc(ns) { return new Date(seconds(ns)*1000).toISOString().slice(0,19).replace("T"," "); }
  function plain(value) {
    var m=/^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d{1,3}))?$/.exec(String(value));if(!m)return null;
    var digits=m[2]+(m[3]||''),point=m[2].length+Number(m[4]||0);if(Math.abs(point)>200)return null;
    return (m[1]==='-'?'-':'')+(point<=0?'0.'+'0'.repeat(-point)+digits:point>=digits.length?digits+'0'.repeat(point-digits.length):digits.slice(0,point)+'.'+digits.slice(point));
  }
  function percent(value) {var p=plain(value),m=/^(-?)(\d+)(?:\.(\d+))?$/.exec(p||'');if(!m)return '—';var f=(m[3]||'').padEnd(2,'0');return fmt(m[1]+m[2]+f.slice(0,2)+'.'+(f.slice(2)||'0'))+' %';}
  function percentInput(value) {
    var text=plain(value),parts=(text||'0').split('.'),f=(parts[1]||'').padEnd(2,'0');
    return (parts[0]+f.slice(0,2)).replace(/^0+(?=\d)/,'')+(f.slice(2)?'.'+f.slice(2):'');
  }
  function asFraction(value) {
    var text=plain(String(value).trim().replace(',','.'));
    if(!text||text.charAt(0)==='-')return null;
    var parts=text.split('.'),digits=parts[0].padStart(3,'0');
    return digits.slice(0,-2)+'.'+digits.slice(-2)+(parts[1]||'');
  }
  function fmt(value,places) {
    var m=/^(-?)(\d+)(?:\.(\d+))?$/.exec(plain(value)||'');if(!m)return "—";
    places=places==null?2:places;var fraction=(m[3]||"").padEnd(places+1,"0");
    var units=BigInt(m[2]+fraction.slice(0,places));if(Number(fraction[places])>=5)units+=1n;
    var digits=units.toString().padStart(places+1,"0"),whole=places?digits.slice(0,-places):digits;
    return (m[1]&&units!==0n?"−":"")+whole.replace(/\B(?=(\d{3})+(?!\d))/g,".")+(places?","+digits.slice(-places):"");
  }
  function amount(value,places) {return '<span data-exact="'+esc(value)+'" title="'+esc(value)+'">'+esc(fmt(value,places))+'</span>';}
  function mount(root) {
    var alive=true,generation=0,controller=new AbortController(),busy=false,bundle=null;
    var versions=[],datasets=[],offset=0,total=0,tradePage=0,eventPage=0,priceChart=null,equityChart=null,priceSeries=null,lines=[],observer,chartObserver;
    root.innerHTML='<div class="row"><span class="statusbadge" data-help="Reglas actuales aplicadas al pasado y trayectorias OHLC hipotéticas. La reproducción verifica el modelo; no acredita ejecución histórica del exchange.">Experimental</span></div>'+
      '<form id="perp-run-form" class="perp-run-config">'+
      '<label class="field">Versión<select name="version_id" required></select></label>'+
      '<label class="field">Datos<select name="dataset_id" required></select></label>'+
      '<label class="field">Margen<select name="margin_mode"><option value="ISOLATED">Aislado</option><option value="CROSS">Cross</option></select></label>'+
      '<label class="field">Apalancamiento<input name="leverage" value="5" inputmode="decimal" maxlength="80" required></label>'+
      '<label class="field" data-help="Orden intravela hipotético del precio de ejecución. Cambiarlo puede cambiar el resultado.">TRADE<select name="trade_path"><option value="O_H_L_C">Máximo → mínimo</option><option value="O_L_H_C">Mínimo → máximo</option></select></label>'+
      '<label class="field" data-help="Orden intravela hipotético del precio de margen; separado de TRADE.">MARK<select name="mark_path"><option value="O_H_L_C">Máximo → mínimo</option><option value="O_L_H_C">Mínimo → máximo</option></select></label>'+
      '<label class="field">Entrada<select name="entry_kind"><option value="MARKET">Mercado</option><option value="LIMIT">Límite</option></select></label>'+
      '<label class="field" data-limit-entry hidden data-help="Distancia desde el cierre de la vela de señal. Compra por debajo y venta por encima.">Distancia %<input name="limit_offset" value="2" inputmode="decimal" maxlength="80" disabled></label>'+
      '<label class="field" data-limit-entry hidden>Vigencia · velas<input name="limit_bars" type="number" min="1" max="10000" step="1" value="3" disabled></label>'+
      '<details class="perp-partial-config" id="perp-partial-config"><summary>Salidas parciales · LAB</summary><div id="perp-partial-rows"></div>'+
      '<button type="button" id="perp-partial-add" class="ghost" data-help="Cada parte se calcula sobre la cantidad inicial. El objetivo final de la estrategia cierra el resto.">Añadir objetivo</button></details>'+
      '<div class="row"><button type="submit" disabled data-help="Guarda un perfil de esta versión y una prueba con margen, comisiones, financiación y liquidación. No activa PAPER ni operaciones reales.">Probar</button></div></form>'+
      '<div id="perp-run-config-summary" class="muted"></div><p id="perp-run-status" class="state" role="status"></p>'+
      '<div class="row"><label class="field">Pruebas guardadas<select id="perp-run-selection"><option value="">Selecciona prueba</option></select></label>'+
      '<button type="button" id="perp-run-refresh" class="ghost">Actualizar</button><button type="button" id="perp-run-prev" class="ghost" disabled>Anterior</button><span id="perp-run-page"></span><button type="button" id="perp-run-next" class="ghost" disabled>Siguiente</button></div>'+
      '<section id="perp-run-results" hidden><div class="row"><span id="perp-run-verification" class="statusbadge"></span><span id="perp-run-sample" class="statusbadge perp-sample-warning" data-help="Menos de 30 operaciones: las estadísticas pueden ser muy inestables." hidden>Muestra pequeña</span>'+
      '<button type="button" id="perp-run-replay" class="ghost">Reproducir</button><button type="button" id="perp-run-export" class="ghost">Exportar JSON</button></div>'+
      '<div id="perp-run-metrics" class="perp-run-metrics"></div><div id="perp-run-open" class="state" hidden></div>'+
      '<details id="perp-run-comparison" class="real-panel"><summary data-help="Compara dos pruebas reproducidas. Las diferencias y las curvas requieren condiciones equivalentes.">Comparar pruebas guardadas</summary>'+
      '<div class="row"><label class="field">Otra prueba<select disabled></select></label>'+
      '<button type="button" class="ghost" data-run-compare-more hidden>Más pruebas</button>'+
      '<button type="button" data-run-compare disabled>Comparar pruebas</button><button type="button" class="ghost" data-run-compare-export disabled>Exportar comparación</button></div>'+
      '<p class="muted" role="status"></p><div class="tablewrap"><table aria-label="Comparación de backtests de derivados" hidden>'+
      '<thead><tr><th scope="col">Métrica</th><th scope="col">Base</th><th scope="col">Alternativa</th><th scope="col" data-help="Alternativa menos base. Las diferencias de porcentajes son puntos porcentuales.">Diferencia</th></tr></thead><tbody></tbody></table></div>'+
      '<small class="muted" data-run-compare-legend hidden>Capital en fronteras de vela · Base verde · Alternativa azul</small>'+
      '<div data-run-compare-range></div><div id="perp-run-compare-equity" data-run-compare-chart class="perp-research-chart" role="img" aria-label="Curvas de capital comparadas" hidden></div></details>'+
      '<details id="perp-cost-comparison" class="real-panel"><summary data-help="Repite la prueba con los mismos datos, reglas y financiación. Solo cambia los costes de ejecución.">Sensibilidad de costes</summary>'+
      '<form class="row"><label class="field">Comisión por ejecución %<input name="fee_rate" inputmode="decimal" maxlength="80" required></label>'+
      '<label class="field">Deslizamiento %<input name="slippage_rate" inputmode="decimal" maxlength="80" required></label>'+
      '<button type="submit" disabled>Comparar costes</button><button type="button" class="ghost" data-cost-export disabled>Exportar JSON</button></form>'+
      '<p class="muted" role="status"></p><div class="tablewrap"><table aria-label="Sensibilidad de costes del backtest" hidden>'+
      '<thead><tr><th scope="col">Métrica</th><th scope="col">Base</th><th scope="col">Alternativa</th><th scope="col" data-help="Alternativa menos base. Los porcentajes se comparan en puntos porcentuales.">Diferencia</th></tr></thead><tbody></tbody></table></div></details>'+
      '<details id="perp-margin-comparison" class="real-panel"></details>'+
      '<details id="perp-rule-comparison" class="real-panel"><summary data-help="Aplica otra captura de GRVT al mismo periodo como supuesto. Las capturas no prueban la vigencia histórica de las reglas.">Comparar reglas GRVT</summary>'+
      '<div class="muted" data-rules-window aria-live="polite"></div>'+
      '<div class="row"><label class="field">Captura alternativa<select disabled></select></label>'+
      '<button type="button" class="ghost" data-rules-more hidden>Más capturas</button>'+
      '<button type="button" data-rules-compare disabled>Comparar</button><button type="button" class="ghost" data-rules-export disabled>Exportar comparación</button></div>'+
      '<p class="muted" role="status"></p><div class="tablewrap"><table aria-label="Comparación experimental de reglas GRVT" hidden>'+
      '<thead><tr><th scope="col">Métrica</th><th scope="col">Prueba original</th><th scope="col">Otra captura</th></tr></thead><tbody></tbody></table></div></details>'+
      '<div class="row"><label class="field" data-help="El margen usa MARK; los fills usan TRADE. Los marcadores se sitúan en su vela; el diario conserva el instante exacto.">Velas<select id="perp-run-price-kind"><option>TRADE</option><option>MARK</option></select></label></div>'+
      '<div id="perp-run-chart-range"></div><div id="perp-run-chart" class="perp-research-chart" aria-label="Operaciones de la prueba perpetua"></div>'+
      '<div id="perp-run-equity-range"></div><div id="perp-run-equity" class="perp-research-equity" aria-label="Capital de la prueba perpetua" data-help="Curva agrupada a un segundo; las métricas conservan todos los eventos del modelo."></div>'+
      '<h4>Operaciones</h4><div class="tablewrap" id="perp-run-trades"></div><div class="row"><button type="button" id="perp-run-trades-prev" class="ghost">Anterior</button><span id="perp-run-trades-page"></span><button type="button" id="perp-run-trades-next" class="ghost">Siguiente</button></div>'+
      '<details id="perp-run-trade-detail" hidden><summary>Detalle de operación</summary><ul id="perp-run-trade-partials" hidden></ul><pre></pre></details>'+
      '<details><summary>Diario</summary><div class="tablewrap" id="perp-run-events"></div><div class="row"><button type="button" id="perp-run-events-prev" class="ghost">Anterior</button><span id="perp-run-events-page"></span><button type="button" id="perp-run-events-next" class="ghost">Siguiente</button></div><pre id="perp-run-event-detail" hidden></pre></details>'+
      '<details><summary>Supuestos</summary><ul id="perp-run-assumptions"></ul></details></section>';
    function el(id) {return root.querySelector('#'+id);}
    var form=el('perp-run-form'),status=el('perp-run-status'),selection=el('perp-run-selection');
    var marginComparison=window.OrbisPerpMarginComparison.mount(el('perp-margin-comparison'),api,utc,fmt);
    var costComparison=window.OrbisPerpCostComparison.mount(el('perp-cost-comparison'),api,percentInput,asFraction,fmt);
    var ruleComparison=window.OrbisPerpRuleComparison.mount(el('perp-rule-comparison'),api,utc,fmt);
    var runComparison=window.OrbisPerpRunComparison.mount(el('perp-run-comparison'),api,utc,fmt,function(id) {
      var version=versions.find(function(item){return item.id===id;});
      return version ? version.name+' · v'+version.version : id.slice(0,8);
    });
    function current(ticket) {return alive&&root.isConnected&&ticket===generation;}
    function begin() {controller.abort();controller=new AbortController();return ++generation;}
    function destroyCharts() {lines=[];priceSeries=null;if(priceChart)priceChart.remove();if(equityChart)equityChart.remove();priceChart=equityChart=null;}
    function resizeCharts() {
      [['perp-run-chart',priceChart],['perp-run-equity',equityChart]].forEach(function(row){
        var node=el(row[0]);if(row[1]&&node.clientWidth>0&&node.clientHeight>0)row[1].resize(node.clientWidth,node.clientHeight);
      });
    }
    function clear() {bundle=null;ruleComparison.setRun(null);runComparison.setRun(null);costComparison.setRun(null);marginComparison.setRun(null);destroyCharts();el('perp-run-results').hidden=true;el('perp-run-trade-detail').hidden=true;el('perp-run-event-detail').hidden=true;}
    function ready() {
      Array.from(form.elements).forEach(function(input){input.disabled=busy;});
      var limited=form.elements.entry_kind.value==='LIMIT';
      form.querySelectorAll('[data-limit-entry]').forEach(function(label){label.hidden=!limited;label.querySelector('input').disabled=busy||!limited;label.querySelector('input').required=limited;});
      form.querySelector('button[type="submit"]').disabled=busy||!form.elements.version_id.value||!form.elements.dataset_id.value;
      el('perp-partial-add').disabled=busy||el('perp-partial-rows').children.length>=5;
    }
    function addPartial(target,fraction) {
      var row=document.createElement('div');row.className='row perp-partial-row';
      row.innerHTML='<label class="field">Objetivo %<input data-partial-target inputmode="decimal" maxlength="80" required></label>'+
        '<label class="field">Parte %<input data-partial-fraction inputmode="decimal" maxlength="80" required></label>'+
        '<button type="button" class="ghost">Quitar</button>';
      row.querySelector('[data-partial-target]').value=target;
      row.querySelector('[data-partial-fraction]').value=fraction;
      row.querySelector('button').addEventListener('click',function(){row.remove();changedConfig();});
      el('perp-partial-rows').append(row);
    }
    function changedConfig() {begin();clear();busy=false;summary();ready();status.textContent='Configuración preparada.';}
    async function api(path,options) {
      var response=await fetch('/lab'+path,Object.assign({cache:'no-store',signal:controller.signal,headers:{'Content-Type':'application/json'}},options||{}));
      var body=await response.json();if(!response.ok){var error=new Error(response.status===423?'LAB bloqueado · desbloquea en Ajustes.':typeof body.detail==='string'?body.detail:'No se pudo verificar la prueba.');error.status=response.status;throw error;}return body;
    }
    function summary() {
      var version=versions.find(function(v){return v.id===form.elements.version_id.value;});
      if(!version){el('perp-run-config-summary').textContent='';return;}
      var d=version.definition;el('perp-run-config-summary').innerHTML=esc(d.direction)+' · Capital '+amount(d.initial_capital)+' USDT · Asignación '+esc(percent(d.allocation))+' · Comisión '+esc(percent(d.fee_rate))+' · SL '+esc(percent(d.stop_loss))+' · TP '+esc(percent(d.take_profit));
    }
    async function library(ticket,wanted) {
      var page=await api('/perp/runs?offset='+offset+'&limit=25');if(!current(ticket))return;
      total=page.total;selection.innerHTML='<option value="">Selecciona prueba</option>'+page.items.map(function(row){return '<option value="'+esc(row.id)+'">'+esc(row.symbol+' · '+row.timeframe+' · '+(row.profile.entry_order?'Límite':'Mercado')+' · '+row.created_at.slice(0,19).replace('T',' '))+'</option>';}).join('');
      selection.value=wanted||'';el('perp-run-prev').disabled=offset===0;el('perp-run-next').disabled=offset+25>=total;
      el('perp-run-page').textContent=total?(offset+1)+'–'+Math.min(offset+25,total)+' / '+total:'0';
    }
    async function load() {
      var ticket=begin();clear();busy=true;status.textContent='Cargando…';ready();
      try {
        var data=await Promise.all([api('/strategy-versions'),api('/perp/datasets')]);if(!current(ticket))return;
        var oldVersion=form.elements.version_id.value,oldData=form.elements.dataset_id.value;versions=data[0];datasets=data[1];
        form.elements.version_id.innerHTML='<option value="">Selecciona versión</option>'+versions.map(function(v){return '<option value="'+esc(v.id)+'">'+esc(v.name+' · v'+v.version+' · '+v.definition.direction)+'</option>';}).join('');
        form.elements.dataset_id.innerHTML='<option value="">Selecciona datos</option>'+datasets.map(function(d){return '<option value="'+esc(d.id)+'">'+esc(d.symbol+' · '+d.timeframe+' · '+new Date(d.start*1000).toISOString().slice(0,10))+'</option>';}).join('');
        form.elements.version_id.value=oldVersion;form.elements.dataset_id.value=oldData;summary();ready();await library(ticket);
        if(current(ticket))status.textContent=versions.length&&datasets.length?'Selecciona versión y datos.':'Crea una estrategia y descarga Datos de derivados.';
      } catch(error) {if(current(ticket)){versions=[];datasets=[];form.elements.version_id.innerHTML='';form.elements.dataset_id.innerHTML='';status.textContent=error.message;}}
      finally {if(current(ticket)){busy=false;ready();}}
    }
    function draw() {
      if(!bundle)return;destroyCharts();
      var lw=window.LightweightCharts;if(!lw)throw new Error('Gráficos no disponibles.');
      var data=bundle.dataset,run=bundle.execution.result,kind=el('perp-run-price-kind').value;
      var bars=kind==='MARK'?data.mark.bars:data.trade.bars;
      var options={layout:{background:{type:'solid',color:'#101b18'},textColor:'#c9d6ce'},grid:{vertLines:{color:'#24362f'},horzLines:{color:'#24362f'}},timeScale:{timeVisible:true,minBarSpacing:0.1}};
      var chartNode=el('perp-run-chart');
      priceChart=lw.createChart(chartNode,Object.assign({},options,{width:chartNode.clientWidth,height:chartNode.clientHeight}));
      priceSeries=priceChart.addSeries(lw.CandlestickSeries,{upColor:'#42c88a',downColor:'#ff7b78',borderVisible:false,wickUpColor:'#42c88a',wickDownColor:'#ff7b78'});
      priceSeries.setData(bars.map(function(b){return {time:b.t,open:Number(b.o),high:Number(b.h),low:Number(b.l),close:Number(b.c)};}));
      var step={"1m":60,"5m":300,"15m":900,"1h":3600,"4h":14400,"1d":86400}[data.timeframe],long=bundle.strategy.definition.direction==='LONG';
      var entries=0,exits=0,liquidations=0,partials=0;
      var partialByIndex=new Map((run.partial_events||[]).filter(function(e){return e.status==='FILLED';}).map(function(e){return [e.journal_index,e];}));
      var markers=run.journal.filter(function(e){return e.kind==='FILL'||e.kind==='ISOLATED_LIQUIDATION'||e.kind==='CROSS_POSITION_LIQUIDATED';}).map(function(e){
        var liq=e.kind!=='FILL',entry=!liq&&((!e.size_delta.startsWith('-'))===long),partial=partialByIndex.get(e.index);
        if(liq)liquidations+=1;else if(entry)entries+=1;else if(partial)partials+=1;else exits+=1;
        var time=Math.max(bars[0].t,Math.min(bars[bars.length-1].t,Math.floor(seconds(e.cursor.time_ns)/step)*step));
        return {time:time,position:entry?(long?'belowBar':'aboveBar'):'aboveBar',shape:liq?'square':entry?(long?'arrowUp':'arrowDown'):'circle',color:liq?'#e9b764':entry?'#42c88a':partial?'#8eb8ff':'#ff7b78',text:liq?'LIQ':entry?(long?'L':'S'):partial?'P'+partial.target_index:'×',id:String(e.index)};
      }).sort(function(a,b){return a.time-b.time||Number(a.id)-Number(b.id);});
      lw.createSeriesMarkers(priceSeries,markers);
      var target=el('perp-run-chart');target.dataset.entryCount=String(entries);target.dataset.exitCount=String(exits);target.dataset.liquidationCount=String(liquidations);target.dataset.runId=bundle.run.id;target.dataset.priceKind=kind;
      target.dataset.partialCount=String(partials);
      if(window.OrbisChartRange)window.OrbisChartRange.mount(el('perp-run-chart-range'),priceChart,bars,{selected:'all',storageKey:'orbis.chartRange.perp-research'});else priceChart.timeScale().fitContent();
      var equityNode=el('perp-run-equity');equityChart=lw.createChart(equityNode,Object.assign({},options,{width:equityNode.clientWidth,height:equityNode.clientHeight}));var line=equityChart.addSeries(lw.LineSeries,{color:'#42c88a',lineWidth:2});
      var points=new Map();run.equity.forEach(function(p){points.set(seconds(p.time_ns),{time:seconds(p.time_ns),value:Number(p.equity)});});
      var curve=Array.from(points.values()).sort(function(a,b){return a.time-b.time;});line.setData(curve);
      if(window.OrbisChartRange&&curve.length>1)window.OrbisChartRange.mount(el('perp-run-equity-range'),equityChart,curve,{selected:'all',storageKey:'orbis.chartRange.perp-equity'});else equityChart.timeScale().fitContent();
    }
    var reasons={TAKE_PROFIT:'Objetivo',STOP_OR_TRAILING:'Stop',LIQUIDATION:'Liquidación',TIME_LIMIT:'Tiempo',EXIT_RULE_NEXT_OPEN:'Regla',END_OF_WINDOW_ARTIFICIAL_CLOSE:'Fin de tramo'};
    function tradeTable() {
      if(!bundle)return;var trades=bundle.execution.result.trades,start=tradePage*25;
      el('perp-run-trades').innerHTML='<table><thead><tr><th>Entrada UTC</th><th>Lado</th><th>Precio</th><th>Salida</th><th>Motivo</th><th>Neto USDT</th><th></th></tr></thead><tbody>'+trades.slice(start,start+25).map(function(t,i){return '<tr><td>'+esc(utc(t.entry_time_ns))+'</td><td>'+esc(t.direction)+'</td><td>'+amount(t.entry_price)+'</td><td>'+amount(t.exit_price)+'</td><td>'+esc(reasons[t.exit_reason]||t.exit_reason)+'</td><td>'+amount(t.net_profit)+'</td><td><button type="button" class="ghost" data-trade="'+(start+i)+'">Ver</button></td></tr>';}).join('')+'</tbody></table>';
      el('perp-run-trades-page').textContent=trades.length?(start+1)+'–'+Math.min(start+25,trades.length)+' / '+trades.length:'Sin operaciones';
      el('perp-run-trades-prev').disabled=tradePage===0;el('perp-run-trades-next').disabled=start+25>=trades.length;
    }
    function eventTable() {
      if(!bundle)return;var events=bundle.execution.result.journal,start=eventPage*50;
      var names={MARK:'Marca',FILL:'Ejecución',FUNDING:'Financiación',FUNDING_NO_POSITION:'Sin posición',ORDER_ACCEPTED:'Orden',ORDER_CANCELLED:'Cancelada',ORDER_REJECTED:'Rechazada',ISOLATED_LIQUIDATION:'Liquidación aislada',CROSS_POSITION_LIQUIDATED:'Posición liquidada',CROSS_ACCOUNT_LIQUIDATION:'Cuenta liquidada'};
      el('perp-run-events').innerHTML='<table><thead><tr><th>UTC</th><th>Evento</th><th>Cantidad</th><th>Precio</th><th>Comisión</th><th>Funding</th><th></th></tr></thead><tbody>'+events.slice(start,start+50).map(function(e){return '<tr><td>'+esc(utc(e.cursor.time_ns))+'</td><td>'+esc(names[e.kind]||e.kind)+'</td><td>'+amount(e.size_delta,6)+'</td><td>'+amount(e.price)+'</td><td>'+amount(e.fee,6)+'</td><td>'+amount(e.funding_payment,6)+'</td><td><button type="button" class="ghost" data-event="'+e.index+'">Detalle</button></td></tr>';}).join('')+'</tbody></table>';
      el('perp-run-events-page').textContent=events.length?(start+1)+'–'+Math.min(start+50,events.length)+' / '+events.length:'0';
      el('perp-run-events-prev').disabled=eventPage===0;el('perp-run-events-next').disabled=start+50>=events.length;
    }
    function show(value) {
      if(['orbis.lab.perp.bundle.v1','orbis.lab.perp.bundle.v2','orbis.lab.perp.bundle.v3'].indexOf(value.schema)<0||value.run.verification!=='REPLAY_VERIFIED')throw new Error('La prueba no está reproducida.');
      bundle=value;tradePage=eventPage=0;el('perp-run-results').hidden=false;
      ruleComparison.setRun(value.run);
      runComparison.setRun(value.run);
      costComparison.setRun(value);
      marginComparison.setRun(value.run);
      var result=value.execution.result,m=result.metrics,p=value.run.profile;
      if(!Array.from(selection.options).some(function(o){return o.value===value.run.id;}))selection.add(new Option(value.run.symbol+' · '+value.run.created_at.slice(0,19),value.run.id));selection.value=value.run.id;
      if(!versions.some(function(v){return v.id===value.strategy.version_id;})){versions.push({id:value.strategy.version_id,name:'Archivada',version:'',definition:value.strategy.definition});form.elements.version_id.add(new Option('Versión archivada · '+value.strategy.definition.direction,value.strategy.version_id));}
      if(!Array.from(form.elements.dataset_id.options).some(function(o){return o.value===value.run.dataset_id;}))form.elements.dataset_id.add(new Option(value.dataset.symbol+' · '+value.dataset.timeframe,value.run.dataset_id));
      form.elements.version_id.value=value.strategy.version_id;form.elements.dataset_id.value=value.run.dataset_id;['margin_mode','leverage','trade_path','mark_path'].forEach(function(k){form.elements[k].value=p[k];});
      form.elements.entry_kind.value=p.entry_order?'LIMIT':'MARKET';
      if(p.entry_order){
        var limitParts=plain(p.entry_order.offset).split('.'),limitFraction=(limitParts[1]||'').padEnd(2,'0');
        form.elements.limit_offset.value=(limitParts[0]+limitFraction.slice(0,2)).replace(/^0+(?=\d)/,'')+'.'+limitFraction.slice(2);
        form.elements.limit_bars.value=String(p.entry_order.valid_bars);
      }
      el('perp-partial-rows').replaceChildren();
      (p.partial_targets||[]).forEach(function(row){addPartial(percentInput(row.target),percentInput(row.fraction));});
      el('perp-partial-config').open=!!p.partial_targets;
      summary();ready();
      el('perp-run-export').disabled=false;
      el('perp-run-verification').textContent='Reproducible · experimental';
      el('perp-run-sample').hidden=result.warnings.indexOf('SMALL_SAMPLE')<0;
      el('perp-run-metrics').innerHTML=[['Capital final',m.final_equity],['Variación neta',m.net_equity_change],['Comisiones',m.fees_paid],['Funding',m.funding_paid],['Drawdown %',m.max_drawdown_pct],['Acierto %',m.win_rate_pct],['Expectativa',m.expectancy],['Profit factor',m.profit_factor],['Exposición %',m.exposure_pct],['Duración media · s',m.average_duration_seconds]].map(function(row){return '<div><span class="muted">'+esc(row[0])+'</span><strong>'+amount(row[1])+'</strong></div>';}).join('');
      el('perp-run-open').hidden=!m.open_position;el('perp-run-open').textContent=m.open_position?'Salida no ejecutable · posición abierta. Resultado parcial.':'';
      status.textContent=(result.status==='COMPLETE'?'Prueba completa':'Prueba interrumpida')+' · '+result.trades.length+(result.trades.length===1?' operación · ':' operaciones · ')+(p.margin_mode==='ISOLATED'?'Aislado':'Cross')+' · '+p.leverage+'×';
      if(p.entry_order)status.textContent+=' · LIMIT · LAB';
      if(p.partial_targets)status.textContent+=' · '+m.partial_exits+' parciales · LAB';
      if(result.window.entry_start_index>0||result.window.end_index<value.dataset.trade.bars.length)status.textContent+=' · Velas '+(result.window.entry_start_index+1)+'–'+result.window.end_index;
      var rulesAt=utc(value.dataset.rules.fetched_at_ns),specAt=utc(value.dataset.specification.fetched_at_ns);
      var rulesDates=rulesAt===specAt?'Captura GRVT · '+rulesAt+' UTC':'Margen · '+rulesAt+' UTC. Instrumento · '+specAt+' UTC.';
      el('perp-run-assumptions').innerHTML=[rulesDates+' · Aplicadas al periodo como supuesto.',
        'TRADE: '+(p.trade_path==='O_H_L_C'?'apertura → máximo → mínimo → cierre':'apertura → mínimo → máximo → cierre')+'. MARK: '+(p.mark_path==='O_H_L_C'?'apertura → máximo → mínimo → cierre':'apertura → mínimo → máximo → cierre')+'. Trayectorias hipotéticas.',
        'Financiación antes de operar en cada frontera; liquidación MARK antes de una salida simultánea.',
        'Margen, pérdida inmediata y comisiones incluidos en la asignación de capital.',
        'Una posición. Entradas detenidas tras liquidación. Cierre final artificial.',
        'Sin liquidez histórica ni libro de órdenes. Importes exactos en detalle y JSON.'].map(function(text){return '<li>'+esc(text)+'</li>';}).join('');
      if(p.entry_order)el('perp-run-assumptions').insertAdjacentHTML('beforeend','<li>LIMIT al tocar TRADE, cantidad completa hipotética y deslizamiento acotado al límite. Vigencia '+esc(p.entry_order.valid_bars)+' velas.</li>');
      if(p.partial_targets)el('perp-run-assumptions').insertAdjacentHTML('beforeend','<li>Partes sobre la posición inicial. Stop y objetivo final cierran el resto. Disponible en LAB.</li>');
      draw();tradeTable();eventTable();
    }
    async function select(id,externalCurrent) {
      var ticket=begin();clear();busy=false;ready();if(!id){status.textContent='Selecciona prueba.';return;}
      status.textContent='Reproduciendo originales…';
      try {var value=await api('/perp/runs/'+encodeURIComponent(id));if(current(ticket)&&selection.value===id&&(!externalCurrent||externalCurrent())){show(value);return true;}}
      catch(error){if(current(ticket)){clear();status.textContent=error.message;}}
      return false;
    }
    form.addEventListener('change',changedConfig);
    el('perp-partial-add').addEventListener('click',function(){if(busy||el('perp-partial-rows').children.length>=5)return;addPartial('','');changedConfig();});
    form.addEventListener('submit',async function(event){
      event.preventDefault();if(busy)return;var ticket=begin();clear();busy=true;
      var fields=new FormData(form),body={version_id:fields.get('version_id'),margin_mode:fields.get('margin_mode'),leverage:fields.get('leverage'),trade_path:fields.get('trade_path'),mark_path:fields.get('mark_path')},datasetId=fields.get('dataset_id');
      if(fields.get('entry_kind')==='LIMIT'){
        var limitDistance=asFraction(fields.get('limit_offset'));
        if(!limitDistance){status.textContent='Revisa la distancia del límite.';busy=false;ready();return;}
        body.entry_order={reference:'CLOSED_SIGNAL_TRADE_CLOSE',offset:limitDistance,valid_bars:Number(fields.get('limit_bars'))};
      }
      var partialTargets=Array.from(el('perp-partial-rows').children,function(row){
        return {target:asFraction(row.querySelector('[data-partial-target]').value),
                fraction:asFraction(row.querySelector('[data-partial-fraction]').value)};
      });
      if(partialTargets.some(function(row){return !row.target||!row.fraction;})){
        status.textContent='Revisa los objetivos y las partes.';busy=false;ready();return;
      }
      if(partialTargets.length)body.partial_targets=partialTargets;
      ready();status.textContent='Probando y guardando…';
      try {
        var savedProfile=await api('/perp/profiles',{method:'POST',body:JSON.stringify(body)});if(!current(ticket))return;
        var run=await api('/perp/runs',{method:'POST',body:JSON.stringify({dataset_id:datasetId,profile_id:savedProfile.id})});if(!current(ticket))return;
        offset=0;await library(ticket,run.id);if(!current(ticket))return;
        var value=await api('/perp/runs/'+encodeURIComponent(run.id));if(current(ticket))show(value);
      } catch(error){if(current(ticket)){clear();status.textContent=error.message;}}
      finally {if(current(ticket)){busy=false;ready();}}
    });
    selection.addEventListener('change',function(){select(selection.value);});
    el('perp-run-refresh').addEventListener('click',load);
    el('perp-run-prev').addEventListener('click',async function(){var ticket=begin();clear();busy=false;ready();offset=Math.max(0,offset-25);try{await library(ticket);}catch(e){if(current(ticket))status.textContent=e.message;}});
    el('perp-run-next').addEventListener('click',async function(){var ticket=begin();clear();busy=false;ready();offset+=25;try{await library(ticket);}catch(e){if(current(ticket))status.textContent=e.message;}});
    el('perp-run-replay').addEventListener('click',function(){if(bundle)select(bundle.run.id);});
    el('perp-run-export').addEventListener('click',async function(){
      if(!bundle)return;var id=bundle.run.id,ticket=++generation,button=el('perp-run-export');button.disabled=true;
      try {var value=await api('/perp/runs/'+encodeURIComponent(id)+'/export');if(!current(ticket)||!bundle||bundle.run.id!==id)return;
        var url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'})),link=document.createElement('a');link.href=url;link.download='orbis-perp-run-'+id+'.json';link.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);
      } catch(error){if(current(ticket)){clear();status.textContent=error.message;}}
      finally {if(current(ticket))button.disabled=false;}
    });
    el('perp-run-price-kind').addEventListener('change',function(){
      if(!bundle||!priceSeries)return;var kind=el('perp-run-price-kind').value;
      var bars=kind==='MARK'?bundle.dataset.mark.bars:bundle.dataset.trade.bars;
      // Both series have the identical verified timeline. Reuse this chart:
      // reading/reapplying a range before the previous frame paints can turn
      // a pending fit/zoom into the library's default200-bar viewport.
      priceSeries.setData(bars.map(function(b){return {time:b.t,open:Number(b.o),high:Number(b.h),low:Number(b.l),close:Number(b.c)};}));
      el('perp-run-chart').dataset.priceKind=kind;
    });
    [['trades',function(){return tradePage;},function(v){tradePage=v;tradeTable();}],['events',function(){return eventPage;},function(v){eventPage=v;eventTable();}]].forEach(function(row){
      el('perp-run-'+row[0]+'-prev').addEventListener('click',function(){row[2](Math.max(0,row[1]()-1));});el('perp-run-'+row[0]+'-next').addEventListener('click',function(){row[2](row[1]()+1);});
    });
    root.addEventListener('click',function(event){
      var tradeButton=event.target.closest('[data-trade]'),eventButton=event.target.closest('[data-event]');if(!bundle)return;
      if(eventButton){
        var item=bundle.execution.result.journal[Number(eventButton.dataset.event)];
        var order=bundle.execution.result.account.orders.find(function(row){return row.id===item.order_id;});
        el('perp-run-event-detail').hidden=false;
        el('perp-run-event-detail').textContent=JSON.stringify(order?{event:item,order:order}:item,null,2);
      }
      if(!tradeButton)return;var t=bundle.execution.result.trades[Number(tradeButton.dataset.trade)],detail=el('perp-run-trade-detail');detail.hidden=false;detail.open=true;
      var partialList=el('perp-run-trade-partials'),partials=t.partial_exits||[];
      partialList.hidden=!partials.length;partialList.innerHTML=partials.map(function(row){
        return '<li>'+esc('P'+row.target_index+' · '+utc(row.time_ns)+' UTC · '+(row.status==='FILLED'?'Ejecutada · '+row.quantity+' · '+row.price+' USDT · Resto '+row.remaining_after:row.status==='SKIPPED_MINIMUM'?'Por debajo del mínimo':'Salida interrumpida'))+'</li>';
      }).join('');
      detail.querySelector('pre').textContent=JSON.stringify({trade:t,signal:bundle.execution.result.signals.find(function(s){return s.source_candle_t===t.source_candle_t;})},null,2);
      if(!priceSeries||!priceChart)return;lines.forEach(function(line){priceSeries.removePriceLine(line);});lines=[];
      [[t.entry_price,'Entrada','#42c88a'],[t.exit_price,'Salida','#ff7b78']].forEach(function(row){if(row[0]!=null)lines.push(priceSeries.createPriceLine({price:Number(row[0]),color:row[2],title:row[1],lineWidth:1,lineStyle:2,axisLabelVisible:true}));});
      var bars=bundle.dataset.trade.bars,from=bars.findIndex(function(b){return b.t>=seconds(t.entry_time_ns);}),to=bars.findIndex(function(b){return b.t>=seconds(t.exit_time_ns);});
      if(from<0)from=bars.length-1;if(to<0)to=bars.length-1;priceChart.timeScale().setVisibleLogicalRange({from:Math.max(-1,from-3),to:Math.min(bars.length,to+3)});
    });
    var widget={load:load,openRun:async function(id,externalCurrent){
      await load();if(!alive||(externalCurrent&&!externalCurrent()))return false;
      if(!Array.from(selection.options).some(function(o){return o.value===id;}))selection.add(new Option('Tramo archivado',id));
      selection.value=id;return select(id,externalCurrent);
    },dispose:function(){if(!alive)return;alive=false;generation+=1;controller.abort();ruleComparison.setRun(null);runComparison.setRun(null);costComparison.setRun(null);marginComparison.setRun(null);if(observer)observer.disconnect();if(chartObserver)chartObserver.disconnect();window.removeEventListener('resize',resizeCharts);destroyCharts();}};
    if(window.ResizeObserver){chartObserver=new ResizeObserver(resizeCharts);chartObserver.observe(el('perp-run-chart'));chartObserver.observe(el('perp-run-equity'));}
    window.addEventListener('resize',resizeCharts);
    observer=new MutationObserver(function(){if(!root.isConnected)widget.dispose();});observer.observe(document.body,{childList:true,subtree:true});
    return widget;
  }
  window.OrbisPerpExperiments={mount:mount};
})();
