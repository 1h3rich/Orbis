/* Compare two archived rule captures with the same replayed LAB run. */
(function () {
  "use strict";
  var metrics = [
    ["Capital final · USDT", "final_equity"],
    ["Operaciones", "trades"],
    ["Comisiones · USDT", "fees_paid"],
    ["Financiación · USDT", "funding_paid"],
    ["Drawdown máx. %", "max_drawdown_pct"],
    ["Liquidación", "liquidated"]
  ];
  function mount(panel, api, utc, format) {
    var select = panel.querySelector("select"), status = panel.querySelector("[role=status]");
    var coverage = panel.querySelector("[data-rules-window]");
    var more = panel.querySelector("[data-rules-more]"), compare = panel.querySelector("[data-rules-compare]");
    var exportButton = panel.querySelector("[data-rules-export]"), table = panel.querySelector("table");
    var body = table.querySelector("tbody"), run = null, cursor = null, report = null, busy = false;
    var controller = new AbortController();
    function begin() {
      controller.abort();
      controller = new AbortController();
      return controller.signal;
    }
    function active(signal) { return run && panel.isConnected && panel.open && !signal.aborted; }
    function controls() {
      select.disabled = busy || select.options.length < 2;
      more.hidden = !cursor;
      more.disabled = busy;
      compare.disabled = busy || !select.value;
      exportButton.disabled = busy || !report;
    }
    function clearResult() {
      report = null;
      body.replaceChildren();
      table.hidden = true;
    }
    function reset() {
      begin();
      cursor = null;
      busy = false;
      select.replaceChildren(new Option("Selecciona captura", ""));
      status.textContent = "";
      coverage.replaceChildren();
      clearResult();
      controls();
    }
    function show(value) {
      report = value;
      var original = value.original, alternative = value.alternative;
      function row(label, a, b) {
        var tr = document.createElement("tr"), heading = document.createElement("th");
        heading.scope = "row";
        heading.textContent = label;
        tr.append(heading);
        [a,b].forEach(function (item) {
          var cell = document.createElement("td");
          cell.textContent = item;
          tr.append(cell);
        });
        body.append(tr);
      }
      function metric(scenario, key) {
        var number = scenario.metrics[key];
        if (typeof number === "boolean") return number ? "Sí" : "No";
        return typeof number === "number" ? String(number) : format(number);
      }
      body.replaceChildren();
      row("Captura UTC", utc(original.observed_at_ns), utc(alternative.observed_at_ns));
      row("Estado", original.status === "COMPLETE" ? "Completa" : "Interrumpida",
        alternative.status === "COMPLETE" ? "Completa" : "Interrumpida");
      metrics.forEach(function (item) { row(item[0], metric(original,item[1]), metric(alternative,item[1])); });
      row("Órdenes rechazadas", original.rejected_orders, alternative.rejected_orders);
      table.hidden = false;
      status.textContent = value.final_equity_difference === null ? "Tramos incomparables · revisa el estado." :
        "Diferencia de capital · " + format(value.final_equity_difference) + " USDT";
    }
    function addCapture(item, prefix) {
      if (Array.from(select.options).some(function (option) { return option.value === item.hash; })) return;
      select.add(new Option((prefix || "") + utc(item.observed_at_ns) + " UTC · " + item.hash.slice(0,8), item.hash));
    }
    function showWindow(report) {
      if (report.schema !== "orbis.lab.perp.rule-window.v1" || report.original_run.id !== run.id ||
          report.original_run.hash !== run.hash) throw new Error("El periodo pertenece a otra prueba.");
      var observed = report.within, positions = {BEFORE:"anterior", WITHIN:"dentro del periodo", AFTER:"posterior"};
      function line(text) {
        var item = document.createElement("p");
        item.textContent = text;
        coverage.append(item);
      }
      function boundary(point) {
        if (!point) return "—";
        var description = point.changes === null ? "ambigua" : point.changes.length ?
          point.changes.map(function (key) { return key === "rules" ? "margen distinto" : "instrumento distinto"; }).join(", ") : "coincide";
        return utc(point.observed_at_ns) + " (" + description + ")";
      }
      coverage.replaceChildren();
      line("Reglas de la prueba · " + utc(report.assumed.observed_at_ns) + " UTC · " + positions[report.assumed.location]);
      var within = observed.capture_count ? "Coincidencias " + observed.matching_instants + " · Diferencias " +
        observed.different_instants + " · Ambiguas " + observed.ambiguous_instants : "Sin capturas durante la prueba";
      line("Observaciones puntuales · " + within);
      if (observed.first && observed.last) {
        line("Dentro · " + boundary(observed.first) +
          (observed.first.observed_at_ns === observed.last.observed_at_ns ? "" : " → " + boundary(observed.last)));
      }
      line("Anterior · " + boundary(report.before) + "   |   Posterior · " + boundary(report.after));
      if (observed.latest_difference) addCapture(observed.latest_difference, "Durante · ");
    }
    async function loadPage() {
      if (!run || busy) return;
      var signal = begin(), query = new URLSearchParams({symbol:run.symbol});
      var windowRequest = cursor ? null : api("/perp/runs/" + encodeURIComponent(run.id) +
        "/rule-window?run_hash=" + encodeURIComponent(run.hash), {signal:signal});
      if (cursor) { query.set("before_ns", cursor.ns); query.set("before_hash", cursor.hash); }
      busy = true;
      controls();
      try {
        var results = await Promise.allSettled([api("/perp/rules?" + query, {signal:signal}), windowRequest]);
        if (!active(signal)) return;
        if (results[0].status === "rejected") throw results[0].reason;
        if (windowRequest) {
          try {
            if (results[1].status === "rejected") throw results[1].reason;
            showWindow(results[1].value);
          } catch (error) { coverage.textContent = error.message; }
        }
        var page = results[0].value;
        page.items.forEach(function (item) { addCapture(item); });
        cursor = page.next_before;
        if (!report) status.textContent = select.options.length > 1 ? "" : "Sin capturas guardadas.";
      } catch (error) {
        if (active(signal)) {
          if (error.status === 423) reset();
          status.textContent = error.message;
        }
      } finally {
        if (active(signal)) { busy = false; controls(); }
      }
    }
    async function calculate(download) {
      if (busy || !run || !select.value || download && !report) return;
      if (download && !window.confirm("El JSON descargado queda sin cifrar. ¿Exportar la comparación?")) return;
      var signal = begin(), expected = download ? report.hash : null;
      busy = true;
      clearResult();
      controls();
      status.textContent = "Reproduciendo escenarios…";
      try {
        var value = await api("/perp/runs/" + encodeURIComponent(run.id) + "/compare-rules", {
          method:"POST", body:JSON.stringify({observation_hash:select.value}), signal:signal
        });
        if (!active(signal)) return;
        if (value.schema !== "orbis.lab.perp.rule-comparison.v1" || value.original_run.id !== run.id ||
            value.original_run.hash !== run.hash || value.alternative.observation_hash !== select.value ||
            expected && value.hash !== expected) throw new Error("La comparación cambió. Vuelve a calcularla.");
        show(value);
        if (download) {
          var url = URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:"application/json"}));
          var link = document.createElement("a");
          link.href = url;
          link.download = "orbis-perp-reglas-" + value.hash.slice(0,12) + ".json";
          link.click();
          setTimeout(function () { URL.revokeObjectURL(url); },1000);
        }
      } catch (error) {
        if (active(signal)) {
          if (error.status === 423) reset();
          status.textContent = error.message;
        }
      } finally {
        if (active(signal)) { busy = false; controls(); }
      }
    }
    panel.addEventListener("toggle",function () { if (panel.open) loadPage(); else reset(); });
    select.addEventListener("change",function () { clearResult(); status.textContent = ""; controls(); });
    more.addEventListener("click",loadPage);
    compare.addEventListener("click",function () { calculate(false); });
    exportButton.addEventListener("click",function () { calculate(true); });
    return {setRun:function (selected) { reset(); panel.open = false; run = selected; }};
  }
  window.OrbisPerpRuleComparison = {mount:mount};
})();
