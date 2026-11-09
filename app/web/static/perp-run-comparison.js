/* Replay two LAB derivative runs before showing differences or exporting. */
(function () {
  "use strict";
  var metrics = [
    ["Capital final · USDT", "final_equity"], ["Variación neta · USDT", "net_equity_change"],
    ["Rentabilidad %", "return_pct"], ["Drawdown máximo %", "max_drawdown_pct"], ["Operaciones", "trades"],
    ["Salidas parciales", "partial_exits"], ["Comisiones · USDT", "fees_paid"], ["Financiación · USDT", "funding_paid"],
    ["Acierto %", "win_rate_pct"], ["Expectativa · USDT", "expectancy"], ["Profit factor", "profit_factor"],
    ["Exposición %", "exposure_pct"], ["Liquidación", "liquidated"], ["Posición abierta", "open_position"]
  ];
  var reasonLabels = {
    MARKET_OR_TIMEFRAME:"mercado o temporalidad", DATASET:"datos o reglas de mercado",
    WINDOW:"periodo", ENGINE:"motor", STARTING_CAPITAL:"capital inicial", COSTS:"costes",
    SIMULATION_ASSUMPTIONS:"supuestos de ejecución", INCOMPLETE:"prueba interrumpida o incompleta"
  };
  var changeLabels = {
    direction:"dirección", allocation:"asignación", entry:"entrada", exit:"salida",
    stop_loss:"stop", take_profit:"objetivo", trailing_stop:"trailing", max_holding_bars:"permanencia",
    initial_capital:"capital inicial", fee_rate:"comisión", slippage_rate:"deslizamiento",
    entry_order:"orden de entrada", partial_targets:"objetivos parciales"
  };
  function node(tag, value) {
    var element = document.createElement(tag);
    if (value !== undefined) element.textContent = value;
    return element;
  }

  function mount(panel, api, utc, format, versionLabel) {
    var outer = panel.closest("#lab-perp-runs");
    var select = panel.querySelector("select"), status = panel.querySelector("[role=status]");
    var more = panel.querySelector("[data-run-compare-more]"), compare = panel.querySelector("[data-run-compare]");
    var exportButton = panel.querySelector("[data-run-compare-export]"), table = panel.querySelector("table");
    var body = table.tBodies[0], chartBox = panel.querySelector("[data-run-compare-chart]");
    var range = panel.querySelector("[data-run-compare-range]"), legend = panel.querySelector("[data-run-compare-legend]");
    var run = null, report = null, chart = null, busy = false, offset = 0, hasMore = true;
    var candidates = new Map(), controller = new AbortController();

    function begin() {
      controller.abort();
      controller = new AbortController();
      return controller.signal;
    }
    function active(signal) {
      return run && panel.isConnected && panel.open && outer.open && !signal.aborted;
    }
    function controls() {
      select.disabled = busy || candidates.size === 0;
      more.hidden = !hasMore;
      more.disabled = busy;
      compare.disabled = busy || !candidates.has(select.value);
      exportButton.disabled = busy || !report;
    }
    function clearResult() {
      report = null;
      if (chart) chart.remove();
      chart = null;
      body.replaceChildren();
      range.replaceChildren();
      legend.hidden = chartBox.hidden = table.hidden = true;
      delete chartBox.dataset.points;
    }
    function reset() {
      begin();
      candidates.clear();
      offset = 0; hasMore = true; busy = false;
      select.replaceChildren(new Option("Selecciona otra prueba", ""));
      status.textContent = "";
      clearResult();
      controls();
    }
    function label(item) {
      return versionLabel(item.profile.strategy_version_id) + " · " + item.symbol + " · " + item.timeframe +
        " · " + (item.profile.entry_order ? "LIMIT" : "Mercado") + " · " +
        item.created_at.slice(0,19).replace("T"," ") + " · " + item.id.slice(0,8);
    }
    async function loadPage() {
      if (!run || busy || !hasMore) return;
      var signal = begin();
      busy = true; controls();
      status.textContent = "Cargando pruebas…";
      try {
        var page = await api("/perp/runs?offset=" + offset + "&limit=25", {signal:signal});
        if (!active(signal)) return;
        if (!Array.isArray(page.items) || page.offset !== offset || !Number.isInteger(page.total))
          throw new Error("Biblioteca de pruebas no verificable");
        page.items.forEach(function (item) {
          if (item.id === run.id || candidates.has(item.id)) return;
          candidates.set(item.id, item);
          select.add(new Option(label(item), item.id));
        });
        offset += page.items.length;
        hasMore = page.items.length > 0 && offset < page.total;
        status.textContent = candidates.size ? "" : hasMore ? "Consulta más pruebas" : "Sin otra prueba guardada";
      } catch (error) {
        if (active(signal)) status.textContent = error.message;
      } finally {
        if (active(signal)) { busy = false; controls(); }
      }
    }
    function addRow(label, values) {
      var tr = node("tr"), name = node("th", label);
      name.scope = "row"; tr.append(name);
      values.forEach(function (value) {
        var cell = node("td", value == null ? "—" : typeof value === "boolean" ? value ? "Sí" : "No" :
          typeof value === "number" ? String(value) : /^-?\d+(?:\.\d+)?$/.test(value) ? format(value) : value);
        if (value != null) cell.title = String(value);
        tr.append(cell);
      });
      body.append(tr);
    }
    function draw(plot) {
      if (!plot) return;
      if (!Number.isSafeInteger(plot.start_t) || plot.start_t <= 0 || !Number.isSafeInteger(plot.interval_seconds) ||
          plot.interval_seconds <= 0 || !Array.isArray(plot.base) || !Array.isArray(plot.other) ||
          plot.base.length !== plot.other.length || plot.base.length < 2 || plot.base.length > 5001)
        throw new Error("Curvas de capital no verificables");
      var series = [plot.base,plot.other].map(function (values) {
        return values.map(function (raw, index) {
          if (typeof raw !== "string" || !/^-?\d+(?:\.\d+)?$/.test(raw) || !Number.isFinite(Number(raw)))
            throw new Error("Capital no verificable");
          return {time:plot.start_t + index * plot.interval_seconds, value:Number(raw)};
        });
      });
      var lw = window.LightweightCharts;
      chartBox.hidden = legend.hidden = false;
      chart = lw.createChart(chartBox, {autoSize:true, width:chartBox.clientWidth, height:chartBox.clientHeight,
        layout:{background:{type:"solid",color:"#101b18"},textColor:"#c9d6ce"},
        grid:{vertLines:{color:"#24362f"},horzLines:{color:"#24362f"}},
        timeScale:{timeVisible:true,minBarSpacing:0.1}});
      chart.addSeries(lw.LineSeries,{color:"#42c88a",lineWidth:2,title:"Base"}).setData(series[0]);
      chart.addSeries(lw.LineSeries,{color:"#8eb8ff",lineWidth:2,title:"Alternativa"}).setData(series[1]);
      chartBox.dataset.points = String(plot.base.length);
      if (window.OrbisChartRange) window.OrbisChartRange.mount(range, chart, series[0],
        {selected:"all",storageKey:"orbis.chartRange.perp-comparison"});
      else chart.timeScale().fitContent();
    }
    function show(value) {
      var first = value.base, second = value.other;
      addRow("Versión", [versionLabel(first.strategy_version_id), versionLabel(second.strategy_version_id), null]);
      addRow("Mercado", [first.symbol + " · " + first.timeframe, second.symbol + " · " + second.timeframe, null]);
      addRow("Periodo UTC", [first,second].map(function (item) {
        return utc(item.window.requested_start_ns) + " → " + utc(item.window.requested_end_ns);
      }).concat(null));
      addRow("Estado", [first,second].map(function (item) {
        var state = item.status === "COMPLETE" ? "Completa" : "Interrumpida";
        return state + (item.warnings.includes("SMALL_SAMPLE") ? " · Muestra pequeña" : "");
      }).concat(null));
      metrics.forEach(function (item) {
        addRow(item[0], [first.metrics[item[1]], second.metrics[item[1]], value.deltas && value.deltas[item[1]]]);
      });
      draw(value.equity);
      table.hidden = false;
      report = value;
      var changes = value.strategy_fields_changed.concat(value.execution_parameters_changed).map(function (key) {
        return changeLabels[key] || key;
      });
      status.textContent = value.difference_blockers.length ?
        "Diferencias sin calcular · " + value.difference_blockers.map(function (key) { return reasonLabels[key] || key; }).join(", ") :
        "Condiciones equivalentes" + (changes.length ? " · Cambios en " + changes.join(", ") : "");
    }
    async function calculate(download) {
      var other = candidates.get(select.value);
      if (busy || !run || !other || download && !report) return;
      if (download && !window.confirm("¿Exportar la comparación LAB a un JSON sin cifrar?")) return;
      var signal = begin(), expected = download ? report.hash : null, base = run;
      busy = true; clearResult(); controls(); status.textContent = "Reproduciendo las dos pruebas…";
      var request = {base_id:base.id,base_hash:base.hash,other_id:other.id,other_hash:other.hash};
      if (expected) request.expected_hash = expected;
      try {
        var value = await api("/perp/runs/compare",{method:"POST",body:JSON.stringify(request),signal:signal});
        if (!active(signal)) return;
        if (value.schema !== "orbis.lab.perp.run-comparison.v1" || value.environment !== "LAB" ||
            value.execution_authority !== "NONE" || value.base.id !== base.id || value.base.hash !== base.hash ||
            value.other.id !== other.id || value.other.hash !== other.hash || !/^[a-f0-9]{64}$/.test(value.hash) ||
            expected && value.hash !== expected || !Array.isArray(value.difference_blockers) ||
            (value.difference_blockers.length ? value.deltas !== null || value.equity !== null : !value.deltas || !value.equity))
          throw new Error("Comparación de pruebas no verificable");
        show(value);
        if (download) {
          var url = URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:"application/json"}));
          var link = node("a"); link.href = url; link.download = "orbis-perp-comparacion-" + value.hash.slice(0,12) + ".json";
          link.click();
          setTimeout(function () { URL.revokeObjectURL(url); },1000);
        }
      } catch (error) {
        if (active(signal)) { clearResult(); status.textContent = error.message; }
      } finally {
        if (active(signal)) { busy = false; controls(); }
      }
    }

    panel.addEventListener("toggle",function () { if (panel.open) loadPage(); else reset(); });
    outer.addEventListener("toggle",function () { if (!outer.open) { panel.open = false; reset(); } });
    select.addEventListener("change",function () { clearResult(); status.textContent = ""; controls(); });
    more.addEventListener("click",loadPage);
    compare.addEventListener("click",function () { calculate(false); });
    exportButton.addEventListener("click",function () { calculate(true); });
    return {setRun:function (selected) { reset(); panel.open = false; run = selected; }};
  }
  window.OrbisPerpRunComparison = {mount:mount};
})();
