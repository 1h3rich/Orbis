/* PAPER GRVT: posiciones manuales o por señales causales; sin órdenes reales. */
(function () {
  "use strict";
  var root = null, U = null, accounts = [], currentId = "", generation = 0, specSeq = 0;
  var signalTimer = null, valuationTimer = null, signalRefreshBusy = false, inspectedSessionId = "", comparisonSeq = 0, gapSeq = 0;
  var sourceCheck = null;
  var observedWidget = null;
  var signalWorkerActive = false;
  var signalStrategies = [];
  var pendingSelection = null;
  var sourceRunSelection = null;
  var signalDatasetReady = false, signalReadinessSeq = 0, freshenBusy = false;
  var signalLineageForId = "", signalLineageSeq = 0;
  var priceChart = null, chartSeq = 0, chartPositions = [], chartEvents = [], chartRange = "all";
  var CHART_STEPS = {"1m":60,"5m":300,"15m":900,"30m":1800,"1h":3600,"4h":14400,"1d":86400,"1w":604800};

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (char) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char];
    });
  }
  function money(value) {
    if (value == null) return "—";
    var text = String(value);
    if (/^-?\d+\.\d+$/.test(text)) text = text.replace(/0+$/, "").replace(/\.$/, "");
    return esc(text) + " USDT";
  }
  function actionReason(code) {
    return {TRAILING_STOP_MARK_STREAM:"Stop móvil · mark",
      TRAILING_STOP_AT_CANDLE_CLOSE:"Stop móvil · cierre"}[code] || code || "—";
  }
  function frequencyMarkup(comparison) {
    return window.OrbisSignalFrequency.markup(comparison.signal_frequency);
  }
  function $id(id) { return root && root.querySelector("#" + id); }
  function candleTime(seconds) { return new Date(Number(seconds) * 1000).toLocaleString("es-ES"); }
  function request(path, options) {
    return U.api(path, options).then(function (response) {
      if (!response.ok) {
        var detail = response.data && response.data.detail;
        throw new Error(typeof detail === "string" ? detail : "No se pudo completar la operación");
      }
      return response.data;
    });
  }
  function message(text, error) {
    var target = $id("perp-message");
    if (!target) return;
    target.textContent = text;
    target.className = "state " + (error ? "error" : "ok");
    target.hidden = false;
  }
  function render(container, ui, selection) {
    if(observedWidget){observedWidget.dispose();observedWidget=null;}
    generation++;
    if (signalTimer) { clearInterval(signalTimer); signalTimer = null; }
    if (valuationTimer) { clearTimeout(valuationTimer); valuationTimer = null; }
    removePriceChart();
    chartPositions = []; chartEvents = [];
    root = container;
    U = ui;
    signalLineageForId = "";
    signalLineageSeq++;
    pendingSelection = selection && typeof selection.get === "function" ? {
      dataset_id: selection.get("dataset"), strategy_id: selection.get("strategy"),
      version_id: selection.get("version")
    } : null;
    sourceRunSelection = pendingSelection && selection.get("run") ? {
      run_id: selection.get("run"), dataset_id: pendingSelection.dataset_id,
      version_id: pendingSelection.version_id
    } : null;
    root.innerHTML = '<section class="card perp-page"><div class="pagina-cabecera"><div>' +
      '<p class="real-eyebrow">GRVT · CAPITAL FICTICIO</p><h2>Perpetuos PAPER</h2></div>' +
      '<span class="statusbadge off">PAPER · SIN ÓRDENES REALES</span></div>' +
      '<p id="perp-message" class="state" role="status" hidden></p>' +
      '<div class="perp-grid"><section class="real-panel"><h3>Cuenta aislada</h3>' +
      '<label class="field">Cuenta<select id="perp-account"><option value="">Cargando…</option></select></label>' +
      '<div id="perp-summary" class="state loading">Cargando saldo…</div>' +
      '<div class="row perp-actions"><button type="button" id="perp-refresh" class="ghost">Actualizar</button>' +
      '<button type="button" id="perp-stop" class="ghost danger" hidden>Detener entradas</button></div>' +
      '<details id="perp-new"><summary>Crear cuenta ficticia</summary>' +
      '<form id="perp-account-form" class="perp-form"><label class="field">Nombre<input name="name" value="GRVT PAPER" maxlength="100" required></label>' +
      '<label class="field">Capital · USDT<input name="initial_balance" value="10000" inputmode="decimal" required></label>' +
      '<label class="field">Máx. por posición · USDT<input name="max_position_notional" value="1000" inputmode="decimal" required></label>' +
      '<label class="field">Exposición total · USDT<input name="max_total_notional" value="2000" inputmode="decimal" required></label>' +
      '<label class="field">Apalancamiento máx.<input name="max_leverage" value="2" inputmode="decimal" required></label>' +
      '<label class="field">Posiciones simultáneas<input name="max_positions" type="number" min="1" max="100" value="2" required></label>' +
      '<label class="field" data-help="Supuesto PAPER, no tarifa verificada de GRVT. 0.0005 equivale a 0,05 % por entrada y salida.">Comisión supuesta<input name="fee_rate" value="0.0005" inputmode="decimal" required></label>' +
      '<label class="field" data-help="Deslizamiento adverso en puntos básicos sobre el mejor bid/ask. 5 pb = 0,05 %.">Slippage · pb<input name="slippage_bps" value="5" inputmode="decimal" required></label>' +
      '<button type="submit">Crear cuenta PAPER</button></form></details></section>' +
      '<section class="real-panel"><h3>Entrada manual</h3>' +
      '<form id="perp-open-form" class="perp-form"><label class="field">Instrumento<select name="symbol">' +
      '<option value="BTC/USDT:PERP">BTC/USDT · PERP</option><option value="ETH/USDT:PERP">ETH/USDT · PERP</option>' +
      '<option value="SOL/USDT:PERP">SOL/USDT · PERP</option></select></label>' +
      '<label class="field">Dirección<select name="side"><option value="LONG">LONG</option><option value="SHORT">SHORT</option></select></label>' +
      '<label class="field">Cantidad · activo base<input name="size" inputmode="decimal" placeholder="Según mínimos GRVT" required></label>' +
      '<label class="field">Apalancamiento<input name="leverage" value="2" inputmode="decimal" required></label>' +
      '<button type="submit" id="perp-open">Abrir en PAPER</button></form>' +
      '<div id="perp-instrument-spec" class="perp-spec state loading" data-help="Restricciones públicas actuales de GRVT; se verifican otra vez al abrir. No garantizan ejecución real.">Consultando límites GRVT…</div>' +
      '<p class="muted">Precio actual de GRVT al confirmar; no se puede introducir uno manual.</p>' +
      '<div class="state blocked perp-limit" role="status">Bid/ask + deslizamiento supuesto. ' +
      'El margen observado es puntual; la financiación PAPER requiere asentamiento y no se simula liquidación real.</div></section></div>' +
      '<section class="real-panel perp-market-chart"><div class="pagina-cabecera"><div>' +
      '<p class="real-eyebrow">GRVT · VELAS PÚBLICAS</p><h3>Precio y decisiones PAPER</h3></div>' +
      '<div class="perp-chart-controls"><label>Instrumento<select id="perp-chart-symbol">' +
      '<option value="BTC/USDT:PERP">BTC/USDT · PERP</option><option value="ETH/USDT:PERP">ETH/USDT · PERP</option>' +
      '<option value="SOL/USDT:PERP">SOL/USDT · PERP</option></select></label>' +
      '<label>Vela<select id="perp-chart-tf"><option>1m</option><option>5m</option><option>15m</option>' +
      '<option>30m</option><option selected>1h</option><option>4h</option><option>1d</option><option>1w</option></select></label>' +
      '<button type="button" id="perp-chart-refresh" class="ghost">Actualizar gráfico</button></div></div>' +
      '<p class="perp-chart-legend"><span class="paper">▲ / ▼ PAPER ejecutado</span>' +
      '<span class="signal">● / ■ Señal observada</span></p>' +
      '<div id="perp-chart-status" class="state loading" role="status">Cargando velas GRVT…</div>' +
      '<div id="perp-chart" role="img" aria-label="Velas GRVT con operaciones PAPER y señales observadas" hidden></div>' +
      '<div id="perp-chart-range" class="lab-history-controls"></div>' +
      '<p id="perp-chart-note" class="muted" hidden></p></section>' +
      '<section class="real-panel perp-positions"><div class="pagina-cabecera"><h3>Posiciones</h3>' +
      '<span class="muted">El cierre manual sigue disponible</span></div><div id="perp-positions" class="state loading">Cargando posiciones…</div>' +
      '<div id="perp-funding-detail" class="state" role="status" aria-live="polite" hidden></div></section>' +
      '<details class="perp-history"><summary>Registro de eventos</summary><div id="perp-events" class="state empty">Sin eventos</div></details>' +
      '<section class="real-panel perp-signals"><div class="pagina-cabecera"><div><p class="real-eyebrow">LAB → PAPER</p><h3>Señales y ejecución ficticia</h3></div>' +
      '<div class="perp-signal-badges"><span id="perp-signal-worker" class="statusbadge" role="status">VELAS · SIN CUENTA</span><span id="perp-risk-worker" class="statusbadge" role="status">RIESGO · SIN CUENTA</span><span id="perp-funding-worker" class="statusbadge" role="status">FUNDING · SIN CUENTA</span><span class="statusbadge off">SIN ÓRDENES REALES</span></div></div>' +
      '<form id="perp-signal-form" class="perp-form"><label class="field">Estrategia versionada<select name="version_id" required></select></label>' +
      '<label class="field">Histórico GRVT verificado<select name="dataset_id" required></select></label>' +
      '<p id="perp-signal-readiness" class="muted" role="status">Comprobando histórico…</p>' +
      '<span id="perp-signal-lineage" class="paper-origin-check" role="status" hidden></span>' +
      '<button id="perp-signal-freshen" type="button" class="ghost" hidden data-help="Descarga 1000 velas públicas GRVT cerradas en un dataset nuevo. No modifica el backtest ni inicia sesiones.">Actualizar histórico</button>' +
      '<label class="field" data-help="Observar guarda señales sin abrir posiciones. Auto PAPER abre ficticiamente al cierre de vela y vigila stop/objetivo con ticker público mientras ORBIS esté abierto. Si la equity cruza mantenimiento estimado con reglas de apertura, detiene la sesión sin inventar liquidación.">Modo<select name="execution_mode"><option value="OBSERVE">Solo observar</option><option value="AUTO_PAPER">Auto PAPER</option></select></label>' +
      '<div id="perp-auto-fields" class="perp-auto-fields" hidden><label class="field">Cantidad · activo base<input name="size" inputmode="decimal" placeholder="Según mínimos GRVT" required disabled></label>' +
      '<label class="field">Apalancamiento<input name="leverage" value="2" inputmode="decimal" required disabled></label>' +
      '<p id="perp-auto-strategy" class="muted" role="status"></p>' +
      '<p class="amber" data-help="Stop y objetivo usan la marca del ticker público GRVT; el cierre PAPER usa bid/ask del mismo mensaje. La cobertura se interrumpe al cerrar ORBIS o perder el feed más de 20 s: la sesión pasa a GAP y la posición queda abierta para cierre manual. No hay órdenes protectoras en GRVT.">Ticker en directo · sin orden protectora</p></div>' +
      '<button type="submit" id="perp-signal-create">Iniciar seguimiento</button></form>' +
      '<div id="perp-signal-status" class="state empty" role="status">El observador local revisa velas cerradas mientras ORBIS esté abierto.</div>' +
      '<div id="perp-signal-sessions" class="state empty">Sin sesiones</div></section></section>';
    if(window.OrbisPerpObserved){
      var observedPanel=document.createElement('details');observedPanel.className='real-panel';observedPanel.id='perp-observed-library';
      observedPanel.innerHTML='<summary data-help="Sesiones con perfil de margen y reserva LAB verificados. Su capital ficticio es independiente de las cuentas manuales.">Estrategias validadas</summary><div></div>';
      root.querySelector('.pagina-cabecera').after(observedPanel);
      observedWidget=window.OrbisPerpObserved.mount(observedPanel.querySelector('div'));
      observedPanel.addEventListener('toggle',function(){if(observedPanel.open&&observedWidget)observedWidget.load();});
    }
    $id("perp-account").addEventListener("change", function () {
      currentId = this.value;
      chartPositions = []; chartEvents = [];
      try { localStorage.setItem("orbis.paper.perp.account", currentId); } catch (error) {}
      reloadDetails();
    });
    $id("perp-refresh").addEventListener("click", reload);
    $id("perp-stop").addEventListener("click", stop);
    $id("perp-account-form").addEventListener("submit", createAccount);
    $id("perp-open-form").addEventListener("submit", open);
    $id("perp-open-form").querySelector('[name="symbol"]').addEventListener("change", loadInstrumentSpec);
    $id("perp-chart-symbol").addEventListener("change", loadPerpChart);
    $id("perp-chart-tf").addEventListener("change", function () {
      chartRange = "all";
      try { localStorage.setItem("orbis.chartRange.grvtPaper", JSON.stringify({mode:"all"})); } catch (error) {}
      loadPerpChart();
    });
    $id("perp-chart-refresh").addEventListener("click", loadPerpChart);
    $id("perp-positions").addEventListener("click", positionAction);
    $id("perp-funding-detail").addEventListener("click", positionAction);
    $id("perp-signal-form").addEventListener("submit", createSignalSession);
    $id("perp-signal-form").querySelector('[name="execution_mode"]').addEventListener("change", updateSignalMode);
    $id("perp-signal-form").querySelector('[name="version_id"]').addEventListener("change", function () {
      sourceRunSelection = null; updateSignalMode();
    });
    $id("perp-signal-form").querySelector('[name="dataset_id"]').addEventListener("change", function () {
      sourceRunSelection = null; checkSignalReadiness();
    });
    $id("perp-signal-freshen").addEventListener("click", refreshSignalSeed);
    $id("perp-signal-sessions").addEventListener("click", signalAction);
    reload();
    loadInstrumentSpec();
    signalTimer = setInterval(function () {
      if (!root || !root.isConnected || !currentId || signalRefreshBusy) return;
      signalRefreshBusy = true;
      reloadSignals(generation).finally(function () { signalRefreshBusy = false; });
    }, 15000);
  }
  function updateSignalMode() {
    var automatic = $id("perp-signal-form").querySelector('[name="execution_mode"]').value === "AUTO_PAPER";
    var fields = $id("perp-auto-fields");
    fields.hidden = !automatic;
    fields.querySelectorAll("input").forEach(function (input) { input.disabled = !automatic; });
    var versionId = $id("perp-signal-form").querySelector('[name="version_id"]').value;
    var strategy = signalStrategies.find(function (row) { return row.version_id === versionId; });
    if (strategy && automatic) {
      var definition = strategy.definition;
      var pct = function (value) { return new Intl.NumberFormat("es-ES", {maximumFractionDigits:2}).format(Number(value) * 100) + "%"; };
      $id("perp-auto-strategy").textContent = definition.direction + " · stop " + pct(definition.stop_loss) +
        (definition.trailing_stop ? " · móvil " + pct(definition.trailing_stop) : "") +
        " · objetivo " + pct(definition.take_profit) + (definition.exit ? " · salida por regla" : "");
    } else $id("perp-auto-strategy").textContent = "";
    var account = accounts.find(function (row) { return row.id === currentId; });
    $id("perp-signal-create").disabled = !account || !!account.stopped_at ||
      !$id("perp-signal-form").querySelector('[name="version_id"]').value ||
      !$id("perp-signal-form").querySelector('[name="dataset_id"]').value ||
      !signalDatasetReady ||
      (automatic && !signalWorkerActive);
  }
  function checkSignalReadiness() {
    if (!root) return;
    var id = $id("perp-signal-form").querySelector('[name="dataset_id"]').value;
    var seq = ++signalReadinessSeq, target = $id("perp-signal-readiness");
    var freshen = $id("perp-signal-freshen");
    signalDatasetReady = false;
    freshen.hidden = true;
    if (id !== signalLineageForId) checkSignalLineage(id);
    updateSignalMode();
    if (!id) { target.textContent = "Elige un histórico GRVT."; return; }
    target.textContent = "Comprobando última vela…";
    request("/paper/perp/signals/readiness?dataset_id=" + encodeURIComponent(id)).then(function (result) {
      if (!root || !root.isConnected || seq !== signalReadinessSeq ||
          $id("perp-signal-form").querySelector('[name="dataset_id"]').value !== id) return;
      signalDatasetReady = !!result.ready;
      target.textContent = result.ready ? "Histórico al día · listo para PAPER." :
        "Histórico no listo: " + result.reason + ". Descarga uno actualizado en LAB.";
      target.className = result.ready ? "muted" : "amber";
      freshen.hidden = !!result.ready;
      freshen.disabled = freshenBusy;
      updateSignalMode();
    }).catch(function (error) {
      if (!root || !root.isConnected || seq !== signalReadinessSeq) return;
      target.textContent = "No se pudo comprobar el histórico: " + error.message;
      target.className = "amber";
    });
  }
  function checkSignalLineage(id) {
    signalLineageForId = id;
    var seq = ++signalLineageSeq, target = $id("perp-signal-lineage");
    target.hidden = true;
    if (!id) return;
    request("/lab/datasets/" + encodeURIComponent(id) + "/lineage").then(function (rows) {
      if (!root || !root.isConnected || seq !== signalLineageSeq || signalLineageForId !== id) return;
      if (!rows.length) return;
      target.textContent = "Origen LAB · " + rows[0].parent_id.slice(0, 8) +
        (rows.length > 1 ? " · +" + (rows.length - 1) : "");
      target.dataset.help = "Esta semilla reciente proviene del histórico LAB " + rows[0].parent_id +
        " (SHA-256 " + rows[0].parent_hash + "). No cambia la prueba original ni demuestra rendimiento futuro.";
      target.hidden = false;
    }).catch(function () {
      if (seq === signalLineageSeq && target.isConnected) target.hidden = true;
    });
  }
  function refreshSignalSeed() {
    if (!root || freshenBusy) return;
    var form = $id("perp-signal-form");
    var datasetId = form.querySelector('[name="dataset_id"]').value;
    var versionId = form.querySelector('[name="version_id"]').value;
    var chosen = signalStrategies.find(function (row) { return row.version_id === versionId; });
    if (!datasetId || !chosen) return;
    freshenBusy = true;
    var button = $id("perp-signal-freshen");
    button.disabled = true;
    button.textContent = "Descargando…";
    request("/lab/datasets/refresh-for-paper", {method:"POST",
      body:JSON.stringify({source_dataset_id:datasetId})}).then(function (fresh) {
      if (!root || !root.isConnected) return;
      pendingSelection = {dataset_id:fresh.id, strategy_id:chosen.strategy_id, version_id:versionId};
      if (sourceRunSelection && sourceRunSelection.dataset_id === datasetId &&
          sourceRunSelection.version_id === versionId) sourceRunSelection.dataset_id = fresh.id;
      signalStatus("Histórico reciente guardado aparte. Comprueba la última vela antes de iniciar PAPER.", false);
      return reloadSignals(generation);
    }).catch(function (error) { signalStatus("No se actualizó el histórico: " + error.message, true); })
      .finally(function () {
        freshenBusy = false;
        if (button.isConnected) { button.disabled = false; button.textContent = "Actualizar histórico"; }
      });
  }
  function loadInstrumentSpec() {
    var seq = ++specSeq;
    var symbol = $id("perp-open-form").querySelector('[name="symbol"]').value;
    var target = $id("perp-instrument-spec");
    target.className = "perp-spec state loading";
    target.textContent = "Consultando límites GRVT…";
    request("/paper/perp/instrument?symbol=" + encodeURIComponent(symbol)).then(function (spec) {
      if (seq !== specSeq || !root || !root.isConnected) return;
      target.className = "perp-spec state ok";
      target.innerHTML = '<strong>Reglas GRVT</strong> · mín. ' + esc(spec.min_size) + ' ' + esc(symbol.split("/")[0]) +
        ' · paso ' + esc(spec.size_step) + ' · nocional ≥ ' + money(spec.min_notional) +
        ' · tick ' + esc(spec.tick_size);
    }).catch(function (error) {
      if (seq !== specSeq || !root || !root.isConnected) return;
      target.className = "perp-spec state error";
      target.textContent = "Límites GRVT no disponibles: " + error.message;
    });
  }
  function accountOptions() {
    var picker = $id("perp-account");
    if (!picker) return;
    var remembered = "";
    try { remembered = localStorage.getItem("orbis.paper.perp.account") || ""; } catch (error) {}
    if (!accounts.some(function (row) { return row.id === currentId; })) {
      currentId = accounts.some(function (row) { return row.id === remembered; }) ? remembered : (accounts[0] && accounts[0].id || "");
    }
    picker.innerHTML = accounts.length ? accounts.map(function (row) {
      return '<option value="' + esc(row.id) + '">' + esc(row.name) + (row.stopped_at ? " · DETENIDA" : "") + "</option>";
    }).join("") : '<option value="">Crea una cuenta PAPER</option>';
    picker.value = currentId;
    $id("perp-new").open = !accounts.length;
  }
  function reload() {
    var seq = ++generation;
    return request("/paper/perp/accounts").then(function (rows) {
      if (seq !== generation || !root || !root.isConnected) return;
      accounts = rows;
      accountOptions();
      return reloadDetails(seq);
    }).catch(function (error) { if (seq === generation) message(error.message, true); });
  }
  function reloadDetails(seq) {
    seq = seq || ++generation;
    if (valuationTimer) { clearTimeout(valuationTimer); valuationTimer = null; }
    var fundingPanel = $id("perp-funding-detail");
    if (fundingPanel) fundingPanel.hidden = true;
    var account = accounts.find(function (row) { return row.id === currentId; });
    if (!account) {
      chartPositions = []; chartEvents = [];
      $id("perp-summary").textContent = "Sin cuenta ficticia";
      $id("perp-positions").textContent = "Crea una cuenta para abrir posiciones";
      $id("perp-events").textContent = "Sin eventos";
      $id("perp-stop").hidden = true;
      $id("perp-open").disabled = true;
      $id("perp-signal-create").disabled = true;
      $id("perp-signal-sessions").textContent = "Crea una cuenta PAPER";
      loadPerpChart();
      return Promise.resolve();
    }
    $id("perp-summary").innerHTML = '<div class="perp-metrics"><div><small>Disponible</small><strong class="num">' + money(account.available_cash) +
      '</strong></div><div><small>Reservado</small><strong class="num">' + money(account.reserved_collateral) +
      '</strong></div><div><small data-help="Incluye financiación PAPER ya asentada; puede faltar cobertura en otras posiciones. No representa cargos reales de GRVT.">P/L registrado</small><strong class="num">' + money(account.realized_pnl) +
      '</strong></div></div><p class="muted">' + esc(account.open_positions) + '/' + esc(account.max_positions) +
      ' posiciones · máximo ' + money(account.max_total_notional) + ' · hasta ' + esc(account.max_leverage) +
      '× · funding PAPER asentado ' + money(account.funding_paid_total) + '</p>';
    $id("perp-stop").hidden = !!account.stopped_at;
    $id("perp-open").disabled = !!account.stopped_at;
    if (account.stopped_at) message("Cuenta detenida: nuevas entradas bloqueadas. Puedes cerrar posiciones abiertas.", false);
    return Promise.all([
      request("/paper/perp/accounts/" + encodeURIComponent(currentId) + "/positions"),
      request("/paper/perp/accounts/" + encodeURIComponent(currentId) + "/events"),
      request("/paper/perp/accounts/" + encodeURIComponent(currentId) + "/valuations")
        .catch(function () { return {}; })
    ]).then(function (result) {
      if (seq !== generation || !root || !root.isConnected) return;
      chartPositions = result[0]; chartEvents = result[1];
      paintPositions(result[0], result[2]);
      armValuationExpiry(result[2], seq);
      paintEvents(result[1]);
      loadPerpChart();
      return reloadSignals(seq);
    }).catch(function (error) { if (seq === generation) message(error.message, true); });
  }
  function signalStatus(text, error) {
    var target = $id("perp-signal-status");
    if (!target) return;
    target.textContent = text;
    target.className = "state " + (error ? "error" : "ok");
  }
  function reloadSignals(seq) {
    if (!currentId) return Promise.resolve();
    return Promise.all([
      request("/lab/strategy-versions"), request("/lab/datasets"),
      request("/paper/perp/signals?account_id=" + encodeURIComponent(currentId)),
      request("/paper/perp/signals/worker")
    ]).then(function (data) {
      if (seq !== generation || !root || !root.isConnected) return;
      var strategies = data[0].map(function (row) {
        return Object.assign({}, row, {version_id:row.id});
      }), datasets = data[1].filter(function (row) {
        return row.verified_public && row.source === "grvt-perp-trade-public" && row.symbol.endsWith(":PERP");
      }), sessions = data[2], worker = data[3], risk = worker.risk_stream || {}, funding = worker.funding || {};
      signalStrategies = strategies;
      var badge = $id("perp-signal-worker");
      signalWorkerActive = !!worker.running && !!risk.running && !!funding.running;
      var healthy = worker.running && worker.heartbeat_age_seconds != null && worker.heartbeat_age_seconds <= 120;
      badge.className = "statusbadge" + (healthy ? " on" : "");
      badge.textContent = healthy ? "OBSERVADOR · ACTIVO" : worker.running && worker.heartbeat_age_seconds == null ?
        "OBSERVADOR · INICIANDO" : worker.running ? "OBSERVADOR · SIN LATIDO" : "OBSERVADOR · INACTIVO";
      badge.title = worker.last_error || (healthy ? "Sólo mientras ORBIS esté abierto; el modo Auto PAPER usa el libro ficticio." :
        "Comprueba manualmente o reinicia ORBIS. Los huecos no se reconstruyen.");
      var riskBadge = $id("perp-risk-worker");
      var riskHealthy = risk.running && risk.connected && risk.last_frame_age_seconds != null && risk.last_frame_age_seconds <= 15;
      var riskNeeded = sessions.some(function (row) { return row.execution_mode === "AUTO_PAPER" && row.status === "ACTIVE" && !!row.position_id; });
      riskBadge.className = "statusbadge" + (riskHealthy ? " on" : "");
      riskBadge.textContent = riskHealthy ? "RIESGO PAPER · EN DIRECTO" : !risk.running ? "RIESGO PAPER · INACTIVO" :
        riskNeeded ? "RIESGO PAPER · SIN FEED" : "RIESGO PAPER · EN ESPERA";
      riskBadge.title = risk.last_error || (riskHealthy ? "Ticker público GRVT; no es una orden protectora real." :
        "Sin cobertura continua verificable. Si hay una posición abierta, comprueba su estado y ciérrala manualmente si es necesario.");
      var fundingBadge = $id("perp-funding-worker");
      fundingBadge.className = "statusbadge" + (funding.running && !funding.last_error ? " on" : "");
      fundingBadge.textContent = !funding.running ? "FUNDING · INACTIVO" :
        funding.last_error ? "FUNDING · PENDIENTE" : "FUNDING · ACTIVO";
      fundingBadge.title = funding.last_error || "Sólo posiciones AUTO PAPER. Consulta tasas públicas y asienta pagos ficticios; la cobertura se indica por posición.";
      var version = $id("perp-signal-form").querySelector('[name="version_id"]');
      var dataset = $id("perp-signal-form").querySelector('[name="dataset_id"]');
      var chosenVersion = pendingSelection && pendingSelection.version_id || version.value;
      var chosenDataset = pendingSelection && pendingSelection.dataset_id || dataset.value;
      version.innerHTML = strategies.length ? strategies.map(function (row) {
        return '<option value="' + esc(row.version_id) + '">' + esc(row.name) + ' · v' + esc(row.version) +
          ' · ' + esc(row.definition.direction) + '</option>';
      }).join("") : '<option value="">Sin estrategias LAB</option>';
      dataset.innerHTML = datasets.length ? datasets.map(function (row) {
        return '<option value="' + esc(row.id) + '">' + esc(row.symbol) + ' · ' + esc(row.timeframe) +
          ' · ' + esc(candleTime(row.end_t)) + '</option>';
      }).join("") : '<option value="">Descarga un histórico GRVT en LAB</option>';
      if (strategies.some(function (row) { return row.version_id === chosenVersion; })) version.value = chosenVersion;
      if (datasets.some(function (row) { return row.id === chosenDataset; })) dataset.value = chosenDataset;
      if (pendingSelection) {
        var matchedVersion = strategies.find(function (row) { return row.version_id === chosenVersion; });
        if (!matchedVersion || matchedVersion.strategy_id !== pendingSelection.strategy_id || dataset.value !== chosenDataset) {
          signalStatus("La versión o el histórico LAB ya no está disponible para GRVT PAPER. Elige una combinación compatible.", true);
        } else {
          signalStatus("Versión v" + matchedVersion.version + " e histórico preseleccionados. Elige modo y confirma para iniciar PAPER.", false);
        }
        pendingSelection = null;
      }
      $id("perp-signal-create").disabled = !strategies.length || !datasets.length ||
        !!accounts.find(function (row) { return row.id === currentId && row.stopped_at; });
      checkSignalReadiness();
      updateSignalMode();
      var target = $id("perp-signal-sessions");
      if (!sessions.length) { target.className = "state empty"; target.textContent = "Sin sesiones de señales"; inspectedSessionId = ""; return; }
      target.className = "tablewrap";
      target.innerHTML = '<table class="perp-table"><thead><tr><th>Mercado</th><th>Versión</th><th>Modo</th><th>Estado</th>' +
        '<th>Última vela</th><th>Señales</th><th>Acción</th></tr></thead><tbody>' +
        sessions.map(function (row) {
          return '<tr><td>' + esc(row.symbol) + ' · ' + esc(row.timeframe) + '</td><td class="mono">' +
            esc(row.version_id.slice(0, 8)) + '</td><td>' + esc(row.execution_mode === "AUTO_PAPER" ? "AUTO PAPER" : "OBSERVAR") +
            '</td><td>' + esc(row.status) + '</td><td class="num">' +
            esc(candleTime(row.last_t)) + '</td><td>' +
            (row.status === "ACTIVE" ? '<button type="button" class="ghost" data-signal-action="poll" data-session="' +
              esc(row.id) + '">Comprobar vela</button> <button type="button" class="ghost danger" data-signal-action="stop" data-session="' +
              esc(row.id) + '" data-open-position="' + esc(row.position_id || '') + '">Parar</button>' : '<span class="muted">' + esc(row.status_reason || "Finalizada") + '</span>') +
            ' <button type="button" class="ghost" data-signal-action="inspect" data-session="' + esc(row.id) + '">Inspeccionar</button></td></tr>';
        }).join("") + '</tbody></table><div id="perp-signal-detail" class="perp-signal-detail"></div>';
      if (inspectedSessionId && sessions.some(function (row) { return row.id === inspectedSessionId; })) {
        return request("/paper/perp/signals/" + encodeURIComponent(inspectedSessionId)).then(function (detail) {
          if (seq === generation && root && root.isConnected) paintSignalDetail(detail);
        });
      }
    }).catch(function (error) { if (seq === generation) signalStatus(error.message, true); });
  }
  function createSignalSession(event) {
    event.preventDefault();
    if (!currentId || !signalDatasetReady) return;
    var form = event.currentTarget, data = new FormData(form);
    var automatic = data.get("execution_mode") === "AUTO_PAPER";
    if (automatic && !window.confirm("¿Activar entradas y salidas con saldo PAPER? El stop usa el ticker público mientras ORBIS esté abierto; no es una orden protectora real. Un hueco deja la posición abierta para cierre manual.")) return;
    submitBusy(form, function () {
      var body = {account_id:currentId, version_id:String(data.get("version_id")),
                  dataset_id:String(data.get("dataset_id")), execution_mode:automatic ? "AUTO_PAPER" : "OBSERVE"};
      if (sourceRunSelection && sourceRunSelection.dataset_id === body.dataset_id &&
          sourceRunSelection.version_id === body.version_id) body.source_run_id = sourceRunSelection.run_id;
      if (automatic) { body.size = String(data.get("size") || "").trim().replace(",", ".");
                       body.leverage = String(data.get("leverage") || "").trim().replace(",", "."); }
      return request("/paper/perp/signals", {method:"POST", body:JSON.stringify(body)}).then(function () {
        sourceRunSelection = null;
        signalStatus(automatic ? "Auto PAPER activado. Comprueba el observador; un hueco detiene la sesión." :
          "Seguimiento iniciado. Comprueba el observador; un hueco detiene la sesión.", false);
        return reloadSignals(generation);
      }).catch(function (error) { signalStatus(error.message, true); });
    });
  }
  function signalAction(event) {
    var button = event.target.closest("button[data-signal-action]");
    if (!button || button.disabled) return;
    var action = button.getAttribute("data-signal-action");
    var sessionId = button.getAttribute("data-session");
    if (action === "stop" && !window.confirm(button.getAttribute("data-open-position") ?
      "¿Parar la sesión? La posición PAPER abierta NO se cerrará: tendrás que cerrarla manualmente." :
      "¿Parar esta sesión de señales PAPER?")) return;
    button.disabled = true;
    if (action === "verify") {
      var sourceBadge = $id("perp-signal-origin");
      if (sourceBadge) { sourceBadge.hidden = false; sourceBadge.dataset.status = "checking";
        sourceBadge.textContent = "Comprobando origen…"; }
      request("/paper/perp/signals/" + encodeURIComponent(sessionId) + "/verify").then(function (result) {
        if (inspectedSessionId !== sessionId) return;
        sourceCheck = {id:sessionId, result:result};
        var currentBadge = $id("perp-signal-origin");
        if (currentBadge) paintSourceCheck(currentBadge, result);
      }).catch(function (error) {
        if (inspectedSessionId !== sessionId) return;
        var result = {status:"unverifiable", verified:false, reason:error.message};
        sourceCheck = {id:sessionId, result:result};
        var currentBadge = $id("perp-signal-origin");
        if (currentBadge) paintSourceCheck(currentBadge, result);
      }).finally(function () { if (button.isConnected) button.disabled = false; });
      return;
    }
    if (action === "gap") {
      var gapBox = $id("perp-gap-report");
      var gapRequest = ++gapSeq;
      if (gapBox) gapBox.textContent = "Consultando velas del hueco…";
      request("/paper/perp/signals/" + encodeURIComponent(sessionId) + "/gap-analysis").then(function (report) {
        if (inspectedSessionId !== sessionId || gapRequest !== gapSeq || !gapBox || !gapBox.isConnected) return;
        gapBox.innerHTML = '<p class="amber">Reconstrucción guardada LAB · ' + esc(report.candles_reviewed) +
          ' velas · ' + esc(report.entry_signals) + ' entradas · ' + esc(report.exit_signals) +
          ' salidas · sin órdenes PAPER · archivo ' + esc(report.archive.id.slice(0, 8)) + '</p>' +
          (report.signals.length ? '<p class="muted">' + report.signals.slice(0, 20).map(function (row) {
            return esc(candleTime(row.candle_t)) + ' ' + (row.entry_match ? 'entrada' : 'salida');
          }).join(' · ') + (report.signals.length > 20 ? ' · +' + esc(report.signals.length - 20) : '') + '</p>' : '');
      }).catch(function (error) { if (gapBox && gapBox.isConnected) gapBox.textContent = "Hueco no verificable: " + error.message; })
        .finally(function () { if (button.isConnected) button.disabled = false; });
      return;
    }
    var path = "/paper/perp/signals/" + encodeURIComponent(sessionId) +
      (action === "poll" ? "/poll" : action === "stop" ? "/stop" : "");
    request(path, action === "inspect" ? undefined : {method:"POST"}).then(function (result) {
      if (action === "poll") signalStatus(result.status !== "ACTIVE" ?
        ("Sesión " + result.status + ": " + (result.status_reason || "revisa la posición PAPER")) :
        result.poll_result === "OBSERVED" ?
          (result.execution_mode === "AUTO_PAPER" ? "Vela observada; revisa la acción PAPER registrada." :
           "Vela observada; no se abrió ninguna posición.") :
          "Comprobación: " + result.poll_result, result.status !== "ACTIVE");
      if (action === "stop") signalStatus("Sesión detenida.", false);
      if (action !== "inspect") return reload().then(function () { paintSignalDetail(result); });
      paintSignalDetail(result);
    }).catch(function (error) { signalStatus(error.message, true); })
      .finally(function () { if (root && root.isConnected) button.disabled = false; });
  }
  function paintSourceCheck(node, result) {
    node.hidden = false;
    node.dataset.status = result.status;
    node.textContent = result.verified ? "Origen verificado" :
      result.status === "mismatch" ? "Origen divergente" : "Origen no verificable";
    node.setAttribute("data-help", result.reason + ". Sólo comprueba procedencia; no garantiza señales ni ejecución.");
  }
  function paintSignalDetail(result) {
    var target = $id("perp-signal-detail");
    if (!target) return;
    var seq = ++comparisonSeq;
    if (inspectedSessionId !== result.id) sourceCheck = null;
    inspectedSessionId = result.id;
    var rows = result.observations || [];
    var actions = result.actions || [];
    var coverage = result.coverage && result.coverage.rules || [];
    var coverageHtml = coverage.length ? '<div class="paper-coverage" aria-label="Cobertura de condiciones superiores">' +
      coverage.map(function (row) {
        var warming = row.status === "INSUFFICIENT";
        var side = row.path.indexOf("exit") === 0 ? "Salida" : "Entrada";
        var count = row.minimum_bars == null ? String(row.complete_bars) + " velas" :
          String(row.complete_bars) + "/" + String(row.minimum_bars);
        var help = warming ? "Aún no hay velas superiores suficientes para evaluar esta condición. No equivale a una señal fallida." :
          row.status === "COVERAGE_ONLY" ? "La estructura no tiene un mínimo fijo que garantice que pueda evaluarse." :
            "El indicador ya tiene calentamiento suficiente; no garantiza una señal.";
        return '<span class="paper-coverage-chip" data-status="' + esc(row.status) + '" data-help="' +
          esc(side + ' · ' + row.kind + '. ' + help) + '">' + esc(side) + ' · ' + esc(row.timeframe) + ' ' +
          esc(count) + (warming ? ' · calentando' : row.status === "COVERAGE_ONLY" ? ' · cobertura' : ' · evaluable') + '</span>';
      }).join("") + '</div>' : '';
    target.innerHTML = '<p class="muted">' + esc(result.symbol) + ' · ' + esc(result.timeframe) + ' · ' +
      esc(rows.length) + ' observaciones · ' + esc(result.execution_mode === "AUTO_PAPER" ? "AUTO PAPER" : "SOLO OBSERVAR") +
      (result.position_id ? ' · posición ' + esc(result.position_id.slice(0, 8)) : '') + '</p>' +
      (result.status === "ACTIVE" && Number.isInteger(result.capacity_remaining) && result.capacity_remaining <= 500 ?
        '<span class="paper-capacity-warning" data-help="La sesión se detiene al llegar a 5000 velas. No se reinicia ni traslada posiciones automáticamente.">Restan ' +
        esc(result.capacity_remaining) + ' velas</span>' : '') +
      '<div class="paper-origin-row"><button type="button" class="ghost" data-signal-action="verify" data-session="' +
        esc(result.id) + '" data-help="Comprueba la versión LAB, el dataset y la semilla histórica. No verifica precios actuales ni rentabilidad.">Verificar origen</button>' +
      '<span id="perp-signal-origin" class="paper-origin-check" role="status" hidden></span></div>' +
      coverageHtml +
      '<div id="perp-signal-comparison" class="perp-signal-compare state loading" role="status">Comparando LAB y PAPER…</div>' +
      (result.status_reason ? '<p class="amber" role="status">' + esc(result.status_reason) +
        (result.position_id ? ' · Cierra la posición manualmente si es necesario.' : '') + '</p>' : '') +
      (result.status === "GAP" ? '<button type="button" class="ghost" data-signal-action="gap" data-session="' +
        esc(result.id) + '" data-help="Calcula señales posteriores usando velas públicas. No simula stops, fills, financiación ni reanuda la sesión.">Revisar hueco</button>' +
        '<div id="perp-gap-report" role="status"></div>' : '') +
      (actions.length ? '<table class="perp-table"><thead><tr><th>Vela</th><th>Acción PAPER</th><th>Estado</th><th>Motivo</th></tr></thead><tbody>' +
        actions.slice(-20).reverse().map(function (item) { return '<tr><td>' + esc(candleTime(item.candle_t)) + '</td><td>' +
          esc(item.action) + '</td><td>' + esc(item.status) + '</td><td data-help="' +
          esc(item.reason === "TRAILING_STOP_MARK_STREAM" ?
            "El mark observado cruzó el stop calculado con velas cerradas; el cierre ficticio usa bid/ask del mismo ticker." :
            item.reason === "TRAILING_STOP_AT_CANDLE_CLOSE" ?
            "El cierre de vela cruzó el nivel anterior; se usa una cotización posterior para el cierre ficticio." :
            item.reason || "Motivo de la acción PAPER") + '">' + esc(actionReason(item.reason)) + '</td></tr>'; }).join('') +
        '</tbody></table>' : '') +
      (rows.length ? '<table class="perp-table"><thead><tr><th>Vela</th><th>Origen</th><th>Entrada</th><th>Salida</th><th>Precio</th><th>Regla</th></tr></thead><tbody>' +
        rows.slice(-30).reverse().map(function (row) {
          var e = row.evidence || {};
          return '<tr><td>' + esc(candleTime(row.candle_t)) + '</td><td>' + esc(e.observer === "LOCAL_WORKER" ? "AUTO" : "MANUAL") + '</td><td>' +
            (row.entry_match ? 'SEÑAL' : '—') + '</td><td>' + (row.exit_match === null ? '—' : row.exit_match ? 'SEÑAL' : '—') +
            '</td><td class="num">' + esc(e.candle && e.candle.c || '—') + '</td><td><code>' +
            esc(JSON.stringify({entrada:e.entry, salida:e.exit})) + '</code></td></tr>';
        }).join("") + '</tbody></table>' : '<p class="state empty">Todavía no se ha observado una vela nueva.</p>');
    if (result.status === "GAP") loadArchivedSignalGap(result.id);
    if (sourceCheck && sourceCheck.id === result.id) paintSourceCheck($id("perp-signal-origin"), sourceCheck.result);
    request("/paper/perp/signals/" + encodeURIComponent(result.id) + "/comparison").then(function (comparison) {
      var box = $id("perp-signal-comparison");
      if (seq !== comparisonSeq || inspectedSessionId !== result.id || !box) return;
      var history = comparison.historical, paper = comparison.paper,
        replay = comparison.signal_replay;
      var paperPnl = paper.funding_incomplete_closed ?
        'P/L cubierto ' + money(paper.realized_pnl_funding_covered_usdt) +
        ' · provisional ' + money(paper.realized_pnl_provisional_usdt) :
        'P/L cerrado ' + money(paper.realized_pnl_usdt);
      box.className = "perp-signal-compare";
      box.innerHTML = '<div><small>LAB · HISTÓRICO TEÓRICO</small>' +
        (history && history.verification && history.verification.verified ?
          '<strong>' + esc(history.metrics.trades) + ' operaciones · ' + money(history.metrics.net_profit) +
          ' neto</strong><span>✓ Verificado · drawdown ' + esc(history.metrics.max_drawdown_pct) + '% · run ' + esc(history.run_id.slice(0,8)) +
          (comparison.source_run_id ? ' · origen fijado al iniciar PAPER' : '') +
          (history.dataset_relation === 'REFRESHED_FROM' ? ' · origen de la semilla PAPER' : '') + '</span>' :
          history ? '<strong class="amber">Histórico no verificable</strong><span>' +
            esc(history.verification && history.verification.reason || 'Sin prueba de integridad') +
            ' · run ' + esc(history.run_id.slice(0,8)) + '</span>' :
          '<strong>Sin backtest guardado de esta versión ni de la semilla original</strong>') + '</div>' +
        '<div><small>PAPER · OBSERVADO</small><strong>' + esc(paper.executed_opens) + ' aperturas · ' +
          esc(paper.executed_closes) + ' cierres</strong><span>' + esc(paper.observed_candles) + ' velas · ' +
          esc(paper.open_positions) + ' abiertas · ' + paperPnl + '</span></div>' +
        '<p class="paper-signal-replay" role="status" data-state="' +
        (replay && replay.verified ? 'verified' : replay && replay.status === 'no_observations' ? 'pending' : 'unverified') + '" data-help="Recalcula cada señal PAPER a partir de las velas guardadas y la versión LAB. No comprueba fills ni autenticidad del exchange.">' +
        (replay && replay.verified ? 'Señales verificadas · ' + esc(replay.checked_observations) +
          (replay.checked_observations === 1 ? ' vela' : ' velas') :
          replay && replay.status === 'no_observations' ? 'Sin velas PAPER para verificar' :
          'Señales no verificables · ' + esc(replay && replay.reason || 'Sin replay')) + '</p>' +
        frequencyMarkup(comparison) +
        '<p class="muted">No son rentabilidades directamente comparables: cambian capital, tamaño, periodo y ejecución.</p>' +
        (paper.funding_incomplete_closed ? '<p class="amber">Financiación sin verificar hasta el cierre en ' +
          esc(paper.funding_incomplete_closed) + ' posición(es); el P/L provisional puede cambiar.</p>' : '');
    }).catch(function (error) {
      var box = $id("perp-signal-comparison");
      if (seq !== comparisonSeq || inspectedSessionId !== result.id || !box) return;
      box.className = "perp-signal-compare state error";
      box.textContent = "Comparación no disponible: " + error.message;
    });
  }
  function loadArchivedSignalGap(id) {
    var seq = ++gapSeq, box = $id("perp-gap-report");
    request("/paper/perp/signals/" + encodeURIComponent(id) + "/gap-analyses").then(function (saved) {
      if (!box || !box.isConnected || seq !== gapSeq || inspectedSessionId !== id || !saved.length) return;
      var latest = saved[0];
      box.innerHTML = '<p class="muted">Última reconstrucción LAB · ' + esc(latest.result.candles_reviewed) +
        ' velas · ' + esc(latest.result.entry_signals) + ' entradas · sin órdenes PAPER · archivo ' +
        esc(latest.id.slice(0, 8)) + '</p>';
    }).catch(function (error) { if (box && box.isConnected && seq === gapSeq) box.textContent = "Archivo no verificable: " + error.message; });
  }
  function armValuationExpiry(valuations, seq) {
    if (valuationTimer) { clearTimeout(valuationTimer); valuationTimer = null; }
    var fresh = Object.keys(valuations).map(function (id) { return valuations[id]; })
      .filter(function (item) { return item && item.status === "FRESH"; });
    if (!fresh.length) return;
    var remaining = Math.min.apply(null, fresh.map(function (item) { return item.remaining_ms; }));
    valuationTimer = setTimeout(function () {
      valuationTimer = null;
      if (!root || !root.isConnected || seq !== generation || !currentId) return;
      request("/paper/perp/accounts/" + encodeURIComponent(currentId) + "/valuations")
        .then(function (latest) {
          if (seq !== generation || !root || !root.isConnected) return;
          paintPositions(chartPositions, latest);
          armValuationExpiry(latest, seq);
        }).catch(function () {
          if (seq === generation && root && root.isConnected) paintPositions(chartPositions, {});
        });
    }, Math.max(1, remaining + 1));
  }
  function paintPositions(rows, valuations) {
    var target = $id("perp-positions");
    $id("perp-funding-detail").hidden = true;
    if (!rows.length) { target.className = "state empty"; target.textContent = "Todavía no hay posiciones"; return; }
    target.className = "tablewrap";
    target.innerHTML = '<table class="perp-table"><thead><tr><th>Instrumento</th><th>Lado</th><th>Cantidad</th><th>Entrada</th>' +
      '<th>Colateral</th><th>Marca</th><th>Estado / resultado</th><th>Acción</th></tr></thead><tbody>' + rows.map(function (row) {
        var active = row.closed_at_ns == null, snapshot = valuations[row.id] || {},
          mark = snapshot.status === "FRESH" ? snapshot.details : null,
          riskAlert = mark && mark.risk_type;
        var result = active ?
          (riskAlert ? (riskAlert === "STREAM_COVERAGE_GAP" ? 'Sin ticker · cierre manual' :
            riskAlert.endsWith("MAINTENANCE_BREACH") ? 'Riesgo de margen · revisar' : 'Margen no verificable · revisar') :
            mark ? 'P/L precio ' + money(mark.unrealized_pnl) :
              snapshot.status === "COVERAGE_GAP" ? 'Sin ticker · cierre manual' :
              snapshot.status === "STALE" ? 'Valoración caducada · pulsa Valorar' :
              snapshot.status === "OUTDATED_BOOKING" ? 'Valoración anterior al funding asentado · pulsa Valorar' :
              snapshot.status === "UNVERIFIABLE" ? 'Valoración no verificable' : 'Sin valoración reciente') :
          ('P/L PAPER ' + money(row.realized_pnl) +
            (row.funding_coverage === 'CLOSED_COMPLETE' ? ' · funding cubierto' : ' · funding pendiente'));
        var risk = active && mark && mark.margin_buffer != null ?
          '<span class="perp-risk ' + (riskAlert || mark.maintenance_status === "AT_OR_BELOW_MAINTENANCE_ON_MARK" ? 'perp-risk-low' : '') +
          '" data-help="' + (riskAlert ? (riskAlert.startsWith('CURRENT_RULES') ? 'Alarma con reglas públicas recientes; financiación no asentada puede cambiar el resultado. No confirma liquidación.' : 'Alarma histórica con reglas guardadas al abrir; financiación o reglas actuales pueden cambiar el resultado. No confirma liquidación.') :
            'Margen calculado con una cotización y reglas actuales; incluye financiación PAPER asentada, no la aún pendiente. No confirma liquidación GRVT.') + '">' +
          (riskAlert || mark.maintenance_status === "AT_OR_BELOW_MAINTENANCE_ON_MARK" ? '⚠ Bajo mantenimiento' : 'Margen') +
          ' · ' + money(mark.margin_buffer) + '</span>' : '';
        var threshold = active && mark && !riskAlert && mark.estimated_maintenance_mark != null ?
          '<span class="perp-risk" data-help="Umbral teórico de mantenimiento aislado según esta valoración guardada, reglas GRVT de ese momento y financiación PAPER asentada. No es precio de liquidación, no predice funding futuro ni garantiza continuidad del mark.">Umbral ref. ' +
          esc(mark.estimated_maintenance_mark) + '</span>' : '';
        var fundingStatus = active && row.mode === "PAPER_AUTO" ?
          (row.funding_coverage === "UNASSESSED" ? " · funding sin verificar" : " · funding parcial") : "";
        var legacyFunding = row.funding_anchor_status === "LEGACY_UNANCHORED" ?
          ' · financiación legada sin ancla pública' : '';
        var staleRisk = active && snapshot.status !== "FRESH" && snapshot.last_risk_type ?
          (snapshot.last_risk_type === "STREAM_COVERAGE_GAP" ?
            '<span class="perp-risk perp-risk-low" data-help="Se perdió la cobertura del ticker GRVT. La posición ficticia sigue abierta y no se reconstruye una salida automática."> ⚠ Sin ticker · posición abierta</span>' :
            '<span class="perp-risk perp-risk-low" data-help="Alarma del último control histórico. La cotización puede haber caducado o cambiado la financiación: no demuestra el margen actual."> ⚠ Último riesgo de margen · ' +
            (snapshot.status === "STALE" ? 'caducado' : 'sin vigencia') + '</span>') : '';
        return '<tr><td>' + esc(row.symbol) + (row.mode === "PAPER_AUTO" ? ' · AUTO' : '') + '</td><td class="' + (row.side === "LONG" ? "up" : "down") + '">' + esc(row.side) +
          '</td><td class="num">' + esc(row.size) + '</td><td class="num">' + esc(row.entry_price) +
          '</td><td class="num">' + money(row.collateral) + '</td><td class="num">' +
          (active ? (mark ? esc(mark.mark_price) : '—') : esc(row.exit_mark || row.entry_mark)) +
          '</td><td>' + result + fundingStatus + legacyFunding + (row.funding_paid !== '0' ? ' · funding ' + money(row.funding_paid) : '') + risk + threshold + staleRisk + '</td><td>' + (active ?
            '<button type="button" class="ghost" data-perp-action="observe" data-position="' + esc(row.id) + '">Valorar</button> ' +
            '<button type="button" class="ghost" data-perp-action="close" data-position="' + esc(row.id) + '">Cerrar</button>' :
            '<span class="muted">Cerrada</span>') +
            ' <button type="button" class="ghost" data-perp-action="funding" data-position="' + esc(row.id) +
            '" data-help="Consulta tasas públicas GRVT; es una estimación que no modifica el saldo PAPER.">Financiación</button></td></tr>';
      }).join("") + '</tbody></table>';
  }
  function paintFunding(result) {
    var target = $id("perp-funding-detail");
    if (!target) return;
    var amount = String(result.estimated_funding_paid);
    var rows = result.settlements || [];
    var direction = amount[0] === "-" ? "Cobro estimado" :
      rows.length ? "Coste estimado" : "Sin liquidaciones";
    var magnitude = amount[0] === "-" ? amount.slice(1) : amount;
    target.className = "state ok perp-funding-detail";
    target.hidden = false;
    target.innerHTML = '<div class="pagina-cabecera"><div><p class="real-eyebrow">Estimación pública · ' +
      (result.paper_coverage === 'CLOSED_COMPLETE' ? 'PAPER cubierto' :
        result.paper_coverage === 'PARTIAL' ? 'PAPER parcialmente asentado' : 'no contabilizada') + '</p>' +
      '<h4>Financiación · ' + esc(result.instrument) + ' · ' + esc(result.side) + '</h4></div>' +
      '<strong class="num ' + (amount[0] === "-" ? 'perp-funding-credit' : 'perp-funding-cost') + '">' +
      direction + ' ' + money(magnitude) + '</strong></div>' +
      '<p class="muted">' + esc(rows.length) + ' liquidaciones públicas desde la apertura. ' +
      'La consulta no modifica el libro; PAPER asentado ' + money(result.paper_booked_funding_paid || '0') +
      '. No acredita cargos o abonos de tu cuenta GRVT.</p>' +
      '<button type="button" class="ghost" data-perp-action="settle-funding" data-position="' + esc(result.position_id) +
      '" data-help="Asienta sólo liquidaciones nuevas en el libro ficticio, con deduplicación y cobertura completa. Nunca envía una orden ni toca GRVT real.">Asentar en PAPER</button>' +
      (rows.length ? '<details><summary>Ver últimas liquidaciones</summary><div class="tablewrap"><table class="perp-table">' +
        '<thead><tr><th>Hora</th><th>Tasa</th><th>Mark</th><th>Coste (+) / cobro (−)</th></tr></thead><tbody>' +
        rows.slice(-20).reverse().map(function (row) {
          return '<tr><td>' + esc(new Date(Number(row.funding_time_ns) / 1000000).toLocaleString("es-ES")) +
            '</td><td class="num">' + esc(row.rate_percent) + ' %</td><td class="num">' + money(row.mark_price) +
            '</td><td class="num">' + money(row.estimated_payment) + '</td></tr>';
        }).join("") + '</tbody></table></div></details>' : '');
  }
  function paintEvents(rows) {
    var target = $id("perp-events");
    if (!rows.length) { target.className = "state empty"; target.textContent = "Sin eventos"; return; }
    target.className = "tablewrap";
    target.innerHTML = '<table class="perp-table"><thead><tr><th>Registro</th><th>Evento</th><th>Posición</th><th>Precio</th><th>Resultado</th></tr></thead><tbody>' +
      rows.slice().reverse().map(function (row) {
        var d = row.details || {};
        var gap = d.risk_type === "STREAM_COVERAGE_GAP";
        var kind = gap ? "SIN TICKER" :
          d.risk_type && d.risk_type.endsWith("MAINTENANCE_BREACH") ? "RIESGO MARGEN" :
          d.risk_type && d.risk_type.endsWith("UNVERIFIABLE") ? "MARGEN INCIERTO" : row.kind;
        var gapSeconds = Number(d.gap_ns) / 1000000000;
        var result = gap ? (Number.isFinite(gapSeconds) && gapSeconds > 0 ?
          '≥' + Math.floor(gapSeconds) + ' s sin cobertura' : 'Cobertura perdida') :
          d.risk_type ? (d.margin_buffer != null ? "Margen " + d.margin_buffer : "Revisar") :
          d.realized_pnl || d.unrealized_pnl || d.payment || "—";
        return '<tr><td>' + esc(U.fmtDateTime(row.recorded_at)) + '</td><td' +
          (gap ? ' data-help="Sesión AUTO PAPER detenida. La posición sigue abierta; no se inventa un cierre durante el hueco."' : '') +
          '>' + esc(kind) +
          '</td><td class="mono">' + esc(row.position_id.slice(0, 8)) + '</td><td class="num">' +
          esc(d.fill_price || d.mark_price || "—") + '</td><td class="num">' +
          esc(result) + '</td></tr>';
      }).join("") + '</tbody></table>';
  }
  function removePriceChart() {
    if (!priceChart) return;
    try { priceChart.remove(); } catch (error) {}
    priceChart = null;
  }
  function chartBars(payload, symbol, timeframe) {
    if (!payload || !payload.available || payload.symbol !== symbol ||
        payload.timeframe !== timeframe || payload.resolved_provider !== "grvt") {
      throw new Error(payload && payload.reason || "Velas públicas GRVT no disponibles para este par y periodo");
    }
    var raw = payload.candles;
    if (!Array.isArray(raw) || raw.length < 2) throw new Error("GRVT no devolvió suficientes velas");
    var previous = -1;
    return raw.map(function (row) {
      var time = Number(row.t), open = Number(row.o), high = Number(row.h);
      var low = Number(row.l), close = Number(row.c);
      if (!Number.isSafeInteger(time) || time <= previous || !isFinite(open) || !isFinite(high) ||
          !isFinite(low) || !isFinite(close) || low <= 0 || low > Math.min(open, close) ||
          high < Math.max(open, close)) throw new Error("Velas GRVT malformadas o desordenadas");
      previous = time;
      return {time:time, open:open, high:high, low:low, close:close};
    });
  }
  function chartMarks(bars, positions, events, signals, step) {
    var available = new Set(bars.map(function (bar) { return bar.time; }));
    function containingBar(second) {
      if (!Number.isFinite(second) || !Number.isFinite(step) || step <= 0) return null;
      var lo = 0, hi = bars.length;
      while (lo < hi) {
        var mid = Math.floor((lo + hi) / 2);
        if (bars[mid].time <= second) lo = mid + 1;
        else hi = mid;
      }
      if (!lo) return null;
      var start = bars[lo - 1].time;
      return second < start + step ? start : null;
    }
    var byId = {};
    positions.forEach(function (position) { byId[position.id] = position; });
    var marks = [], actions = [], paperCount = 0, signalCount = 0, outside = 0, pending = 0;
    events.forEach(function (event) {
      if (event.kind !== "OPEN" && event.kind !== "CLOSE") return;
      var position = byId[event.position_id];
      if (!position) return;
      var second = Math.floor(Number(event.observed_at_ns) / 1000000000);
      var time = containingBar(second);
      if (time === null && bars.length && second >= bars[0].time &&
          second < bars[bars.length - 1].time + 2 * step) {
        time = bars[0].time + Math.floor((second - bars[0].time) / step) * step;
      }
      var price = Number(event.details && event.details.fill_price);
      if (time === null || !Number.isFinite(price) || price <= 0) { outside++; return; }
      var buying = (position.side === "LONG") === (event.kind === "OPEN");
      actions.push({time:time, price:price, marker:{time:time,
        position:buying ? "belowBar" : "aboveBar", shape:buying ? "arrowUp" : "arrowDown",
        color:buying ? "#41c691" : "#ef7373",
        text:event.kind === "OPEN" ? "P·IN" : "P·OUT", id:event.id}});
      paperCount++;
      if (!available.has(time)) pending++;
    });
    signals.forEach(function (signal) {
      if (!available.has(signal.candle_t)) { outside++; return; }
      if (signal.entry_match) {
        marks.push({time:signal.candle_t, position:signal.direction === "SHORT" ? "aboveBar" : "belowBar",
          shape:"circle", color:"#71a9ec", text:"S·IN", id:signal.session_id + ":" + signal.candle_t + ":entry"});
        signalCount++;
      }
      if (signal.exit_match) {
        marks.push({time:signal.candle_t, position:signal.direction === "SHORT" ? "belowBar" : "aboveBar",
          shape:"square", color:"#eebd63", text:"S·OUT", id:signal.session_id + ":" + signal.candle_t + ":exit"});
        signalCount++;
      }
    });
    marks.sort(function (a, b) { return a.time - b.time || a.text.localeCompare(b.text); });
    actions.sort(function (a, b) { return a.time - b.time || a.marker.id.localeCompare(b.marker.id); });
    return {items:marks, actions:actions, paperCount:paperCount,
      signalCount:signalCount, outside:outside, pending:pending};
  }
  function fetchSignalMarkerPages(requestFn, currentFn, accountId, symbol, timeframe, bars) {
    var signals = [];
    function next(afterT, afterSessionId) {
      if (!currentFn()) return Promise.resolve(null);
      var path = "/paper/perp/signals/chart-markers-page?account_id=" + encodeURIComponent(accountId) +
        "&symbol=" + encodeURIComponent(symbol) + "&timeframe=" + encodeURIComponent(timeframe) +
        "&since_t=" + bars[0].time + "&until_t=" + bars[bars.length - 1].time + "&limit=500";
      if (afterT !== null) path += "&after_t=" + afterT + "&after_session_id=" + encodeURIComponent(afterSessionId);
      return requestFn(path).then(function (result) {
        if (!currentFn()) return null;
        if (result.source !== "GRVT_PAPER_OBSERVED_SIGNALS" ||
            result.execution !== "SIGNALS_SEPARATE_FROM_PAPER_EVENTS" ||
            result.symbol !== symbol || result.timeframe !== timeframe || !Array.isArray(result.markers)) {
          throw new Error("Procedencia de señales no verificable");
        }
        signals.push.apply(signals, result.markers);
        if (!result.has_more) return {bars:bars, signals:signals, warning:""};
        var nextT = result.next_after_t, nextId = result.next_after_session_id;
        if (!Number.isInteger(nextT) || typeof nextId !== "string" || !nextId ||
            (afterT !== null && (nextT < afterT || (nextT === afterT && nextId <= afterSessionId)))) {
          throw new Error("Cursor de señales GRVT inválido");
        }
        return next(nextT, nextId);
      });
    }
    return next(null, "");
  }
  function loadPerpChart() {
    var host = $id("perp-chart"), status = $id("perp-chart-status"), note = $id("perp-chart-note");
    if (!host || !status || !note) return Promise.resolve();
    var seq = ++chartSeq, accountId = currentId;
    var symbol = $id("perp-chart-symbol").value, timeframe = $id("perp-chart-tf").value;
    var button = $id("perp-chart-refresh");
    if (!CHART_STEPS[timeframe]) return Promise.resolve();
    removePriceChart();
    host.hidden = true; note.hidden = true; status.hidden = false;
    status.className = "state loading";
    status.textContent = "Cargando velas públicas GRVT…";
    button.disabled = true;
    var current = function () { return seq === chartSeq && accountId === currentId && root && root.isConnected; };
    return request("/market/candles?symbol=" + encodeURIComponent(symbol) +
                   "&provider=grvt&timeframe=" + encodeURIComponent(timeframe) +
                   "&limit=" + (timeframe === "1d" ? "1000" : "500"))
      .then(function (payload) {
        if (!current()) return null;
        var bars = chartBars(payload, symbol, timeframe);
        if (!accountId) return {bars:bars, signals:[], warning:""};
        return fetchSignalMarkerPages(request, current, accountId, symbol, timeframe, bars).catch(function (error) {
          return {bars:bars, signals:[], warning:"Señales no disponibles: " + error.message};
        });
      }).then(function (data) {
        if (!data || !current()) return;
        var LW = window.LightweightCharts;
        if (!LW || !LW.CandlestickSeries || !LW.LineSeries || !LW.createSeriesMarkers) {
          throw new Error("Biblioteca local de gráficos no disponible");
        }
        host.hidden = false;
        priceChart = LW.createChart(host, {
          autoSize:true, layout:{background:{color:"#08100f"}, textColor:"#9caaa4"},
          grid:{vertLines:{color:"#172521"}, horzLines:{color:"#172521"}},
          timeScale:{timeVisible:true,minBarSpacing:0.1,rightOffset:8}, localization:{locale:"es-ES"}
        });
        var series = priceChart.addSeries(LW.CandlestickSeries, {
          upColor:"#26a69a", downColor:"#ef5350", borderUpColor:"#26a69a",
          borderDownColor:"#ef5350", wickUpColor:"#26a69a", wickDownColor:"#ef5350"
        });
        series.setData(data.bars);
        var marks = chartMarks(data.bars, chartPositions.filter(function (row) { return row.symbol === symbol; }),
                               chartEvents, data.signals, CHART_STEPS[timeframe]);
        LW.createSeriesMarkers(series, marks.items);
        var lanes = [];
        marks.actions.forEach(function (action) {
          var lane = lanes.find(function (candidate) { return !candidate.times.has(action.time); });
          if (!lane) { lane = {times:new Set(), actions:[]}; lanes.push(lane); }
          lane.times.add(action.time);
          lane.actions.push(action);
        });
        lanes.forEach(function (lane) {
          var actionSeries = priceChart.addSeries(LW.LineSeries, {lineVisible:false,
            pointMarkersVisible:false,crosshairMarkerVisible:false,
            priceLineVisible:false,lastValueVisible:false});
          actionSeries.setData(lane.actions.map(function (action) {
            return {time:action.time,value:action.price};
          }));
          LW.createSeriesMarkers(actionSeries, lane.actions.map(function (action) { return action.marker; }));
        });
        priceChart.timeScale().fitContent();
        if (window.OrbisChartRange) window.OrbisChartRange.mount($id("perp-chart-range"), priceChart, data.bars, {
          selected:chartRange,
          storageKey:"orbis.chartRange.grvtPaper",
          eventTimes:marks.actions.map(function (action) { return action.time; }),
          onChange:function (value) {
            chartRange = value;
            if (value !== "all" && timeframe !== "1d") {
              $id("perp-chart-tf").value = "1d";
              loadPerpChart();
              return true;
            }
            return false;
          }
        });
        priceChart.timeScale().applyOptions({rightOffset:8});
        host.dataset.paperMarkers = String(marks.paperCount);
        host.dataset.signalMarkers = String(marks.signalCount);
        status.hidden = true;
        note.hidden = false;
        note.textContent = data.bars.length + " velas · " + marks.paperCount + " marcas PAPER · " +
          marks.signalCount + (marks.signalCount === 1 ? " señal" : " señales") +
          (marks.pending ? " · " + marks.pending + (marks.pending === 1 ? " operación sin OHLC aún" : " operaciones sin OHLC aún") : "") +
          (marks.outside ? " · " + marks.outside + " sin marca: fuera de cobertura o precio no disponible" : "") +
          (data.warning ? " · " + data.warning : "");
      }).catch(function (error) {
        if (!current()) return;
        removePriceChart();
        host.hidden = true;
        status.hidden = false;
        status.className = "state error";
        status.textContent = "Gráfico GRVT no disponible: " + error.message + ". Pulsa Actualizar gráfico.";
      }).finally(function () { if (current()) button.disabled = false; });
  }
  function submitBusy(form, callback) {
    var button = form.querySelector('button[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    callback().finally(function () { if (root && root.isConnected) button.disabled = false; });
  }
  function createAccount(event) {
    event.preventDefault();
    var form = event.currentTarget, data = new FormData(form), body = {};
    ["name", "initial_balance", "max_position_notional", "max_total_notional", "max_leverage", "fee_rate", "slippage_bps"].forEach(function (key) {
      body[key] = String(data.get(key) || "").trim().replace(",", ".");
    });
    body.max_positions = Number(data.get("max_positions"));
    submitBusy(form, function () {
      return request("/paper/perp/accounts", {method:"POST", body:JSON.stringify(body)}).then(function (account) {
        currentId = account.id;
        $id("perp-new").open = false;
        message("Cuenta PAPER creada. No se ha conectado ninguna cuenta real.", false);
        return reload();
      }).catch(function (error) { message(error.message, true); });
    });
  }
  function open(event) {
    event.preventDefault();
    if (!currentId) { message("Crea una cuenta PAPER primero.", true); return; }
    var form = event.currentTarget, data = new FormData(form);
    var body = {request_id:U.newRequestId(), symbol:String(data.get("symbol")), side:String(data.get("side")),
                size:String(data.get("size") || "").trim().replace(",", "."),
                leverage:String(data.get("leverage") || "").trim().replace(",", ".")};
    submitBusy(form, function () {
      return request("/paper/perp/accounts/" + encodeURIComponent(currentId) + "/positions",
                     {method:"POST", body:JSON.stringify(body)}).then(function (result) {
        message("Posición PAPER abierta a " + result.position.entry_price + " USDT. Sin orden real.", false);
        $id("perp-chart-symbol").value = body.symbol;
        return reload();
      }).catch(function (error) { message(error.message, true); });
    });
  }
  function positionAction(event) {
    var button = event.target.closest("button[data-perp-action]");
    if (!button || button.disabled || !currentId) return;
    var action = button.getAttribute("data-perp-action"), positionId = button.getAttribute("data-position");
    button.disabled = true;
    if (action === "settle-funding") {
      var bookingAccount = currentId;
      request("/paper/perp/accounts/" + encodeURIComponent(bookingAccount) + "/positions/" +
              encodeURIComponent(positionId) + "/funding-settle",
              {method:"POST", body:JSON.stringify({request_id:U.newRequestId()})})
        .then(function (result) {
          if (bookingAccount !== currentId || !root || !root.isConnected) return;
          message("Financiación PAPER asentada: " + result.new_settlements +
                  " liquidaciones nuevas · " + result.new_payment + " USDT. Sin pago real.", false);
          return reload();
        }).catch(function (error) { if (bookingAccount === currentId) message(error.message, true); })
        .finally(function () { if (root && root.isConnected) button.disabled = false; });
      return;
    }
    if (action === "funding") {
      var seq = generation, accountId = currentId, detail = $id("perp-funding-detail");
      detail.hidden = false;
      detail.className = "state loading perp-funding-detail";
      detail.textContent = "Consultando liquidaciones públicas GRVT…";
      request("/paper/perp/accounts/" + encodeURIComponent(accountId) + "/positions/" +
              encodeURIComponent(positionId) + "/funding-estimate")
        .then(function (result) {
          if (seq === generation && accountId === currentId && root && root.isConnected) paintFunding(result);
        }).catch(function (error) {
          if (seq !== generation || accountId !== currentId || !root || !root.isConnected) return;
          detail.className = "state error perp-funding-detail";
          detail.textContent = "Estimación no disponible: " + error.message + ". No se ha supuesto un coste cero.";
        }).finally(function () { if (root && root.isConnected) button.disabled = false; });
      return;
    }
    var path = "/paper/perp/accounts/" + encodeURIComponent(currentId) + "/positions/" + encodeURIComponent(positionId) + "/" + action;
    var options = {method:"POST"};
    if (action === "close") options.body = JSON.stringify({request_id:U.newRequestId()});
    request(path, options).then(function (result) {
      message(action === "close" ? "Posición PAPER cerrada: " + result.position.realized_pnl + " USDT; " +
        (result.position.funding_coverage === "CLOSED_COMPLETE" ? "funding PAPER cubierto." : "funding aún pendiente de comprobar.") +
        (result.position.funding_anchor_status === "LEGACY_UNANCHORED" ? " Histórico legado sin ancla pública." : "") :
        "Valoración guardada: " + result.unrealized_pnl + " USDT sin realizar." +
        (result.maintenance_status === "AT_OR_BELOW_MAINTENANCE_ON_MARK" ? " Riesgo de mantenimiento en esta marca; no confirma liquidación." : ""), false);
      if (action === "close") $id("perp-chart-symbol").value = result.position.symbol;
      return reload();
    }).catch(function (error) { message(error.message, true); })
      .finally(function () { if (root && root.isConnected) button.disabled = false; });
  }
  function stop() {
    if (!currentId || !window.confirm("Detener definitivamente nuevas entradas de esta cuenta PAPER? Podrás cerrar las abiertas.")) return;
    var button = $id("perp-stop");
    button.disabled = true;
    request("/paper/perp/accounts/" + encodeURIComponent(currentId) + "/stop", {method:"POST"})
      .then(function () { message("Nuevas entradas detenidas.", false); return reload(); })
      .catch(function (error) { message(error.message, true); })
      .finally(function () { if (root && root.isConnected) button.disabled = false; });
  }
  function cleanup() { if(observedWidget){observedWidget.dispose();observedWidget=null;} generation++; specSeq++; chartSeq++; removePriceChart(); if (signalTimer) { clearInterval(signalTimer); signalTimer = null; } if (valuationTimer) { clearTimeout(valuationTimer); valuationTimer = null; } root = null; U = null; inspectedSessionId = ""; sourceCheck = null; }
  window.OrbisPerp = {render:render, cleanup:cleanup};
})();
