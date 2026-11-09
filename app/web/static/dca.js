/* DCA local: histórico y observación PAPER Spot; nunca envía órdenes. */
(function () {
  "use strict";
  var root = null, chart = null, paperChart = null, paperChartKey = "", paperChartSeq = 0;
  var datasets = [], rules = [], runs = [], paperSessions = [], paperWorker = null, paperTimer = null, paperComparisonSeq = 0, paperGapSeq = 0, paperStarting = false;
  var paperRunChoice = null;
  var paperReserveSession = null, paperReserveSeq = 0;
  var validationSeq = 0;
  var transfer = null;
  function esc(x) { return String(x == null ? "" : x).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }
  function rounded(x, digits) {
    if (x == null) return "—";
    var formatter = window.OrbisUI && window.OrbisUI.fmtDecimal;
    var shown = formatter && formatter(String(x), 0, digits === undefined ? 2 : digits);
    return shown === null || shown === undefined ? "—" : "≈ " + shown;
  }
  function api(path, options) {
    return fetch(path, Object.assign({headers:{"Content-Type":"application/json"}}, options || {})).then(function (response) {
      return response.json().then(function (body) { if (!response.ok) throw new Error(body.detail || "Error DCA"); return body; });
    });
  }
  function notice(message, bad) {
    var target = root && root.querySelector("#dca-notice");
    if (target) { target.textContent = message; target.className = "state " + (bad ? "error" : "ok"); target.hidden = false; }
  }
  function render(container) {
    validationSeq++;
    paperComparisonSeq++;
    if (chart) { chart.remove(); chart = null; }
    clearPaperChart();
    if (paperTimer) { clearInterval(paperTimer); paperTimer = null; }
    root = container;
    transfer = null;
    paperRunChoice = null;
    paperReserveSession = null;
    paperReserveSeq++;
    root.innerHTML = '<section class="card dca-page"><div class="pagina-cabecera"><div><p class="real-eyebrow">AUTOMATIZACIONES · CAPITAL FICTICIO</p>' +
      '<h2 data-help="Compara reglas DCA sobre velas históricas conservadas, con presupuesto, costes y límites de exposición.">DCA</h2></div>' +
      '<span class="statusbadge on">HISTÓRICO · SIN ÓRDENES</span></div><p id="dca-notice" class="state" role="status" hidden></p>' +
      '<div class="dca-grid"><section class="real-panel"><h3>Regla</h3><label class="field">Plan guardado<select id="dca-rule"></select></label>' +
      '<label class="field">Versión<select id="dca-version"><option value="">Guarda un plan</option></select></label>' +
      '<div class="row"><button type="button" class="ghost" id="dca-import">Importar JSON</button>' +
      '<button type="button" class="ghost" id="dca-export" disabled>Exportar versión</button>' +
      '<input type="file" id="dca-import-file" aria-label="Archivo DCA JSON" accept=".json,application/json" hidden></div>' +
      '<section id="dca-import-review" class="state" hidden aria-label="Revisar plan importado">' +
      '<strong id="dca-import-name"></strong><p class="muted" id="dca-import-summary"></p>' +
      '<details><summary>Regla completa</summary><pre id="dca-import-definition" class="lab-events"></pre></details>' +
      '<div class="row"><button type="button" id="dca-import-save">Guardar copia</button>' +
      '<button type="button" id="dca-import-cancel" class="ghost">Cancelar</button></div></section>' +
      '<form id="dca-form" class="dca-form"><label class="field">Nombre<input name="name" value="DCA BTC" maxlength="100" required></label>' +
      '<label class="field" data-help="Primera apertura observada en cada periodo UTC. La primera compra posible requiere una vela cerrada previa.">Frecuencia<select name="frequency"><option value="monthly">Mensual</option><option value="weekly">Semanal</option><option value="daily">Diaria</option></select></label>' +
      '<label class="field">Importe fijo<input name="amount" value="100" inputmode="decimal" required></label>' +
      '<label class="field">Presupuesto total<input name="budget_total" value="2000" inputmode="decimal" required></label>' +
      '<label class="field">Máximo mensual<input name="monthly_limit" value="300" inputmode="decimal" required></label>' +
      '<label class="field" data-help="Fracción máxima del patrimonio ficticio en el activo. 0.8 equivale al 80 %.">Exposición máxima<input name="max_exposure" value="0.8" inputmode="decimal" required></label>' +
      '<label class="field" data-help="Aplica la condición al importe periódico y a los extras.">Condición de compra<select name="sma_condition"><option value="">Desactivada</option>' +
      '<optgroup label="Filtros"><option value="below">Comprar bajo SMA</option><option value="above">Comprar sobre SMA</option>' +
      '<option value="volume">Volumen relativo</option></optgroup>' +
      '<optgroup label="Importe variable"><option value="sma-below">Cierre bajo SMA</option><option value="sma-above">Cierre sobre SMA</option>' +
      '<option value="ema-below">Cierre bajo EMA</option><option value="ema-above">Cierre sobre EMA</option></optgroup></select></label>' +
      '<label class="field">Periodo · velas<input name="sma_period" type="number" min="2" max="120" value="20" required disabled></label>' +
      '<label class="field" data-dca-volume-field hidden data-help="Volumen de la vela cerrada dividido por la media de las velas anteriores. 1.5 exige al menos 150 %.">Volumen mínimo · ×<input name="volume_min_ratio" value="1.5" inputmode="decimal" required disabled></label>' +
      '<label class="field" data-ma-factor hidden data-help="Multiplica el importe base. Con 0 se omite la compra.">Si cumple · ×<input name="matched_factor" value="1.5" inputmode="decimal" required disabled></label>' +
      '<label class="field" data-ma-factor hidden data-help="También se usa si el cierre coincide con la media.">Si no cumple · ×<input name="unmatched_factor" value="0.5" inputmode="decimal" required disabled></label>' +
      '<details class="dca-options"><summary>Caídas y costes</summary><div class="dca-options-grid">' +
      '<label class="field" data-help="Origen de los porcentajes de caída. El máximo sólo usa cierres ya observados; cada nivel se activa una vez por plan.">Referencia de caída<select name="reference_mode"><option value="initial">Primer cierre</option><option value="high_watermark">Máximo observado</option></select></label>' +
      '<label class="field" data-help="Porcentaje de caída desde la referencia elegida. Cada nivel sólo se activa una vez.">Caída 1<input name="drop1" value="0.1" inputmode="decimal"></label>' +
      '<label class="field">Extra 1<input name="extra1" value="50" inputmode="decimal"></label>' +
      '<label class="field">Caída 2<input name="drop2" value="0.2" inputmode="decimal"></label>' +
      '<label class="field">Extra 2<input name="extra2" value="100" inputmode="decimal"></label>' +
      '<label class="field">Caída 3<input name="drop3" inputmode="decimal"></label>' +
      '<label class="field">Extra 3<input name="extra3" inputmode="decimal"></label>' +
      '<label class="field">Caída 4<input name="drop4" inputmode="decimal"></label>' +
      '<label class="field">Extra 4<input name="extra4" inputmode="decimal"></label>' +
      '<label class="field">Máx. extras<input name="max_extra_activations" type="number" min="0" max="1000" value="2" required></label>' +
      '<label class="field">Espera · velas<input name="cooldown_bars" type="number" min="0" max="500" value="0" required></label>' +
      '<label class="field">Comisión<input name="fee_rate" value="0.001" inputmode="decimal" required></label>' +
      '<label class="field">Slippage<input name="slippage_rate" value="0.001" inputmode="decimal" required></label></div></details>' +
      '<button type="submit" id="dca-save">Guardar plan</button></form></section>' +
      '<section class="real-panel"><h3>Simulación</h3><label class="field">Histórico<select id="dca-dataset"></select></label>' +
      '<p class="muted" id="dca-dataset-note"></p><p class="muted">¿No hay datos? <a href="#/laboratorio">Importar histórico</a>.</p>' +
      '<button type="button" id="dca-run" data-help="Simula tres variantes históricas. No activa compras PAPER ni reales.">Comparar</button>' +
      '<label class="field">Pruebas guardadas<select id="dca-run-select"></select></label>' +
      '</section></div><section class="real-panel dca-results" aria-label="Resultados de la comparación DCA">' +
      '<div id="dca-report" class="state">Elige una prueba o ejecuta una.</div></section>' +
      '<details id="dca-validation" class="real-panel"><summary data-help="Reserva un tramo final para evaluar el mismo plan con presupuesto independiente. Mirar la reserva para ajustar reglas contamina la prueba.">Fuera de muestra</summary>' +
      '<div class="row"><label class="field" data-help="Corte para crear otra validación. Cada resultado guardado conserva su propio corte.">Nuevo corte<select id="dca-validation-fraction"><option value="0.7">70 / 30</option><option value="0.6">60 / 40</option><option value="0.8">80 / 20</option></select></label>' +
      '<button type="button" id="dca-validation-start" disabled data-help="Guarda una validación de la versión y el histórico seleccionados; mínimo 30 velas por tramo. No activa PAPER ni operaciones reales.">Validar</button>' +
      '<label class="field">Validaciones guardadas<select id="dca-validation-select"><option value="">Selecciona validación</option></select></label></div>' +
      '<div id="dca-validation-report" class="state" role="status">Elige una versión y un histórico.</div></details>' +
      '<section class="real-panel dca-paper"><div class="pagina-cabecera"><div><p class="real-eyebrow">MERCADO ACTUAL · SALDO FICTICIO</p><h3 data-help="Usa la misma regla que el histórico. Consulta velas cerradas y bid/ask públicos de MEXC; no envía órdenes.">PAPER DCA</h3></div>' +
      '<div class="dca-paper-badges"><span id="dca-paper-worker" class="statusbadge" role="status">OBSERVADOR · COMPROBANDO</span><span class="statusbadge">SIN ÓRDENES</span></div></div><div class="dca-paper-controls"><label class="field">Histórico MEXC · 1d<select id="dca-paper-dataset"></select></label>' +
      '<label class="field" data-help="Fija una simulación DCA verificada de esta versión y este histórico. La comparación PAPER conservará ese run aunque se creen otros; si no eliges uno, no habrá origen fijado.">Prueba de origen<select id="dca-paper-run"></select></label>' +
      '<button type="button" id="dca-paper-start" data-help="Fija la versión seleccionada y el último histórico diario cerrado. La primera observación es mañana a las 00:00 UTC; el observador local comprueba durante los primeros 15 minutos mientras ORBIS esté abierto.">Activar ficticio</button>' +
      '<span id="dca-paper-start-note" class="muted" role="status"></span>' +
      '<label class="field">Sesión<select id="dca-paper-session"></select></label>' +
      '<button type="button" id="dca-paper-poll" data-help="Comprobación manual adicional. El observador local también consulta la sesión mientras ORBIS esté abierto. Sin dato a tiempo se registra un hueco, no una compra retroactiva.">Comprobar ahora</button>' +
      '<button type="button" class="ghost" id="dca-paper-stop">Detener</button></div>' +
      '<div id="dca-paper-report" class="state">Selecciona una sesión PAPER. El observador funciona sólo mientras ORBIS esté abierto.</div>' +
      '<details id="dca-paper-reserve" hidden><summary>Comparar reserva LAB</summary><div class="row">' +
      '<label class="field">Reserva<select id="dca-paper-reserve-select"></select></label>' +
      '<button type="button" id="dca-paper-reserve-compare" disabled>Comparar</button></div>' +
      '<div id="dca-paper-reserve-result" role="status"></div></details>' +
      '<section id="dca-paper-chart-panel" class="dca-paper-chart-panel" hidden><h4 data-help="Velas iniciales verificadas y cierres observados. Flecha en el instante y precio de la compra ficticia; no es un fill del exchange.">Entradas PAPER</h4>' +
      '<div id="dca-paper-chart-range" class="lab-history-controls"></div>' +
      '<div id="dca-paper-chart" role="img" aria-label="Velas DCA con decisiones y compras PAPER observadas"></div>' +
      '<p id="dca-paper-chart-detail" class="muted" role="status"></p></section></section></section>';
    root.querySelector("#dca-form").addEventListener("submit", saveRule);
    root.querySelector("#dca-form").elements.sma_condition.addEventListener("change", function () { syncCondition(this.form); });
    root.querySelector("#dca-rule").addEventListener("change", selectRule);
    root.querySelector("#dca-version").addEventListener("change", selectVersion);
    root.querySelector("#dca-import").addEventListener("click", function () { root.querySelector("#dca-import-file").click(); });
    root.querySelector("#dca-import-file").addEventListener("change", previewImport);
    root.querySelector("#dca-import-cancel").addEventListener("click", dismissImport);
    root.querySelector("#dca-import-save").addEventListener("click", saveImport);
    root.querySelector("#dca-export").addEventListener("click", exportRule);
    root.querySelector("#dca-run").addEventListener("click", execute);
    root.querySelector("#dca-run-select").addEventListener("change", selectRun);
    root.querySelector("#dca-validation-start").addEventListener("click", executeValidation);
    root.querySelector("#dca-validation-select").addEventListener("change", function () { loadValidation(this.value); });
    root.querySelector("#dca-dataset").addEventListener("change", datasetNote);
    root.querySelector("#dca-paper-start").addEventListener("click", startPaper);
    root.querySelector("#dca-paper-dataset").addEventListener("change", syncPaperStart);
    root.querySelector("#dca-paper-run").addEventListener("change", function () { paperRunChoice = this.value; });
    root.querySelector("#dca-paper-session").addEventListener("change", selectPaper);
    root.querySelector("#dca-paper-poll").addEventListener("click", function () { actPaper("poll"); });
    root.querySelector("#dca-paper-stop").addEventListener("click", function () { actPaper("stop"); });
    root.querySelector("#dca-paper-reserve").addEventListener("toggle", function () {
      if (this.open && paperReserveSession) loadPaperReserves();
      else if (!this.open) clearPaperReserveResult();
    });
    root.querySelector("#dca-paper-reserve-select").addEventListener("change", clearPaperReserveResult);
    root.querySelector("#dca-paper-reserve-compare").addEventListener("click", comparePaperReserve);
    reload();
    paperTimer = setInterval(refreshPaper, 15000);
  }
  function definition(form) {
    var f = new FormData(form), dips = [];
    [1,2,3,4].forEach(function (index) {
      var drop = String(f.get("drop" + index) || "").trim(), amount = String(f.get("extra" + index) || "").trim();
      if (drop || amount) dips.push({drop_pct:drop, amount:amount});
    });
    var rule = {frequency:f.get("frequency"), reference_mode:f.get("reference_mode"), amount:f.get("amount"), budget_total:f.get("budget_total"),
      monthly_limit:f.get("monthly_limit"), max_exposure:f.get("max_exposure"), fee_rate:f.get("fee_rate"),
      slippage_rate:f.get("slippage_rate"), max_extra_activations:Number(f.get("max_extra_activations")),
      cooldown_bars:Number(f.get("cooldown_bars")), dips:dips};
    var selection = String(f.get("sma_condition") || "");
    if (selection) {
      var parts = selection.split("-"), period = Number(f.get("sma_period"));
      if (selection === "volume") rule.volume_gate = {period:period, min_ratio:f.get("volume_min_ratio")};
      else if (parts.length === 1) rule.sma_gate = {condition:selection, period:period};
      else rule.ma_adjustment = {kind:parts[0], condition:parts[1], period:period,
        matched_factor:f.get("matched_factor"), unmatched_factor:f.get("unmatched_factor")};
    }
    return rule;
  }
  function syncCondition(form) {
    var selection = form.elements.sma_condition.value;
    form.elements.sma_period.disabled = !selection;
    var adjust = selection.includes("-");
    form.querySelectorAll("[data-ma-factor]").forEach(function (label) {
      label.hidden = !adjust;
      label.querySelector("input").disabled = !adjust;
    });
    form.querySelector("[data-dca-volume-field]").hidden = selection !== "volume";
    form.elements.volume_min_ratio.disabled = selection !== "volume";
  }
  function averageLabel(gate) {
    var condition = "Cierre " + (gate.condition === "below" ? "bajo" : "sobre") + " " +
      (gate.kind || "sma").toUpperCase() + " " + gate.period;
    return gate.kind ? condition + " · ×" + gate.matched_factor + " / ×" + gate.unmatched_factor : condition;
  }
  function volumeLabel(gate) {
    return "Volumen ≥ " + gate.min_ratio + "× · " + gate.period + " velas";
  }
  function fill(item) {
    var form = root.querySelector("#dca-form"), d = item.definition;
    form.elements.name.value = item.name;
    form.elements.reference_mode.value = d.reference_mode || "initial";
    var average = d.ma_adjustment || d.sma_gate;
    form.elements.sma_condition.value = d.volume_gate ? "volume" : average ? (average.kind ? average.kind + "-" : "") + average.condition : "";
    form.elements.sma_period.value = (d.volume_gate || average || {period:20}).period;
    form.elements.volume_min_ratio.value = d.volume_gate ? d.volume_gate.min_ratio : "1.5";
    form.elements.matched_factor.value = d.ma_adjustment ? d.ma_adjustment.matched_factor : "1.5";
    form.elements.unmatched_factor.value = d.ma_adjustment ? d.ma_adjustment.unmatched_factor : "0.5";
    syncCondition(form);
    ["frequency","amount","budget_total","monthly_limit","max_exposure","fee_rate","slippage_rate","max_extra_activations","cooldown_bars"].forEach(function (key) {
      form.elements[key].value = d[key];
    });
    [1,2,3,4].forEach(function (index) {
      var tier = d.dips[index - 1];
      form.elements["drop" + index].value = tier ? tier.drop_pct : "";
      form.elements["extra" + index].value = tier ? tier.amount : "";
    });
    root.querySelector("#dca-save").textContent = "Guardar versión";
  }
  function reload(preferId) {
    return Promise.all([api("/lab/datasets"), api("/automation/dca/rules"), api("/automation/dca/runs"), api("/automation/dca/paper/sessions"), api("/automation/dca/paper/worker"), api("/automation/dca/validations")]).then(function (data) {
      if (!root || !root.isConnected) return;
      datasets = data[0]; rules = data[1]; runs = data[2]; paperSessions = data[3]; paperWorker = data[4];
      renderPaperWorker();
      validationOptions(data[5]);
      var ds = root.querySelector("#dca-dataset"), rs = root.querySelector("#dca-rule"), runSelect = root.querySelector("#dca-run-select");
      var oldDataset = ds.value, oldRule = rs.value, oldRun = runSelect.value;
      var paperDataset = root.querySelector("#dca-paper-dataset"), oldPaperDataset = paperDataset.value;
      var paperSelect = root.querySelector("#dca-paper-session"), oldPaperSession = paperSelect.value;
      ds.innerHTML = '<option value="">Selecciona histórico</option>' + datasets.map(function (row) {
        return '<option value="' + esc(row.id) + '">' + esc(row.symbol) + ' · ' + esc(row.timeframe) + ' · ' + esc(row.count) + ' velas</option>';
      }).join("");
      var ruleNames = new Map();
      rules.forEach(function (row) { ruleNames.set(row.name, (ruleNames.get(row.name) || 0) + 1); });
      rs.innerHTML = '<option value="">Nuevo plan</option>' + rules.map(function (row) {
        var suffix = ruleNames.get(row.name) > 1 ? ' · ' + esc(row.id.slice(0,8)) : '';
        return '<option value="' + esc(row.id) + '">' + esc(row.name) + ' · v' + row.version + suffix + '</option>';
      }).join("");
      runSelect.innerHTML = '<option value="">Selecciona prueba</option>' + runs.map(function (row) {
        return '<option value="' + esc(row.id) + '">' + esc(row.created_at.slice(0,16)) + ' · ' + esc(row.id.slice(0,8)) + '</option>';
      }).join("");
      var paperEligible = datasets.filter(function (row) { return row.verified_public && row.source === "mexc-spot-public" && row.timeframe === "1d" && !row.gaps.length; });
      paperDataset.innerHTML = '<option value="">Selecciona histórico verificado</option>' + paperEligible.map(function (row) {
        return '<option value="' + esc(row.id) + '">' + esc(row.symbol) + ' · ' + esc(row.count) + ' velas · ' + esc(row.hash.slice(0,8)) + '</option>';
      }).join("");
      paperDataset.value = paperEligible.some(function (row) { return row.id === oldPaperDataset; }) ? oldPaperDataset : "";
      paperSelect.innerHTML = '<option value="">Selecciona sesión</option>' + paperSessions.map(function (row) {
        return '<option value="' + esc(row.id) + '">' + esc(row.symbol) + ' · ' + esc(row.status) + ' · ' + esc(row.id.slice(0,8)) + '</option>';
      }).join("");
      paperSelect.value = paperSessions.some(function (row) { return row.id === oldPaperSession; }) ? oldPaperSession : "";
      ds.value = datasets.some(function (row) { return row.id === oldDataset; }) ? oldDataset : (datasets[0] && datasets[0].id || "");
      rs.value = rules.some(function (row) { return row.id === (preferId || oldRule); }) ? (preferId || oldRule) : "";
      runSelect.value = runs.some(function (row) { return row.id === oldRun; }) ? oldRun : "";
      datasetNote();
      if (paperSelect.value) selectPaper();
      if (rs.value) return selectRule();
      syncPaperStart();
    }).catch(function (error) { notice(error.message, true); });
  }
  function datasetNote() {
    syncValidationStart();
    var current = datasets.find(function (row) { return row.id === root.querySelector("#dca-dataset").value; });
    root.querySelector("#dca-dataset-note").textContent = current ?
      current.source + " · " + (current.gaps.length ? "Huecos: prueba bloqueada" : "Cobertura continua") + " · SHA-256 " + current.hash.slice(0,12) + "…" : "Sin histórico";
  }
  function selectRule() {
    var view = root, exporter = view.querySelector("#dca-export");
    exporter.disabled = true;
    var selected = rules.find(function (row) { return row.id === root.querySelector("#dca-rule").value; });
    if (!selected) {
      root.querySelector("#dca-version").innerHTML = '<option value="">Guarda un plan</option>';
      root.querySelector("#dca-form").reset();
      syncCondition(root.querySelector("#dca-form"));
      root.querySelector("#dca-save").textContent = "Guardar plan";
      syncPaperStart();
      return;
    }
    fill(selected);
    return api("/automation/dca/rules/" + encodeURIComponent(selected.id) + "/versions").then(function (versions) {
      if (root !== view || !exporter.isConnected || view.querySelector("#dca-rule").value !== selected.id) return;
      var control = view.querySelector("#dca-version");
      control.innerHTML = versions.map(function (row) { return '<option value="' + esc(row.id) + '">v' + row.version + ' · ' + esc(row.created_at.slice(0,16)) + '</option>'; }).join("");
      control.value = selected.version_id;
      exporter.disabled = !control.value;
      syncPaperStart();
    }).catch(function (error) { notice(error.message, true); });
  }
  function selectVersion() {
    syncPaperStart();
    var selected = rules.find(function (row) { return row.id === root.querySelector("#dca-rule").value; });
    if (!selected) return;
    var view = root, versionId = view.querySelector("#dca-version").value;
    api("/automation/dca/rules/" + encodeURIComponent(selected.id) + "/versions").then(function (versions) {
      if (root !== view || view.querySelector("#dca-rule").value !== selected.id ||
          view.querySelector("#dca-version").value !== versionId) return;
      var version = versions.find(function (row) { return row.id === versionId; });
      if (version) fill({name:selected.name, definition:version.definition});
    }).catch(function (error) { notice(error.message, true); });
  }
  function saveRule(event) {
    event.preventDefault();
    var selected = rules.find(function (row) { return row.id === root.querySelector("#dca-rule").value; });
    var path = selected ? "/automation/dca/rules/" + encodeURIComponent(selected.id) + "/versions" : "/automation/dca/rules";
    var body = selected ? {definition:definition(event.currentTarget)} :
      {name:event.currentTarget.elements.name.value.trim(), definition:definition(event.currentTarget)};
    var button = root.querySelector("#dca-save"); button.disabled = true;
    api(path, {method:"POST", body:JSON.stringify(body)}).then(function (saved) {
      notice("Plan guardado · v" + saved.version, false);
      return reload(saved.id);
    }).catch(function (error) { notice(error.message, true); }).finally(function () { button.disabled = false; });
  }
  function dismissImport() {
    transfer = null;
    root.querySelector("#dca-import-review").hidden = true;
  }
  function previewImport(event) {
    var view = root, input = event.currentTarget, file = input.files && input.files[0];
    if (!file) return;
    input.value = "";
    dismissImport();
    if (file.size > 32768) { notice("El archivo DCA supera 32 KiB.", true); return; }
    var job = {file:file, id:crypto.randomUUID()};
    transfer = job;
    api("/automation/dca/rules/import/preview", {method:"POST", body:file, cache:"no-store"}).then(function (review) {
      if (root !== view || transfer !== job) return;
      job.review = review;
      var rule = review.definition, frequency = {daily:"Diaria", weekly:"Semanal", monthly:"Mensual"};
      var origin = review.integrity === "CHECKSUM_VERIFIED" ? "Huella verificada · origen v" + review.source_version : "Plantilla JSON";
      view.querySelector("#dca-import-name").textContent = review.name;
      view.querySelector("#dca-import-summary").textContent = origin + " · " + frequency[rule.frequency] +
        " · Base " + rule.amount + " · Presupuesto " + rule.budget_total;
      view.querySelector("#dca-import-definition").textContent = JSON.stringify(rule, null, 2);
      view.querySelector("#dca-import-review").hidden = false;
    }).catch(function (error) {
      if (root === view && transfer === job) { dismissImport(); notice(error.message, true); }
    });
  }
  function saveImport() {
    var view = root, job = transfer;
    if (!job || !job.review || job.saving) return;
    job.saving = true;
    var controls = ["#dca-import", "#dca-import-save", "#dca-import-cancel"].map(function (selector) { return view.querySelector(selector); });
    controls.forEach(function (control) { control.disabled = true; });
    api("/automation/dca/rules/import", {method:"POST", body:job.file, cache:"no-store", headers:{
      "Content-Type":"application/json", "X-Orbis-Import-Hash":job.review.review_hash, "X-Orbis-Import-Id":job.id
    }}).then(function (saved) {
      if (root !== view || transfer !== job) return;
      dismissImport();
      return reload(saved.id).then(function () {
        if (root === view) notice("Copia guardada · v1", false);
      });
    }).catch(function (error) {
      if (root === view && transfer === job) notice(error.message, true);
    }).finally(function () {
      job.saving = false;
      controls.forEach(function (control) { control.disabled = false; });
    });
  }
  function exportRule() {
    var view = root, button = view.querySelector("#dca-export"), plan = view.querySelector("#dca-rule");
    var version = view.querySelector("#dca-version"), planId = plan.value, versionId = version.value;
    if (!planId || !versionId) return;
    button.disabled = true;
    api("/automation/dca/rules/" + encodeURIComponent(planId) + "/versions/" +
      encodeURIComponent(versionId) + "/export", {cache:"no-store"}).then(function (bundle) {
      if (root !== view || !button.isConnected || plan.value !== planId || version.value !== versionId) return;
      var file = new Blob([JSON.stringify(bundle, null, 2)], {type:"application/json"});
      var url = URL.createObjectURL(file), link = document.createElement("a");
      link.href = url;
      link.download = "orbis-dca-v" + bundle.plan.version + ".json";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    }).catch(function (error) { if (root === view) notice(error.message, true); }).finally(function () {
      if (root === view && button.isConnected && plan.value === planId && version.value === versionId) button.disabled = false;
    });
  }
  function execute() {
    var datasetId = root.querySelector("#dca-dataset").value, versionId = root.querySelector("#dca-version").value;
    if (!datasetId || !versionId) { notice("Selecciona histórico y guarda un plan.", true); return; }
    var button = root.querySelector("#dca-run"); button.disabled = true;
    api("/automation/dca/runs", {method:"POST", body:JSON.stringify({dataset_id:datasetId, version_id:versionId})})
      .then(function (run) { notice("Comparación guardada.", false); return reload().then(function () { showRun(run); }); })
      .catch(function (error) { notice(error.message, true); }).finally(function () { button.disabled = false; });
  }
  function selectRun() {
    var id = root.querySelector("#dca-run-select").value;
    if (id) api("/automation/dca/runs/" + encodeURIComponent(id)).then(showRun).catch(function (error) { notice(error.message, true); });
  }
  function utc(seconds) { return new Date(seconds * 1000).toISOString().slice(0,16).replace("T", " ") + " UTC"; }
  function syncPaperStart() {
    syncValidationStart();
    if (!root || !root.isConnected) return;
    var versionId = root.querySelector("#dca-version").value;
    var datasetId = root.querySelector("#dca-paper-dataset").value;
    var dataset = datasets.find(function (row) { return row.id === datasetId; });
    var compatible = runs.filter(function (run) {
      return dataset && run.version_id === versionId && run.dataset_id === datasetId &&
        run.dataset_hash === dataset.hash;
    });
    var source = root.querySelector("#dca-paper-run");
    source.innerHTML = '<option value="">Sin prueba de origen</option>' + compatible.map(function (run) {
      return '<option value="' + esc(run.id) + '">' + esc(run.created_at.slice(0, 16)) +
        ' · ' + esc(run.id.slice(0, 8)) + '</option>';
    }).join("");
    source.value = compatible.some(function (run) { return run.id === paperRunChoice; })
      ? paperRunChoice : paperRunChoice === "" ? "" : compatible.length ? compatible[0].id : "";
    var active = paperSessions.find(function (row) {
      return row.version_id === versionId && row.dataset_id === datasetId && row.status === "ACTIVE";
    });
    var button = root.querySelector("#dca-paper-start");
    button.disabled = paperStarting || !!active || !versionId || !datasetId;
    button.textContent = active ? "Ya activo" : "Activar ficticio";
    root.querySelector("#dca-paper-start-note").textContent = active ?
      "Seguimiento en curso · detén la sesión para iniciar otra vez." : "";
  }
  function syncValidationStart() {
    var button = root && root.querySelector("#dca-validation-start");
    if (button) button.disabled = button.dataset.pending === "true" ||
      !root.querySelector("#dca-dataset").value || !root.querySelector("#dca-version").value;
  }
  function validationOptions(rows, selected) {
    var control = root.querySelector("#dca-validation-select"), old = selected || control.value;
    control.innerHTML = '<option value="">Selecciona validación</option>' + rows.map(function (row) {
      return '<option value="' + esc(row.id) + '">' + esc(row.created_at.slice(0, 16)) + ' · ' +
        esc(row.train_bars) + ' / ' + esc(row.test_bars) + ' velas · ' + esc(row.id.slice(0, 8)) + '</option>';
    }).join("");
    control.value = rows.some(function (row) { return row.id === old; }) ? old : "";
  }
  function executeValidation() {
    var renderedRoot = root, seq = ++validationSeq;
    var button = root.querySelector("#dca-validation-start");
    var body = {dataset_id:root.querySelector("#dca-dataset").value,
      version_id:root.querySelector("#dca-version").value,
      train_fraction:root.querySelector("#dca-validation-fraction").value};
    button.dataset.pending = "true"; button.disabled = true;
    var report = root.querySelector("#dca-validation-report");
    report.className = "state"; report.textContent = "Calculando reserva…"; report.dataset.state = "checking";
    api("/automation/dca/validations", {method:"POST", body:JSON.stringify(body)}).then(function (saved) {
      if (root !== renderedRoot || !root.isConnected || seq !== validationSeq) return;
      return api("/automation/dca/validations").then(function (rows) {
        if (root !== renderedRoot || !root.isConnected || seq !== validationSeq) return;
        validationOptions(rows, saved.id);
        if (paperReserveSession && root.querySelector("#dca-paper-reserve").open) loadPaperReserves();
        return loadValidation(saved.id);
      });
    }).catch(function (error) {
      if (root !== renderedRoot || !root.isConnected || seq !== validationSeq) return;
      report.className = "state error"; report.textContent = error.message; report.dataset.state = "error";
    }).finally(function () {
      if (root !== renderedRoot || !root.isConnected) return;
      button.dataset.pending = "false"; syncValidationStart();
    });
  }
  function loadValidation(id) {
    var renderedRoot = root, seq = ++validationSeq;
    var target = root.querySelector("#dca-validation-report");
    target.className = "state"; target.dataset.state = id ? "checking" : "empty";
    target.textContent = id ? "Comprobando reserva…" : "Selecciona una validación.";
    if (!id) return Promise.resolve();
    target.dataset.runId = id;
    return api("/automation/dca/validations/" + encodeURIComponent(id) + "?require_verified=true&view=summary").then(function (saved) {
      if (root !== renderedRoot || !root.isConnected || seq !== validationSeq) return;
      if (!saved.verification || saved.verification.verified !== true) throw new Error("Reserva no verificada");
      var names = {conditional:"Condicionado", fixed:"Fijo", lump:"Compra inicial"};
      var rows = Object.keys(names).map(function (mode) {
        var train = saved.train.results[mode].metrics, reserve = saved.out_of_sample.results[mode].metrics;
        function cell(value, suffix) { return '<td class="num" title="' + esc(value) + '">' + rounded(value) + (suffix || "") + '</td>'; }
        return '<tr><td>' + names[mode] + '</td>' + cell(train.return_on_initial_capital_pct, "%") +
          cell(reserve.return_on_initial_capital_pct, "%") + cell(reserve.invested) + cell(reserve.remaining_cash) +
          cell(reserve.max_drawdown_pct, "%") + '<td class="num">' + esc(reserve.buys) + '</td></tr>';
      }).join("");
      target.className = ""; target.dataset.state = "verified";
      target.innerHTML = '<div class="row"><span class="statusbadge on">REPRODUCIBLE</span><span class="dca-reference"></span></div><p class="muted">' +
        esc(saved.train_bars) + ' / ' + esc(saved.test_bars) + ' velas · ' +
        '<span title="' + esc(saved.version_id) + '">Versión ' + esc(saved.version_id.slice(0, 8)) + '</span> · ' +
        '<span title="' + esc(saved.dataset_id) + '">Histórico ' + esc(saved.dataset_id.slice(0, 8)) + '</span></p>' +
        '<p class="muted">Reserva UTC: ' + esc(new Date(saved.test_start_t * 1000).toISOString().slice(0, 10)) + ' → ' +
        esc(new Date(saved.test_end_t * 1000).toISOString().slice(0, 10)) + '</p>' +
        '<div class="tablewrap"><table><thead><tr><th>Variante</th><th>Desarrollo %</th><th>Reserva %</th><th>Reserva invertida</th><th>Reserva caja</th><th>Reserva DD</th><th>Compras</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
        '<details><summary>Supuestos y cifras exactas</summary><ul>' + saved.warnings.map(function (warning) { return '<li>' + esc(warning) + '</li>'; }).join("") +
        '</ul><pre class="lab-events">' + esc(JSON.stringify(saved, null, 2)) + '</pre></details>';
      var reference = window.OrbisResearchReference.mount(target.querySelector(".dca-reference"), {
        url:"/automation/dca/validations/" + encodeURIComponent(id) + "/reference",
        fileName:"orbis-referencia-dca-" + id + ".json",
        isCurrent:function () {
          return root === renderedRoot && seq === validationSeq && target.dataset.state === "verified" && target.dataset.runId === id;
        },
        onError:function (error) {
          target.className = "state error"; target.dataset.state = "error"; target.textContent = error.message;
        }
      });
      reference.setEnabled(true);
    }).catch(function (error) {
      if (root !== renderedRoot || !root.isConnected || seq !== validationSeq) return;
      target.className = "state error"; target.dataset.state = "error"; target.textContent = error.message;
    });
  }
  function renderPaperWorker() {
    var badge = root && root.querySelector("#dca-paper-worker"); if (!badge) return;
    var healthy = paperWorker && paperWorker.running && paperWorker.heartbeat_age_seconds != null && paperWorker.heartbeat_age_seconds <= 120;
    badge.className = "statusbadge" + (healthy ? " on" : "");
    badge.textContent = healthy ? "OBSERVADOR · ACTIVO" : paperWorker && paperWorker.running && paperWorker.heartbeat_age_seconds == null ?
      "OBSERVADOR · INICIANDO" : paperWorker && paperWorker.running ? "OBSERVADOR · SIN LATIDO" : "OBSERVADOR · INACTIVO";
    badge.title = paperWorker && paperWorker.last_error ? paperWorker.last_error :
      healthy ? "Seguimiento local automático; se detiene al cerrar ORBIS." : "Comprueba manualmente o reinicia ORBIS. No se reconstruyen compras perdidas.";
  }
  function refreshPaper() {
    if (!root || !root.isConnected) return;
    var view = root;
    Promise.all([api("/automation/dca/paper/worker"), api("/automation/dca/paper/sessions")]).then(function (data) {
      if (root !== view || !view.isConnected) return;
      paperWorker = data[0]; paperSessions = data[1]; renderPaperWorker();
      var control = root.querySelector("#dca-paper-session"), id = control.value;
      control.innerHTML = '<option value="">Selecciona sesión</option>' + paperSessions.map(function (row) {
        return '<option value="' + esc(row.id) + '">' + esc(row.symbol) + ' · ' + esc(row.status) + ' · ' + esc(row.id.slice(0,8)) + '</option>';
      }).join("");
      control.value = id;
      if (id) selectPaper();
      syncPaperStart();
    }).catch(function (error) {
      if (root === view && view.isConnected) { syncPaperReserve(null); notice(error.message, true); }
    });
  }
  function startPaper() {
    var versionId = root.querySelector("#dca-version").value, datasetId = root.querySelector("#dca-paper-dataset").value;
    if (!versionId || !datasetId) { notice("Guarda una versión y selecciona histórico MEXC diario verificado.", true); return; }
    var button = root.querySelector("#dca-paper-start");
    paperStarting = true; syncPaperStart();
    var body = {version_id:versionId, dataset_id:datasetId};
    var sourceRunId = root.querySelector("#dca-paper-run").value;
    if (sourceRunId) body.source_run_id = sourceRunId;
    api("/automation/dca/paper/sessions", {method:"POST", body:JSON.stringify(body)})
      .then(function (session) { notice("PAPER iniciado; primera comprobación: " + utc(session.next_exec_t), false); return reload().then(function () {
        root.querySelector("#dca-paper-session").value = session.id; selectPaper();
      }); }).catch(function (error) { notice(error.message, true); }).finally(function () { paperStarting = false; syncPaperStart(); });
  }
  function selectPaper() {
    var view = root, id = view.querySelector("#dca-paper-session").value;
    if (paperReserveSession && paperReserveSession.id !== id) syncPaperReserve(null);
    if (!id) { paperComparisonSeq++; clearPaperChart(); root.querySelector("#dca-paper-report").textContent = "Selecciona una sesión PAPER. El observador funciona sólo mientras ORBIS esté abierto."; return; }
    api("/automation/dca/paper/sessions/" + encodeURIComponent(id)).then(function (session) {
      if (root === view && view.isConnected) showPaper(session);
    }).catch(function (error) {
      if (root === view && view.isConnected && view.querySelector("#dca-paper-session").value === id) {
        syncPaperReserve(null);
        notice(error.message, true);
      }
    });
  }
  function actPaper(action) {
    var id = root.querySelector("#dca-paper-session").value;
    if (!id) { notice("Selecciona una sesión PAPER.", true); return; }
    var button = root.querySelector(action === "poll" ? "#dca-paper-poll" : "#dca-paper-stop"); button.disabled = true;
    api("/automation/dca/paper/sessions/" + encodeURIComponent(id) + "/" + action, {method:"POST"})
      .then(function (session) { notice(action === "stop" ? "Sesión PAPER detenida." : "Estado: " + session.poll_result, session.poll_result === "GAP");
        return reload().then(function () { root.querySelector("#dca-paper-session").value = id; showPaper(session); });
      }).catch(function (error) { notice(error.message, true); }).finally(function () { button.disabled = false; });
  }
  function showPaper(session) {
    var target = root && root.querySelector("#dca-paper-report"); if (!target) return;
    if (session.id !== root.querySelector("#dca-paper-session").value) return;
    paperGapSeq++;
    var state = session.state && typeof session.state === "object" ? session.state : null;
    var quote = session.last_quote;
    var reasons = {SMA_CONDITION_NOT_MET:"Fuera del filtro SMA", SMA_WARMUP:"SMA sin historial suficiente",
      MA_WARMUP:"Media móvil sin historial suficiente", MA_ZERO_AMOUNT:"Importe ×0",
      VOLUME_WARMUP:"Volumen sin historial suficiente", VOLUME_BASELINE_ZERO:"Volumen medio cero",
      VOLUME_BELOW_MINIMUM:"Volumen por debajo del mínimo"};
    var average = session.ma_adjustment || session.sma_gate;
    var rows = (session.events || []).map(function (event) {
      var description = event.kind === "BUY" ? event.trigger_kind + " · " + rounded(event.spent) + " USDT · " + rounded(event.units, 8) + " " + session.symbol.split("/")[0] :
        event.kind === "SKIP" ? event.trigger_kind + " · " + (reasons[event.reason] || event.reason) : "Vela cerrada + bid/ask observado";
      if (event.sma_value != null) description += " · SMA " + event.sma_period + " = " + rounded(event.sma_value, 4);
      if (event.ma_value != null && ["sma", "ema"].includes(event.ma_kind)) description += " · " + event.ma_kind.toUpperCase() + " " + event.ma_period +
        " = " + rounded(event.ma_value, 4) + " · ×" + event.amount_factor;
      if (event.volume_ratio != null) description += " · " + rounded(event.volume_ratio, 4) + "×";
      return '<tr><td>' + esc(utc(event.execution_t)) + '</td><td>' + esc(event.kind) + '</td><td>' + esc(description) + '</td><td>' + esc(new Date(event.observed_at_ns / 1e6).toISOString().slice(0,19).replace("T"," ")) + ' UTC</td></tr>';
    }).join("");
    target.className = "";
    var balance = state ? '<span>Saldo · ' + rounded(state.cash) + ' USDT</span><span>Unidades · ' +
      rounded(state.units, 8) + '</span><span>Gastado · ' + rounded(state.spent) + ' USDT</span>' :
      '<span>Saldo y unidades · no disponibles</span>';
    target.innerHTML = '<div class="dca-paper-state"><span class="statusbadge ' + (session.status === "ACTIVE" ? "on" : "") + '">' + esc(session.status) + '</span>' +
      '<span>Próxima · ' + esc(utc(session.next_exec_t)) + '</span>' + balance + '</div>' +
      (session.status_reason ? '<p class="amber">' + esc(session.status_reason) + '</p>' : '') +
      '<p class="muted">' + esc(session.symbol) + ' · versión ' + esc(session.version_id.slice(0,8)) + ' · datos ' + esc(session.dataset_hash.slice(0,12)) + '… · ' + esc(session.mode) +
      ' · referencia ' + (session.reference_mode === "high_watermark" ? 'máximo observado' : 'primer cierre') +
      (average ? ' · ' + esc(averageLabel(average)) : '') +
      (session.volume_gate ? ' · ' + esc(volumeLabel(session.volume_gate)) : '') +
      (quote ? ' · bid/ask ' + rounded(quote.bid) + '/' + rounded(quote.ask) : '') + '</p>' +
      (session.status === "ACTIVE" && state ? '<div class="row"><button type="button" class="ghost" data-dca-mexc-handoff data-help="Abre la revisión de esta sesión DCA en MEXC REAL. Define allí asignación, límite mensual y permiso.">Preparar MEXC REAL</button>' +
        (session.symbol === "BNB/USDT" ? '<button type="button" class="ghost" data-dca-dex-handoff data-help="Revisa esta sesión DCA para compras USDT/WBNB en Uniswap v3, BNB Chain.">Preparar DEX</button>' : '') + '</div>' : '') +
      '<div id="dca-paper-comparison" class="dca-paper-compare state loading" role="status">Enlazando DCA histórico y PAPER…</div>' +
      (session.status === "GAP" ? '<button type="button" class="ghost" id="dca-paper-gap" ' +
        'data-help="Analiza el hueco con cierres históricos y guarda el informe local para revisarlo. No crea compras PAPER ni conoce el bid/ask perdido.">Investigar hueco</button>' +
        '<label class="field dca-paper-gap-archive" id="dca-paper-gap-archive-field" hidden>Informes guardados' +
        '<select id="dca-paper-gap-archive" data-help="Cada informe conserva el histórico y el día exactos usados en el cálculo. Todos son hipótesis, no operaciones PAPER."></select></label>' +
        '<div id="dca-paper-gap-report" class="dca-paper-gap" role="status" hidden></div>' : '') +
      '<div class="tablewrap dca-paper-events"><table><thead><tr><th>Ventana</th><th>Evento</th><th>Detalle</th><th>Observado</th></tr></thead><tbody>' +
      (rows || '<tr><td colspan="4">Sin observaciones todavía</td></tr>') + '</tbody></table></div>' +
      '<details><summary>Estado y procedencia exactos</summary><pre class="lab-events">' + esc(JSON.stringify(session, null, 2)) + '</pre></details>';
    root.querySelector("#dca-paper-poll").disabled = session.status !== "ACTIVE";
    root.querySelector("#dca-paper-stop").disabled = session.status !== "ACTIVE";
    var handoff = target.querySelector("[data-dca-mexc-handoff]");
    if (handoff) handoff.addEventListener("click", function () {
      if (root && session.id === root.querySelector("#dca-paper-session").value)
        window.OrbisReal.prepareMexcSource("DCA", session.id);
    });
    var dexHandoff = target.querySelector("[data-dca-dex-handoff]");
    if (dexHandoff) dexHandoff.addEventListener("click", function () {
      if (root && session.id === root.querySelector("#dca-paper-session").value)
        window.OrbisReal.prepareDexSource("DCA", session.id);
    });
    if (session.status === "GAP") root.querySelector("#dca-paper-gap").addEventListener("click", function () {
      loadPaperGap(session.id);
    });
    if (session.status === "GAP") root.querySelector("#dca-paper-gap-archive").addEventListener("change", function (event) {
      loadArchivedPaperGap(session.id, event.target.value);
    });
    if (session.status === "GAP") loadSavedPaperGap(session.id);
    syncPaperReserve(state ? session : null);
    loadPaperComparison(session.id);
    loadPaperChart(session);
  }
  function clearPaperChart() {
    paperChartSeq++;
    paperChartKey = "";
    if (paperChart) { paperChart.remove(); paperChart = null; }
    if (root) {
      var panel = root.querySelector("#dca-paper-chart-panel");
      if (panel) panel.hidden = true;
    }
  }
  function loadPaperChart(session) {
    var key = session.id + ":" + (session.events || []).length;
    if (key === paperChartKey) return;
    var seq = ++paperChartSeq, sessionId = session.id;
    var panel = root && root.querySelector("#dca-paper-chart-panel");
    var detail = root && root.querySelector("#dca-paper-chart-detail");
    if (!panel || !detail) return;
    panel.hidden = false;
    detail.textContent = "Cargando entradas PAPER…";
    api("/automation/dca/paper/sessions/" + encodeURIComponent(sessionId) + "/chart").then(function (data) {
      if (!root || !root.isConnected || seq !== paperChartSeq ||
          root.querySelector("#dca-paper-session").value !== sessionId) return;
      if (data.schema !== "orbis.paper-dca.chart.v1" || data.environment !== "PAPER" ||
          data.execution !== "NO_EXCHANGE_ORDERS" || data.session_id !== sessionId ||
          data.dataset_hash !== session.dataset_hash || !Array.isArray(data.candles) ||
          !Array.isArray(data.buys)) throw new Error("Evidencia PAPER incompatible");
      var LW = window.LightweightCharts, host = root.querySelector("#dca-paper-chart");
      if (!LW || !host) throw new Error("Gráfico local no disponible");
      if (paperChart) { paperChart.remove(); paperChart = null; }
      paperChart = LW.createChart(host, {autoSize:true,
        layout:{background:{type:"solid",color:"#101b18"},textColor:"#c9d6ce"},
        grid:{vertLines:{color:"#24362f"},horzLines:{color:"#24362f"}},
        timeScale:{timeVisible:true,minBarSpacing:0.1,rightOffset:6}});
      var series = paperChart.addSeries(LW.CandlestickSeries, {upColor:"#42c88a",downColor:"#ff7b78",
        borderVisible:false,wickUpColor:"#42c88a",wickDownColor:"#ff7b78"});
      series.setData(data.candles.map(function (bar) { return {time:bar.t,open:Number(bar.o),
        high:Number(bar.h),low:Number(bar.l),close:Number(bar.c)}; }));
      var actions = paperChart.addSeries(LW.LineSeries, {lineVisible:false,pointMarkersVisible:false,
        crosshairMarkerVisible:false,priceLineVisible:false,lastValueVisible:false});
      actions.setData(data.buys.map(function (buy) { return {time:buy.execution_t,value:Number(buy.price)}; }));
      LW.createSeriesMarkers(series, data.buys.map(function (buy) { return {time:buy.signal_t,
        position:"aboveBar",color:"#efbd63",shape:"circle",text:"Señal"}; }));
      LW.createSeriesMarkers(actions, data.buys.map(function (buy) { return {time:buy.execution_t,
        position:"belowBar",color:"#88b8ff",shape:"arrowUp",
        text:buy.count > 1 ? "PAPER ×" + buy.count : "PAPER"}; }));
      var inspected = new Map();
      data.buys.forEach(function (buy) {
        inspected.set(buy.signal_t, "Señal · cierre observado " + utc(buy.signal_t));
        inspected.set(buy.execution_t, "Compra PAPER · " + buy.count + " activación(es) · " +
          buy.triggers.join(" + ") + " · " + buy.spent.join(" + ") + " USDT · precio ficticio " + buy.price);
      });
      paperChart.subscribeCrosshairMove(function (param) {
        if (!root || !root.isConnected) return;
        var current = root.querySelector("#dca-paper-chart-detail");
        if (current) current.textContent = param.time !== undefined && inspected.has(param.time) ?
          inspected.get(param.time) : "● señal al cierre · ↑ compra PAPER al precio observado";
      });
      paperChartKey = key;
      host.dataset.executionCount = String(data.buys.reduce(function (total, buy) { return total + buy.count; }, 0));
      host.dataset.observedCandles = String(data.observed_count);
      detail.textContent = data.candles.length + " velas · " + data.observed_count + " cierres PAPER observados · " +
        host.dataset.executionCount + " compras ficticias" +
        (data.buys.some(function (buy) { return buy.execution_t > data.candles[data.candles.length - 1].t; }) ?
          " · última ejecución sin OHLC" : "") + ".";
      if (window.OrbisChartRange) window.OrbisChartRange.mount(root.querySelector("#dca-paper-chart-range"),
        paperChart, data.candles, {storageKey:"orbis.chartRange.dcaPaper",selected:"1",
          eventTimes:data.buys.map(function (buy) { return buy.execution_t; })});
    }).catch(function (error) {
      if (!root || !root.isConnected || seq !== paperChartSeq ||
          root.querySelector("#dca-paper-session").value !== sessionId) return;
      if (paperChart) { paperChart.remove(); paperChart = null; }
      paperChartKey = "";
      detail.textContent = "Gráfico PAPER no verificable: " + error.message;
    });
  }
  function renderPaperGap(report, archive, box) {
    box.hidden = false;
    box.innerHTML = '<strong>TEORÍA · ' + esc(report.missed_days) + ' días</strong>' +
      '<span>No observado en directo · sin compras PAPER · guardado local ' + esc(archive.id.slice(0, 8)) +
      ' · huella ' + esc(archive.hash.slice(0, 12)) + '…</span>' +
      '<div class="tablewrap"><table><thead><tr><th>Señal</th><th>Cierre proxy</th><th>Compras hipotéticas</th></tr></thead><tbody>' +
      report.steps.map(function (step) { return '<tr><td>' + esc(utc(step.signal_t)) + '</td><td>' +
        rounded(step.close_proxy, 4) + '</td><td>' + (step.hypothetical_buys.length ?
          step.hypothetical_buys.map(function (buy) { return esc(buy.kind) + ' ' + rounded(buy.spent) + ' USDT'; }).join(' · ') :
          'Ninguna') + '</td></tr>'; }).join('') + '</tbody></table></div>';
  }
  function loadArchivedPaperGap(sessionId, reportId) {
    var seq = ++paperGapSeq, box = root && root.querySelector("#dca-paper-gap-report");
    if (!box || !reportId) return;
    box.hidden = false;
    box.textContent = "Verificando informe guardado…";
    api("/automation/dca/paper/sessions/" + encodeURIComponent(sessionId) + "/gap-analyses/" + encodeURIComponent(reportId)).then(function (detail) {
      if (box.isConnected && seq === paperGapSeq && root.querySelector("#dca-paper-session").value === sessionId)
        renderPaperGap(detail.result, detail, box);
    }).catch(function (error) {
      if (box.isConnected && seq === paperGapSeq) box.textContent = "Archivo DCA no verificable: " + error.message;
    });
  }
  function loadSavedPaperGap(sessionId, preferredId, showSelected) {
    var seq = ++paperGapSeq, box = root && root.querySelector("#dca-paper-gap-report");
    if (!box) return;
    api("/automation/dca/paper/sessions/" + encodeURIComponent(sessionId) + "/gap-analyses").then(function (saved) {
      if (!box.isConnected || seq !== paperGapSeq || root.querySelector("#dca-paper-session").value !== sessionId) return;
      var field = root.querySelector("#dca-paper-gap-archive-field"), select = root.querySelector("#dca-paper-gap-archive");
      field.hidden = !saved.length;
      if (!saved.length) return;
      select.innerHTML = saved.map(function (item) {
        var date = new Date(item.created_at);
        var stamp = Number.isNaN(date.getTime()) ? item.created_at : date.toLocaleString("es-ES", {dateStyle:"short", timeStyle:"short"});
        return '<option value="' + esc(item.id) + '">' + esc(stamp) + ' · ' + esc(item.missed_days) + ' d · ' + esc(item.id.slice(0, 8)) + '</option>';
      }).join("");
      select.value = saved.some(function (item) { return item.id === preferredId; }) ? preferredId : saved[0].id;
      if (showSelected !== false) loadArchivedPaperGap(sessionId, select.value);
    }).catch(function (error) {
      if (box.isConnected && seq === paperGapSeq) { box.hidden = false; box.textContent = "Archivo DCA no verificable: " + error.message; }
    });
  }
  function loadPaperGap(sessionId) {
    var seq = ++paperGapSeq, box = root && root.querySelector("#dca-paper-gap-report");
    if (!box) return;
    box.hidden = false;
    box.textContent = "Consultando velas cerradas…";
    api("/automation/dca/paper/sessions/" + encodeURIComponent(sessionId) + "/gap-analyses", {method:"POST"}).then(function (report) {
      if (!box.isConnected || seq !== paperGapSeq || root.querySelector("#dca-paper-session").value !== sessionId) return;
      renderPaperGap(report, report.archive, box);
      loadSavedPaperGap(sessionId, report.archive.id, false);
    }).catch(function (error) {
      if (!box.isConnected || seq !== paperGapSeq || root.querySelector("#dca-paper-session").value !== sessionId) return;
      box.textContent = "Hueco no verificable: " + error.message;
    });
  }
  function loadPaperComparison(sessionId) {
    var seq = ++paperComparisonSeq;
    api("/automation/dca/paper/sessions/" + encodeURIComponent(sessionId) + "/comparison").then(function (data) {
      var box = root && root.querySelector("#dca-paper-comparison");
      if (!box || !box.isConnected || seq !== paperComparisonSeq ||
          root.querySelector("#dca-paper-session").value !== sessionId) return;
      var history = data.historical, paper = data.paper;
      var historicalText = history && history.verification && history.verification.verified
        ? '<small>HISTÓRICO · VERIFICADO</small><strong>' + esc(history.conditional_metrics.buys) +
          ' compras · ' + rounded(history.conditional_metrics.invested) + ' USDT</strong><span>Run ' +
          esc(history.run_id.slice(0, 8)) + (data.source_run_id ? ' · origen fijado' : '') + '</span>'
        : history ? '<small>HISTÓRICO · NO VERIFICABLE</small><strong>Resultado oculto</strong><span>' +
          esc(history.verification && history.verification.reason || 'No se pudo comprobar') + '</span>'
        : '<small>HISTÓRICO · SIN REFERENCIA</small><strong>Sin simulación guardada</strong><span>Misma versión y dataset.</span>';
      box.className = "dca-paper-compare";
      box.innerHTML = '<div>' + historicalText + '</div><div><small>PAPER · OBSERVADO</small><strong>' +
        esc(paper.paper_buys) + ' compras · ' + rounded(paper.spent_usdt) + ' USDT</strong><span>' +
        esc(paper.observed_days) + ' días · comisiones ' + rounded(paper.fees_usdt) +
        ' USDT</span></div><p data-help="El histórico compra en aperturas pasadas; PAPER usa bid/ask observado en fechas posteriores. No se suman capitales ni se calcula un delta de rentabilidad.">Misma regla · periodos y ejecución distintos.</p>';
    }).catch(function (error) {
      var box = root && root.querySelector("#dca-paper-comparison");
      if (!box || !box.isConnected || seq !== paperComparisonSeq ||
          root.querySelector("#dca-paper-session").value !== sessionId) return;
      box.className = "dca-paper-compare state error";
      box.textContent = "Comparación no disponible: " + error.message;
    });
  }
  function clearPaperReserveResult() {
    paperReserveSeq++;
    var result = root && root.querySelector("#dca-paper-reserve-result");
    if (!result) return;
    result.replaceChildren();
    root.querySelector("#dca-paper-reserve-compare").disabled =
      !root.querySelector("#dca-paper-reserve-select").value;
  }
  function syncPaperReserve(session) {
    var details = root.querySelector("#dca-paper-reserve"), previous = paperReserveSession;
    details.hidden = !session;
    if (!session || !previous || session.id !== previous.id) {
      paperReserveSession = session;
      details.open = false;
      root.querySelector("#dca-paper-reserve-select").replaceChildren();
      clearPaperReserveResult();
    } else if (session.tick_index !== previous.tick_index || session.status !== previous.status) {
      paperReserveSession = session;
      clearPaperReserveResult();
    }
    if (session && details.open) {
      var issued = paperReserveSeq, scope = paperReserveSession;
      api("/lab/status", {cache:"no-store"}).then(function (status) {
        if (details.isConnected && issued === paperReserveSeq && scope === paperReserveSession &&
            status.format === "encrypted_or_invalid" && !status.unlocked) {
          clearPaperReserveResult();
          root.querySelector("#dca-paper-reserve-result").textContent = "Desbloquea LAB";
        }
      }).catch(function () {
        if (details.isConnected && issued === paperReserveSeq) clearPaperReserveResult();
      });
    }
  }
  function loadPaperReserves() {
    var session = paperReserveSession, select = root.querySelector("#dca-paper-reserve-select");
    var issued = ++paperReserveSeq;
    api("/automation/dca/validations", {cache:"no-store"}).then(function (rows) {
      if (!select.isConnected || issued !== paperReserveSeq || session !== paperReserveSession) return;
      var choices = rows.filter(function (row) {
        return row.version_id === session.version_id && row.dataset_id === session.dataset_id && row.dataset_hash === session.dataset_hash;
      });
      var selected = select.value;
      select.replaceChildren(new Option(choices.length ? "Selecciona una reserva" : "Sin reserva de esta versión", ""));
      choices.forEach(function (row) {
        select.add(new Option(row.test_bars + " días · " + row.created_at.slice(0, 16) + " · " + row.id.slice(0, 8), row.id));
      });
      select.value = choices.some(function (row) { return row.id === selected; }) ? selected : "";
      clearPaperReserveResult();
    }).catch(function (error) {
      if (!select.isConnected || issued !== paperReserveSeq) return;
      select.replaceChildren();
      clearPaperReserveResult();
      root.querySelector("#dca-paper-reserve-result").textContent = error.message;
    });
  }
  function comparePaperReserve() {
    var session = paperReserveSession, validationId = root.querySelector("#dca-paper-reserve-select").value;
    if (!session || !validationId) return;
    var result = root.querySelector("#dca-paper-reserve-result");
    var button = root.querySelector("#dca-paper-reserve-compare"), issued = ++paperReserveSeq;
    button.disabled = true;
    result.textContent = "Verificando LAB y PAPER…";
    function current() { return result.isConnected && issued === paperReserveSeq && session === paperReserveSession; }
    api("/automation/dca/paper/sessions/" + encodeURIComponent(session.id) + "/reserve-comparison?validation_id=" +
      encodeURIComponent(validationId), {cache:"no-store"}).then(function (data) {
      if (!current()) return;
      var reserve = data.reserve, paper = data.paper;
      var paperPeriod = paper.last_observed_at_ns === null ? "Sin observaciones" :
        utc(paper.first_execution_t) + " → " + utc(Number(paper.last_observed_at_ns.slice(0, -9)));
      var metrics = [
        ["Días", reserve.days, paper.observed_days], ["Compras", reserve.metrics.buys, paper.metrics.buys],
        ["Presupuesto", "initial_capital"], ["Gastado", "invested"], ["Caja", "remaining_cash"],
        ["Unidades", "units", 8], ["Comisiones", "fees"], ["Patrimonio", "final_equity"],
        ["Resultado", "net_profit"], ["Rentabilidad %", "return_on_initial_capital_pct"],
        ["Drawdown %", "max_drawdown_pct"]
      ];
      var rows = metrics.map(function (row) {
        if (typeof row[1] === "number") return "<tr><th scope='row'>" + row[0] +
          "</th><td>" + esc(row[1]) + "</td><td>" + esc(row[2]) + "</td></tr>";
        return "<tr><th scope='row'>" + row[0] + "</th>" + [reserve.metrics, paper.metrics].map(function (values) {
          return '<td class="num" title="' + esc(values[row[1]]) + '">' + esc(rounded(values[row[1]], row[2])) + '</td>';
        }).join("") + "</tr>";
      }).join("");
      result.innerHTML = '<div class="tablewrap"><table aria-label="Reserva LAB y PAPER DCA"><thead>' +
        '<tr><th scope="col">Métrica</th><th scope="col">Reserva LAB</th><th scope="col">PAPER observado</th></tr></thead><tbody>' +
        '<tr><th scope="row">Periodo</th><td>' + esc(utc(reserve.start_t) + " → " + utc(reserve.end_t)) +
        '</td><td>' + esc(paperPeriod) + '</td></tr>' + rows + '</tbody></table></div>' +
        '<small class="muted">USDT · LAB al cierre · PAPER al bid observado · periodos distintos</small>';
    }).catch(function (error) { if (current()) result.textContent = error.message; })
      .finally(function () { if (current()) button.disabled = false; });
  }
  function showRun(run) {
    var target = root.querySelector("#dca-report"); if (!target) return;
    root.querySelector("#dca-run-select").value = run.id;
    var names = {conditional:"DCA condicionado", fixed:"DCA fijo", lump:"Compra inicial"};
    var rows = ["conditional","fixed","lump"].map(function (mode) {
      var m = run.results[mode].metrics;
      return '<tr><th scope="row">' + names[mode] + '</th><td title="' + esc(m.invested) + '">' + rounded(m.invested) +
        '</td><td title="' + esc(m.remaining_cash) + '">' + rounded(m.remaining_cash) +
        '</td><td title="' + esc(m.units) + '">' + rounded(m.units, 8) + '</td><td title="' + esc(m.average_cost || "") + '">' + rounded(m.average_cost) +
        '</td><td title="' + esc(m.asset_value) + '">' + rounded(m.asset_value) + '</td><td title="' + esc(m.net_profit) + '">' + rounded(m.net_profit) +
        '</td><td title="' + esc(m.return_on_initial_capital_pct == null ? 'No calculado por el motor de esta ejecución' : m.return_on_initial_capital_pct) + '">' +
        (m.return_on_initial_capital_pct == null ? '—' : rounded(m.return_on_initial_capital_pct) + '%') +
        '</td><td title="' + esc(m.fees) + '">' + rounded(m.fees) + '</td><td>' + rounded(m.max_drawdown_pct) +
        '%</td><td title="' + esc(m.equity_volatility_per_bar_pct == null ? 'No calculado por el motor de esta ejecución' :
          m.equity_volatility_per_bar_pct + '% · ' + m.equity_return_observations + ' cambios de patrimonio' +
          (m.equity_return_observations < 30 ? ' · muestra pequeña' : '')) + '">' +
        (m.equity_volatility_per_bar_pct == null ? '—' : rounded(m.equity_volatility_per_bar_pct) + '%') +
        '</td><td>' + esc(m.buys) + '</td></tr>';
    }).join("");
    var calendar = run.results.conditional.events.map(function (event) {
      return '<tr><td>' + esc(new Date(event.execution_time * 1000).toISOString().slice(0,16)) + '</td><td>' +
        esc(event.kind + (event.amount_factor != null ? " · ×" + event.amount_factor : "") +
          (event.volume_ratio != null ? " · " + rounded(event.volume_ratio, 4) + "× vol." : "")) +
        '</td><td title="' + esc(event.spent) + '">' + rounded(event.spent) + '</td><td title="' + esc(event.units) + '">' + rounded(event.units, 8) +
        '</td><td title="' + esc(event.price) + '">' + rounded(event.price, 4) + '</td></tr>';
    }).join("");
    var average = run.rule && (run.rule.ma_adjustment || run.rule.sma_gate);
    var averageSkips = average && run.results.conditional.skipped.filter(function (event) {
      return ["SMA_WARMUP", "SMA_CONDITION_NOT_MET", "MA_WARMUP", "MA_ZERO_AMOUNT"].includes(event.reason);
    }).length;
    var volume = run.rule && run.rule.volume_gate;
    var volumeSkips = volume && run.results.conditional.skipped.filter(function (event) {
      return event.reason.startsWith("VOLUME_");
    }).length;
    target.className = "";
    target.dataset.runId = run.id;
    target.dataset.integrity = "unchecked";
    var generation = Number(target.dataset.verifyGeneration || 0) + 1;
    target.dataset.verifyGeneration = String(generation);
    target.innerHTML = '<p class="muted">' + esc(run.engine_version) + ' · dataset ' + esc(run.dataset_id) + ' · versión ' + esc(run.version_id) +
      ' · referencia ' + (run.rule && run.rule.reference_mode === "high_watermark" ? 'máximo observado' : 'primer cierre') + '</p>' +
      (average ? '<p class="muted" data-dca-sma-summary>' + esc(averageLabel(average)) + ' · ' + averageSkips +
        (averageSkips === 1 ? ' señal descartada' : ' señales descartadas') + '</p>' : '') +
      (volume ? '<p class="muted" data-dca-volume-summary>' + esc(volumeLabel(volume)) + ' · ' + volumeSkips +
        (volumeSkips === 1 ? ' señal descartada' : ' señales descartadas') + '</p>' : '') +
      '<div class="lab-verify" data-state="unchecked"><span class="lab-verify-badge">SIN COMPROBAR</span>' +
      '<button type="button" class="ghost" data-help="Repite las tres variantes con el histórico y el plan guardados. No garantiza resultados futuros.">Verificar</button>' +
      '<span class="lab-verify-detail" role="status" aria-live="polite"></span></div>' +
      '<p class="muted" data-help="Círculo: condición conocida al cierre previo. Flecha: compra ficticia en la apertura siguiente. No es una orden en mercado actual.">● Decisión · ↑ Compra simulada</p>' +
      '<div id="dca-chart-range" class="lab-history-controls"></div>' +
      '<div id="dca-chart" role="img" aria-label="Velas históricas con decisiones y compras DCA simuladas"></div><p id="dca-chart-detail" class="muted" role="status">Mueve el cursor sobre una marca para ver su motivo.</p>' +
      '<div class="tablewrap"><table><thead><tr><th>Variante</th><th>Invertido</th><th data-help="Presupuesto inicial aún sin invertir.">Efectivo</th><th>Unidades</th><th>Medio</th><th>Valor</th><th>Resultado</th><th data-help="(Patrimonio final − presupuesto inicial) / presupuesto inicial. Incluye efectivo no invertido; no mide riesgo ni iguala fechas de compra.">Rent. capital</th><th>Comisiones</th><th>DD máx.</th><th data-help="Desviación de cambios porcentuales del patrimonio entre cierres. Incluye efectivo y no se anualiza. Consulta el número de cambios en cada cifra; menos de 30 es una muestra pequeña.">Vol./vela</th><th>Compras</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<h4>Compras condicionadas</h4><div class="tablewrap dca-calendar"><table><thead><tr><th>Fecha UTC</th><th>Motivo</th><th>Importe</th><th>Unidades</th><th>Precio</th></tr></thead><tbody>' +
      (calendar || '<tr><td colspan="5">Sin compras</td></tr>') + '</tbody></table></div>' +
      '<details><summary>Supuestos y cifras exactas</summary><p class="muted">' + esc(run.assumptions.join(" · ")) + '</p><pre class="lab-events">' + esc(JSON.stringify(run, null, 2)) + '</pre></details>';
    var panel = target.querySelector(".lab-verify"), button = panel.querySelector("button");
    button.addEventListener("click", function () {
      var requestGeneration = Number(target.dataset.verifyGeneration) + 1;
      target.dataset.verifyGeneration = String(requestGeneration);
      button.disabled = true;
      panel.dataset.state = "checking";
      panel.querySelector(".lab-verify-badge").textContent = "COMPROBANDO";
      panel.querySelector(".lab-verify-detail").textContent = "";
      api("/automation/dca/runs/" + encodeURIComponent(run.id) + "/verify").then(function (result) {
        if (!target.isConnected || target.dataset.runId !== run.id ||
            Number(target.dataset.verifyGeneration) !== requestGeneration) return;
        var labels = {verified:"REPRODUCIBLE", mismatch:"DIFERENCIA DETECTADA",
          engine_unavailable:"MOTOR NO DISPONIBLE", legacy_unpinned:"LEGADO SIN HUELLAS"};
        var status = Object.prototype.hasOwnProperty.call(labels, result.status) ? result.status : "error";
        panel.dataset.state = status;
        panel.querySelector(".lab-verify-badge").textContent = labels[status] || "NO VERIFICABLE";
        panel.querySelector(".lab-verify-detail").textContent = result.reason || "No se pudo comprobar DCA.";
        target.dataset.integrity = status;
        if (status === "mismatch") {
          if (chart) { chart.remove(); chart = null; }
          target.querySelector("#dca-chart").textContent = "Gráfico bloqueado: la comparación guardada no coincide con la repetición.";
          target.querySelector("#dca-chart-range").textContent = "";
        }
      }).catch(function (error) {
        if (!target.isConnected || target.dataset.runId !== run.id ||
            Number(target.dataset.verifyGeneration) !== requestGeneration) return;
        panel.dataset.state = "error";
        panel.querySelector(".lab-verify-badge").textContent = "NO VERIFICADO";
        panel.querySelector(".lab-verify-detail").textContent = error.message;
      }).finally(function () {
        if (target.isConnected && target.dataset.runId === run.id &&
            Number(target.dataset.verifyGeneration) === requestGeneration) button.disabled = false;
      });
    });
    api("/lab/datasets/" + encodeURIComponent(run.dataset_id)).then(function (dataset) {
      if (!root || !root.isConnected || root.querySelector("#dca-run-select").value !== run.id ||
          target.dataset.integrity === "mismatch") return;
      if (dataset.hash !== run.dataset_hash) throw new Error("El histórico no coincide con la huella guardada");
      draw(dataset.candles, run.results.conditional.events);
    }).catch(function (error) { notice("No se pudo dibujar DCA: " + error.message, true); });
  }
  function draw(candles, events) {
    if (chart) { chart.remove(); chart = null; }
    var target = root.querySelector("#dca-chart"), lw = window.LightweightCharts;
    if (!target || !lw) { notice("Gráfico no disponible.", true); return; }
    chart = lw.createChart(target, {autoSize:true, height:300,
      layout:{background:{type:"solid", color:"#101b18"},textColor:"#c9d6ce"},
      grid:{vertLines:{color:"#24362f"},horzLines:{color:"#24362f"}},timeScale:{timeVisible:true,minBarSpacing:0.1}});
    var series = chart.addSeries(lw.CandlestickSeries, {upColor:"#42c88a",downColor:"#ff7b78",borderVisible:false,wickUpColor:"#42c88a",wickDownColor:"#ff7b78"});
    series.setData(candles.map(function (bar) { return {time:bar.t,open:Number(bar.o),high:Number(bar.h),low:Number(bar.l),close:Number(bar.c)}; }));
    var grouped = new Map(), byTime = new Map();
    events.forEach(function (event) {
      var group = grouped.get(event.execution_time) || [];
      group.push(event); grouped.set(event.execution_time, group);
      var signal = byTime.get(event.signal_time) || [];
      signal.push("Decisión " + event.kind + ": cierre " + event.signal_close + (event.drop_pct ? " · caída " + event.drop_pct : ""));
      byTime.set(event.signal_time, signal);
      var order = byTime.get(event.execution_time) || [];
      order.push("Compra simulada " + event.kind + ": " + event.spent + " a " + event.price);
      byTime.set(event.execution_time, order);
    });
    var markers = [];
    grouped.forEach(function (group, time) {
      markers.push({time:group[0].signal_time,position:"aboveBar",color:"#efbd63",shape:"circle",text:group.length > 1 ? "Decisión ×" + group.length : "Decisión"});
      markers.push({time:time,position:"belowBar",color:"#88b8ff",shape:"arrowUp",text:group.length > 1 ? "Compra ×" + group.length : "Compra"});
    });
    lw.createSeriesMarkers(series, markers.sort(function (a,b) { return a.time - b.time; }));
    target.dataset.decisionCount = String(grouped.size);
    target.dataset.executionCount = String(events.length);
    chart.subscribeCrosshairMove(function (param) {
      var detail = root && root.querySelector("#dca-chart-detail");
      if (detail) detail.textContent = param.time === undefined || !byTime.has(param.time) ?
        "Mueve el cursor sobre una marca para ver su motivo." : byTime.get(param.time).join(" · ");
    });
    chart.timeScale().fitContent();
    if (window.OrbisChartRange) window.OrbisChartRange.mount(root.querySelector("#dca-chart-range"), chart, candles, {
      storageKey:"orbis.chartRange.dca"
    });
  }
  function cleanup() { validationSeq++; if (chart) { chart.remove(); chart = null; } clearPaperChart(); if (paperTimer) { clearInterval(paperTimer); paperTimer = null; } transfer = null; root = null; }
  window.OrbisDca = {render:render, cleanup:cleanup};
})();
