/* Selector de rango sobre los datos que ya están cargados. */
(function () {
  "use strict";
  var coverageKinds = {
    candles:{label:"velas", empty:"velas ni operaciones cargadas", counted:"Últimas"},
    portfolio:{label:"días del libro", empty:"días del libro cargados", counted:"Últimos"},
    fills:{label:"ejecuciones", empty:"ejecuciones cargadas", counted:"Últimas"}
  };
  function timestamp(row) { return Number(row && (row.t === undefined ? row.time : row.t)); }
  function day(t) { return new Date(t * 1000).toISOString().slice(0, 10); }
  function yearsBeforeDay(t, years) {
    var end = new Date(t * 1000), year = end.getUTCFullYear() - years, month = end.getUTCMonth();
    var lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
    return Date.UTC(year, month, Math.min(end.getUTCDate(), lastDay)) / 1000;
  }
  function savedRange(key, modes, legacySpot) {
    if (typeof key !== "string" || !/^orbis\.chartRange\.[a-zA-Z0-9.-]+$/.test(key)) return null;
    try {
      var raw = localStorage.getItem(key), saved;
      try { saved = JSON.parse(raw); } catch (error) { saved = null; }
      if (saved && typeof saved === "object" && modes.includes(saved.mode)) {
        if (saved.mode !== "custom" || (/^\d{4}-\d{2}-\d{2}$/.test(saved.from || "") &&
            /^\d{4}-\d{2}-\d{2}$/.test(saved.to || "") && saved.from <= saved.to)) return saved;
      }
      // Spot PAPER guardaba un valor simple y las fechas en dos claves. Leerlo
      // conserva la elección existente; el siguiente cambio guarda el formato común.
      if (!legacySpot) return null;
      var legacyMode = {"100":"last:100", "500":"last:500", "1y":"1", "5y":"5", "10y":"10"}[raw] || raw;
      if (!modes.includes(legacyMode)) return null;
      if (legacyMode !== "custom") return {mode:legacyMode};
      var from = localStorage.getItem(key + ".from"), to = localStorage.getItem(key + ".to");
      return /^\d{4}-\d{2}-\d{2}$/.test(from || "") && /^\d{4}-\d{2}-\d{2}$/.test(to || "") &&
        from <= to ? {mode:"custom",from:from,to:to} : null;
    } catch (error) { return null; }
  }
  function mount(host, chart, rows, options) {
    options = options || {};
    if (!host || !chart || !Array.isArray(rows) || !rows.length) return;
    var coverage = Object.hasOwn(coverageKinds, options.kind) ? coverageKinds[options.kind] : coverageKinds.candles;
    var counts = (Array.isArray(options.lastCounts) ? options.lastCounts : [])
      .filter(function (count) { return Number.isInteger(count) && count > 0 && count <= 10000; });
    var modes = ["1", "5", "10", "all", "custom"].concat(counts.map(function (count) { return "last:" + count; }));
    var coverageTimes = rows.map(timestamp).filter(Number.isFinite).sort(function (a, b) { return a - b; });
    if (coverageTimes.length < 2) return;
    var times = Array.from(new Set(coverageTimes.concat((options.eventTimes || []).map(Number)
      .filter(Number.isFinite)))).sort(function (a, b) { return a - b; });
    var first = coverageTimes[0], coverageLast = coverageTimes[coverageTimes.length - 1];
    var last = times[times.length - 1];
    var coverageEnd = Number.isFinite(options.coverageEnd) && options.coverageEnd > coverageLast ? options.coverageEnd : null;
    var extraNote = (times[0] < first ? " · operaciones desde " + day(times[0]) + " (sin OHLC)" : "")+
      ((coverageEnd === null ? last > coverageLast : last >= coverageEnd) ? " · operaciones hasta " + day(last) + " (sin OHLC)" : "");
    host.innerHTML = '<div class="chart-range-control">' +
      '<label class="field" data-help="Ajusta el rango visible dentro de los datos cargados.">Rango <select aria-label="Rango visible del gráfico">' +
      counts.map(function (count) { return '<option value="last:' + count + '">' + coverage.counted + ' ' + count + '</option>'; }).join("") +
      '<option value="1">1 año</option><option value="5">5 años</option><option value="10">10 años</option><option value="all">Todo disponible</option><option value="custom">Fechas…</option></select></label>' +
      '<button type="button" class="ghost chart-range-edit" data-range-edit hidden aria-expanded="false">Cambiar fechas</button>' +
      '<div class="chart-custom-dates" data-range-dates hidden role="group" aria-label="Elegir fechas del gráfico">' +
      '<div class="chart-range-fields"><label class="field">Desde <input type="date" data-range-from aria-label="Desde fecha"></label>' +
      '<label class="field">Hasta <input type="date" data-range-to aria-label="Hasta fecha"></label></div>' +
      '<span class="amber" data-range-error role="status" hidden></span>' +
      '<div class="chart-range-actions"><button type="button" class="ghost" data-range-cancel>Cancelar</button>' +
      '<button type="button" data-range-apply>Aplicar</button></div></div></div>' +
      '<span class="muted" data-range-coverage role="status"></span>';
    var select = host.querySelector("select"), note = host.querySelector("[data-range-coverage]");
    var dates = host.querySelector("[data-range-dates]");
    var fromInput = host.querySelector("[data-range-from]");
    var toInput = host.querySelector("[data-range-to]");
    var edit = host.querySelector("[data-range-edit]");
    var error = host.querySelector("[data-range-error]");
    var remembered = savedRange(options.storageKey, modes, options.legacySpot);
    var applied = remembered ? remembered.mode :
      (modes.includes(options.selected) ? options.selected : "all");
    fromInput.min = toInput.min = day(times[0]);
    fromInput.max = toInput.max = day(last);
    fromInput.value = remembered && remembered.mode === "custom" ? remembered.from : day(times[0]);
    toInput.value = remembered && remembered.mode === "custom" ? remembered.to : day(last);
    var committedFrom = fromInput.value, committedTo = toInput.value;
    select.value = applied;

    function save(mode) {
      if (!options.storageKey || !/^orbis\.chartRange\.[a-zA-Z0-9.-]+$/.test(options.storageKey)) return;
      var value = mode === "custom" ? {mode:mode, from:fromInput.value, to:toInput.value} : {mode:mode};
      try { localStorage.setItem(options.storageKey, JSON.stringify(value)); } catch (error) {}
    }

    function apply(mode) {
      if (mode === "custom") {
        var fromDay = fromInput.value, toDay = toInput.value;
        if (!fromDay || !toDay || fromDay > toDay) {
          error.textContent = "Elige dos fechas en orden.";
          error.hidden = false;
          return false;
        }
        var fromRequested = Date.parse(fromDay + "T00:00:00Z") / 1000;
        var toRequested = Date.parse(toDay + "T23:59:59Z") / 1000;
        if (!Number.isFinite(fromRequested) || !Number.isFinite(toRequested)) {
          error.textContent = "Las fechas no son válidas.";
          error.hidden = false;
          return false;
        }
        var visible = times.filter(function (time) { return time >= fromRequested && time <= toRequested; });
        if (!visible.length) {
          error.textContent = "No hay " + coverage.empty + " entre esas fechas.";
          error.hidden = false;
          return false;
        }
        if (visible.length === 1) {
          var index = times.indexOf(visible[0]);
          chart.timeScale().setVisibleLogicalRange({from:index - 0.5, to:index + 0.5});
        } else {
          chart.timeScale().setVisibleRange({from:visible[0], to:visible[visible.length - 1]});
        }
        note.textContent = "Visible: " + day(visible[0]) + " → " + day(visible[visible.length - 1]) +
          ". Cobertura de " + coverage.label + ": " + day(first) + " → " + day(coverageLast) + extraNote + ".";
      } else if (mode === "all") {
        chart.timeScale().fitContent();
        note.textContent = "Cobertura: " + day(first) + " → " + day(coverageLast) + extraNote + ".";
      } else if (mode.startsWith("last:")) {
        var count = Number(mode.slice(5));
        chart.timeScale().setVisibleLogicalRange({from:Math.max(0, times.length - count), to:times.length - 1});
        note.textContent = "Tramo visible: " + Math.min(count, times.length) + " de " + times.length +
          " puntos. Cobertura de " + coverage.label + ": " + day(first) + " → " + day(coverageLast) + extraNote + ".";
      } else {
        var requested = yearsBeforeDay(last, Number(mode));
        var from = times.find(function (time) { return time >= requested; });
        chart.timeScale().setVisibleRange({from:from === undefined ? first : from, to:last});
        note.textContent = (first > requested + 7 * 86400 ? "Cobertura parcial: " : "Cobertura: ") +
          day(first) + " → " + day(coverageLast) + extraNote + ".";
      }
      host.dataset.view = mode;
      error.hidden = true;
      return true;
    }
    function close() {
      dates.hidden = true;
      edit.setAttribute("aria-expanded", "false");
    }
    function open() {
      error.hidden = true;
      dates.hidden = false;
      edit.hidden = false;
      edit.textContent = applied === "custom" ? "Cambiar fechas" : "Elegir fechas";
      edit.setAttribute("aria-expanded", "true");
      fromInput.focus();
    }
    function cancel() {
      close();
      select.value = applied;
      edit.hidden = applied !== "custom";
      fromInput.value = committedFrom;
      toInput.value = committedTo;
    }
    select.addEventListener("change", function () {
      if (select.value === "custom") {
        // El valor visible continúa describiendo el periodo ya aplicado.
        select.value = applied;
        open();
        return;
      }
      var next = select.value;
      save(next);
      if (options.onChange && options.onChange(next) === true) return;
      applied = next;
      edit.hidden = true;
      close();
      apply(next);
    });
    edit.addEventListener("click", open);
    host.querySelector("[data-range-cancel]").addEventListener("click", cancel);
    host.querySelector("[data-range-apply]").addEventListener("click", function () {
      if (!apply("custom")) return;
      committedFrom = fromInput.value;
      committedTo = toInput.value;
      applied = "custom";
      select.value = "custom";
      save("custom");
      edit.textContent = "Cambiar fechas";
      close();
    });
    dates.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        cancel();
        select.focus();
      }
    });
    edit.hidden = applied !== "custom";
    if (!apply(applied)) {
      applied = "all";
      select.value = "all";
      edit.hidden = true;
      apply("all");
      note.textContent = "Fechas guardadas fuera de la cobertura. " + note.textContent;
    }
  }
  window.OrbisChartRange = {mount:mount, yearsBeforeDay:yearsBeforeDay};
})();
