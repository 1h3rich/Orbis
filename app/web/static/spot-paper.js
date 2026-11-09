/* Estrategias LONG Spot PAPER: capital ficticio por sesión, sin órdenes reales. */
(function () {
  "use strict";
  var root = null, timer = null, chart = null, chartKey = "", chartTimes = [], inspectedTime = null;
  var chartEvents = {}, chartActions = {}, executionByCandle = {};
  var datasets = [], strategies = [], versions = [], sessions = [];
  var generation = 0, chartSeq = 0, comparisonSeq = 0, gapSeq = 0, detailSeq = 0, summarySeq = 0;
  var pendingSelection = null;
  var sourceRunSelection = null;
  var datasetReady = false, readinessSeq = 0, spotWorkerRunning = false, startBusy = false, freshenBusy = false;
  var lineageForId = "", lineageSeq = 0;
  var verifiedOriginSessionId = "";
  var renderedDetailSignature = "";
  var displayedDetailId = "", displayedEventCount = -1, reserveComparison = null;
  var exportRequest = null;
  function detailSignature(item) {
    if (!item) return "";
    return JSON.stringify([item.id, item.last_t, item.status, item.status_reason,
      item.state, item.last_quote, item.valuation]);
  }
  function esc(value) { return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  }); }
  function amount(value, digits) {
    var number = Number(value);
    return Number.isFinite(number) ? "≈ " + new Intl.NumberFormat("es-ES", {maximumFractionDigits: digits || 4}).format(number) : "—";
  }
  function frequencyMarkup(data) {
    return window.OrbisSignalFrequency.markup(data.signal_frequency);
  }
  function reasonLabel(code) {
    return {ENTRY_RULE:"Entrada",EXIT_RULE:"Regla de salida",STOP_CLOSE:"Stop al cierre",TARGET_CLOSE:"Objetivo al cierre",
      STOP_OBSERVED_BID:"Stop · bid observado",TARGET_OBSERVED_BID:"Objetivo · bid observado",
      TRAILING_STOP_CLOSE:"Stop móvil · cierre",TRAILING_STOP_OBSERVED_BID:"Stop móvil · bid",
      MANUAL_CLOSE:"Manual"}[code] || code || "—";
  }
  function nsSecond(value) {
    return typeof value === "string" && /^\d{1,19}$/.test(value) ? Number(value.slice(0, -9)) : null;
  }
  function exactUtc(value) {
    var second = nsSecond(value);
    return second === null ? "—" : new Date(second * 1000).toISOString().slice(0, 19).replace("T", " ") +
      "." + value.slice(-9).padStart(9, "0") + " UTC";
  }
  function inspectButton(time, label) {
    return '<button type="button" class="ghost spot-paper-inspect" data-inspect-time="' +
      esc(time) + '">' + esc(label) + '</button>';
  }
  function eventRows(events) {
    return events.map(function (event) { var action = event.action || {};
      var execution = action.side ? nsSecond(event.observed_at_ns_exact) : null;
      return '<tr><td>' + esc(new Date(event.observed_at_ns / 1000000).toLocaleString("es-ES")) + '</td><td>' +
        (event.entry_match ? (event.entry_status === "SKIPPED_POSITION_OPEN" ? 'SEÑAL SIN ENTRADA' : 'SEÑAL LONG') :
          event.exit_match ? 'SEÑAL SALIDA' : '—') + ' ' +
        (event.kind === "OBSERVATION" ? inspectButton(event.candle_t, "Reglas") : '') + '</td><td>' + esc(action.side || '—') +
        (execution === null ? '' : ' ' + inspectButton(execution, "Ver ejecución")) +
        '</td><td>' + esc(action.price || '—') + '</td><td title="' + esc(action.reason || '') + '">' +
        esc(reasonLabel(action.reason)) + '</td></tr>';
    }).join("");
  }
  function coverageMarkup(coverage) {
    var rows = coverage && coverage.rules || [];
    if (!rows.length) return "";
    return '<div class="paper-coverage" aria-label="Cobertura de condiciones superiores">' + rows.map(function (row) {
      var warming = row.status === "INSUFFICIENT";
      var path = row.path.indexOf("exit") === 0 ? "Salida" : "Entrada";
      var count = row.minimum_bars == null ? String(row.complete_bars) + " velas" :
        String(row.complete_bars) + "/" + String(row.minimum_bars);
      var help = warming ? "Aún no hay velas superiores completas suficientes para evaluar esta condición. No equivale a una señal fallida." :
        row.status === "COVERAGE_ONLY" ? "La estructura no tiene un mínimo fijo que garantice que pueda evaluarse." :
          "El indicador ya tiene calentamiento suficiente; no garantiza que se produzca una señal.";
      return '<span class="paper-coverage-chip" data-status="' + esc(row.status) + '" data-help="' +
        esc(path + ' · ' + row.kind + '. ' + help) + '">' + esc(path) + ' · ' + esc(row.timeframe) +
        ' ' + esc(count) + (warming ? ' · calentando' : row.status === "COVERAGE_ONLY" ? ' · cobertura' : ' · evaluable') + '</span>';
    }).join("") + '</div>';
  }
  function request(path, options) {
    return fetch(path, Object.assign({headers:{"Content-Type":"application/json"}}, options || {})).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) throw new Error(body.detail || "No se pudo consultar PAPER spot");
        return body;
      });
    });
  }
  function mountReserve(panel) {
    var select = panel.querySelector("select"), button = panel.querySelector("button");
    var status = panel.querySelector("[role=status]"), table = panel.querySelector("table");
    var header = table.querySelector("thead tr"), body = table.querySelector("tbody");
    var session = null, pending = null;

    function reset() {
      if (pending) pending.abort();
      pending = null;
      table.hidden = true;
      status.textContent = "";
      button.disabled = !select.value;
    }
    function begin() { reset(); pending = new AbortController(); return pending; }
    function active(current) { return pending === current && panel.isConnected && panel.open; }

    async function load() {
      if (!session) return;
      select.replaceChildren(new Option("Selecciona reserva", ""));
      var current = begin();
      status.textContent = "Buscando reservas…";
      try {
        var rows = await request("/lab/validations", {cache:"no-store", signal:current.signal});
        if (!active(current)) return;
        rows.filter(function (row) {
          var dataset = datasets.find(function (item) { return item.id === row.dataset_id; });
          return row.version_id === session.version_id && dataset && dataset.symbol === session.symbol &&
            dataset.timeframe === session.timeframe && dataset.source === "mexc-spot-public" &&
            dataset.end_t < nsSecond(String(session.started_at_ns));
        }).forEach(function (row) {
          var label = row.method === "ROLLING_FIXED_VERSION" ? "Ventanas sucesivas" : "Reserva cronológica";
          select.add(new Option(label + " · " + row.created_at.slice(0, 10) + " · " + row.id.slice(0, 8), row.id));
        });
        if (select.options.length === 2) select.selectedIndex = 1;
        status.textContent = select.options.length > 1 ? "" : "Sin reservas compatibles · créalas en Laboratorio.";
      } catch (error) {
        if (active(current)) status.textContent = error.message;
      } finally {
        if (pending === current) { pending = null; button.disabled = !select.value; }
      }
    }
    function utc(t) { return new Date(t * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC"; }
    function show(report) {
      var observed = report.paper, reserves = report.reserves;
      function row(label, values, numeric) {
        return '<tr><th scope="row">' + esc(label) + '</th>' + values.map(function (value) {
          return '<td' + (numeric && value != null ? ' title="' + esc(value) + '"' : '') + '>' +
            esc(value == null ? "—" : numeric ? amount(value, 4) : value) + '</td>';
        }).join("") + '</tr>';
      }
      header.innerHTML = '<th scope="col">Métrica</th><th scope="col">PAPER</th>' + reserves.map(function (_, i) {
        return '<th scope="col">LAB · Reserva' + (reserves.length > 1 ? ' ' + (i + 1) : '') + '</th>';
      }).join("");
      body.innerHTML = row("Desde", [utc(nsSecond(observed.started_at_ns))].concat(reserves.map(function (r) { return utc(r.from_t); }))) +
        row("Hasta", [utc(nsSecond(observed.through_ns))].concat(reserves.map(function (r) { return utc(r.end_t); }))) +
        row("Velas del tramo", [observed.observed_candles].concat(reserves.map(function (r) { return r.bars; }))) +
        [["Capital inicial · USDT","initial_capital"],["Capital final · USDT","equity"],
          ["P/L realizado · USDT","realized_pnl"],["Comisiones · USDT","fees_paid"],
          ["Rentabilidad %","return_pct"],["Operaciones cerradas","closed_trades"]].map(function (metric) {
          return row(metric[0], [observed.metrics[metric[1]]].concat(reserves.map(function (r) {
            return r.metrics[metric[1]];
          })), metric[1] !== "closed_trades");
        }).join("");
      status.textContent = [observed.observed_candles ? "Periodos y ejecución distintos" : "Sin velas PAPER observadas",
        observed.position_open ? "Posición abierta" : "", observed.status === "GAP" ? "Hueco PAPER" : "",
        [observed].concat(reserves).some(function (item) { return item.metrics.closed_trades < 30; }) ? "Muestra pequeña" : "",
        report.saved_before_paper ? "" : "Reserva guardada después de iniciar PAPER"].filter(Boolean).join(" · ");
      table.hidden = false;
    }
    async function compare() {
      if (!session || !select.value || pending) return;
      var id = session.id, validation = select.value, current = begin();
      button.disabled = true;
      status.textContent = "Reproduciendo reserva y señales…";
      try {
        var report = await request("/paper/spot/strategies/" + encodeURIComponent(id) +
          "/reserve-comparison?validation_id=" + encodeURIComponent(validation), {cache:"no-store", signal:current.signal});
        if (!active(current)) return;
        if (report.schema !== "orbis.paper.spot.reserve-comparison.v1" ||
            report.paper.session_id !== id || report.reference.artifact_id !== validation)
          throw new Error("La comparación corresponde a otra sesión.");
        show(report);
      } catch (error) {
        if (active(current)) status.textContent = error.message;
      } finally {
        if (pending === current) { pending = null; button.disabled = !select.value; }
      }
    }

    panel.addEventListener("toggle", function () { if (panel.open) load(); else reset(); });
    select.addEventListener("change", reset);
    button.addEventListener("click", compare);
    return {
      setSession: function (next) {
        if (session && next && session.id === next.id) return;
        session = next;
        panel.open = false;
        panel.hidden = !next;
        select.replaceChildren();
        reset();
      }
    };
  }
  function resetExport() {
    if (exportRequest) exportRequest.abort();
    exportRequest = null;
    if (!root) return;
    var state = root.querySelector("#spot-paper-export-state"), button = root.querySelector("#spot-paper-export");
    if (state) state.textContent = "";
    if (button) button.disabled = displayedEventCount < 1;
  }
  async function exportResearch() {
    var button = root.querySelector("#spot-paper-export");
    var id = root.querySelector("#spot-paper-session").value;
    if (!id || button.disabled || exportRequest) return;
    var state = root.querySelector("#spot-paper-export-state"), current = exportRequest = new AbortController();
    var path = "/paper/spot/strategies/" + encodeURIComponent(id) + "/research-export";
    var schema = "orbis.paper.spot.research.v1";
    function active() {
      return exportRequest === current && state.isConnected && root.querySelector("#spot-paper-session").value === id;
    }
    button.disabled = true;
    state.textContent = "Verificando el diario…";
    try {
      var preview = await request(path, {cache:"no-store", signal:current.signal});
      if (!active()) return;
      if (preview.schema !== schema || !/^[0-9a-f]{64}$/.test(preview.revision) ||
          !Number.isInteger(preview.event_count) || preview.event_count < 1 ||
          preview.record_count !== preview.event_count + 1) throw new Error("Diario de exportación no verificable.");
      if (!window.confirm("¿Descargar " + preview.event_count + " eventos Spot PAPER en un JSONL sin cifrar?")) {
        state.textContent = "";
        return;
      }
      var file = await request(path, {method:"POST", cache:"no-store", signal:current.signal,
        body:JSON.stringify({revision:preview.revision, confirmation:"EXPORTAR_JSONL_SPOT_PAPER"})});
      if (await window.OrbisResearchDownload.save(file, {
        schema:schema, revision:preview.revision, count:preview.record_count,
        filename:"orbis-spot-paper", maxBytes:12*1024*1024
      }, active)) state.textContent = preview.event_count + (preview.event_count === 1 ? " evento exportado" : " eventos exportados");
    } catch (error) {
      if (active()) state.textContent = error.message;
    } finally {
      if (active()) { exportRequest = null; button.disabled = displayedEventCount < 1; }
    }
  }
  function notice(message, bad) {
    var node = root && root.querySelector("#spot-paper-notice");
    if (!node) return;
    node.textContent = message;
    node.className = "state " + (bad ? "error" : "ok");
    node.hidden = false;
  }
  function render(container, selection) {
    cleanup();
    root = container;
    lineageForId = "";
    lineageSeq++;
    pendingSelection = selection && typeof selection.get === "function" ? {
      dataset_id: selection.get("dataset"), strategy_id: selection.get("strategy"),
      version_id: selection.get("version")
    } : null;
    sourceRunSelection = pendingSelection && selection.get("run") ? {
      run_id: selection.get("run"), dataset_id: pendingSelection.dataset_id,
      version_id: pendingSelection.version_id
    } : null;
    root.innerHTML = '<section class="card spot-paper-page"><div class="pagina-cabecera"><div>' +
      '<p class="real-eyebrow">ESTRATEGIAS · MERCADO ACTUAL</p><h2>Spot PAPER</h2></div>' +
      '<div class="spot-paper-badges"><span id="spot-paper-worker" class="statusbadge" role="status">OBSERVADOR · COMPROBANDO</span>' +
      '<span class="statusbadge">DINERO FICTICIO · SIN ÓRDENES</span></div></div>' +
      '<p id="spot-paper-notice" class="state" role="status" hidden></p>' +
      '<div class="spot-paper-grid"><section class="real-panel"><h3>Activar estrategia</h3>' +
      '<p class="muted">LONG sobre MEXC Spot. Cada sesión tiene su propio capital ficticio; no mueve la Cartera PAPER ni la real.</p>' +
      '<label class="field">Histórico MEXC<select id="spot-paper-dataset"></select></label>' +
      '<p id="spot-paper-readiness" class="muted" role="status">Comprobando histórico…</p>' +
      '<span id="spot-paper-lineage" class="paper-origin-check" role="status" hidden></span>' +
      '<button id="spot-paper-freshen" type="button" class="ghost" hidden data-help="Descarga 1000 velas públicas cerradas de MEXC en un dataset nuevo. No modifica la prueba LAB ni activa PAPER.">Actualizar histórico</button>' +
      '<p class="muted">¿No aparece un histórico reciente? <a href="#/laboratorio">Descárgalo en Laboratorio</a> con MEXC Spot y vuelve aquí.</p>' +
      '<label class="field">Estrategia LAB<select id="spot-paper-strategy"></select></label>' +
      '<label class="field">Versión<select id="spot-paper-version"></select></label>' +
      '<button id="spot-paper-start" type="button" data-help="Activa la versión elegida sólo con velas cerradas y cotización MEXC pública. Compra y vende ficticiamente; no transmite órdenes.">Activar ficticio</button>' +
      '<p class="muted spot-paper-note" data-help="Con posición abierta, Orbis comprueba bids públicos aproximadamente cada 5 s mientras funciona. Puede perder movimientos entre consultas o durante una desconexión; no coloca stops en MEXC.">Entradas al cierre · stop/objetivo al bid observado · sin orden protectora.</p></section>' +
      '<section class="real-panel"><h3>Seguimiento</h3><label class="field">Sesión<select id="spot-paper-session"></select></label>' +
      '<div class="spot-paper-actions"><button id="spot-paper-poll" type="button" class="ghost">Comprobar</button>' +
      '<button id="spot-paper-stop" type="button" class="ghost">Detener</button>' +
      '<button id="spot-paper-close" type="button" class="ghost" data-help="Vende sólo las unidades ficticias de esta sesión al bid público actual. Sirve también tras un hueco; no reconstruye precios perdidos.">Cerrar posición ficticia</button>' +
      '<button id="spot-paper-export" type="button" class="ghost" disabled data-help="Guarda velas, señales y ejecuciones ficticias verificadas para investigación local.">Exportar JSONL</button>' +
      '<button id="spot-paper-verify" type="button" class="ghost" disabled data-help="Comprueba la versión LAB, el dataset y la semilla histórica guardada. No verifica precios actuales ni rentabilidad.">Verificar origen</button>' +
      '<button id="spot-paper-mexc" type="button" class="ghost" hidden data-help="Abre la revisión de esta sesión en MEXC REAL. Capital, límites y permiso se eligen allí.">Preparar MEXC REAL</button>' +
      '<button id="spot-paper-dex" type="button" class="ghost" hidden data-help="Abre la revisión para USDT/WBNB en Uniswap v3, BNB Chain. La señal conserva las velas BNB/USDT de MEXC.">Preparar DEX</button></div>' +
      '<span id="spot-paper-origin" class="paper-origin-check" role="status" hidden></span>' +
      '<span id="spot-paper-export-state" class="muted" role="status"></span>' +
      '<details id="spot-paper-reserve" hidden><summary data-help="Coteja una reserva LAB anterior con el diario y las señales PAPER. Cada tramo conserva su propio periodo y modelo.">Comparar reserva LAB</summary>' +
      '<div class="row"><label class="field">Reserva<select></select></label><button type="button" disabled>Comparar</button></div>' +
      '<p class="muted" role="status"></p><div class="tablewrap"><table class="perp-table" aria-label="Reserva LAB y Spot PAPER" hidden><thead><tr></tr></thead><tbody></tbody></table></div></details>' +
      '<div id="spot-paper-detail" class="state">Selecciona una sesión.</div></section></div>' +
      '<section class="real-panel spot-paper-chart-panel"><div class="pagina-cabecera"><div>' +
      '<p class="real-eyebrow">SEÑALES · EJECUCIONES FICTICIAS</p><h3>Gráfica de seguimiento</h3></div>' +
      '<div id="spot-paper-range-control" class="lab-history-controls spot-paper-range-control"></div></div>' +
      '<div id="spot-paper-chart" role="img" aria-label="Velas MEXC con señales y operaciones ficticias"></div>' +
      '<p id="spot-paper-chart-status" class="muted" role="status">Selecciona una sesión para ver sus velas.</p>' +
      '<div id="spot-paper-inspector" class="spot-paper-inspector" aria-label="Detalle de señal o ejecución" tabindex="-1">' +
      '<p class="muted">Selecciona una señal o ejecución.</p></div>' +
      '<div class="spot-paper-legend"><span class="start">Inicio PAPER</span><span class="signal">Señal</span>' +
      '<span class="skipped">Señal sin entrada</span><span class="buy">Compra ficticia</span>' +
      '<span class="sell">Venta ficticia</span></div>' +
      '</section></section>';
    reserveComparison = mountReserve(root.querySelector("#spot-paper-reserve"));
    root.querySelector("#spot-paper-export").addEventListener("click", exportResearch);
    root.querySelector("#spot-paper-strategy").addEventListener("change", function () {
      sourceRunSelection = null; loadVersions();
    });
    root.querySelector("#spot-paper-version").addEventListener("change", function () { sourceRunSelection = null; });
    root.querySelector("#spot-paper-dataset").addEventListener("change", function () {
      sourceRunSelection = null; checkDatasetReadiness();
    });
    root.querySelector("#spot-paper-freshen").addEventListener("click", refreshPaperSeed);
    root.querySelector("#spot-paper-session").addEventListener("change", loadDetail);
    root.querySelector("#spot-paper-detail").addEventListener("click", function (event) {
      var gap = event.target.closest("button[data-gap-analysis]");
      if (gap && root.contains(gap)) { inspectGap(gap); return; }
      openChartDetail(event);
    });
    root.querySelector("#spot-paper-inspector").addEventListener("click", openChartDetail);
    root.querySelector("#spot-paper-start").addEventListener("click", start);
    root.querySelector("#spot-paper-verify").addEventListener("click", verifyOrigin);
    root.querySelector("#spot-paper-mexc").addEventListener("click", function () {
      if (!this.hidden && this.dataset.session === root.querySelector("#spot-paper-session").value)
        window.OrbisReal.prepareMexcSource("STRATEGY", this.dataset.session);
    });
    root.querySelector("#spot-paper-dex").addEventListener("click", function () {
      if (!this.hidden && this.dataset.session === root.querySelector("#spot-paper-session").value)
        window.OrbisReal.prepareDexSource("STRATEGY", this.dataset.session);
    });
    ["poll", "stop", "close"].forEach(function (action) {
      root.querySelector("#spot-paper-" + action).addEventListener("click", function () { act(action); });
    });
    refresh();
    timer = setInterval(refresh, 15000);
  }
  function refresh() {
    var current = ++generation;
    Promise.all([request("/lab/datasets"), request("/lab/strategy-versions"),
      request("/paper/spot/strategies"), request("/paper/spot/strategies/worker")]).then(function (values) {
      if (!root || !root.isConnected || current !== generation) return;
      datasets = values[0].filter(function (item) { return item.verified_public &&
        item.source === "mexc-spot-public" && /\/USDT$/.test(item.symbol) && !item.gaps.length; });
      var longVersions = values[1].filter(function (item) { return item.definition.direction === "LONG"; });
      strategies = longVersions.filter(function (item, index, rows) {
        return rows.findIndex(function (row) { return row.strategy_id === item.strategy_id; }) === index;
      }).map(function (item) { return {id:item.strategy_id, name:item.name}; });
      sessions = values[2];
      var worker = values[3], workerNode = root.querySelector("#spot-paper-worker");
      spotWorkerRunning = !!worker.running;
      workerNode.textContent = worker.running ? "OBSERVADOR · ACTIVO" : "OBSERVADOR · DETENIDO";
      workerNode.className = "statusbadge " + (worker.running ? "on" : "off");
      var ds = root.querySelector("#spot-paper-dataset"), st = root.querySelector("#spot-paper-strategy"),
        session = root.querySelector("#spot-paper-session");
      var oldDs = pendingSelection && pendingSelection.dataset_id || ds.value;
      var oldSt = pendingSelection && pendingSelection.strategy_id || st.value;
      var oldSession = session.value;
      ds.innerHTML = '<option value="">Selecciona un histórico</option>' + datasets.map(function (row) {
        return '<option value="' + esc(row.id) + '">' + esc(row.symbol) + ' · ' + esc(row.timeframe) +
          ' · ' + esc(row.count) + ' velas</option>';
      }).join("");
      st.innerHTML = '<option value="">Selecciona una estrategia LONG</option>' + strategies.map(function (row) {
        return '<option value="' + esc(row.id) + '">' + esc(row.name) + '</option>';
      }).join("");
      ds.value = datasets.some(function (row) { return row.id === oldDs; }) ? oldDs : (datasets[0] && datasets[0].id || "");
      st.value = strategies.some(function (row) { return row.id === oldSt; }) ? oldSt : (strategies[0] && strategies[0].id || "");
      if (pendingSelection && (ds.value !== oldDs || st.value !== oldSt)) {
        notice("El histórico o la estrategia LAB ya no está disponible para Spot PAPER. Elige uno compatible.", true);
      }
      session.innerHTML = '<option value="">Selecciona una sesión</option>' + sessions.map(function (row) {
        return '<option value="' + esc(row.id) + '">' + esc(row.symbol) + ' · ' + esc(row.timeframe) +
          ' · ' + esc(row.status) + ' · ' + esc(row.id.slice(0, 8)) + '</option>';
      }).join("");
      session.value = sessions.some(function (row) { return row.id === oldSession; }) ? oldSession : (sessions[0] && sessions[0].id || "");
      updateStartEnabled();
      checkDatasetReadiness();
      loadVersions();
      var selected = sessions.find(function (row) { return row.id === session.value; });
      if (detailSignature(selected) !== renderedDetailSignature ||
          !root.querySelector("#spot-paper-detail .spot-paper-status")) loadDetail();
    }).catch(function (error) { notice(error.message, true); });
  }
  function updateStartEnabled() {
    if (!root) return;
    root.querySelector("#spot-paper-start").disabled = startBusy || !spotWorkerRunning || !datasetReady ||
      !root.querySelector("#spot-paper-dataset").value || !root.querySelector("#spot-paper-version").value;
  }
  function checkDatasetReadiness() {
    if (!root) return;
    var id = root.querySelector("#spot-paper-dataset").value, seq = ++readinessSeq;
    var status = root.querySelector("#spot-paper-readiness");
    var freshen = root.querySelector("#spot-paper-freshen");
    datasetReady = false;
    freshen.hidden = true;
    if (id !== lineageForId) checkDatasetLineage(id);
    updateStartEnabled();
    if (!id) { status.textContent = "Elige un histórico MEXC."; return; }
    status.textContent = "Comprobando última vela…";
    request("/paper/spot/strategies/readiness?dataset_id=" + encodeURIComponent(id)).then(function (result) {
      if (!root || !root.isConnected || seq !== readinessSeq || root.querySelector("#spot-paper-dataset").value !== id) return;
      datasetReady = !!result.ready;
      status.textContent = result.ready ? "Histórico al día · listo para PAPER." :
        "Histórico no listo: " + result.reason + ". Descarga uno actualizado en LAB.";
      status.className = result.ready ? "muted" : "amber";
      freshen.hidden = !!result.ready;
      freshen.disabled = freshenBusy;
      updateStartEnabled();
    }).catch(function (error) {
      if (!root || !root.isConnected || seq !== readinessSeq) return;
      status.textContent = "No se pudo comprobar el histórico: " + error.message;
      status.className = "amber";
    });
  }
  function checkDatasetLineage(id) {
    lineageForId = id;
    var seq = ++lineageSeq, target = root.querySelector("#spot-paper-lineage");
    target.hidden = true;
    if (!id) return;
    request("/lab/datasets/" + encodeURIComponent(id) + "/lineage").then(function (rows) {
      if (!root || !root.isConnected || seq !== lineageSeq || lineageForId !== id) return;
      if (!rows.length) return;
      target.textContent = "Origen LAB · " + rows[0].parent_id.slice(0, 8) +
        (rows.length > 1 ? " · +" + (rows.length - 1) : "");
      target.dataset.help = "Esta semilla reciente proviene del histórico LAB " + rows[0].parent_id +
        " (SHA-256 " + rows[0].parent_hash + "). No cambia la prueba original ni demuestra rendimiento futuro.";
      target.hidden = false;
    }).catch(function () {
      if (seq === lineageSeq && target.isConnected) target.hidden = true;
    });
  }
  function refreshPaperSeed() {
    if (!root || freshenBusy) return;
    var datasetId = root.querySelector("#spot-paper-dataset").value;
    var strategyId = root.querySelector("#spot-paper-strategy").value;
    var versionId = root.querySelector("#spot-paper-version").value;
    if (!datasetId) return;
    freshenBusy = true;
    var button = root.querySelector("#spot-paper-freshen");
    button.disabled = true;
    button.textContent = "Descargando…";
    request("/lab/datasets/refresh-for-paper", {method:"POST",
      body:JSON.stringify({source_dataset_id:datasetId})}).then(function (fresh) {
      if (!root || !root.isConnected) return;
      pendingSelection = {dataset_id:fresh.id, strategy_id:strategyId, version_id:versionId};
      if (sourceRunSelection && sourceRunSelection.dataset_id === datasetId &&
          sourceRunSelection.version_id === versionId) sourceRunSelection.dataset_id = fresh.id;
      notice("Histórico reciente guardado aparte. Comprueba la última vela antes de iniciar PAPER.", false);
      refresh();
    }).catch(function (error) { notice("No se actualizó el histórico: " + error.message, true); })
      .finally(function () {
        freshenBusy = false;
        if (button.isConnected) { button.disabled = false; button.textContent = "Actualizar histórico"; }
      });
  }
  function loadVersions() {
    if (!root) return;
    var select = root.querySelector("#spot-paper-version"), strategyId = root.querySelector("#spot-paper-strategy").value;
    var old = pendingSelection && pendingSelection.version_id || select.value, current = generation;
    select.innerHTML = '<option value="">Cargando versiones…</option>';
    if (!strategyId) { select.innerHTML = '<option value="">Sin estrategia</option>'; return; }
    request("/lab/strategies/" + encodeURIComponent(strategyId) + "/versions").then(function (items) {
      if (!root || !root.isConnected || current !== generation || root.querySelector("#spot-paper-strategy").value !== strategyId) return;
      versions = items.filter(function (item) { return item.definition.direction === "LONG"; });
      select.innerHTML = versions.map(function (item) {
        return '<option value="' + esc(item.id) + '">v' + esc(item.version) + '</option>';
      }).join("") || '<option value="">Sin versiones LONG</option>';
      select.value = versions.some(function (item) { return item.id === old; }) ? old : (versions[versions.length - 1] && versions[versions.length - 1].id || "");
      if (pendingSelection && select.value !== old) notice("La versión LAB indicada ya no está disponible para Spot PAPER.", true);
      pendingSelection = null;
      updateStartEnabled();
    }).catch(function (error) { notice(error.message, true); });
  }
  function loadDetail() {
    if (!root) return;
    var id = root.querySelector("#spot-paper-session").value, current = generation, requestSequence = ++summarySeq;
    if (id !== displayedDetailId) {
      reserveComparison.setSession(null);
      resetExport();
      root.querySelector("#spot-paper-export").disabled = true;
    }
    var handoff = root.querySelector("#spot-paper-mexc"), dexHandoff = root.querySelector("#spot-paper-dex");
    [handoff, dexHandoff].forEach(function (button) { button.hidden = true; delete button.dataset.session; });
    root.querySelector("#spot-paper-verify").disabled = !id;
    if (id !== verifiedOriginSessionId) {
      var origin = root.querySelector("#spot-paper-origin");
      origin.hidden = true;
      origin.textContent = "";
      verifiedOriginSessionId = "";
    }
    if (!id) {
      renderedDetailSignature = "";
      displayedDetailId = "";
      displayedEventCount = -1;
      detailSeq++;
      comparisonSeq++;
      root.querySelector("#spot-paper-detail").textContent = "Todavía no hay sesiones.";
      clearChart();
      return;
    }
    request("/paper/spot/strategies/" + encodeURIComponent(id) + "/summary").then(function (item) {
      if (!root || !root.isConnected || current !== generation || requestSequence !== summarySeq || root.querySelector("#spot-paper-session").value !== id) return;
      if (item.id === id && item.status === "ACTIVE") {
        handoff.dataset.session = id;
        handoff.hidden = false;
        if (item.symbol === "BNB/USDT") {
          dexHandoff.dataset.session = id;
          dexHandoff.hidden = false;
        }
      }
      renderedDetailSignature = detailSignature(sessions.find(function (row) { return row.id === id; }));
      reserveComparison.setSession(item);
      var previousBody = root.querySelector("#spot-paper-event-rows");
      var previousButton = root.querySelector("#spot-paper-more-events");
      var preserveDiary = displayedDetailId === id && displayedEventCount === item.event_count &&
        previousBody && previousButton;
      var previousWrap = preserveDiary ? previousBody.closest(".tablewrap") : null;
      var state = item.state || {}, quote = item.last_quote, valuation = item.valuation || {};
      var stateValid = valuation.basis !== "UNAVAILABLE_INVALID_STATE";
      var equity = valuation.equity_if_closed == null ? "—" : amount(valuation.equity_if_closed, 2);
      var valuationNote = !stateValid ?
        "Estado PAPER incoherente: cifras no disponibles y acciones detenidas." :
        valuation.basis === "UNAVAILABLE_STALE_QUOTE" ?
        "Cotización caducada: valoración abierta no disponible hasta una nueva observación." :
        valuation.basis === "UNAVAILABLE_NO_QUOTE" ?
          "Sin cotización: valoración abierta no disponible." :
          "Estimación al bid tras costes; no es una orden ni patrimonio real.";
      root.querySelector("#spot-paper-detail").className = "spot-paper-report";
      root.querySelector("#spot-paper-detail").innerHTML = '<div class="spot-paper-status"><span class="statusbadge ' +
        (item.status === "ACTIVE" ? "on" : "off") + '">' + esc(item.status) + '</span><span>' +
        esc(item.symbol) + ' · ' + esc(item.timeframe) + ' · ' + esc(item.engine_version) + '</span>' +
        (item.status === "ACTIVE" && Number.isInteger(item.capacity_remaining) && item.capacity_remaining <= 500 ?
          '<span class="paper-capacity-warning" data-help="La sesión se detiene al llegar a 5000 velas. No se reinicia ni traslada posiciones automáticamente.">Restan ' +
          esc(item.capacity_remaining) + ' velas</span>' : '') + '</div>' +
        (item.status_reason ? '<p class="amber">' + esc(item.status_reason) + '</p>' : '') +
        (item.status === "GAP" ? '<button type="button" class="ghost" data-gap-analysis="' + esc(item.id) +
          '" data-help="Consulta velas públicas posteriores al hueco y calcula señales de la misma versión LAB. No recrea operaciones ni reanuda PAPER.">Revisar hueco</button>' +
          '<div id="spot-paper-gap-report" role="status" hidden></div>' : '') +
        coverageMarkup(item.coverage) +
        '<div class="spot-paper-metrics"><div><small>Caja ficticia</small><strong>' + (stateValid ? amount(state.cash, 2) : '—') + ' USDT</strong></div>' +
        '<div><small>Unidades</small><strong>' + (stateValid ? amount(state.units, 8) : '—') + '</strong></div>' +
        '<div><small>P/L realizado</small><strong>' + (stateValid ? amount(state.realized_pnl, 2) : '—') + ' USDT</strong>' +
        (stateValid && Number(state.units) > 0 ? '<small>Abierto si cierra: ' +
          (valuation.unrealized_pnl_if_closed == null ? '—' : amount(valuation.unrealized_pnl_if_closed, 2)) +
          ' USDT</small>' : '') + '</div>' +
        '<div><small>Patrimonio si cierra</small><strong>' + equity + ' USDT</strong></div></div>' +
        '<div id="spot-paper-comparison" class="spot-paper-compare state loading" role="status">Enlazando LAB y PAPER…</div>' +
        '<p class="muted">' + esc(item.event_count) + ' observaciones/eventos · ' + (stateValid ? esc(state.closed_trades) : '—') +
        ' cierres · último bid ' + esc(quote ? new Date(quote.event_time_ns / 1000000).toLocaleString("es-ES") : "sin cotización") +
        '. ' + esc(valuationNote) + '</p>' +
        '<div class="tablewrap"><table class="perp-table"><thead><tr><th>Hora</th><th>Señal y reglas</th><th>Acción ficticia</th><th>Precio</th><th>Motivo</th></tr></thead><tbody id="spot-paper-event-rows"><tr><td colspan="5">Cargando diario…</td></tr></tbody></table></div>' +
        '<button id="spot-paper-more-events" type="button" class="ghost" hidden>Ver anteriores</button>';
      displayedDetailId = id;
      displayedEventCount = item.event_count;
      root.querySelector("#spot-paper-export").disabled = !!exportRequest || item.event_count < 1 || !stateValid;
      if (preserveDiary) {
        root.querySelector("#spot-paper-event-rows").closest(".tablewrap").replaceWith(previousWrap);
        root.querySelector("#spot-paper-more-events").replaceWith(previousButton);
      } else {
      var sequence = ++detailSeq;
      var eventBody = root.querySelector("#spot-paper-event-rows");
      var moreButton = root.querySelector("#spot-paper-more-events");
      var beforeT = null, beforeKind = "", eventBusy = false, eventLoaded = false;
      function loadEventPage(older) {
        if (eventBusy || !root || !root.isConnected || sequence !== detailSeq) return;
        eventBusy = true;
        moreButton.disabled = true;
        var path = "/paper/spot/strategies/" + encodeURIComponent(id) + "/events/recent?limit=30";
        if (older) path += "&before_t=" + encodeURIComponent(beforeT) + "&before_kind=" + encodeURIComponent(beforeKind);
        request(path).then(function (page) {
          if (!root || !root.isConnected || sequence !== detailSeq ||
              root.querySelector("#spot-paper-session").value !== id || !eventBody.isConnected) return;
          if (page.session_id !== id || !Array.isArray(page.events)) throw new Error("Página del diario PAPER inválida");
          if (!older) eventBody.innerHTML = "";
          eventBody.insertAdjacentHTML("beforeend", eventRows(page.events));
          if (!eventBody.children.length) eventBody.innerHTML = '<tr><td colspan="5">Sin eventos todavía.</td></tr>';
          eventLoaded = true;
          beforeT = page.next_before_t;
          beforeKind = page.next_before_kind || "";
          moreButton.textContent = "Ver anteriores";
          moreButton.hidden = !page.has_more;
        }).catch(function (error) {
          if (sequence !== detailSeq || !eventBody.isConnected) return;
          if (!older) {
            eventBody.innerHTML = '<tr><td colspan="5">No se pudo cargar el diario.</td></tr>';
            moreButton.textContent = "Reintentar diario";
            moreButton.hidden = false;
          }
          notice(error.message, true);
        }).finally(function () {
          if (sequence === detailSeq && moreButton.isConnected) {
            eventBusy = false;
            moreButton.disabled = false;
          }
        });
      }
      moreButton.addEventListener("click", function () { loadEventPage(eventLoaded); });
      loadEventPage(false);
      }
      root.querySelector("#spot-paper-stop").disabled = item.status !== "ACTIVE";
      root.querySelector("#spot-paper-close").disabled = !stateValid || !(Number(state.units) > 0);
      root.querySelector("#spot-paper-poll").disabled = item.status !== "ACTIVE";
      if (item.status === "GAP") loadArchivedGap(item.id);
      if (stateValid) loadComparison(item, current);
      else {
        comparisonSeq++;
        root.querySelector("#spot-paper-comparison").textContent = "Comparación no disponible: estado PAPER incoherente.";
      }
      loadChart(item, current);
    }).catch(function (error) { notice(error.message, true); });
  }
  function verifyOrigin() {
    var id = root && root.querySelector("#spot-paper-session").value;
    if (!id) return;
    var button = root.querySelector("#spot-paper-verify");
    var badge = root.querySelector("#spot-paper-origin");
    button.disabled = true;
    badge.hidden = false;
    badge.dataset.status = "checking";
    badge.textContent = "Comprobando origen…";
    request("/paper/spot/strategies/" + encodeURIComponent(id) + "/verify").then(function (result) {
      if (!root || !root.isConnected || root.querySelector("#spot-paper-session").value !== id) return;
      verifiedOriginSessionId = id;
      badge.dataset.status = result.status;
      badge.textContent = result.verified ? "Origen verificado" :
        result.status === "mismatch" ? "Origen divergente" : "Origen no verificable";
      badge.setAttribute("data-help", result.reason + ". Sólo comprueba procedencia; no garantiza señales ni ejecución.");
    }).catch(function (error) {
      if (!root || !root.isConnected || root.querySelector("#spot-paper-session").value !== id) return;
      badge.dataset.status = "unverifiable";
      badge.textContent = "Verificación indisponible";
      badge.setAttribute("data-help", error.message);
    }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function inspectGap(button) {
    var id = button.dataset.gapAnalysis, box = root.querySelector("#spot-paper-gap-report");
    if (!box || root.querySelector("#spot-paper-session").value !== id) return;
    var seq = ++gapSeq;
    button.disabled = true;
    box.hidden = false;
    box.textContent = "Consultando velas del hueco…";
    request("/paper/spot/strategies/" + encodeURIComponent(id) + "/gap-analysis").then(function (report) {
      if (!root || !root.isConnected || seq !== gapSeq || root.querySelector("#spot-paper-session").value !== id || !box.isConnected) return;
      box.innerHTML = '<p class="amber">Reconstrucción guardada LAB · ' + esc(report.candles_reviewed) +
        ' velas · ' + esc(report.entry_signals) + ' señales de entrada · sin operaciones PAPER</p>' +
        '<small>Archivo ' + esc(report.archive.id.slice(0, 8)) + '</small>' +
        (report.signals.length ? '<p class="muted">' + report.signals.slice(0, 20).map(function (signal) {
          return esc(new Date(signal.candle_t * 1000).toLocaleString("es-ES")) +
            ' ' + (signal.entry_match ? 'entrada' : 'salida');
        }).join(' · ') + (report.signals.length > 20 ? ' · +' + esc(report.signals.length - 20) : '') + '</p>' : '');
    }).catch(function (error) { if (box.isConnected) box.textContent = "Hueco no verificable: " + error.message; })
      .finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function loadArchivedGap(id) {
    var seq = ++gapSeq, box = root && root.querySelector("#spot-paper-gap-report");
    request("/paper/spot/strategies/" + encodeURIComponent(id) + "/gap-analyses").then(function (saved) {
      if (!box || !box.isConnected || seq !== gapSeq || root.querySelector("#spot-paper-session").value !== id || !saved.length) return;
      var latest = saved[0];
      box.hidden = false;
      box.innerHTML = '<p class="muted">Última reconstrucción LAB · ' + esc(latest.result.candles_reviewed) +
        ' velas · ' + esc(latest.result.entry_signals) + ' entradas · sin operaciones PAPER · archivo ' +
        esc(latest.id.slice(0, 8)) + '</p>';
    }).catch(function (error) { if (box && box.isConnected && seq === gapSeq) box.textContent = "Archivo no verificable: " + error.message; });
  }
  function loadComparison(item, current) {
    var seq = ++comparisonSeq;
    request("/paper/spot/strategies/" + encodeURIComponent(item.id) + "/comparison").then(function (data) {
      var box = root && root.querySelector("#spot-paper-comparison");
      if (!box || !box.isConnected || current !== generation || seq !== comparisonSeq ||
          root.querySelector("#spot-paper-session").value !== item.id) return;
      var history = data.historical, paper = data.paper, replay = data.signal_replay;
      var historyLabel = history && history.verification && history.verification.verified
        ? '<small>LAB · HISTÓRICO VERIFICADO</small><strong>' + esc(history.metrics.trades) +
          ' operaciones · ' + amount(history.metrics.net_profit, 2) + ' USDT netos</strong><span>Run ' +
          esc(history.run_id.slice(0, 8)) +
          (data.source_run_id ? ' · origen fijado al iniciar PAPER' : '') +
          (history.dataset_relation === 'REFRESHED_FROM' ? ' · origen de la semilla PAPER' : '') + '</span>'
        : history ? '<small>LAB · NO VERIFICABLE</small><strong>Resultado oculto</strong><span>' +
          esc(history.verification && history.verification.reason || 'No se pudo comprobar') + '</span>'
        : '<small>LAB · SIN REFERENCIA</small><strong>No hay backtest guardado</strong><span>De esta versión ni del origen de la semilla.</span>';
      box.className = "spot-paper-compare";
      box.innerHTML = '<div>' + historyLabel + '</div><div><small>PAPER · OBSERVADO</small><strong>' +
        esc(paper.paper_buys) + ' compras · ' + esc(paper.paper_sells) + ' ventas</strong><span>' +
        esc(paper.observed_candles) + ' velas · P/L realizado ' + amount(paper.realized_pnl_usdt, 2) +
        ' USDT</span></div><p class="paper-signal-replay" role="status" data-state="' +
        (replay && replay.verified ? 'verified' : replay && replay.status === 'no_observations' ? 'pending' : 'unverified') + '" data-help="Recalcula cada señal PAPER a partir de las velas guardadas y la versión LAB. No comprueba fills ni autenticidad del exchange.">' +
        (replay && replay.verified ? 'Señales verificadas · ' + esc(replay.checked_observations) +
          (replay.checked_observations === 1 ? ' vela' : ' velas') :
          replay && replay.status === 'no_observations' ? 'Sin velas PAPER para verificar' :
          'Señales no verificables · ' + esc(replay && replay.reason || 'Sin replay')) + '</p>' +
        frequencyMarkup(data) +
        '<p data-help="Periodos, capital, bid/ask y cobertura de stops pueden diferir. No se calcula una diferencia de rentabilidad entre LAB y PAPER.">Comparación de trazabilidad · no de rentabilidad.</p>';
    }).catch(function (error) {
      var box = root && root.querySelector("#spot-paper-comparison");
      if (!box || !box.isConnected || current !== generation || seq !== comparisonSeq ||
          root.querySelector("#spot-paper-session").value !== item.id) return;
      box.className = "spot-paper-compare state error";
      box.textContent = "Comparación no disponible: " + error.message;
    });
  }
  function clearChart() {
    chartSeq++;
    if (chart) { chart.remove(); chart = null; }
    chartTimes = []; inspectedTime = null; chartEvents = {}; chartActions = {}; executionByCandle = {}; chartKey = "";
    var inspector = root && root.querySelector("#spot-paper-inspector");
    if (inspector) inspector.innerHTML = '<p class="muted">Selecciona una señal o ejecución.</p>';
    var rangeControl = root && root.querySelector("#spot-paper-range-control");
    if (rangeControl) rangeControl.textContent = "";
    var host = root && root.querySelector("#spot-paper-chart");
    if (host) {
      ["buyCount", "sellCount", "signalCount", "signalskippedCount"].forEach(function (key) { delete host.dataset[key]; });
    }
  }
  function evidenceRows(node) {
    if (!node) return '<li>No se guardó explicación de esta regla.</li>';
    if (node.op) return '<li><strong>Grupo ' + esc(node.op === "AND" ? "Y" : "O") +
      '</strong> · ' + (node.matched ? 'cumple' : 'no cumple') + '<ul>' +
      (node.children || []).map(evidenceRows).join("") + '</ul></li>';
    var names = {BEARISH_STREAK:"Velas bajistas seguidas",BULLISH_STREAK:"Velas alcistas seguidas",
      VOLUME_ASCENDING_BARS:"Volumen creciente",VOLUME_RISING_STREAK:"Subidas de volumen · legado",
      RSI_LT:"RSI menor que",RSI_GT:"RSI mayor que",
      ATR_PCT_LT:"ATR % menor que",ATR_PCT_GT:"ATR % mayor que",
      CLOSE_ABOVE_EMA:"Cierre sobre EMA",CLOSE_BELOW_EMA:"Cierre bajo EMA",
      VOLUME_ABOVE_AVG:"Volumen sobre media",RETEST_ABOVE_RANGE:"Ruptura y retesteo alcista",
      RETEST_BELOW_RANGE:"Ruptura y retesteo bajista"};
    var label = names[node.kind] || node.kind || "Regla";
    var values = node.observed == null ? "sin valor disponible" : esc(node.observed);
    if (node.threshold != null) values += ' / umbral ' + esc(node.threshold);
    if (node.period != null) label += ' (' + esc(node.period) + ')';
    return '<li><span class="' + (node.matched ? 'spot-paper-pass' : 'spot-paper-fail') + '">' +
      (node.matched ? '✓' : '×') + '</span> ' + esc(label) + ' · ' + values + '</li>';
  }
  function openChartDetail(event) {
    var button = event.target.closest("button[data-inspect-time]");
    if (!button || !root.contains(button)) return;
    var time = Number(button.dataset.inspectTime);
    if (!chartTimes.includes(time)) return;
    inspectedTime = time;
    inspectCandle(time, true);
  }
  function actionMarkup(mark) {
    return '<p class="muted"><strong>' + esc(mark.label) + ' · ' + esc(mark.price) + ' USDT</strong> · ' +
      esc(reasonLabel(mark.reason)) + ' · ' + esc(exactUtc(mark.observed_at_ns_exact)) +
      (mark.pending_candle ? ' · vela aún sin OHLC' : '') +
      (mark.source_candle_t !== null && chartEvents[mark.source_candle_t] ?
        ' ' + inspectButton(mark.source_candle_t, "Ver reglas") : '') + '</p>' +
      (mark.quote_bid == null || mark.quote_ask == null ? '' :
        '<p class="muted">Bid ' + esc(mark.quote_bid) + ' · Ask ' + esc(mark.quote_ask) +
        (mark.quote_time_ns_exact && mark.quote_time_ns_exact !== mark.observed_at_ns_exact ?
          ' · Ticker ' + esc(exactUtc(mark.quote_time_ns_exact)) : '') + '</p>');
  }
  function inspectCandle(time, center) {
    if (!root || !Number.isFinite(time)) return;
    var inspector = root.querySelector("#spot-paper-inspector"), event = chartEvents[time];
    var actions = chartActions[time] || [];
    var actionDetails = actions.map(actionMarkup).join("");
    if (!event) {
      inspector.innerHTML = actionDetails || '<p class="muted">' +
        esc(new Date(time * 1000).toLocaleString("es-ES")) + ' · Sin decisión PAPER</p>';
    } else {
      var action = event.action || {}, status = event.entry_status ||
        (action.side === "BUY" ? "EXECUTED_PAPER" : event.entry_match ? "UNSPECIFIED" : null);
      var statusText = status === "EXECUTED_PAPER" ? "Entrada ficticia" :
        status === "SKIPPED_POSITION_OPEN" ? (action.side === "SELL" ?
          "Entrada descartada; salida ficticia" : "Sin entrada: posición abierta") :
        status === "SKIPPED_NO_CAPITAL" ? "Sin entrada: capital insuficiente" :
        event.entry_match ? "Señal sin entrada documentada" : "Sin señal de entrada";
      inspector.innerHTML = '<div class="spot-paper-inspector-head"><strong>' +
        esc(new Date(time * 1000).toLocaleString("es-ES")) + ' · ' + esc(statusText) + '</strong><span>' +
        (action.side ? esc(action.side + ' PAPER · ' + action.price + ' USDT · ' + reasonLabel(action.reason)) : 'Sin operación') +
        (executionByCandle[time] === undefined ? '' : ' ' + inspectButton(executionByCandle[time], "Ver ejecución")) +
        '</span></div>' +
        '<div class="spot-paper-rules"><div><small>Entrada</small><ul>' + evidenceRows(event.entry) + '</ul></div>' +
        (event.exit ? '<div><small>Salida</small><ul>' + evidenceRows(event.exit) + '</ul></div>' : '') + '</div>' +
        actionDetails;
    }
    if (center && chart) {
      var index = chartTimes.indexOf(time);
      if (index >= 0) chart.timeScale().setVisibleLogicalRange({from:Math.max(0, index - 20),
        to:Math.min(chartTimes.length - 1, index + 20)});
      inspector.focus({preventScroll:true});
      inspector.scrollIntoView({block:"nearest",behavior:"smooth"});
    }
  }
  function fetchSpotChartPages(requestFn, currentFn, item) {
    var id = encodeURIComponent(item.id), snapshot = null;
    function accept(page) {
      if (page.session_id !== item.id || page.symbol !== item.symbol || page.timeframe !== item.timeframe ||
          !page.snapshot || page.snapshot.last_t !== item.last_t ||
          page.snapshot.event_count !== item.event_count) {
        throw new Error("La sesión cambió mientras se cargaba el gráfico");
      }
      var stamp = JSON.stringify(page.snapshot);
      if (snapshot !== null && snapshot !== stamp) throw new Error("Páginas PAPER de instantes distintos");
      snapshot = stamp;
    }
    var candles = [];
    function candlePage(afterT) {
      if (!currentFn()) return Promise.resolve(null);
      var path = "/paper/spot/strategies/" + id + "/candles?limit=1000";
      if (afterT !== null) path += "&after_t=" + afterT;
      return requestFn(path).then(function (page) {
        if (!currentFn()) return null;
        accept(page);
        if (!Array.isArray(page.candles)) throw new Error("Velas PAPER ilegibles");
        page.candles.forEach(function (entry) {
          var candle = entry && entry.candle;
          if (!candle || !Number.isInteger(candle.t) ||
              (candles.length && candle.t <= candles[candles.length - 1].t)) {
            throw new Error("Orden de velas PAPER inválido");
          }
          candles.push(candle);
        });
        if (!page.has_more) return candles;
        if (!page.candles.length || !Number.isInteger(page.next_after_t) ||
            page.next_after_t !== candles[candles.length - 1].t ||
            (afterT !== null && page.next_after_t <= afterT)) {
          throw new Error("Cursor de velas PAPER inválido");
        }
        return candlePage(page.next_after_t);
      });
    }
    var markers = [], observations = [], initialLastT = null;
    function markerPage(afterT, afterKind) {
      if (!currentFn()) return Promise.resolve(null);
      var path = "/paper/spot/strategies/" + id + "/chart-markers-page?limit=500";
      if (afterT !== null) path += "&after_t=" + afterT + "&after_kind=" + encodeURIComponent(afterKind);
      return requestFn(path).then(function (page) {
        if (!currentFn()) return null;
        accept(page);
        if (page.execution !== "PAPER_ACTION_AFTER_SIGNAL_CANDLE_CLOSE_NO_REAL_ORDER" ||
            !Array.isArray(page.markers) || !Array.isArray(page.observations)) {
          throw new Error("Marcas PAPER ilegibles");
        }
        if (initialLastT === null) initialLastT = page.initial_last_t;
        markers.push.apply(markers, page.markers);
        observations.push.apply(observations, page.observations);
        if (!page.has_more) return true;
        var nextT = page.next_after_t, nextKind = page.next_after_kind;
        if (!Number.isInteger(nextT) || typeof nextKind !== "string" || !nextKind ||
            (afterT !== null && (nextT < afterT || (nextT === afterT && nextKind <= afterKind)))) {
          throw new Error("Cursor de marcas PAPER inválido");
        }
        return markerPage(nextT, nextKind);
      });
    }
    return Promise.all([candlePage(null), markerPage(null, "")]).then(function (parts) {
      if (!parts[0] || !parts[1] || !currentFn()) return null;
      if (!candles.length || candles[candles.length - 1].t !== item.last_t) {
        throw new Error("Cobertura de velas PAPER incompleta");
      }
      return {candles:candles, markers:markers, observations:observations,
        initial_last_t:initialLastT};
    });
  }
  function plotActions(LW, kind, actions, style) {
    var lanes = [], previous, lane = 0;
    actions.filter(function (mark) { return mark.kind === kind; }).forEach(function (mark) {
      // Lightweight Charts only accepts one data point per second in a series.
      lane = mark.execution_time === previous ? lane + 1 : 0;
      previous = mark.execution_time;
      (lanes[lane] ||= []).push(mark);
    });
    lanes.forEach(function (marks) {
      var series = chart.addSeries(LW.LineSeries, {lineVisible:false,pointMarkersVisible:false,
        crosshairMarkerVisible:false,priceLineVisible:false,lastValueVisible:false});
      series.setData(marks.map(function (mark) {
        return {time:mark.execution_time,value:Number(mark.price)};
      }));
      LW.createSeriesMarkers(series, marks.map(function (mark) {
        return Object.assign({time:mark.execution_time}, style);
      }));
    });
  }
  function loadChart(item, current) {
    var key = item.id + ":" + item.last_t + ":" + item.event_count;
    if (key === chartKey) return;
    if (chartKey && chartKey.indexOf(item.id + ":") !== 0) clearChart();
    var seq = ++chartSeq, status = root.querySelector("#spot-paper-chart-status");
    status.textContent = "Cargando velas y marcas PAPER…";
    fetchSpotChartPages(request, function () {
      return !!root && root.isConnected && current === generation && seq === chartSeq &&
        root.querySelector("#spot-paper-session").value === item.id;
    }, item).then(function (data) {
      if (!data) return;
      if (!root || !root.isConnected || current !== generation || seq !== chartSeq ||
          root.querySelector("#spot-paper-session").value !== item.id) return;
      var LW = window.LightweightCharts, host = root.querySelector("#spot-paper-chart");
      if (!LW || !LW.CandlestickSeries || !LW.createSeriesMarkers) throw new Error("Gráficos locales no disponibles");
      if (chart) { chart.remove(); chart = null; }
      chart = LW.createChart(host, {autoSize:true,
        layout:{background:{type:"solid",color:"#101b18"},textColor:"#c9d6ce"},
        grid:{vertLines:{color:"#24362f"},horzLines:{color:"#24362f"}},
        timeScale:{timeVisible:true,secondsVisible:true,minBarSpacing:0.1}, localization:{locale:"es-ES"}});
      var series = chart.addSeries(LW.CandlestickSeries, {upColor:"#42c88a",downColor:"#ff7b78",
        borderVisible:false,wickUpColor:"#42c88a",wickDownColor:"#ff7b78"});
      var chartBars = data.candles.map(function (bar) { return {time:bar.t,open:Number(bar.o),
        high:Number(bar.h),low:Number(bar.l),close:Number(bar.c)}; });
      series.setData(chartBars);
      chartEvents = {};
      chartActions = {};
      executionByCandle = {};
      data.observations.forEach(function (event) {
        chartEvents[event.candle_t] = event;
      });
      var actions = data.markers.filter(function (mark) { return mark.kind === "BUY" || mark.kind === "SELL"; });
      actions.sort(function (a, b) {
        return a.execution_time - b.execution_time || a.observed_at_ns_exact.localeCompare(b.observed_at_ns_exact);
      });
      actions.forEach(function (mark) {
        if (!Number.isSafeInteger(mark.execution_time) ||
            mark.execution_time !== nsSecond(mark.observed_at_ns_exact) || !Number.isFinite(Number(mark.price))) {
          throw new Error("Ejecución PAPER ilegible");
        }
        (chartActions[mark.execution_time] ||= []).push(mark);
        if (mark.source_candle_t !== null && chartEvents[mark.source_candle_t]) {
          executionByCandle[mark.source_candle_t] = mark.execution_time;
        }
      });
      var actionTimes = actions.map(function (mark) { return mark.execution_time; });
      chartTimes = Array.from(new Set(chartBars.map(function (bar) { return bar.time; }).concat(actionTimes)))
        .sort(function (a, b) { return a - b; });
      chart.subscribeCrosshairMove(function (param) {
        if (inspectedTime === null && typeof param.time === "number" &&
            (chartEvents[param.time] || chartActions[param.time])) inspectCandle(param.time, false);
      });
      chart.subscribeClick(function (param) {
        if (typeof param.time === "number") {
          inspectedTime = param.time;
          inspectCandle(param.time, false);
        }
      });
      var styles = {
        START:{position:"belowBar",color:"#88b8ff",shape:"square",text:"Inicio"},
        SIGNAL:{position:"aboveBar",color:"#efbd63",shape:"circle",text:"Señal"},
        SIGNAL_SKIPPED:{position:"aboveBar",color:"#9caaa4",shape:"circle",text:"Sin entrada"},
        BUY:{position:"belowBar",color:"#42c88a",shape:"arrowUp",text:"PAPER +"},
        SELL:{position:"aboveBar",color:"#ff7b78",shape:"arrowDown",text:"PAPER −"}
      };
      var decisionMarkers = data.markers.filter(function (mark) {
        return styles[mark.kind] && mark.kind !== "BUY" && mark.kind !== "SELL";
      }).map(function (mark) { return Object.assign({time:mark.time}, styles[mark.kind]); })
        .sort(function (a, b) { return a.time - b.time; });
      LW.createSeriesMarkers(series, decisionMarkers);
      ["BUY", "SELL"].forEach(function (kind) {
        plotActions(LW, kind, actions, styles[kind]);
      });
      ["BUY", "SELL", "SIGNAL", "SIGNAL_SKIPPED"].forEach(function (kind) {
        host.dataset[kind.toLowerCase().replace("_", "") + "Count"] = String(data.markers.filter(function (mark) { return mark.kind === kind; }).length);
      });
      chartKey = key;
      var lastBar = chartBars[chartBars.length - 1], previousBar = chartBars[chartBars.length - 2];
      if (window.OrbisChartRange) window.OrbisChartRange.mount(
        root.querySelector("#spot-paper-range-control"), chart, chartBars,
        {storageKey:"orbis.chartRange.spotPaper", selected:"last:100",
          lastCounts:[100, 500], legacySpot:true,
          eventTimes:actionTimes,coverageEnd:previousBar && 2 * lastBar.time - previousBar.time}
      );
      if (inspectedTime !== null && chartTimes.includes(inspectedTime)) inspectCandle(inspectedTime, false);
      var pending = actions.filter(function (mark) { return mark.pending_candle; }).length;
      status.textContent = data.candles.length + " velas · " + data.markers.length +
        " marcas" + (pending ? " · " + pending + (pending === 1 ? " ejecución" : " ejecuciones") + " sin OHLC" : "");
    }).catch(function (error) {
      if (!root || !root.isConnected || current !== generation || seq !== chartSeq) return;
      clearChart();
      status.textContent = "Gráfica no disponible: " + error.message;
    });
  }
  function start() {
    if (!datasetReady || startBusy) { notice("Comprueba que el histórico esté actualizado antes de iniciar PAPER.", true); return; }
    var version = root.querySelector("#spot-paper-version").value, dataset = root.querySelector("#spot-paper-dataset").value;
    if (!version || !dataset) { notice("Elige histórico y versión LONG.", true); return; }
    if (!window.confirm("¿Activar seguimiento Spot PAPER con capital ficticio? No se enviarán órdenes.")) return;
    startBusy = true;
    updateStartEnabled();
    var body = {version_id:version, dataset_id:dataset};
    if (sourceRunSelection && sourceRunSelection.dataset_id === dataset &&
        sourceRunSelection.version_id === version) body.source_run_id = sourceRunSelection.run_id;
    request("/paper/spot/strategies", {method:"POST", body:JSON.stringify(body)})
      .then(function (item) { sourceRunSelection = null; notice("Sesión ficticia activada: " + item.symbol + ".", false); return refresh(); })
      .catch(function (error) { notice(error.message, true); })
      .finally(function () { startBusy = false; if (root && root.isConnected) updateStartEnabled(); });
  }
  function act(action) {
    var id = root.querySelector("#spot-paper-session").value;
    if (!id) return;
    if (action === "close" && !window.confirm("¿Cerrar la posición ficticia al bid MEXC actual?")) return;
    var button = root.querySelector("#spot-paper-" + action); button.disabled = true;
    request("/paper/spot/strategies/" + encodeURIComponent(id) + "/" + action, {method:"POST"})
      .then(function (item) { notice(action === "poll" ? "Comprobación: " + item.poll_result :
        action === "close" ? "Cierre ficticio: " + item.close_result : "Sesión detenida.", false); return refresh(); })
      .catch(function (error) { notice(error.message, true); })
      .finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function cleanup() {
    verifiedOriginSessionId = "";
    renderedDetailSignature = "";
    displayedDetailId = "";
    displayedEventCount = -1;
    summarySeq++;
    detailSeq++;
    if (reserveComparison) { reserveComparison.setSession(null); reserveComparison = null; }
    resetExport();
    if (timer) { clearInterval(timer); timer = null; }
    clearChart();
    generation++;
    root = null;
  }
  window.OrbisSpotPaper = {render:render, cleanup:cleanup};
})();
