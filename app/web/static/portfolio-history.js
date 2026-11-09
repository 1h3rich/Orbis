/* Daily valuation view. Results exist only in the current, unmasked view. */
(function () {
  "use strict";
  var DAY = 86400, PAGE_ROWS = 100, MAX_LINE_SEGMENTS = 32;

  function mount(panel, requestHistory, baseCurrency, hidden) {
    var form = panel.querySelector("form");
    var status = panel.querySelector("[data-history-status]");
    var result = panel.querySelector("[data-history-result]");
    var currency = form.elements.currency;
    var submit = form.querySelector("button");
    var chart = null, pending = null, disposed = false;

    function setCurrencies(currencies) {
      var available = Array.from(new Set([baseCurrency].concat(currencies)));
      if (available.join(",") === Array.from(currency.options, function (option) { return option.value; }).join(",")) return;
      var previous = currency.value;
      currency.replaceChildren.apply(currency, available.map(function (code) { return new Option(code, code); }));
      currency.value = available.includes(previous) ? previous : baseCurrency;
    }

    function reset() {
      if (pending) pending.abort();
      pending = null;
      if (chart) chart.remove();
      chart = null;
      result.replaceChildren();
      status.textContent = hidden ? "Importes ocultos" : "";
      submit.disabled = hidden;
    }

    function draw(container, rows, unit) {
      var LW = window.LightweightCharts;
      if (!LW) return;
      var groups = [], segment = null;
      rows.forEach(function (row) {
        if (row.equity === null || !Number.isFinite(Number(row.equity))) {
          segment = null;
          return;
        }
        if (!segment) { segment = []; groups.push(segment); }
        segment.push({ time: row.date_t, value: Number(row.equity) });
      });
      if (!groups.length) return;
      var selector = document.createElement("div");
      container.appendChild(selector);
      var host = document.createElement("div");
      host.className = "portfolio-history-chart";
      host.setAttribute("role", "img");
      host.setAttribute("aria-label", "Patrimonio diario en " + unit);
      container.appendChild(host);
      chart = LW.createChart(host, {
        autoSize: true,
        layout: { background: { color: "#101917" }, textColor: "#a8bbb3", attributionLogo: true },
        grid: { vertLines: { color: "#22322d" }, horzLines: { color: "#22322d" } },
        rightPriceScale: { borderColor: "#394e46" },
        timeScale: { borderColor: "#394e46", lockVisibleTimeRangeOnResize: true, minBarSpacing: 0.05 }
      });
      var range = rows.map(function (row) { return {time: row.date_t}; });
      var pointsOnly = groups.length > MAX_LINE_SEGMENTS;
      if (pointsOnly) groups = [groups.flat()];
      groups.forEach(function (points, index) {
        var series = chart.addSeries(LW.LineSeries, {
          color: "#56d399", lineWidth: 2, lastValueVisible: false, priceLineVisible: false,
          lineVisible: !pointsOnly, pointMarkersVisible: pointsOnly || points.length <= 7,
          pointMarkersRadius: pointsOnly ? 2 : 3
        });
        if (index === 0) {
          var values = new Map(points.map(function (point) { return [point.time, point]; }));
          series.setData(range.map(function (point) { return values.get(point.time) || point; }));
        } else {
          series.setData(points);
        }
      });
      if (rows.length > 1 && window.OrbisChartRange) {
        window.OrbisChartRange.mount(selector, chart, range, {kind:"portfolio", selected:"all", lastCounts:[30,90]});
      } else {
        chart.timeScale().setVisibleLogicalRange({from: -0.5, to: rows.length - 0.5});
      }
    }

    function appendRows(rows) {
      var details = document.createElement("details");
      details.className = "portfolio-history-table";
      var summary = document.createElement("summary");
      summary.textContent = "Valores diarios";
      details.appendChild(summary);
      var wrap = document.createElement("div");
      wrap.className = "tablewrap";
      wrap.tabIndex = 0;
      wrap.setAttribute("role", "region");
      wrap.setAttribute("aria-label", "Desplazar valores diarios");
      var table = document.createElement("table");
      table.setAttribute("aria-label", "Valores al cierre UTC");
      var headers = table.createTHead().insertRow();
      ["Día UTC", "Patrimonio", "Efectivo", "Realizado", "No realizado", "Resultado",
        "Aportaciones", "Retiradas", "Cobertura"].forEach(function (label) {
        var th = document.createElement("th");
        th.scope = "col";
        th.textContent = label;
        headers.appendChild(th);
      });
      var body = table.createTBody();
      var page = 0, ordered = rows.slice().reverse();
      var pages = Math.ceil(ordered.length / PAGE_ROWS);
      var pagination = document.createElement("div");
      pagination.className = "row";
      pagination.hidden = pages <= 1;
      var previous = document.createElement("button"), next = document.createElement("button");
      previous.type = next.type = "button";
      previous.className = next.className = "ghost";
      previous.textContent = "Anterior";
      next.textContent = "Siguiente";
      var position = document.createElement("span");
      position.className = "muted";
      position.setAttribute("aria-live", "polite");
      pagination.append(previous, position, next);
      previous.addEventListener("click", function () { page--; renderPage(); });
      next.addEventListener("click", function () { page++; renderPage(); });

      function renderPage() {
        body.replaceChildren();
        ordered.slice(page * PAGE_ROWS, (page + 1) * PAGE_ROWS).forEach(appendRow);
        wrap.scrollTop = 0;
        position.textContent = (page + 1) + " / " + pages;
        previous.disabled = page === 0;
        next.disabled = page + 1 === pages;
      }
      function appendRow(row) {
        var tr = body.insertRow();
        tr.dataset.day = new Date(row.date_t * 1000).toISOString().slice(0, 10);
        tr.insertCell().textContent = tr.dataset.day;
        ["equity", "cash", "realized_pnl", "unrealized_pnl", "total_pnl",
          "contributions", "withdrawals"].forEach(function (key) {
          var cell = tr.insertCell();
          cell.className = "num";
          cell.textContent = row[key] === null ? "—" : row[key];
        });
        var issues = [];
        if (row.negative_cash) issues.push("Efectivo negativo");
        if (row.missing_assets.length) issues.push("Sin cierre · " + row.missing_assets.join(", "));
        tr.insertCell().textContent = issues.length ? issues.join(" · ") : "Completa";
      }
      renderPage();
      wrap.appendChild(table);
      details.append(pagination, wrap);
      result.appendChild(details);
    }

    function render(data) {
      if (!data.rows.length) {
        status.textContent = "Sin movimientos para esta divisa al cierre del periodo.";
        return;
      }
      var missing = data.rows.filter(function (row) { return row.equity === null; }).length;
      var period = data.currency + " · " + data.provider.toUpperCase() + " · Cierres UTC";
      status.textContent = missing ? period + " · " + missing + (missing === 1 ? " día incompleto" : " días incompletos") : period;
      draw(result, data.rows, data.currency);
      appendRows(data.rows);
    }

    function selectAccount(code) {
      baseCurrency = code;
      currency.replaceChildren();
      setCurrencies([]);
      reset();
    }

    form.hidden = hidden;
    setCurrencies([]);
    reset();
    form.addEventListener("change", reset);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (hidden || disposed || pending) return;
      reset();
      var controller = new AbortController();
      pending = controller;
      submit.disabled = true;
      status.textContent = "Consultando cierres diarios…";
      var params = new URLSearchParams(new FormData(form));
      var years = /^(5|10)y$/.exec(params.get("days"));
      if (years) {
        var today = Math.floor(Date.now() / (DAY * 1000)) * DAY;
        params.set("days", String((today - window.OrbisChartRange.yearsBeforeDay(today, Number(years[1]))) / DAY));
      }
      function current() { return pending === controller && panel.isConnected && !disposed; }
      requestHistory(params, controller.signal).then(function (data) {
        if (current()) render(data);
      }).catch(function (error) {
        if (current()) status.textContent = error.message;
      }).finally(function () {
        if (!current()) return;
        pending = null;
        submit.disabled = false;
      });
    });
    return {
      setCurrencies: setCurrencies,
      selectAccount: selectAccount,
      dispose: function () { disposed = true; reset(); }
    };
  }

  window.OrbisPortfolioHistory = { mount: mount };
})();
