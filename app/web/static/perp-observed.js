/* Explicit, fictitious observed sessions. Browser numbers only draw prices. */
(function () {
  'use strict';
  function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function amount(v){return '<span data-exact="'+esc(v)+'" title="'+esc(v)+'">'+esc(v==null?'—':window.OrbisUI.fmtDecimal(String(v),2,2))+'</span>';}
  function second(ns){return Number(BigInt(ns)/1000000000n);}
  function utc(ns){return new Date(second(ns)*1000).toISOString().slice(0,19).replace('T',' ');}
  var labels={RUNNING:'Observando',CLOSE_ONLY:'Sólo cierre',STOPPED:'Finalizado',GAP:'Interrumpido',HALTED:'Revisar',LIQUIDATED:'Liquidado',RESTARTED:'Reinicio',PAUSED:'Pausado'};
  var pendingKey='orbis.paper.perp.observed.pending';
  function mount(root){
    var alive=true,busy=false,generation=0,controller=new AbortController(),preparation=null,pending=null,cancelRequested=false;
    var sessionId=null,bundle=null,offset=0,total=0,tradeOffset=0,rows=[],bars=[],markers=[],chart=null,resizeObserver,observer,poller,gapWidget,estimatedClosure=null;
    var comparisonGeneration=0,comparing=false,labUnlocked=null;
    root.innerHTML='<div class="row"><span class="statusbadge" data-help="Cuenta ficticia nueva. Ejecución aproximada con bid/ask, MARK, financiación y margen; no coloca órdenes en GRVT.">PAPER · experimental</span><button type="button" class="ghost" id="observed-refresh">Actualizar</button></div>'+
      '<p id="observed-status" class="state" role="status"></p><section id="observed-review" hidden><div id="observed-review-values" class="perp-run-metrics"></div><div class="row"><button type="button" id="observed-activate">Activar ficticio</button><button type="button" class="ghost" id="observed-discard">Descartar</button></div></section>'+
      '<div id="observed-request" class="row" hidden><button type="button" class="ghost" id="observed-check">Consultar activación</button><button type="button" class="ghost" id="observed-cancel">Cancelar activación</button><span id="observed-request-state" role="status"></span></div>'+
      '<div id="observed-sessions" class="tablewrap"></div><div class="row"><button type="button" class="ghost" id="observed-prev">Anterior</button><span id="observed-page"></span><button type="button" class="ghost" id="observed-next">Siguiente</button></div>'+
      '<section id="observed-inspection" hidden><div class="row"><span id="observed-proof" class="statusbadge"></span><button type="button" class="ghost" id="observed-replay">Reproducir</button><button type="button" class="ghost" id="observed-export">Exportar JSONL</button><button type="button" class="ghost" id="observed-compare" data-help="Reproduce esta sesión y sus reservas LAB originales. Los periodos y el modelo de ejecución son distintos.">Comparar reserva LAB</button><button type="button" class="ghost" id="observed-real" hidden data-help="Abre la revisión de esta sesión en GRVT MAINNET. Selecciona allí la subcuenta y autoriza los límites reales.">Preparar GRVT REAL</button></div><div id="observed-metrics" class="perp-run-metrics"></div>'+
      '<section id="observed-comparison" hidden><p id="observed-comparison-status" class="state" role="status"></p><div id="observed-comparison-table" class="tablewrap"></div></section>'+
      '<div class="row"><button type="button" class="ghost" id="observed-older">Cargar anteriores</button><span id="observed-chart-coverage" class="muted"></span></div><div id="observed-range"></div><div id="observed-chart" class="perp-research-chart" aria-label="Entradas y salidas PAPER observadas"></div>'+
      '<details id="observed-margin"><summary data-help="Reglas aplicadas en PAPER según las cotizaciones archivadas. Cada fecha de captura es evidencia puntual de GRVT.">Margen GRVT</summary><div id="observed-margin-history"></div></details>'+
      '<h4>Operaciones</h4><div class="tablewrap" id="observed-trades"></div><div class="row"><button type="button" class="ghost" id="observed-trades-prev">Anterior</button><span id="observed-trades-page"></span><button type="button" class="ghost" id="observed-trades-next">Siguiente</button></div>'+
      '<details><summary>Diario · últimos 30 eventos</summary><pre id="observed-journal"></pre></details><details><summary>Procedencia</summary><pre id="observed-origin"></pre></details></section><section id="observed-gap-close" hidden></section>';
    function el(id){return root.querySelector('#'+id);}
    function current(ticket){return alive&&root.isConnected&&(ticket==null||ticket===generation);}
    function remember(){try{if(pending)sessionStorage.setItem(pendingKey,JSON.stringify(pending));else sessionStorage.removeItem(pendingKey);}catch(e){}el('observed-request').hidden=!pending;}
    try{var saved=JSON.parse(sessionStorage.getItem(pendingKey));if(saved&&/^[0-9a-f-]{36}$/.test(saved.request_id))pending=saved;}catch(e){}
    function ready(){el('observed-activate').disabled=busy||!preparation||!!pending;el('observed-discard').disabled=busy;el('observed-refresh').disabled=busy;el('observed-prev').disabled=busy||offset===0;el('observed-next').disabled=busy||offset+25>=total;el('observed-compare').disabled=busy||comparing||!bundle;root.querySelectorAll('[data-observed-view],[data-observed-stop],[data-observed-close],[data-observed-gap],[data-observed-gap-view]').forEach(function(b){b.disabled=busy||b.dataset.unavailable==='1';});}
    function clearComparison(){
      comparisonGeneration++;comparing=false;
      el('observed-comparison').hidden=true;
      el('observed-comparison-status').textContent='';
      el('observed-comparison-table').replaceChildren();
    }
    function clearInspection(){clearComparison();estimatedClosure=null;bundle=null;sessionId=null;bars=[];markers=[];if(chart)chart.remove();chart=null;el('observed-inspection').hidden=true;el('observed-journal').textContent='';el('observed-origin').textContent='';el('observed-metrics').innerHTML='';el('observed-margin').open=false;el('observed-margin-history').replaceChildren();}
    function fail(error){if(!alive)return;if(error.status===423){preparation=null;el('observed-review').hidden=true;el('observed-review-values').innerHTML='';rows=[];el('observed-sessions').innerHTML='';clearInspection();if(gapWidget)gapWidget.clear();}el('observed-status').textContent=error.message;}
    async function api(path,options){var r=await fetch('/paper/perp/observed'+path,Object.assign({cache:'no-store',signal:controller.signal,headers:{'Content-Type':'application/json'}},options||{}));var value=await r.json();if(!r.ok){var e=new Error(typeof value.detail==='string'?value.detail:'No se pudo verificar PAPER.');e.status=r.status;throw e;}return value;}
    function state(row){var w=row.worker;if(w&&w.running&&['RUNNING','CLOSE_ONLY'].indexOf(row.stored_status)>=0)return w.coverage_current?labels[row.stored_status]:'Conectando / sin cobertura';return row.gap_close?'Cierre guardado':labels[row.status]||row.status;}
    function restingOrder(checkpoint){
      if(!checkpoint.resting)return null;
      return checkpoint.account.orders.find(function(order){return order.id===checkpoint.resting.order_id;})||null;
    }
    function renderLibrary(){
      el('observed-sessions').innerHTML=rows.length?'<table><thead><tr><th>Mercado</th><th>Creación UTC</th><th>Estado</th><th>Caja</th><th>Posición u orden</th><th></th></tr></thead><tbody>'+rows.map(function(row){var cp=row.checkpoint,w=row.worker,can=!!(w&&w.running&&row.active_in_this_process&&['RUNNING','CLOSE_ONLY'].indexOf(row.stored_status)>=0),position=cp.opened,order=restingOrder(cp);
        var instrument=cp.account.markets[0].instrument.replace('_USDT_Perp','/USDT:PERP');
        var isPartial=cp.engine==='perp-observed-3.0',isLimit=cp.engine==='perp-observed-2.0'||cp.entry_kind==='LIMIT';
        var size=position&&cp.account.positions[0].size;
        var remaining=size&&(isPartial?size.replace(/^-/,''):position.quantity);
        return '<tr data-observed-session="'+esc(row.id)+'"><td>'+esc(instrument+' · '+cp.timeframe+(isLimit?' · Límite':'')+(isPartial?' · Parciales':''))+'</td><td>'+esc(row.created_at.slice(0,19).replace('T',' '))+'</td><td title="'+esc(w&&w.last_error||cp.reason||'')+'">'+esc(state(row))+'</td><td>'+amount(cp.account.cash)+'</td><td>'+esc(position?remaining+' · '+(size.startsWith('-')?'SHORT':'LONG'):order?(can?'Límite pendiente':'Último límite')+' · '+window.OrbisUI.fmtDecimal(order.price_bound,2,2):'—')+'</td><td><div class="row"><button type="button" class="ghost" data-observed-view="'+esc(row.id)+'">Ver</button><button type="button" class="ghost" data-observed-stop="'+esc(row.id)+'" data-unavailable="'+(can?'0':'1')+'" data-help="Impide nuevas entradas. Con posición, mantiene observación y salidas; no simula un cierre al pulsar.">Parar entradas</button><button type="button" class="ghost" data-observed-close="'+esc(row.id)+'" data-unavailable="'+(can&&position?'0':'1')+'" data-help="Solicita cerrar a la próxima cotización verificada.">Cerrar posición</button>'+(row.gap_close?'<button type="button" class="ghost" data-observed-gap-view="'+esc(row.id)+'">Ver cierre</button>':position&&!can&&!row.active_in_this_process&&!(w&&w.running)?'<button type="button" class="ghost" data-observed-gap="'+esc(row.id)+'" data-help="Estima cerrar la posición después del intervalo sin observación, sin alterar su resultado original ni activar otro bot.">Estimar cierre</button>':'')+'</div></td></tr>';}).join('')+'</tbody></table>':'<p class="muted">Sin sesiones. Elige una reserva en Validación de derivados.</p>';
      el('observed-page').textContent=total?(offset+1)+'–'+Math.min(offset+25,total)+' / '+total:'0';ready();
    }
    async function load(silent){
      var ticket=generation;
      try{
        var page=await api('/sessions?offset='+offset+'&limit=25');
        if(!current(ticket))return;
        if(page.lab_unlocked===false&&labUnlocked!==false&&(comparing||el('observed-comparison-table').childElementCount))clearComparison();
        labUnlocked=page.lab_unlocked;rows=page.items;total=page.total;renderLibrary();
        if(!silent&&!busy&&!preparation&&!pending)el('observed-status').textContent='Sesiones ficticias · '+total;
      }catch(error){if(current(ticket))fail(error);}
    }
    function resize(){if(chart&&el('observed-chart').clientWidth>0)chart.resize(el('observed-chart').clientWidth,el('observed-chart').clientHeight);}
    function draw(){
      if(chart)chart.remove();var node=el('observed-chart'),lw=window.LightweightCharts;
      if(!lw)throw new Error('Gráficos no disponibles.');
      chart=lw.createChart(node,{width:node.clientWidth,height:node.clientHeight,layout:{background:{type:'solid',color:'#101b18'},textColor:'#c9d6ce'},grid:{vertLines:{color:'#24362f'},horzLines:{color:'#24362f'}},timeScale:{timeVisible:true,secondsVisible:true,minBarSpacing:.1,rightOffset:20}});
      var candleSeries=chart.addSeries(lw.CandlestickSeries,{upColor:'#42c88a',downColor:'#ff7b78',borderVisible:false,wickUpColor:'#42c88a',wickDownColor:'#ff7b78'});
      candleSeries.setData(bars.map(function(b){return {time:b.t,open:Number(b.o),high:Number(b.h),low:Number(b.l),close:Number(b.c)};}));
      var partialTimes=new Set(markers.filter(function(m){return m.partial_index;}).map(function(m){return second(m.time_ns);}));
      ['entry','exit','partial'].forEach(function(kind){
        var entry=kind==='entry',partial=kind==='partial',color=entry?'#e9b764':partial?'#91b7ff':'#ff7b78';
        var selected=markers.filter(function(m){return kind===(m.entry?'entry':m.partial_index?'partial':'exit');});
        if(!selected.length)return;
        selected.sort(function(a,b){return second(a.time_ns)-second(b.time_ns)||(a.partial_index||0)-(b.partial_index||0);});
        var fillSeries=chart.addSeries(lw.LineSeries,{lineVisible:false,pointMarkersVisible:true,pointMarkersRadius:4,color:color,priceLineVisible:false,lastValueVisible:false});
        var points=new Map();selected.forEach(function(m){var t=second(m.time_ns);points.set(t,{time:t,value:Number(m.price)});});
        fillSeries.setData(Array.from(points.values()));
        lw.createSeriesMarkers(fillSeries,selected.map(function(m){
          var t=second(m.time_ns),below=entry||(!partial&&partialTimes.has(t));
          return {time:t,position:below?'belowBar':'aboveBar',shape:entry?(m.direction==='LONG'?'arrowUp':'arrowDown'):'circle',color:color,text:entry?(m.direction==='LONG'?'L':'S'):partial?'P'+m.partial_index:'×',id:m.id};
        }));
      });
      node.dataset.entryCount=String(markers.filter(function(m){return m.entry;}).length);node.dataset.exitCount=String(markers.filter(function(m){return !m.entry;}).length);node.dataset.sessionId=sessionId;
      node.dataset.partialCount=String(markers.filter(function(m){return m.partial_index;}).length);
      var times=new Map();bars.forEach(function(b){times.set(b.t,{t:b.t});});markers.forEach(function(m){times.set(second(m.time_ns),{t:second(m.time_ns)});});
      var estimate=estimatedClosure&&estimatedClosure.receipt.source.session_id===sessionId&&estimatedClosure.receipt.source.head_hash===bundle.session.head_hash?estimatedClosure.result:null;
      node.dataset.estimatedCloseCount='0';delete node.dataset.estimatedCloseTimeNs;
      if(estimate&&estimate.exit_reference_price!==null){var t=second(estimate.exit_time_ns),series=chart.addSeries(lw.LineSeries,{lineVisible:false,pointMarkersVisible:true,pointMarkersRadius:6,color:'#a78bfa',priceLineVisible:false,lastValueVisible:false});series.setData([{time:t,value:Number(estimate.exit_reference_price)}]);lw.createSeriesMarkers(series,[{time:t,position:'aboveBar',shape:'circle',color:'#a78bfa',text:estimate.exit_price===null?'≈ LQ':'≈ cierre',id:'gap-close'}]);times.set(t,{t:t});node.dataset.estimatedCloseCount='1';node.dataset.estimatedCloseTimeNs=estimate.exit_time_ns;}
      var range=Array.from(times.values()).sort(function(a,b){return a.t-b.t;});if(window.OrbisChartRange)window.OrbisChartRange.mount(el('observed-range'),chart,range,{selected:'all',storageKey:'orbis.chartRange.observed-paper'});else chart.timeScale().fitContent();
      el('observed-chart-coverage').textContent=bars.length+' / '+bundle.view.candle_count+' velas · '+markers.length+' ejecuciones observadas'+(estimate?' · cierre estimado en violeta':'');
    }
    function partialDetails(events){
      if(!events||!events.length)return '';
      return '<details><summary>'+events.length+' parciales</summary>'+events.map(function(e){
        var result=e.kind==='PARTIAL_TARGET_FILLED'?e.quantity+' @ '+e.price+' · resta '+e.remaining_after:e.kind==='PARTIAL_TARGET_SKIPPED_MINIMUM'?'Bajo mínimo':'Rechazado';
        return '<div data-partial-target="'+esc(e.target_index)+'">P'+esc(e.target_index)+' · '+esc(result)+'</div>';
      }).join('')+'</details>';
    }
    function realEntry(value) {
      if (!value || !value.view.trade_total || value.session.gap_close) return null;
      var checkpoint = value.session.checkpoint, status = value.session.stored_status;
      if (checkpoint.engine === 'perp-observed-1.0' && ['RUNNING','CLOSE_ONLY','STOPPED'].includes(status))
        return 'MARKET';
      if (status === 'STOPPED' && !checkpoint.opened && !checkpoint.resting &&
          (checkpoint.engine === 'perp-observed-2.0' || checkpoint.engine === 'perp-observed-3.0' && checkpoint.entry_kind === 'LIMIT'))
        return 'LIMIT';
      return null;
    }
    function showMargin(history) {
      var changeLabels={POSITION_LIMIT:'Posición máxima',RISK_TIERS:'Tramos'};
      var count=history.change_total, shown=history.changes.length;
      var coverage='Sin cambios';
      if(count===1)coverage='1 cambio';
      else if(count>1)coverage=(shown===count?count:shown+' de '+count)+' cambios';
      function marginPoint(point,label) {
        var rules=point.rules, rows=rules.risk_brackets.map(function(tier) {
          return '<tr><th scope="row">'+esc(tier.tier)+'</th><td>'+esc(tier.notional_floor)+'–'+esc(tier.notional_cap==null?'∞':tier.notional_cap)+
            '</td><td>'+esc(tier.max_leverage)+'×</td><td>'+esc(tier.initial_margin_rate)+'</td><td>'+
            esc(tier.maintenance_margin_rate)+'</td><td>'+esc(tier.cumulative_maintenance_amount)+'</td></tr>';
        }).join('');
        return '<details data-margin-sequence="'+esc(point.sequence)+'"><summary>'+esc(utc(point.applied_at_ns))+' UTC · '+esc(label)+'</summary>'+
          '<dl><dt>Captura UTC</dt><dd>'+esc(utc(rules.fetched_at_ns))+'</dd><dt>Posición máxima</dt><dd data-margin-max="'+esc(rules.max_position_size)+'">'+esc(rules.max_position_size)+'</dd></dl>'+
          '<div class="tablewrap"><table aria-label="Tramos de margen GRVT"><thead><tr><th>Tramo</th><th>Nocional USDT</th><th>Apalancamiento máx.</th>'+
          '<th>Tasa inicial</th><th>Tasa de mantenimiento</th><th>Deducción acumulada USDT</th></tr></thead><tbody>'+rows+'</tbody></table></div></details>';
      }
      el('observed-margin-history').innerHTML='<p class="muted">'+esc(coverage)+' · Última captura aplicada '+esc(utc(history.last_capture_at_ns))+' UTC</p>'+
        history.changes.map(function(point) {
          return marginPoint(point,point.changes.map(function(key){return changeLabels[key];}).join(' · '));
        }).join('')+marginPoint(history.initial,'Reglas iniciales');
    }
    function show(value,older){
      if(value.session.verification!=='OBSERVED_REPLAY_VERIFIED'||!value.view||value.view.verification!=='OBSERVED_REPLAY_VERIFIED')throw new Error('La sesión no está reproducida.');
      if(value.view.has_more_markers)throw new Error('Acota la ventana para mostrar todas sus ejecuciones.');
      bundle=value;sessionId=value.session.id;var v=value.view,cp=value.session.checkpoint,m=v.metrics;
      if(older){var b=new Map(bars.map(function(row){return [row.t,row];}));v.candles.forEach(function(row){b.set(row.t,row);});bars=Array.from(b.values()).sort(function(a,b){return a.t-b.t;});var mm=new Map(markers.map(function(row){return [row.id,row];}));v.markers.forEach(function(row){mm.set(row.id,row);});markers=Array.from(mm.values()).sort(function(a,b){return second(a.time_ns)-second(b.time_ns)||a.id.localeCompare(b.id);});}
      else{bars=v.candles;markers=v.markers;}
      el('observed-inspection').hidden=false;el('observed-proof').textContent='Reproducido · '+utc(cp.last_received_ns)+' UTC';
      el('observed-real').hidden = !realEntry(value);
      var metrics=[['Capital',m.initial_capital],['Patrimonio ficticio',m.equity],['Resultado neto',m.net_profit],['Caja',m.cash],['Comisiones',m.fees_paid],['Financiación pagada',m.funding_paid]];
      var order=restingOrder(cp);
      if(order){
        metrics.push([value.session.active_in_this_process?'Límite pendiente':'Último límite',order.price_bound]);
        metrics.push(['Disponible',cp.order_reservation.available]);
      }
      el('observed-metrics').innerHTML=metrics.map(function(item){return '<div><span class="muted">'+esc(item[0])+'</span><strong>'+amount(item[1])+'</strong></div>';}).join('');
      if(order)el('observed-metrics').insertAdjacentHTML('beforeend','<div><span class="muted">Vencimiento UTC</span><strong>'+esc(utc(cp.resting.expires_at_ns))+'</strong></div>');
      if(cp.opened&&cp.opened.partial_exits){
        var size=cp.account.positions[0].size.replace(/^-/,'');
        el('observed-metrics').insertAdjacentHTML('beforeend','<div><span class="muted">Cantidad abierta</span><strong>'+esc(size)+'</strong>'+partialDetails(cp.opened.partial_exits)+'</div>');
      }
      el('observed-trades').innerHTML='<table><thead><tr><th>Dirección</th><th>Entrada UTC</th><th>Salida UTC</th><th>Precio entrada</th><th>Precio salida</th><th>Neto</th><th>Motivo</th></tr></thead><tbody>'+v.trades.map(function(t){return '<tr><td>'+esc(t.direction)+'</td><td>'+esc(utc(t.entry_time_ns))+'</td><td>'+esc(utc(t.exit_time_ns))+'</td><td>'+amount(t.entry_price)+'</td><td>'+amount(t.exit_price)+'</td><td>'+amount(t.net_profit)+'</td><td>'+esc(t.exit_reason)+partialDetails(t.partial_exits)+'</td></tr>';}).join('')+'</tbody></table>';
      el('observed-trades-page').textContent=v.trade_total?Math.min(tradeOffset+1,v.trade_total)+'–'+Math.min(tradeOffset+50,v.trade_total)+' / '+v.trade_total:'0';el('observed-trades-prev').disabled=tradeOffset===0;el('observed-trades-next').disabled=tradeOffset+50>=v.trade_total;
      showMargin(v.margin_rules);
      el('observed-journal').textContent=JSON.stringify(v.recent_events,null,2);el('observed-origin').textContent=JSON.stringify(value.origin,null,2);el('observed-older').disabled=!v.has_older;draw();
    }
    async function inspect(id,older){clearComparison();if(id!==sessionId){el('observed-margin').open=false;estimatedClosure=null;if(gapWidget)gapWidget.clear();}var ticket=++generation;busy=true;ready();el('observed-status').textContent='Reproduciendo…';try{var path='/sessions/'+encodeURIComponent(id)+'/view?trade_offset='+tradeOffset+(older&&bars.length?'&before_t='+bars[0].t:'');if(older&&bundle)path+='&snapshot_sequence='+bundle.session.sequence+'&snapshot_hash='+bundle.session.head_hash;var value=await api(path);if(!current(ticket))return;show(value,older);el('observed-status').textContent=labels[value.session.status]||value.session.status;}catch(error){if(current(ticket)){clearInspection();fail(error);}}finally{if(current(ticket)){busy=false;ready();}}}
    async function compareReserve(){
      if(busy||comparing||!bundle)return;
      var ticket=++comparisonGeneration,selected=bundle.session;
      function stillCurrent(){return current()&&ticket===comparisonGeneration&&bundle&&bundle.session===selected;}
      var status=el('observed-comparison-status'),table=el('observed-comparison-table');
      comparing=true;table.replaceChildren();el('observed-comparison').hidden=false;status.textContent='Reproduciendo PAPER y LAB…';ready();
      try{
        var query=new URLSearchParams({snapshot_sequence:String(selected.sequence),snapshot_hash:selected.head_hash});
        var result=await api('/sessions/'+encodeURIComponent(selected.id)+'/reserve-comparison?'+query.toString());
        if(!stillCurrent())return;
        if(result.schema!=='orbis.paper.perp.reserve-comparison.v1'||result.paper.session_id!==selected.id||
           result.paper.head_hash!==selected.head_hash||result.paper.sequence!==selected.sequence||!result.reserves.length)
          throw new Error('La comparación no corresponde a esta sesión.');
        function comparisonRow(label,detail,row){
          var m=row.metrics;
          return '<tr><th scope="row">'+esc(label)+'<br><small>'+esc(detail)+'</small></th><td>'+esc(utc(row.from_ns))+
            '<br>'+esc(utc(row.to_ns))+'</td><td>'+amount(m.initial_capital)+'</td><td>'+amount(m.net_profit)+
            '</td><td>'+amount(m.return_pct)+' %</td><td>'+esc(m.closed_trades)+'</td><td>'+amount(m.fees_paid)+
            '</td><td>'+amount(m.funding_paid)+'</td></tr>';
        }
        var paper=result.paper,description=(labels[paper.status]||paper.status)+(paper.position_open?' · Posición abierta':'');
        if(!paper.is_latest)description+=' · Instantánea anterior';
        table.innerHTML='<table><thead><tr><th>Origen</th><th>Periodo UTC</th><th>Capital</th><th>Resultado neto</th>'+
          '<th>Rentabilidad</th><th>Cierres</th><th>Comisiones</th><th>Financiación</th></tr></thead><tbody>'+
          comparisonRow('PAPER observado',description,paper)+result.reserves.map(function(row,index){
            return comparisonRow('LAB · Reserva '+(index+1),row.bars+' velas · Simulada',row);
          }).join('')+'</tbody></table>';
        status.textContent='Misma estrategia · '+result.symbol+' · '+result.timeframe;
      }catch(error){
        if(stillCurrent())status.textContent=error.message;
      }finally{
        if(stillCurrent()){comparing=false;ready();}
      }
    }
    async function prepare(validationId, options) {
      if (pending || busy) {
        el('observed-status').textContent = pending ? 'Consulta o cancela la activación pendiente.' : 'Hay una revisión en curso.';
        return false;
      }
      options = options || {};
      var ticket = ++generation;
      var stillCurrent = function () { return current(ticket) && (!options.isCurrent || options.isCurrent()); };
      busy = true; preparation = null; el('observed-review').hidden = true; ready();
      el('observed-status').textContent = 'Verificando reserva…';
      try {
        var value = await api('/prepare',{method:'POST',body:JSON.stringify({validation_id:validationId,preparation_id:crypto.randomUUID()})});
        if (!stillCurrent()) return false;
        if (value.phase !== 'READY') throw new Error('Preparación '+value.phase.toLowerCase());
        var ref = value.reference;
        var source = value.optimization_origin;
        if (value.validation_id !== validationId || ref.artifact_id !== validationId ||
            (options.origin && (!source || ['optimization_id','optimization_hash','version_id','reserve_run_id']
              .some(function (key) { return source[key] !== options.origin[key]; })))) {
          throw new Error('La reserva PAPER no corresponde a la variante seleccionada.');
        }
        var fields = [['Mercado',ref.symbol],['Temporalidad',ref.timeframe],['Dirección',value.direction],
          ['Capital ficticio',value.initial_capital],['Margen',ref.profile.margin_mode+' · '+ref.profile.leverage+'×']];
        var order = ref.profile.entry_order;
        fields.push(['Entrada',order?'Límite':'Mercado']);
        if (order) fields.push(['Vigencia',order.valid_bars+(order.valid_bars===1?' vela':' velas')]);
        if (ref.profile.partial_targets) fields.push(['Objetivos parciales',String(ref.profile.partial_targets.length)]);
        if (source) {
          var reserve = ref.windows[0];
          fields.push(['Origen','Reserva optimizada'],['Reserva',reserve.bars+' velas'],
            ['Desde UTC',utc(String(reserve.from_t)+'000000000')],['Hasta UTC',utc(String(reserve.to_t)+'000000000')],
            ['Stop %',value.exit_parameters_pct.stop_loss.replace('.',',')],
            ['Objetivo %',value.exit_parameters_pct.take_profit.replace('.',',')]);
          if (ref.warnings.includes('SMALL_SAMPLE')) fields.push(['Estadística','Muestra pequeña']);
        }
        preparation = value;
        el('observed-review-values').innerHTML=fields.map(function(row){return '<div><span class="muted">'+esc(row[0])+'</span><strong>'+esc(row[1])+'</strong></div>';}).join('');
        el('observed-review').hidden = false;
        el('observed-status').textContent = 'Reserva verificada · lista para confirmar';
        return true;
      } catch (error) {
        if (stillCurrent()) fail(error);
        return false;
      } finally {
        if (current(ticket)) {
          if (!stillCurrent()) el('observed-status').textContent = '';
          busy = false; ready();
        }
      }
    }
    function finishRequest(value){cancelRequested=cancelRequested||!!value.cancel_requested;el('observed-request-state').textContent={ACTIVATING:'Recogiendo datos…',ACTIVATED:'Activación registrada',CANCELLED:'Cancelada',INTERRUPTING:'Interrumpiendo…',FAILED:'Revisar activación'}[value.phase]||value.phase;if((value.phase==='ACTIVATED'&&!cancelRequested)||value.phase==='CANCELLED'||value.phase==='FAILED'||(value.phase==='INTERRUPTING'&&!value.in_progress&&(!value.worker||!value.worker.running))){pending=null;remember();ready();}return value.session_id;}
    el('observed-activate').addEventListener('click',async function(){if(busy||!preparation||pending)return;cancelRequested=false;var p=preparation,ticket=++generation;pending={validation_id:p.validation_id,preparation_id:p.preparation_id,request_id:crypto.randomUUID()};remember();busy=true;ready();el('observed-status').textContent='Activando ficticio…';try{var value=await api('/activate',{method:'POST',body:JSON.stringify(pending)});if(!current(ticket))return;finishRequest(value);preparation=null;el('observed-review').hidden=true;await load(true);el('observed-status').textContent=value.phase==='ACTIVATED'?'Sesión creada · comprobando cobertura':value.phase==='CANCELLED'?'Activación cancelada':'Consulta la activación pendiente';}catch(error){if(current(ticket)){fail(error);el('observed-request-state').textContent='Respuesta pendiente · consulta antes de repetir';}}finally{if(current(ticket)){busy=false;ready();}}});
    async function checkRequest(){if(!pending)return;var id=pending.request_id;try{var result=await api('/requests/'+encodeURIComponent(id));if(!alive||!pending||pending.request_id!==id)return;finishRequest(result);await load(true);}catch(error){fail(error);}}
    async function cancelRequest(){if(!pending)return;cancelRequested=true;try{var value=await api('/requests/'+encodeURIComponent(pending.request_id)+'/cancel',{method:'POST'});if(alive){el('observed-request-state').textContent=value.phase==='CANCELLED'?'Cancelada':'Cancelación solicitada';await checkRequest();}}catch(error){fail(error);}}
    gapWidget=window.OrbisPerpGapClose.mount(el('observed-gap-close'),{request:api,amount:amount,onBusy:function(value){busy=value;ready();},onLocked:fail,onReview:async function(value){var ticket=++generation,id=value.session.id;var original=await api('/sessions/'+encodeURIComponent(id)+'/view');if(!current(ticket))throw new Error('La vista ha cambiado.');if(original.session.head_hash!==value.session.head_hash)throw new Error('La sesión cambió; vuelve a revisarla.');estimatedClosure=null;tradeOffset=0;show(original,false);},onClear:function(){estimatedClosure=null;if(chart&&bundle)draw();},onShow:async function(value){var ticket=++generation,id=value.receipt.source.session_id;var original=await api('/sessions/'+encodeURIComponent(id)+'/view');if(!current(ticket))throw new Error('La vista ha cambiado.');if(original.session.head_hash!==value.receipt.source.head_hash)throw new Error('El cierre no corresponde a este prefijo.');estimatedClosure=value;tradeOffset=0;show(original,false);await load(true);}});
    el('observed-check').addEventListener('click',checkRequest);el('observed-cancel').addEventListener('click',cancelRequest);
    el('observed-discard').addEventListener('click',function(){preparation=null;el('observed-review').hidden=true;ready();});
    el('observed-refresh').addEventListener('click',function(){load(false);});
    [['prev',-25],['next',25]].forEach(function(row){el('observed-'+row[0]).addEventListener('click',function(){offset=Math.max(0,offset+row[1]);load(false);});});
    el('observed-sessions').addEventListener('click',async function(event){if(busy)return;var gap=event.target.closest('[data-observed-gap],[data-observed-gap-view]');if(gap){if(gap.dataset.observedGap)gapWidget.open(gap.dataset.observedGap);else gapWidget.showSaved(gap.dataset.observedGapView);return;}var view=event.target.closest('[data-observed-view]');if(view){tradeOffset=0;inspect(view.dataset.observedView,false);return;}var button=event.target.closest('[data-observed-stop],[data-observed-close]');if(!button||busy||button.disabled)return;var id=button.dataset.observedStop||button.dataset.observedClose,kind=button.dataset.observedStop?'stop':'close';busy=true;ready();try{await api('/sessions/'+id+'/'+kind,{method:'POST',body:JSON.stringify({request_id:crypto.randomUUID()})});if(current()){el('observed-status').textContent=kind==='stop'?'Nuevas entradas detenidas':'Cierre solicitado · espera cotización';await load(true);}}catch(error){fail(error);}finally{if(alive){busy=false;ready();}}});
    el('observed-replay').addEventListener('click',function(){if(sessionId&&!busy)inspect(sessionId,false);});el('observed-older').addEventListener('click',function(){if(sessionId&&!busy)inspect(sessionId,true);});
    el('observed-compare').addEventListener('click',compareReserve);
    el('observed-real').addEventListener('click',function () {
      var entry = !busy && realEntry(bundle);
      if (entry && !this.hidden) window.OrbisReal.prepareGrvtSource(entry, sessionId);
    });
    [['prev',-50],['next',50]].forEach(function(row){el('observed-trades-'+row[0]).addEventListener('click',function(){if(sessionId&&!busy){tradeOffset=Math.max(0,tradeOffset+row[1]);inspect(sessionId,false);}});});
    el('observed-export').addEventListener('click',async function(){if(!sessionId||busy)return;var ticket=generation,id=sessionId;try{var response=await fetch('/paper/perp/observed/sessions/'+id+'/export',{cache:'no-store',signal:controller.signal});if(!response.ok){var error=new Error('No se pudo exportar la sesión.');error.status=response.status;throw error;}var data=await response.text();if(!current(ticket))return;var lines=data.trimEnd().split('\n'),footer=JSON.parse(lines[lines.length-1]);if(footer.kind!=='COMPLETE')throw new Error('Exportación incompleta.');var url=URL.createObjectURL(new Blob([data],{type:'application/x-ndjson'})),a=document.createElement('a');a.href=url;a.download='orbis-paper-perp-'+id+'.jsonl';a.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);}catch(error){if(current(ticket))fail(error);}});
    var widget={prepare:prepare,load:function(){if(!busy){if(pending)checkRequest();return load(false);}},dispose:function(){if(!alive)return;alive=false;generation++;if(gapWidget)gapWidget.dispose();controller.abort();clearInterval(poller);if(observer)observer.disconnect();if(resizeObserver)resizeObserver.disconnect();if(chart)chart.remove();if(pending)fetch('/paper/perp/observed/requests/'+encodeURIComponent(pending.request_id)+'/cancel',{method:'POST',keepalive:true}).catch(function(){});}};
    observer=new MutationObserver(function(){if(!root.isConnected)widget.dispose();});observer.observe(document.body,{childList:true,subtree:true});if(window.ResizeObserver){resizeObserver=new ResizeObserver(resize);resizeObserver.observe(el('observed-chart'));}
    poller=setInterval(function(){if(current()&&!busy&&!document.hidden&&root.offsetParent!==null){if(pending)checkRequest();else load(true);}},3000);remember();ready();return widget;
  }
  window.OrbisPerpObserved={mount:mount};
})();
