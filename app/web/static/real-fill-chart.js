/* Shared REAL fill chart. Adapters verify their own receipt/booking evidence. */
(function () {
  "use strict";
  function create(adapter) {
  var research = adapter.researchExport;
  var inspected = null, signature = null, exportRequest = null;
  var root = null,api = null,chart = null,selected = null,epoch = 0,sequence = 0,data = null,bars = [],market = null,bySecond = new Map(),userViewport = false;
  function dispose() { if (chart) chart.remove(); chart = null; }
  function clear() { epoch += 1; sequence += 1; exportRequest = null; dispose(); if (root) { root.replaceChildren(); root.hidden = true; }
    root = api = selected = data = market = null; bars = []; bySecond.clear(); userViewport = false; }
  function current(host,account,issued,request) {
    return root === host && host.isConnected && api && api.getAccountId() === account && epoch === issued && sequence === request;
  }
  function node(tag,text,cls) { var item = document.createElement(tag); item.textContent = text; if (cls) item.className = cls; return item; }
  function state(text) { if (root) root.querySelector("[data-real-chart-state]").textContent = text; }
  function close() { sequence += 1; dispose(); selected = data = market = null; bars = []; bySecond.clear(); if (root) { root.hidden = true;
      ["canvas","inspect","range","fill"].forEach(function (part) { root.querySelector("[data-real-chart-"+part+"]").replaceChildren(); });
      var canvas = root.querySelector("[data-real-chart-canvas]");
      ["fillCount","candleCount","buyCount","sellCount"].forEach(function (key) { delete canvas.dataset[key]; });
      state(""); root.querySelector("[data-real-chart-title]").textContent = "";
    } inspected = signature = null; userViewport = false; resetExport(); }
  function exportControl() {
    if (!root || !research) return;
    root.querySelector("[data-real-chart-export]").disabled = !!exportRequest || !data || data.total < 1 ||
      data.total > 5000 || api.amountsHidden();
  }
  function resetExport() {
    exportRequest = null;
    if (root && research) root.querySelector("[data-real-chart-export-state]").textContent = "";
    exportControl();
  }
  async function exportFills() {
    var button = root.querySelector("[data-real-chart-export]");
    if (button.disabled || !window.confirm(research.prompt)) return;
    var host = root, account = api.getAccountId(), issued = epoch, item = selected, snapshot = data;
    var state = host.querySelector("[data-real-chart-export-state]");
    var request = {}; exportRequest = request; exportControl();
    function stillCurrent() {
      return root === host && host.isConnected && api.getAccountId() === account && epoch === issued &&
        selected === item && data && data.revision === snapshot.revision && exportRequest === request && !api.amountsHidden();
    }
    state.textContent = "Verificando exportación…";
    try {
      var result = await api.request(research.path(account,item),{method:"POST",body:JSON.stringify({
        revision:snapshot.revision,confirmation:research.confirmation
      })},true);
      if (!await window.OrbisResearchDownload.save(result,{
        schema:research.schema,revision:snapshot.revision,count:snapshot.total,filename:research.filename
      },stillCurrent)) return;
      state.textContent = result.record_count+(result.record_count === 1 ? research.singular : research.plural);
    } catch (error) {
      if (stillCurrent()) state.textContent = error.message || "No se pudo verificar la exportación";
    } finally {
      if (exportRequest === request) {
        if (!stillCurrent()) state.textContent = "";
        exportRequest = null; exportControl();
      }
    }
  }
  async function executions(item,account,isCurrent) {
    var offset = 0,revision = null,all = [],first = null;
    do {
      var page = await api.request(adapter.path(account,item)+"?limit=500&offset="+offset+(revision ? "&revision="+revision : ""),null,true);
      if (!isCurrent()) return null;
      if (!adapter.matches(page,item,account) ||
          !/^[0-9a-f]{64}$/.test(page.revision || "") || !Number.isInteger(page.total) || page.total < 0 || page.total > 100000 ||
          page.offset !== offset || !Array.isArray(page.executions) || page.executions.length > 500 ||
          revision && (page.revision !== revision || page.total !== first.total)) throw new Error("Instantánea de fills REAL incoherente; actualiza.");
      if (!first) first = page;
      revision = page.revision; all.push.apply(all,page.executions);
      if (page.next_offset !== null && (page.next_offset !== all.length || page.next_offset <= offset || page.next_offset >= page.total))
        throw new Error("Paginación REAL sin avance verificable.");
      offset = page.next_offset;
    } while (offset !== null);
    if (all.length !== first.total || new Set(all.map(function (fill) { return fill.fill_id; })).size !== all.length)
      throw new Error("Faltan fills REAL o están duplicados.");
    all.forEach(function (fill) {
      if (!["BUY","SELL"].includes(fill.side) || !Number.isSafeInteger(fill.time_ms) || fill.time_ms <= 0 ||
          typeof fill.price !== "string" || !Number.isFinite(Number(fill.price)) || Number(fill.price) <= 0 ||
          !["quantity_base","gross_quote","fee","fee_asset","reason","occurred_at","intent_id"].every(function (key) { return typeof fill[key] === "string"; }) || !adapter.fillMatches(fill))
        throw new Error("Fill REAL no representable con seguridad.");
    });
    return Object.assign({},first,{executions:all});
  }
  async function candles(item,before) {
    var controller = new AbortController(),timer = setTimeout(function () { controller.abort(); },15000),result;
    try {
      var response = await fetch("/market/candles?provider=mexc&symbol="+encodeURIComponent(item.symbol)+"&timeframe="+
        encodeURIComponent(item.timeframe)+"&limit=1000"+(before ? "&before="+before : ""),
        {cache:"no-store",credentials:"omit",referrerPolicy:"no-referrer",signal:controller.signal});
      if (!response.ok) throw new Error("Velas MEXC no disponibles");
      result = await response.json();
    } finally { clearTimeout(timer); }
    if (result.resolved_provider !== "mexc" || result.symbol !== item.symbol || result.timeframe !== item.timeframe)
      throw new Error("Fuente de velas distinta de MEXC o del mercado del bot");
    if (result.available !== true || !Array.isArray(result.candles)) throw new Error(result.reason || "Sin velas MEXC");
    var rows = result.candles.map(function (row) {
      var point = {time:row.t,open:Number(row.o),high:Number(row.h),low:Number(row.l),close:Number(row.c),closed:row.closed};
      if (!Number.isSafeInteger(point.time) || point.time <= 0 || before && point.time >= before ||
          ![point.open,point.high,point.low,point.close].every(function (value) { return Number.isFinite(value) && value > 0; }) ||
          point.high < Math.max(point.open,point.close) || point.low > Math.min(point.open,point.close) || point.low > point.high)
        throw new Error("OHLC MEXC no válido");
      return point;
    });
    rows.sort(function (a,b) { return a.time-b.time; });
    if (new Set(rows.map(function (row) { return row.time; })).size !== rows.length) throw new Error("Velas MEXC duplicadas");
    return {bars:rows,older:result.older_pagination_supported === true && !result.history_exhausted && rows.length > 0};
  }
  function inspect(time) {
    inspected = time;
    var target = root.querySelector("[data-real-chart-inspect]"); target.replaceChildren();
    var matches = bySecond.get(time) || [];
    if (!matches.length) return;
    target.append(node("strong","Fills · REAL"));
    matches.forEach(function (fill) {
      var exact = api.exact || String;
      var line = node("p",(fill.side === "BUY" ? "Compra" : "Venta")+" · "+fill.occurred_at.replace("T"," ")+" · "+
        exact(fill.quantity_base)+" "+data.base_asset+" a "+exact(fill.price)+" "+data.quote_asset+
        " · Comisión "+exact(fill.fee)+" "+fill.fee_asset+" · "+fill.reason+(fill.partial ? " · Parcial" : ""),"muted");
      line.dataset.fillId = fill.fill_id;
      var link = node("small",adapter.description(fill),"muted");
      target.append(line,link);
      if (adapter.detail) target.append(node("p",adapter.detail(fill,data,exact),"muted"));
    });
  }
  function candleEnd(start) {
    var match = /^(\d+)([mhdwM])$/.exec(selected.timeframe);
    if (!match) return start;
    var end;
    if (match[2] === "M") { var date = new Date(start*1000); end = Date.UTC(date.getUTCFullYear(),date.getUTCMonth()+Number(match[1]),1)/1000; }
    else end = start+Number(match[1])*({m:60,h:3600,d:86400,w:604800}[match[2]]);
    return end;
  }
  function covered(time) {
    var low = 0,high = bars.length;
    while (low < high) { var mid = Math.floor((low+high)/2); if (bars[mid].time <= time) low = mid+1; else high = mid; }
    return !!low && time < candleEnd(bars[low-1].time);
  }
  function draw(preserve) {
    var previous = preserve && chart && (userViewport || root.querySelector("[data-real-chart-range]").dataset.view === "custom") ? chart.timeScale().getVisibleRange() : null;
    dispose();
    var host = root.querySelector("[data-real-chart-canvas]"); host.replaceChildren();
    var lw = window.LightweightCharts;
    if (!lw || !lw.CandlestickSeries || !lw.LineSeries || !lw.createSeriesMarkers) throw new Error("Gráficos locales no disponibles");
    chart = lw.createChart(host,{autoSize:true,layout:{background:{type:"solid",color:"#101b18"},textColor:"#c9d6ce"},
      grid:{vertLines:{color:"#24362f"},horzLines:{color:"#24362f"}},timeScale:{timeVisible:true,secondsVisible:true,minBarSpacing:0.01,lockVisibleTimeRangeOnResize:true},localization:{locale:"es-ES"}});
    var price = chart.addSeries(lw.CandlestickSeries,{upColor:"#42c88a",downColor:"#ff7b78",borderVisible:false,wickUpColor:"#42c88a",wickDownColor:"#ff7b78"});
    price.setData(bars);
    var times = [];
    ["BUY","SELL"].forEach(function (side) {
      var groups = new Map();
      data.executions.filter(function (fill) { return fill.side === side; }).forEach(function (fill) {
        var time = Math.floor(fill.time_ms/1000),group = groups.get(time) || [];
        group.push(fill); groups.set(time,group);
      });
      var points = Array.from(groups.entries()).sort(function (a,b) { return a[0]-b[0]; });
      if (!points.length) return;
      var series = chart.addSeries(lw.LineSeries,{lineVisible:false,pointMarkersVisible:false,crosshairMarkerVisible:false,priceLineVisible:false,lastValueVisible:false});
      series.setData(points.map(function (entry) { times.push(entry[0]); return {time:entry[0],value:Number(entry[1][entry[1].length-1].price)}; }));
      lw.createSeriesMarkers(series,points.map(function (entry) { return {time:entry[0],position:side === "BUY" ? "belowBar" : "aboveBar",
        shape:side === "BUY" ? "arrowUp" : "arrowDown",color:side === "BUY" ? "#42c88a" : "#ff7b78",
        text:(side === "BUY" ? "REAL +" : "REAL −")+(entry[1].length > 1 ? " ×"+entry[1].length : "")}; }));
    });
    host.dataset.fillCount = String(data.total);
    host.dataset.candleCount = String(bars.length);
    host.dataset.buyCount = String(data.executions.filter(function (fill) { return fill.side === "BUY"; }).length);
    host.dataset.sellCount = String(data.executions.filter(function (fill) { return fill.side === "SELL"; }).length);
    chart.subscribeClick(function (event) { if (typeof event.time === "number") inspect(event.time); });
    chart.subscribeCrosshairMove(function (event) { if (typeof event.time === "number" && bySecond.has(event.time)) inspect(event.time); });
    var range = root.querySelector("[data-real-chart-range]"); range.replaceChildren();
    if (bars.length > 1 && window.OrbisChartRange) window.OrbisChartRange.mount(range,chart,bars,
      {storageKey:adapter.storageKey,selected:"all",lastCounts:[100,500],eventTimes:times,
        coverageEnd:candleEnd(bars[bars.length-1].time)});
    else chart.timeScale().fitContent();
    if (previous) chart.timeScale().setVisibleRange(previous);
    root.querySelector("[data-real-chart-older]").disabled = !market || !market.older || bars.length >= 12000;
    var missing = data.executions.filter(function (fill) { return !covered(Math.floor(fill.time_ms/1000)); }).length;
    state(adapter.caption(data)+" · "+data.total+" fills · "+bars.length+" velas MEXC"+(missing ? " · "+missing+" fills sin OHLC" : "")+
      (bars.some(function (bar) { return bar.closed === false; }) ? " · vela en formación" : "")+
      (bars.length >= 12000 ? " · máximo de 12.000 velas cargadas" : "")+(market && market.error ? " · "+market.error : ""));
    var select = root.querySelector("[data-real-chart-fill]"); select.replaceChildren(new Option("Inspeccionar fill…",""));
    data.executions.forEach(function (fill) { select.add(new Option(fill.occurred_at.slice(0,23).replace("T"," ")+" UTC · "+fill.side,fill.fill_id)); });
  }
  async function load(older) {
    if (!selected || !root || !root.isConnected) return;
    var host = root,account = api.getAccountId(),issued = epoch,request = ++sequence,item = selected;
    var isCurrent = function () { return current(host,account,issued,request); };
    var preserve = !!data;
    root.querySelector("[data-real-chart-older]").disabled = true;
    state(older ? "Cargando histórico MEXC anterior…" : "Verificando fills REAL…");
    try {
      var result = await executions(item,account,isCurrent);
      if (!result || !isCurrent()) return;
      var prices;
      try { prices = await candles(item,older && bars.length ? bars[0].time : null); }
      catch (error) { prices = {bars:[],older:false,error:error.name === "AbortError" ? "Consulta de precios sin respuesta; actualiza" : error.message || "Velas no disponibles"}; }
      if (!isCurrent()) return;
      var changed = !data || data.revision !== result.revision;
      data = result;
      if (changed) resetExport();
      bySecond.clear(); data.executions.forEach(function (fill) {
        var time = Math.floor(fill.time_ms/1000),group = bySecond.get(time) || []; group.push(fill); bySecond.set(time,group);
      });
      if (older) {
        if (prices.error) throw new Error(prices.error);
        var known = new Set(bars.map(function (bar) { return bar.time; }));
        bars = prices.bars.filter(function (bar) { return !known.has(bar.time); }).concat(bars).slice(-12000);
      } else if (preserve) {
        if (bars.length && prices.bars.length && bars[0].time < prices.bars[0].time) prices.older = market && market.older;
        var merged = new Map(bars.map(function (bar) { return [bar.time,bar]; }));
        prices.bars.forEach(function (bar) { merged.set(bar.time,bar); });
        bars = Array.from(merged.values()).sort(function (a,b) { return a.time-b.time; }).slice(-12000);
      } else bars = prices.bars;
      market = prices;
      draw(preserve); inspect(data.executions.length ? Math.floor(data.executions[data.executions.length-1].time_ms/1000) : null);
    } catch (error) {
      if (!isCurrent()) return;
      dispose(); data = null; bars = []; market = null; bySecond.clear(); resetExport();
      var canvas = root.querySelector("[data-real-chart-canvas]"); canvas.replaceChildren();
      delete canvas.dataset.fillCount; delete canvas.dataset.candleCount; delete canvas.dataset.buyCount; delete canvas.dataset.sellCount;
      root.querySelector("[data-real-chart-range]").replaceChildren();
      root.querySelector("[data-real-chart-inspect]").replaceChildren();
      root.querySelector("[data-real-chart-fill]").replaceChildren();
      state("Gráfico no verificable: "+(error.message === "Not Found" ? "reinicia ORBIS para cargar la API nueva" : error.message || "revisa la sesión REAL"));
    }
  }
  function mount(element,client) {
    clear(); root = element; api = client; root.hidden = true;
    root.innerHTML = '<div class="row"><strong data-real-chart-title></strong><span class="statusbadge">REAL · fills</span>'+
      '<button type="button" class="ghost" data-real-chart-refresh>Actualizar gráfico</button>'+
      (research ? '<button type="button" class="ghost" data-real-chart-export disabled>Exportar JSONL</button>' : '')+
      '<button type="button" class="ghost" data-real-chart-close>Cerrar gráfico</button></div>'+
      (research ? '<small class="muted" data-real-chart-export-state role="status"></small>' : '')+'<div data-real-chart-range></div><button type="button" class="ghost" data-real-chart-older disabled>Histórico anterior</button>'+
      '<p class="muted" data-real-chart-state role="status"></p><div class="real-bot-chart" data-real-chart-canvas role="img" aria-label="Velas MEXC y fills REAL del bot"></div>'+
      '<label class="field" data-real-chart-help>Ejecución<select data-real-chart-fill aria-label="Inspeccionar fill REAL"><option value="">Inspeccionar fill…</option></select></label>'+
      '<div data-real-chart-inspect role="status" aria-live="polite"></div>';
    root.querySelector("[data-real-chart-help]").setAttribute("data-help",adapter.help+" Varias ejecuciones del mismo lado en un segundo se agrupan en el último precio; la inspección conserva todos sus datos.");
    root.querySelector("[data-real-chart-canvas]").setAttribute("aria-label",adapter.ariaLabel);
    root.querySelector("[data-real-chart-close]").addEventListener("click",close);
    root.querySelector("[data-real-chart-refresh]").addEventListener("click",function () { load(false); });
    if (research) {
      var exportButton = root.querySelector("[data-real-chart-export]");
      exportButton.setAttribute("data-help",research.help);
      exportButton.addEventListener("click",exportFills);
    }
    root.querySelector("[data-real-chart-older]").addEventListener("click",function () { if (market && market.older && bars.length < 12000) load(true); });
    ["wheel","pointerdown"].forEach(function (event) {
      root.querySelector("[data-real-chart-canvas]").addEventListener(event,function () { userViewport = true; },{passive:true});
    });
    root.querySelector("[data-real-chart-range]").addEventListener("change",function () { userViewport = false; });
    root.querySelector("[data-real-chart-range]").addEventListener("click",function (event) { if (event.target.closest("[data-range-apply]")) userViewport = false; });
    root.querySelector("[data-real-chart-fill]").addEventListener("change",function () {
      var fill = data && data.executions.find(function (item) { return item.fill_id === this.value; },this);
      if (fill) inspect(Math.floor(fill.time_ms/1000));
    });
  }
  window.addEventListener("hashchange",function () { if (root && location.hash.split("?")[0] !== "#/real") clear(); });
  return {mount:mount,clear:clear,reset:close,redact:function () { if (root && data) inspect(inspected); if (api && api.amountsHidden()) resetExport(); else exportControl(); },
    sync:function (item) { if (selected && selected.id === item.id && adapter.signature) {
      var next = adapter.signature(item); if (next !== signature) { signature = next; load(false); }
    } },refresh:function () { if (selected) load(false); },
    show:function (raw) { if (!root) return; var item = adapter.item ? adapter.item(raw) : raw; signature = adapter.signature ? adapter.signature(raw) : null; var same = selected && selected.id === item.id; if (!same) { dispose(); data = null; bars = []; market = null; userViewport = false; resetExport(); }
      selected = {id:item.id,symbol:item.symbol,timeframe:item.timeframe}; root.hidden = false;
      root.querySelector("[data-real-chart-title]").textContent = item.symbol+" · "+item.timeframe;
      load(false);
      var header = document.querySelector(".cabecera-principal");
      root.style.scrollMarginTop = ((header ? header.getBoundingClientRect().height : 0)+12)+"px";
      root.scrollIntoView({block:"start"}); }};
  }
  window.OrbisRealFillChart = {create:create};
})();
