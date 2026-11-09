/* Laboratorio v1: histórico conservado, reglas versionadas y backtesting real.
 * El handoff PAPER requiere activación explícita. Los cálculos viven en Python. */
(function () {
  "use strict";
  var root = null, chart = null, equityChart = null, researchChart = null, perpDataWidget = null, perpRunsWidget = null, perpValidationWidget = null, perpOptimizationWidget = null, perpObservedWidget = null;
  var datasets = [], strategies = [], runs = [], researchRuns = [], validations = [], optimizations = [];
  var activeDataset = null, activeRun = null, activeResearch = null, researchPage = 0, researchCandles = [];
  var runVerifyEpoch = 0, runSelectionEpoch = 0, verifiedPaperTarget = null;
  var strategySelectionEpoch = 0, versionSelectionEpoch = 0;
  var aiArchiveWidget = null;
  function esc(x) { return String(x == null ? "" : x).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }
  var focusResearchPivots = null;
  function clearResearchChart() {
    if (researchChart) researchChart.remove();
    researchChart = focusResearchPivots = null;
  }
  function displayDecimal(x, digits) {
    if (x == null) return "—";
    var value = Number(x);
    if (!Number.isFinite(value)) return "—";
    return "≈ " + new Intl.NumberFormat("es-ES", { maximumFractionDigits:digits || 4 }).format(value);
  }
  function displayPercent(x, digits) { return x == null ? "—" : displayDecimal(x, digits) + "%"; }
  var EXIT_REASONS = {
    STOP: ["Stop", "El precio alcanzó el stop; si abrió más allá, se usó esa apertura adversa."],
    TARGET: ["Objetivo", "El precio alcanzó el objetivo dentro de la vela simulada."],
    TARGET_OPEN: ["Objetivo · apertura", "La vela abrió más allá del objetivo; se usó el precio límite sin presumir mejora."],
    TRAILING_STOP: ["Stop móvil", "El stop se ajustó con extremos de velas anteriores ya cerradas."],
    TRAILING_STOP_AND_TARGET_AMBIGUOUS: ["Stop móvil · vela ambigua", "Stop móvil y objetivo aparecen en la misma vela sin conocer el orden; prevaleció el stop."],
    STOP_AND_TARGET_AMBIGUOUS: ["Stop · vela ambigua", "Stop y objetivo aparecen en la misma vela sin conocer el orden; se atribuyó al stop."],
    EXIT_RULE: ["Regla de salida", "La condición de cierre se detectó al cerrar la vela previa y se ejecutó en esta apertura."],
    TIME_LIMIT: ["Límite de velas", "La posición completó el número máximo de velas y se cerró en la apertura siguiente."],
    END_OF_DATA: ["Fin del histórico", "La posición simulada se cerró al terminar las velas disponibles."]
  };
  function exitReason(reason) {
    var known = EXIT_REASONS[reason];
    return known ? '<span data-help="' + esc(known[1]) + '">' + esc(known[0]) + '</span>'
      : '<span data-help="Motivo no reconocido por esta interfaz; conserva el código original.">' + esc(reason) + '</span>';
  }
  function api(path, options) {
    return fetch("/lab" + path, Object.assign({ headers: {"Content-Type":"application/json"} }, options || {})).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) {
          var error = new Error(body.detail || "Error en Laboratorio");
          error.status = response.status;
          throw error;
        }
        return body;
      });
    });
  }
  function notice(message, bad) {
    var el = root && root.querySelector("#lab-notice");
    if (el) { el.textContent = message; el.className = "state " + (bad ? "error" : "ok"); el.hidden = false; }
  }
  function optionList(items, label) {
    return '<option value="">' + label + '</option>' + items.map(function (item) {
      return '<option value="' + esc(item.id) + '">' + esc(item.name || item.symbol) + (item.version ? " · v" + item.version : " · " + item.timeframe + " · " + item.count + " velas") + '</option>';
    }).join("");
  }
  function render(container) {
    if (aiArchiveWidget) { aiArchiveWidget.dispose(); aiArchiveWidget = null; }
    if (perpDataWidget) { perpDataWidget.dispose(); perpDataWidget = null; }
    if (perpRunsWidget) { perpRunsWidget.dispose(); perpRunsWidget = null; }
    if (perpValidationWidget) { perpValidationWidget.dispose(); perpValidationWidget = null; }
    if (perpOptimizationWidget) { perpOptimizationWidget.dispose(); perpOptimizationWidget = null; }
    if (perpObservedWidget) { perpObservedWidget.dispose(); perpObservedWidget = null; }
    root = container;
    strategySelectionEpoch += 1; versionSelectionEpoch += 1;
    runVerifyEpoch += 1;
    runSelectionEpoch += 1;
    verifiedPaperTarget = null;
    activeRun = null;
    if (chart) { chart.remove(); chart = null; }
    if (equityChart) { equityChart.remove(); equityChart = null; }
    clearResearchChart();
    activeResearch = null; researchCandles = []; researchPage = 0;
    root.innerHTML = '<section class="card lab-page"><div class="pagina-cabecera"><div><p class="real-eyebrow">LABORATORIO · CAPITAL FICTICIO</p>' +
      '<h2 data-help="Carga datos, define reglas, ejecuta y recupera pruebas reproducibles.">Laboratorio</h2><p class="muted">Backtesting · capital ficticio</p></div>' +
      '<span class="statusbadge on">LAB · TEÓRICO</span></div><p id="lab-notice" class="state" role="status" hidden></p>' +
      '<details class="real-panel lab-ai-panel" id="lab-ai-archive-index"><summary data-help="Abre las respuestas guardadas en LAB cifrado y la evidencia agregada que acompañó a cada consulta.">Archivo de IA</summary>' +
      '<div class="row"><label class="field">Tipo<select><option value="">Todos</option><option value="research">Estructuras</option>' +
      '<option value="backtest">Backtest</option></select></label><button type="button" class="ghost" data-ai-archive-refresh>Actualizar</button></div>' +
      '<p class="muted" data-ai-archive-status role="status"></p><div class="tablewrap"><table aria-label="Análisis guardados"><thead><tr>' +
      '<th scope="col">Incluir</th><th>Consulta</th><th>Tipo</th><th>Mercado</th><th>Fecha UTC</th><th>Modelo</th></tr></thead><tbody></tbody></table></div>' +
      '<div class="row lab-ai-archive-actions"><button type="button" class="ghost" data-ai-archive-prev disabled>Anterior</button>' +
      '<button type="button" class="ghost" data-ai-archive-next disabled>Siguiente</button>' +
      '<button type="button" class="ghost" data-ai-archive-jsonl disabled data-help="Exporta hasta 50 análisis seleccionados en claro. Sus respuestas son anotaciones del modelo.">Exportar JSONL</button>' +
      '</div><div data-ai-archive-detail hidden></div></details>' +
      '<div class="lab-grid"><section class="real-panel"><h3 data-help="Spot, perpetuos GRVT o CSV local. Las velas se conservan con huella; un hueco bloquea la prueba.">1 · Histórico</h3>' +
      '<form id="lab-download" class="lab-form"><label class="field">Fuente<select name="provider"><option value="binance">Binance · Spot</option><option value="mexc">MEXC · Spot</option><option value="grvt">GRVT · PERP</option></select></label>' +
      '<label class="field">Par<input name="symbol" value="BTC/USDT" required></label>' +
      '<label class="field">Temporalidad<select name="timeframe"><option>1h</option><option>4h</option><option>1d</option><option>15m</option><option>5m</option><option>1m</option></select></label>' +
      '<label class="field">Inicio UTC<input name="start" type="datetime-local" required></label><label class="field">Fin UTC<input name="end" type="datetime-local" required></label>' +
      '<button type="submit" data-help="Descarga únicamente velas cerradas, comprueba toda la cobertura y guarda una huella SHA-256.">Descargar</button>' +
      '<p id="lab-download-market-note" class="muted">Spot: mercado al contado. No se mezclará con PERP.</p></form>' +
      '<button type="button" id="lab-btc-archive" class="ghost" data-help="Conserva las velas diarias cerradas de BTC/USD de Bitstamp desde 2012. Requiere conexión para la descarga inicial; no genera órdenes.">BTC/USD · desde 2012</button>' +
      '<form id="lab-csv" class="lab-form"><label class="field" data-help="Columnas requeridas: time,open,high,low,close,volume. Tiempo UTC en segundos, milisegundos o ISO.">Archivo CSV<input name="file" type="file" accept=".csv,text/csv" required></label>' +
      '<label class="field">Par del archivo<input name="symbol" value="BTC/USDT" required></label>' +
      '<label class="field">Temporalidad<select name="timeframe"><option>1h</option><option>4h</option><option>1d</option><option>15m</option><option>5m</option><option>1m</option></select></label>' +
      '<button type="submit" class="ghost">Importar CSV</button></form>' +
      '<label class="field">Dataset conservado<select id="lab-dataset"></select></label><div id="lab-dataset-status" class="muted"></div></section>' +
      '<section class="real-panel"><h3 data-help="RSI, EMA, ATR % y volumen combinables con Y/O. Señal al cierre; entrada en la apertura siguiente.">2 · Estrategia</h3>' +
      '<label class="field">Definición guardada<select id="lab-strategy"></select></label><label class="field">Versión para ejecutar<select id="lab-version"><option value="">Guarda una estrategia</option></select></label>' +
      '<div class="lab-root-actions"><button type="button" id="lab-export-strategy" class="ghost" disabled data-help="Descarga esta versión exacta de LAB con su huella. No incluye datos de cartera ni autoriza órdenes.">Exportar versión</button>' +
      '<button type="button" id="lab-import-strategy" class="ghost" data-help="Importa un archivo de estrategia LAB verificando formato, reglas y huella. Crea una estrategia nueva; no sustituye la original.">Importar JSON</button>' +
      '<input id="lab-import-strategy-file" type="file" accept=".json,application/json" hidden></div>' +
      '<form id="lab-strategy-form" class="lab-form"><label class="field">Nombre<input name="name" maxlength="100" required placeholder="EMA + volumen"></label>' +
      '<label class="field" data-help="SHORT es una simulación experimental sin préstamo, financiación ni liquidación.">Dirección<select name="direction"><option>LONG</option><option>SHORT</option></select></label>' +
      '<label class="field" data-help="AND exige todas las condiciones; OR admite cualquiera.">Combinar<select name="op"><option>AND</option><option>OR</option></select></label>' +
      '<div id="lab-rule-list"></div><div class="lab-root-actions"><button type="button" id="lab-add-rule" class="ghost" data-help="Añade una condición al grupo principal.">＋ Condición</button>' +
      '<button type="button" id="lab-add-group" class="ghost" data-help="Agrupa condiciones con Y/O; admite hasta cuatro niveles y ocho elementos por grupo.">＋ Grupo Y/O</button></div>' +
      '<label class="field lab-exit-toggle" data-help="Opcional: evalúa un cierre independiente sobre la vela cerrada y sale en la siguiente apertura."><input name="exit_enabled" type="checkbox"> Cierre por regla</label>' +
      '<div id="lab-exit-editor" class="lab-exit-editor" hidden><label class="field">Combinar salida<select name="exit_op"><option>AND</option><option>OR</option></select></label>' +
      '<div id="lab-exit-rule-list"></div><div class="lab-root-actions"><button type="button" id="lab-add-exit-rule" class="ghost" data-help="Añade una condición de cierre.">＋ Condición</button>' +
      '<button type="button" id="lab-add-exit-group" class="ghost" data-help="Agrupa condiciones de cierre con Y/O.">＋ Grupo Y/O</button></div></div>' +
      '<div class="lab-params"><label class="field">Capital ficticio<input name="initial_capital" value="1000" inputmode="decimal" required></label>' +
      '<label class="field" data-help="Parte del capital ficticio asignada a cada posición; 1 equivale al 100 %.">Asignación<input name="allocation" value="1" inputmode="decimal" required></label>' +
      '<label class="field" data-help="Comisión proporcional aplicada tanto a la entrada como a la salida.">Comisión<input name="fee_rate" value="0.001" inputmode="decimal" required></label>' +
      '<label class="field" data-help="Deslizamiento proporcional adverso en la entrada y salida.">Slippage<input name="slippage_rate" value="0.001" inputmode="decimal" required></label>' +
      '<label class="field" data-help="Distancia fraccional al stop; 0.03 equivale al 3 %.">Stop loss<input name="stop_loss" value="0.03" inputmode="decimal" required></label>' +
      '<label class="field" data-help="Distancia fraccional al objetivo; 0.08 equivale al 8 %.">Take profit<input name="take_profit" value="0.08" inputmode="decimal" required></label>' +
      '<label class="field" data-help="LAB, Spot PAPER y GRVT PAPER. Se ajusta con extremos de velas cerradas. PAPER usa bid o mark observados y se detiene ante huecos; REAL aún no lo admite.">Stop móvil<input name="trailing_stop" inputmode="decimal" placeholder="Sin stop móvil"></label>' +
      '<label class="field" data-help="LAB sale en la apertura siguiente tras N velas. PAPER Spot y GRVT salen tras N velas cerradas observadas, usando precio público posterior; si hay un hueco, se detienen sin inventar una salida.">Máx. velas<input name="max_holding_bars" type="number" min="1" max="10000" step="1" placeholder="Sin límite"></label></div>' +
      '<button type="submit" id="lab-save-strategy">Guardar estrategia</button></form></section></div>' +
      '<section class="real-panel lab-research-panel"><div class="pagina-cabecera"><div><p class="real-eyebrow">INVESTIGACIÓN · SIN ÓRDENES</p>' +
      '<h3 data-help="Busca cada aparición en velas cerradas y calcula movimientos posteriores. No crea operaciones ni simula capital.">3 · Estructuras</h3></div>' +
      '<span class="statusbadge">OBSERVACIÓN</span></div>' +
      '<form id="lab-research-form" class="lab-research-form"><label class="field">Consulta<select name="source"><option value="streak">Caída + volumen creciente</option><option value="editor">Reglas del editor</option></select></label>' +
      '<label class="field" id="lab-research-period" data-help="Número de velas bajistas consecutivas y con volumen creciente (2–10).">Velas<input name="period" type="number" min="2" max="10" value="3" required></label>' +
      '<button type="submit" data-help="Usa el dataset seleccionado, conserva cada caso y calcula distribuciones posteriores. Sin entradas ni salidas simuladas.">Buscar casos</button></form>' +
      '<label class="field">Consultas guardadas<select id="lab-research-select"><option value="">Sin consultas</option></select></label>' +
      '<details id="lab-cohort" hidden><summary>Combinar consultas</summary>' +
      '<form id="lab-cohort-form"><fieldset class="lab-cohort-pick"><legend>Consultas · 2–8</legend>' +
      '<div id="lab-cohort-options"></div></fieldset><div class="row">' +
      '<label class="field" data-help="Desde las 00:00 UTC. Se excluyen del desarrollo las etiquetas de siete días que alcancen la reserva.">Reserva desde (UTC)<input name="boundary" type="date" required></label>' +
      '<button type="submit" class="ghost" disabled>Exportar conjunto</button></div>' +
      '<p class="muted" id="lab-cohort-result" role="status"></p></form></details>' +
      '<div id="lab-research-report" class="state">Elige un histórico y busca una estructura.</div></section>' +
      '<section class="real-panel lab-run-panel"><div class="pagina-cabecera"><div><h3 data-help="Una posición a la vez. Si stop y objetivo coinciden en una vela, se aplica el stop.">4 · Backtest</h3><p class="amber">Resultados teóricos; SHORT sin financiación ni liquidación.</p></div>' +
      '<button id="lab-run" type="button" disabled data-help="Ejecuta la versión seleccionada sobre el dataset conservado. No crea órdenes PAPER ni REAL.">Ejecutar prueba</button></div>' +
      '<label class="field">Pruebas guardadas<select id="lab-run-select"></select></label><div id="lab-report" class="state">Selecciona una prueba o ejecuta una.</div>' +
      '<h3 data-help="Elige dos backtests. ORBIS repite y verifica ambos antes de comparar; solo calcula diferencias con el mismo dataset y motor.">Comparar pruebas</h3>' +
      '<div class="lab-compare-controls"><label class="field">Base<select id="lab-compare-left"></select></label>' +
      '<label class="field">Alternativa<select id="lab-compare-right"></select></label>' +
      '<button type="button" id="lab-compare-action" data-help="Compara rentabilidad porcentual normalizada, drawdown y operaciones. No suma capitales ni sustituye validación fuera de muestra.">Comparar</button></div>' +
      '<div id="lab-comparison" class="state">Guarda dos pruebas para compararlas.</div></section>' +
      '<section class="real-panel lab-validation-panel"><div class="pagina-cabecera"><div><p class="real-eyebrow">ROBUSTEZ · LAB</p>' +
      '<h3>5 · Fuera de muestra</h3></div><span class="statusbadge">CRONOLÓGICA</span></div>' +
      '<div class="lab-validation-controls"><label class="field">Desarrollo / reserva<select id="lab-validation-fraction"><option value="0.7">70 / 30</option><option value="0.6">60 / 40</option><option value="0.8">80 / 20</option></select></label>' +
      '<button id="lab-validate" type="button" data-help="Exige 60 velas o más. Repite la misma versión sin optimizarla sobre el tramo reservado.">Validar versión</button>' +
      '<label class="field">Validaciones guardadas<select id="lab-validation-select"></select></label></div>' +
      '<div class="lab-rolling-controls"><span class="muted">Ventanas sucesivas · misma versión</span>' +
      '<label class="field" data-help="Cada reserva contiene al menos 30 velas; el entrenamiento crece y las reservas no se solapan.">Reservas<select id="lab-rolling-folds"><option value="2">2</option><option value="3" selected>3</option><option value="4">4</option><option value="5">5</option></select></label>' +
      '<button id="lab-rolling" type="button" data-help="Evalúa una versión fija en reservas sucesivas. No optimiza parámetros ni suma beneficios de capitales reiniciados.">Evaluar ventanas</button></div>' +
      '<div id="lab-validation-report" class="state">Selecciona un histórico y una versión para separar desarrollo y reserva.</div></section>' +
      '<section class="real-panel lab-opt-panel"><div class="pagina-cabecera"><div><p class="real-eyebrow">SENSIBILIDAD · LAB</p>' +
      '<h3 data-help="Compara stops y objetivos en desarrollo; la reserva se consulta sólo para el elegido.">6 · Parámetros</h3></div><span class="statusbadge">TEÓRICO</span></div>' +
      '<div class="lab-opt-controls"><label class="field">Método<select id="lab-opt-method"><option value="holdout">Reserva final</option><option value="walk-forward">Ventanas sucesivas</option></select></label>' +
      '<label class="field lab-opt-split">Desarrollo / reserva<select id="lab-opt-fraction"><option value="0.7">70 / 30</option><option value="0.6">60 / 40</option><option value="0.8">80 / 20</option></select></label>' +
      '<label class="field lab-opt-folds" hidden>Reservas<select id="lab-opt-fold-count"><option value="2">2</option><option value="3" selected>3</option><option value="4">4</option><option value="5">5</option></select></label>' +
      '<label class="field lab-opt-folds" hidden data-help="El tramo final queda fuera de todas las ventanas y se evalúa una sola vez, después de elegir parámetros con el desarrollo.">Reserva final<select id="lab-opt-final-fraction"><option value="0.1">10 %</option><option value="0.2" selected>20 %</option><option value="0.3">30 %</option><option value="0.4">40 %</option></select></label>' +
      '<label class="field" data-help="Fracciones separadas por espacios: 0.02 equivale a 2 %. Máximo tres stops.">Stops<input id="lab-opt-stops" value="0.02 0.04 0.06" required></label>' +
      '<label class="field" data-help="Fracciones separadas por espacios: 0.05 equivale a 5 %. Máximo tres objetivos.">Objetivos<input id="lab-opt-targets" value="0.05 0.10 0.15" required></label>' +
      '<button id="lab-optimize" type="button" data-help="Prueba hasta nueve combinaciones con la misma versión y costes. Exige diez operaciones en desarrollo; no crea órdenes.">Explorar</button></div>' +
      '<label class="field">Exploraciones guardadas<select id="lab-opt-select"></select></label>' +
      '<div id="lab-opt-report" class="state">Selecciona un histórico y una versión para explorar parámetros.</div></section></section>';
    aiArchiveWidget = window.OrbisLabAiArchive.mount(root.querySelector("#lab-ai-archive-index"), api, downloadEvidence);
    var perpPanel = document.createElement("details");
    perpPanel.className = "real-panel";
    perpPanel.id = "lab-perp-data";
    perpPanel.innerHTML = '<summary data-help="Archivo separado de precios TRADE, MARK y financiación de GRVT. Conserva reglas actuales y procedencia.">Datos de derivados</summary><div id="lab-perp-data-content"></div>';
    root.querySelector(".lab-grid").after(perpPanel);
    if (window.OrbisPerpDatasets) {
      perpDataWidget = window.OrbisPerpDatasets.mount(perpPanel.querySelector("div"));
      perpPanel.addEventListener("toggle", function () { if (perpPanel.open && perpDataWidget) perpDataWidget.load(); });
    }
    var perpRunsPanel = document.createElement("details");
    perpRunsPanel.className = "real-panel";perpRunsPanel.id = "lab-perp-runs";
    perpRunsPanel.innerHTML = '<summary data-help="Prueba una versión con margen y financiación usando trayectorias hipotéticas TRADE/MARK. Conserva perfil, resultados y diario en LAB cifrado.">Pruebas de derivados</summary><div></div>';
    perpPanel.after(perpRunsPanel);
    if (window.OrbisPerpExperiments) {
      var openingPerpWindow = false;
      perpRunsWidget = window.OrbisPerpExperiments.mount(perpRunsPanel.querySelector("div"));
      perpRunsPanel.addEventListener("toggle", function () { if (perpRunsPanel.open && perpRunsWidget && !openingPerpWindow) perpRunsWidget.load(); });
    }
    async function openPerpWindow(id, isCurrent) {
      if (!perpRunsWidget) return false;
      openingPerpWindow = true; perpRunsPanel.open = true;
      try {
        var opened = await perpRunsWidget.openRun(id, isCurrent);
        if (opened && isCurrent()) perpRunsPanel.scrollIntoView({block:"start"});
        return opened;
      } finally { openingPerpWindow = false; }
    }
    async function openPaperReview(id, options) {
      if (!perpObservedWidget || (options && !options.isCurrent())) return false;
      observedPanel.open = true;
      var prepared = await perpObservedWidget.prepare(id, options);
      if (!options || options.isCurrent()) {
        observedPanel.querySelector(prepared ? '#observed-review' : '#observed-status').scrollIntoView({block:'center'});
      }
      return prepared;
    }
    var perpValidationPanel = document.createElement("details");
    perpValidationPanel.className = "real-panel";perpValidationPanel.id = "lab-perp-validations";
    perpValidationPanel.innerHTML = '<summary data-help="Reserva cronológica, ventanas sucesivas y sensibilidad a recorridos. Versión fija y capital independiente por tramo.">Validación de derivados</summary><div></div>';
    perpRunsPanel.after(perpValidationPanel);
    if (window.OrbisPerpValidation) {
      perpValidationWidget = window.OrbisPerpValidation.mount(perpValidationPanel.querySelector("div"),{onInspect:openPerpWindow,
        onPaper:openPaperReview});
      perpValidationPanel.addEventListener("toggle",function(){if(perpValidationPanel.open&&perpValidationWidget)perpValidationWidget.load();});
    }
    var observedPanel=document.createElement('details');observedPanel.className='real-panel';observedPanel.id='lab-perp-observed';
    observedPanel.innerHTML='<summary data-help="Prueba una reserva validada con precios actuales y capital ficticio nuevo. Gráfico, ejecuciones y diario separados de los backtests.">PAPER de derivados</summary><div></div>';
    var optimizationPanel = document.createElement("details");
    optimizationPanel.className = "real-panel"; optimizationPanel.id = "lab-perp-optimization";
    optimizationPanel.innerHTML = '<summary data-help="Compara stops y objetivos. La reserva posterior sólo evalúa la combinación elegida en desarrollo.">Optimizar derivados</summary><div></div>';
    perpValidationPanel.after(optimizationPanel);
    if (window.OrbisPerpOptimization) {
      perpOptimizationWidget = window.OrbisPerpOptimization.mount(optimizationPanel.querySelector("div"), {
        onInspect:openPerpWindow,
        onPaper:openPaperReview,
        onVersion:async function (variant, isCurrent) {
          await reload(variant.strategy_id, null, isCurrent);
          if (!isCurrent()) return false;
          var selector = root.querySelector("#lab-version");
          if (!Array.from(selector.options).some(function (option) { return option.value === variant.version_id; })) return false;
          if (selector.value !== variant.version_id) {
            selector.value = variant.version_id;
            await selectVersion();
          }
          if (!isCurrent() || selector.dataset.ready !== "true") return false;
          root.querySelector("#lab-strategy-form").scrollIntoView({block:"start"});
          return true;
        }
      });
      optimizationPanel.addEventListener("toggle", function () {
        if (!perpOptimizationWidget || !optimizationPanel.isConnected) return;
        if (optimizationPanel.open) perpOptimizationWidget.load(); else perpOptimizationWidget.close();
      });
    }
    optimizationPanel.after(observedPanel);
    if(window.OrbisPerpObserved){perpObservedWidget=window.OrbisPerpObserved.mount(observedPanel.querySelector('div'));observedPanel.addEventListener('toggle',function(){if(observedPanel.open)perpObservedWidget.load();});}
    root.querySelector("#lab-download").addEventListener("submit", download);
    root.querySelector("#lab-btc-archive").addEventListener("click", importBtcArchive);
    root.querySelector('#lab-download [name="provider"]').addEventListener("change", updateDownloadMarket);
    root.querySelector("#lab-csv").addEventListener("submit", importCsv);
    root.querySelector("#lab-add-rule").addEventListener("click", function () { addRule(); });
    root.querySelector("#lab-add-group").addEventListener("click", function () { addGroup(false); });
    root.querySelector("#lab-add-exit-rule").addEventListener("click", function () { addRule(null, null, null, true); });
    root.querySelector("#lab-add-exit-group").addEventListener("click", function () { addGroup(true); });
    root.querySelector('[name="exit_enabled"]').addEventListener("change", updateExitEditor);
    root.querySelector("#lab-strategy-form").addEventListener("submit", saveStrategy);
    root.querySelector("#lab-dataset").addEventListener("change", selectDataset);
    root.querySelector("#lab-strategy").addEventListener("change", selectStrategy);
    root.querySelector("#lab-version").addEventListener("change", selectVersion);
    root.querySelector("#lab-export-strategy").addEventListener("click", exportSelectedStrategy);
    root.querySelector("#lab-import-strategy").addEventListener("click", function () {
      root.querySelector("#lab-import-strategy-file").click();
    });
    root.querySelector("#lab-import-strategy-file").addEventListener("change", importStrategyFile);
    root.querySelector("#lab-run").addEventListener("click", execute);
    root.querySelector("#lab-run-select").addEventListener("change", selectRun);
    root.querySelector("#lab-compare-action").addEventListener("click", compareSelectedRuns);
    ["#lab-compare-left", "#lab-compare-right"].forEach(function (selector) {
      root.querySelector(selector).addEventListener("change", function () {
        root.querySelector("#lab-comparison").textContent = "Pulsa Comparar para verificar estas pruebas.";
      });
    });
    root.querySelector("#lab-validate").addEventListener("click", executeValidation);
    root.querySelector("#lab-rolling").addEventListener("click", executeRollingValidation);
    root.querySelector("#lab-validation-select").addEventListener("change", selectValidation);
    root.querySelector("#lab-optimize").addEventListener("click", executeOptimization);
    root.querySelector("#lab-opt-method").addEventListener("change", updateOptimizationMethod);
    root.querySelector("#lab-opt-select").addEventListener("change", selectOptimization);
    root.querySelector("#lab-research-form").addEventListener("submit", executeResearch);
    root.querySelector('#lab-research-form [name="source"]').addEventListener("change", updateResearchSource);
    root.querySelector("#lab-research-select").addEventListener("change", selectResearch);
    var cohort = root.querySelector("#lab-cohort-form");
    cohort.addEventListener("change", function () { updateCohortButton(cohort); });
    cohort.addEventListener("submit", exportResearchCohort);
    addRule();
    addRule("CLOSE_BELOW_EMA", 20, null, true);
    updateExitEditor();
    updateResearchSource();
    reload();
  }
  function updateExitEditor() {
    var enabled = root.querySelector('[name="exit_enabled"]').checked;
    if (enabled && activeNodeCount() > 64) {
      root.querySelector('[name="exit_enabled"]').checked = false;
      enabled = false;
      notice("Reduce las reglas antes de activar el cierre: máximo 64 nodos por estrategia.", true);
    }
    var editor = root.querySelector("#lab-exit-editor");
    editor.hidden = !enabled;
    editor.querySelectorAll("input,select,button").forEach(function (control) { control.disabled = !enabled; });
  }
  var editableKinds = ["CLOSE_ABOVE_EMA","CLOSE_BELOW_EMA","EMA_CROSS_ABOVE_EMA","EMA_CROSS_BELOW_EMA","CLOSE_ABOVE_SMA","CLOSE_BELOW_SMA","CLOSE_ABOVE_BB_UPPER","CLOSE_BELOW_BB_LOWER","MACD_CROSS_ABOVE_SIGNAL","MACD_CROSS_BELOW_SIGNAL","RSI_LT","RSI_GT","ATR_PCT_GT","ATR_PCT_LT","VOLUME_ABOVE_AVG","RETEST_ABOVE_RANGE","RETEST_BELOW_RANGE","STRUCTURE_HH_HL","STRUCTURE_LH_LL",
        "RSI_BULLISH_DIVERGENCE","RSI_BEARISH_DIVERGENCE","BEARISH_STREAK","BULLISH_STREAK","VOLUME_ASCENDING_BARS","VOLUME_RISING_STREAK"];
  function divergence(kind) { return kind === "RSI_BULLISH_DIVERGENCE" || kind === "RSI_BEARISH_DIVERGENCE"; }
  function ruleMarkup(kind, period, value, retestBars, tolerance, slowPeriod, signalPeriod, pivotBars, timeframe, lookbackBars) {
    return '<div class="lab-rule"><label class="field">Condición<select name="kind">' +
      [["CLOSE_ABOVE_EMA","Cierre > EMA"],["CLOSE_BELOW_EMA","Cierre < EMA"],
        ["EMA_CROSS_ABOVE_EMA","EMA rápida cruza ↑ lenta"],["EMA_CROSS_BELOW_EMA","EMA rápida cruza ↓ lenta"],
        ["CLOSE_ABOVE_SMA","Cierre > SMA"],["CLOSE_BELOW_SMA","Cierre < SMA"],
        ["CLOSE_ABOVE_BB_UPPER","Cierre > BB sup."],["CLOSE_BELOW_BB_LOWER","Cierre < BB inf."],
        ["MACD_CROSS_ABOVE_SIGNAL","MACD cruza ↑ señal"],["MACD_CROSS_BELOW_SIGNAL","MACD cruza ↓ señal"],
        ["RSI_LT","RSI < umbral"],["RSI_GT","RSI > umbral"],
        ["RSI_BULLISH_DIVERGENCE","Divergencia RSI alcista"],["RSI_BEARISH_DIVERGENCE","Divergencia RSI bajista"],
        ["ATR_PCT_GT","ATR % > umbral"],["ATR_PCT_LT","ATR % < umbral"],["VOLUME_ABOVE_AVG","Volumen > media × factor"],
        ["RETEST_ABOVE_RANGE","Ruptura ↑ + retesteo"],["RETEST_BELOW_RANGE","Ruptura ↓ + retesteo"],
        ["STRUCTURE_HH_HL","Tendencia ↑ · máximos/mínimos"],["STRUCTURE_LH_LL","Tendencia ↓ · máximos/mínimos"],
        ["BEARISH_STREAK","Velas bajistas"],["BULLISH_STREAK","Velas alcistas"],
        ["VOLUME_ASCENDING_BARS","Volumen creciente"],["VOLUME_RISING_STREAK","Volumen: subidas consecutivas · legado"]].map(function (item) {
        return '<option value="' + item[0] + '"' + (item[0] === kind ? " selected" : "") + '>' + item[1] + '</option>';
      }).join("") + '</select></label>' +
      '<label class="field lab-timeframe-field" data-help="Base usa el intervalo del histórico. Una temporalidad superior se calcula sólo con velas completas del mismo histórico; si no hay cobertura suficiente, no genera señal.">Marco<select name="rule_timeframe">' +
      [["","Base"],["5m","5m"],["15m","15m"],["1h","1h"],["4h","4h"],["1d","1d"]].map(function (item) {
        return '<option value="' + item[0] + '"' + (item[0] === (timeframe || "") ? " selected" : "") + '>' + item[1] + '</option>';
      }).join("") + '</select></label>' +
      '<label class="field" data-help="Rango del retesteo, periodo del indicador o longitud de la secuencia (2–10 velas).">Periodo / rango<input name="period" type="number" min="2" max="300" value="' + esc(period || 20) + '" required></label>' +
      '<label class="field" data-help="Umbral de RSI, ATR como porcentaje del cierre, factor de volumen o multiplicador de desviación para Bollinger.">Umbral / factor<input name="value" inputmode="decimal" value="' + esc(value == null ? (((kind || "").indexOf("ATR_PCT_") === 0 || (kind || "").indexOf("_BB_") >= 0) ? "2" : "30") : value) + '"></label>' +
      '<label class="field lab-retest-field" data-help="Máximo 10 velas entre ruptura y primera confirmación del nivel.">Retesteo · velas<input name="retest_bars" type="number" min="1" max="10" value="' + esc(retestBars == null ? 3 : retestBars) + '"></label>' +
      '<label class="field lab-retest-field" data-help="Fracción máxima que puede penetrar el nivel; 0.01 equivale al 1 %. El cierre debe volver al lado de la ruptura.">Tolerancia<input name="tolerance_pct" inputmode="decimal" value="' + esc(tolerance == null ? "0.01" : tolerance) + '"></label>' +
      '<label class="field lab-slow-field" data-help="Periodo EMA lenta; debe superar al rápido. El cruce se confirma al cierre de la vela.">Lenta<input name="slow_period" type="number" min="3" max="300" value="' + esc(slowPeriod == null ? 26 : slowPeriod) + '"></label>' +
      '<label class="field lab-signal-field" data-help="Periodo EMA de la línea de señal MACD; se calcula sólo desde el primer MACD válido.">Señal<input name="signal_period" type="number" min="2" max="300" value="' + esc(signalPeriod == null ? 9 : signalPeriod) + '"></label>' +
      '<label class="field lab-pivot-field" data-help="Velas cerradas a cada lado del giro. La señal espera a que se cierre la última.">Giro · velas<input name="pivot_bars" type="number" min="2" max="10" value="' + esc(pivotBars == null ? 2 : pivotBars) + '"></label>' +
      '<label class="field lab-lookback-field" data-help="Separación máxima entre los dos giros consecutivos, en velas del marco de la regla.">Distancia máx. · velas<input name="lookback_bars" type="number" min="3" max="200" value="' + esc(lookbackBars == null ? 40 : lookbackBars) + '"></label>' +
      '<button type="button" class="ghost lab-remove-rule" aria-label="Quitar condición" data-help="Elimina esta condición del editor; no cambia versiones guardadas.">×</button></div>';
  }
  function activeNodeCount() {
    var count = 1 + root.querySelectorAll("#lab-rule-list .lab-rule, #lab-rule-list .lab-subgroup").length;
    if (root.querySelector('[name="exit_enabled"]').checked) count += 1 + root.querySelectorAll("#lab-exit-rule-list .lab-rule, #lab-exit-rule-list .lab-subgroup").length;
    return count;
  }
  function groupDepth(list) {
    var depth = 0, parent = list.closest(".lab-subgroup");
    while (parent) { depth += 1; parent = parent.parentElement.closest(".lab-subgroup"); }
    return depth;
  }
  function mayAdd(list, amount, group) {
    if (list.children.length >= 8) { notice("Máximo 8 elementos por grupo.", true); return false; }
    if ((root.querySelector('[name="exit_enabled"]').checked || !list.closest("#lab-exit-editor")) && activeNodeCount() + amount > 64) {
      notice("Máximo 64 nodos de reglas por estrategia.", true); return false;
    }
    if (group && groupDepth(list) >= 3) { notice("Máximo 4 niveles de reglas (grupo principal incluido).", true); return false; }
    return true;
  }
  function addRule(kind, period, value, isExit, retestBars, tolerance, targetList, slowPeriod, signalPeriod, pivotBars, timeframe, lookbackBars) {
    var list = targetList || root.querySelector(isExit ? "#lab-exit-rule-list" : "#lab-rule-list");
    if (!mayAdd(list, 1, false)) return;
    list.insertAdjacentHTML("beforeend", ruleMarkup(kind || "CLOSE_ABOVE_EMA", period, value, retestBars, tolerance, slowPeriod, signalPeriod, pivotBars, timeframe, lookbackBars));
    var node = list.lastElementChild;
    function updateRule() {
      var selectedKind = node.querySelector('[name="kind"]').value;
      var previousKind = node.dataset.ruleKind || "";
      var valueInput = node.querySelector('[name="value"]');
      if (selectedKind.indexOf("ATR_PCT_") === 0 && previousKind && previousKind.indexOf("ATR_PCT_") !== 0 && valueInput.value === "30") valueInput.value = "2";
      if (selectedKind.indexOf("_BB_") >= 0 && previousKind && previousKind.indexOf("_BB_") < 0 && valueInput.value === "30") valueInput.value = "2";
      if (selectedKind.indexOf("RSI_") === 0 && (previousKind.indexOf("ATR_PCT_") === 0 || previousKind.indexOf("_BB_") >= 0) && valueInput.value === "2") valueInput.value = "30";
      node.dataset.ruleKind = selectedKind;
      var isRetest = selectedKind.indexOf("RETEST_") === 0;
      var isStreak = selectedKind.indexOf("_STREAK") > 0;
      var isMacd = selectedKind.indexOf("MACD_") === 0;
      var isEmaCross = selectedKind.indexOf("EMA_CROSS_") === 0;
      var isStructure = selectedKind.indexOf("STRUCTURE_") === 0;
      var isDivergence = divergence(selectedKind);
      node.classList.toggle("is-retest", isRetest);
      var needsValue = selectedKind === "RSI_LT" || selectedKind === "RSI_GT" || selectedKind.indexOf("ATR_PCT_") === 0 || selectedKind.indexOf("_BB_") >= 0 || selectedKind === "VOLUME_ABOVE_AVG";
      var field = node.querySelector('[name="value"]').closest("label");
      field.hidden = !needsValue;
      field.firstChild.nodeValue = selectedKind.indexOf("_BB_") >= 0 ? "Mult. σ" : "Umbral / factor";
      field.querySelector("input").required = needsValue;
      node.querySelectorAll(".lab-retest-field").forEach(function (label) {
        label.hidden = !isRetest;
        label.querySelector("input").required = isRetest;
      });
      var slowField = node.querySelector(".lab-slow-field"), signalField = node.querySelector(".lab-signal-field");
      slowField.hidden = !(isMacd || isEmaCross);
      slowField.querySelector("input").required = isMacd || isEmaCross;
      signalField.hidden = !isMacd;
      signalField.querySelector("input").required = isMacd;
      var pivotField = node.querySelector(".lab-pivot-field"), pivotInput = pivotField.querySelector("input");
      pivotField.hidden = !(isStructure || isDivergence);
      pivotInput.required = isStructure || isDivergence;
      var lookbackField = node.querySelector(".lab-lookback-field"), lookbackInput = lookbackField.querySelector("input");
      lookbackField.hidden = !isDivergence;
      lookbackInput.disabled = !isDivergence;
      lookbackInput.required = isDivergence;
      lookbackInput.min = String(Number(pivotInput.value) + 1);
      if (Number(lookbackInput.value) < Number(lookbackInput.min)) lookbackInput.value = lookbackInput.min;
      var periodInput = node.querySelector('[name="period"]');
      if ((isMacd || isEmaCross) && previousKind && previousKind.indexOf("MACD_") !== 0 && previousKind.indexOf("EMA_CROSS_") !== 0 && periodInput.value === "20") periodInput.value = "12";
      if (isDivergence && previousKind && !divergence(previousKind) && periodInput.value === "20") periodInput.value = "14";
      periodInput.closest("label").firstChild.nodeValue = isDivergence ? "RSI · periodo" :
        isStructure ? "Ventana" : isMacd || isEmaCross ? "Rápida" : "Periodo / rango";
      periodInput.min = isStructure ? String(6 * Number(pivotInput.value) + 1) : "2";
      periodInput.max = isRetest ? "100" : isStreak ? "10" : "300";
      if (Number(periodInput.value) < Number(periodInput.min)) periodInput.value = periodInput.min;
      if (Number(periodInput.value) > Number(periodInput.max)) periodInput.value = periodInput.max;
    }
    node.querySelector('[name="kind"]').addEventListener("change", updateRule);
    node.querySelector('[name="pivot_bars"]').addEventListener("change", updateRule);
    updateRule();
    node.querySelector(".lab-remove-rule").addEventListener("click", function () {
      if (list.children.length > 1) this.closest(".lab-rule").remove();
    });
    if (isExit && !root.querySelector('[name="exit_enabled"]').checked) updateExitEditor();
  }
  function addGroup(isExit, targetList, definition) {
    var list = targetList || root.querySelector(isExit ? "#lab-exit-rule-list" : "#lab-rule-list");
    if (!mayAdd(list, definition ? 1 : 2, true)) return;
    var node = document.createElement("section");
    node.className = "lab-subgroup";
    node.innerHTML = '<div class="lab-group-head"><label class="field">Grupo<select name="group_op"><option value="AND">Y · todas</option><option value="OR">O · cualquiera</option></select></label>' +
      '<button type="button" class="ghost lab-remove-group" aria-label="Quitar grupo" data-help="Quita este grupo y sus condiciones; las versiones guardadas no cambian.">×</button></div>' +
      '<div class="lab-group-children"></div><div class="lab-group-actions"><button type="button" class="ghost lab-add-nested-rule">＋ Condición</button>' +
      '<button type="button" class="ghost lab-add-nested-group">＋ Grupo Y/O</button></div>';
    list.appendChild(node);
    node.querySelector('.lab-group-head [name="group_op"]').value = definition && definition.op || "AND";
    var children = node.querySelector(".lab-group-children");
    node.querySelector(".lab-add-nested-rule").addEventListener("click", function () { addRule(null, null, null, isExit, null, null, children); });
    var addNestedGroup = node.querySelector(".lab-add-nested-group");
    addNestedGroup.disabled = groupDepth(children) >= 3;
    if (addNestedGroup.disabled) addNestedGroup.title = "Máximo 4 niveles de reglas";
    addNestedGroup.addEventListener("click", function () { addGroup(isExit, children); });
    node.querySelector(".lab-remove-group").addEventListener("click", function () { if (list.children.length > 1) node.remove(); });
    if (definition) definition.children.forEach(function (child) { renderChild(child, children, isExit); });
    else addRule(null, null, null, isExit, null, null, children);
    if (isExit && !root.querySelector('[name="exit_enabled"]').checked) updateExitEditor();
    return node;
  }
  function readLeaf(node) {
      var kind = node.querySelector('[name="kind"]').value;
      var rule = {kind:kind, period:Number(node.querySelector('[name="period"]').value)};
      var timeframe = node.querySelector('[name="rule_timeframe"]').value;
      if (timeframe) rule.timeframe = timeframe;
      if (kind === "RSI_LT" || kind === "RSI_GT" || kind.indexOf("ATR_PCT_") === 0) rule.threshold = node.querySelector('[name="value"]').value.trim();
      if (kind === "VOLUME_ABOVE_AVG") rule.factor = node.querySelector('[name="value"]').value.trim();
      if (kind.indexOf("_BB_") >= 0) rule.multiplier = node.querySelector('[name="value"]').value.trim();
      if (kind.indexOf("MACD_") === 0) {
        rule.slow_period = Number(node.querySelector('[name="slow_period"]').value);
        rule.signal_period = Number(node.querySelector('[name="signal_period"]').value);
      }
      if (kind.indexOf("EMA_CROSS_") === 0) rule.slow_period = Number(node.querySelector('[name="slow_period"]').value);
      if (kind.indexOf("RETEST_") === 0) {
        rule.retest_bars = Number(node.querySelector('[name="retest_bars"]').value);
        rule.tolerance_pct = node.querySelector('[name="tolerance_pct"]').value.trim();
      }
      if (kind.indexOf("STRUCTURE_") === 0 || divergence(kind)) rule.pivot_bars = Number(node.querySelector('[name="pivot_bars"]').value);
      if (divergence(kind)) rule.lookback_bars = Number(node.querySelector('[name="lookback_bars"]').value);
      return rule;
  }
  function readGroup(list, op) {
    return {op:op, children:Array.from(list.children).map(function (node) {
      return node.classList.contains("lab-subgroup") ?
        readGroup(node.querySelector(".lab-group-children"), node.querySelector('.lab-group-head [name="group_op"]').value) : readLeaf(node);
    })};
  }
  function readRuleList(selector, op) {
    return readGroup(root.querySelector(selector), op);
  }
  function readDefinition(form) {
    var fields = new FormData(form);
    var definition = {direction:fields.get("direction"), entry:readRuleList("#lab-rule-list", fields.get("op")),
      initial_capital:fields.get("initial_capital"), allocation:fields.get("allocation"), fee_rate:fields.get("fee_rate"),
      slippage_rate:fields.get("slippage_rate"), stop_loss:fields.get("stop_loss"), take_profit:fields.get("take_profit")};
    if (fields.get("max_holding_bars")) definition.max_holding_bars = Number(fields.get("max_holding_bars"));
    if (fields.get("trailing_stop")) definition.trailing_stop = fields.get("trailing_stop");
    if (fields.get("exit_enabled")) definition.exit = readRuleList("#lab-exit-rule-list", fields.get("exit_op"));
    return definition;
  }
  function editableRuleGroup(group, depth, tally) {
    if (!group || typeof group !== "object" || depth > 4) return false;
    tally.count += 1;
    if (tally.count > 64) return false;
    if (group.op) return (group.op === "AND" || group.op === "OR") && Array.isArray(group.children) &&
      group.children.length >= 1 && group.children.length <= 8 &&
      group.children.every(function (child) { return editableRuleGroup(child, depth + 1, tally); });
    return editableKinds.indexOf(group.kind) >= 0;
  }
  function renderChild(rule, list, isExit) {
    if (rule.op) addGroup(isExit, list, rule);
    else addRule(rule.kind, rule.period, rule.threshold || rule.factor || rule.multiplier, isExit, rule.retest_bars, rule.tolerance_pct, list, rule.slow_period, rule.signal_period, rule.pivot_bars, rule.timeframe, rule.lookback_bars);
  }
  function fillDefinition(item) {
    var form = root.querySelector("#lab-strategy-form");
    form.elements.name.value = item.name;
    var d = item.definition;
    ["direction","initial_capital","allocation","fee_rate","slippage_rate","stop_loss","take_profit"].forEach(function (key) { form.elements[key].value = d[key]; });
    form.elements.max_holding_bars.value = d.max_holding_bars == null ? "" : d.max_holding_bars;
    form.elements.trailing_stop.value = d.trailing_stop == null ? "" : d.trailing_stop;
    form.elements.op.value = d.entry.op || "AND";
    form.elements.exit_enabled.checked = !!d.exit;
    root.querySelector("#lab-rule-list").replaceChildren();
    root.querySelector("#lab-exit-rule-list").replaceChildren();
    var tally = {count:0};
    var editable = editableRuleGroup(d.entry, 0, tally) && (!d.exit || editableRuleGroup(d.exit, 0, tally));
    if (editable) (d.entry.children || [d.entry]).forEach(function (rule) { renderChild(rule, root.querySelector("#lab-rule-list"), false); });
    else root.querySelector("#lab-rule-list").innerHTML = '<pre class="lab-events">' + esc(JSON.stringify(d.entry, null, 2)) + '</pre>';
    form.elements.exit_op.value = d.exit && d.exit.op || "AND";
    if (editable) (d.exit ? (d.exit.children || [d.exit]) : [{kind:"CLOSE_BELOW_EMA",period:20}]).forEach(function (rule) {
      renderChild(rule, root.querySelector("#lab-exit-rule-list"), true);
    });
    else if (d.exit) root.querySelector("#lab-exit-rule-list").innerHTML = '<pre class="lab-events">' + esc(JSON.stringify(d.exit, null, 2)) + '</pre>';
    updateExitEditor();
    root.querySelector("#lab-save-strategy").textContent = "Guardar nueva versión";
    root.querySelector("#lab-save-strategy").disabled = !editable;
    root.querySelector("#lab-add-rule").disabled = !editable;
    root.querySelector("#lab-add-group").disabled = !editable;
    if (!editable) ["#lab-add-exit-rule","#lab-add-exit-group"].forEach(function (selector) { root.querySelector(selector).disabled = true; });
    if (!editable) notice("Esta versión excede los límites del editor o usa una condición desconocida: puede ejecutarse, pero no modificarse aquí.", true);
  }
  function reload(preferStrategyId, preferResearchId, isCurrent) {
    return Promise.all([api("/datasets"), api("/strategies"), api("/runs"), api("/research/runs"), api("/validations"), api("/optimizations")]).then(function (values) {
      if (!root || !root.isConnected || (isCurrent && !isCurrent())) return;
      datasets = values[0]; strategies = values[1]; runs = values[2]; researchRuns = values[3]; validations = values[4]; optimizations = values[5];
      renderResearchCohort();
      var ds = root.querySelector("#lab-dataset"), st = root.querySelector("#lab-strategy"), rs = root.querySelector("#lab-run-select"),
        rr = root.querySelector("#lab-research-select"), vs = root.querySelector("#lab-validation-select"),
        os = root.querySelector("#lab-opt-select");
      var selectedD = ds.value, selectedS = st.value, selectedR = rs.value, selectedResearch = rr.value,
        selectedValidation = vs.value, selectedOptimization = os.value;
      ds.innerHTML = optionList(datasets, "Selecciona un histórico");
      st.innerHTML = optionList(strategies, "Nueva estrategia");
      rs.innerHTML = '<option value="">Selecciona una prueba</option>' + runs.map(function (run) {
        return '<option value="' + esc(run.id) + '">' + esc(run.created_at.slice(0,19)) + ' · pendiente de verificar</option>';
      }).join("");
      rr.innerHTML = '<option value="">Selecciona una consulta</option>' + researchRuns.map(function (run) {
        return '<option value="' + esc(run.id) + '">' + esc(run.created_at.slice(0,19)) + ' · ' + esc(run.summary.cases) + ' casos</option>';
      }).join("");
      vs.innerHTML = '<option value="">Selecciona una validación</option>' + validations.map(function (run) {
        return '<option value="' + esc(run.id) + '">' + esc(run.created_at.slice(0,19)) + ' · ' +
          (run.method === "ROLLING_FIXED_VERSION" ? esc(run.folds) + ' ventanas' : esc(run.train_bars) + ' / ' + esc(run.test_bars) + ' velas') + '</option>';
      }).join("");
      os.innerHTML = '<option value="">Selecciona una exploración</option>' + optimizations.map(function (run) {
        return '<option value="' + esc(run.id) + '">' + esc(run.created_at.slice(0,19)) + ' · ' +
          esc(run.candidate_count) + ' combinaciones' +
          (run.method === "WALK_FORWARD_GRID_STOP_TARGET" ? ' · ' + esc(run.folds) + ' ventanas' : '') + '</option>';
      }).join("");
      ds.value = datasets.some(function (row) { return row.id === selectedD; }) ? selectedD : (datasets[0] && datasets[0].id || "");
      var wanted = preferStrategyId || selectedS;
      st.value = strategies.some(function (row) { return row.id === wanted; }) ? wanted : "";
      rs.value = runs.some(function (row) { return row.id === selectedR; }) ? selectedR : "";
      rr.value = researchRuns.some(function (row) { return row.id === preferResearchId; }) ? preferResearchId :
        researchRuns.some(function (row) { return row.id === selectedResearch; }) ? selectedResearch :
        (researchRuns[0] && researchRuns[0].id || "");
      vs.value = validations.some(function (row) { return row.id === selectedValidation; }) ? selectedValidation : "";
      os.value = optimizations.some(function (row) { return row.id === selectedOptimization; }) ? selectedOptimization : "";
      var compareLeft = root.querySelector("#lab-compare-left"), compareRight = root.querySelector("#lab-compare-right");
      var previousLeft = compareLeft.value, previousRight = compareRight.value;
      var compareOptions = runs.map(function (row) {
        var dataset = datasets.find(function (item) { return item.id === row.dataset_id; });
        return '<option value="' + esc(row.id) + '">' + esc(row.created_at.slice(0,16)) + ' · ' +
          esc(dataset ? dataset.symbol + " " + dataset.timeframe : row.dataset_id.slice(0,8)) +
          ' · ' + esc(row.version_id.slice(0,8)) + '</option>';
      }).join("");
      compareLeft.innerHTML = compareOptions; compareRight.innerHTML = compareOptions;
      compareLeft.value = runs.some(function (row) { return row.id === previousLeft; }) ? previousLeft : (runs[1] && runs[1].id || runs[0] && runs[0].id || "");
      compareRight.value = runs.some(function (row) { return row.id === previousRight; }) ? previousRight : (runs[0] && runs[0].id || "");
      root.querySelector("#lab-compare-action").disabled = runs.length < 2;
      root.querySelector("#lab-comparison").textContent = runs.length < 2 ? "Guarda dos pruebas para compararlas." :
        "Pulsa Comparar para verificar estas pruebas.";
      selectDataset();
      if (rr.value && (!activeResearch || activeResearch.id !== rr.value)) selectResearch();
      if (st.value) return selectStrategy();
    }).catch(function (error) { if (!isCurrent || isCurrent()) notice(error.message, true); });
  }
  function selectDataset() {
    activeDataset = datasets.find(function (row) { return row.id === root.querySelector("#lab-dataset").value; }) || null;
    var el = root.querySelector("#lab-dataset-status");
    el.textContent = activeDataset ? activeDataset.source + " · SHA-256 " + activeDataset.hash.slice(0, 16) + "… · " +
      (activeDataset.gaps.length ? activeDataset.gaps.length + " hueco(s): prueba bloqueada" : "sin huecos detectados") : "Sin histórico guardado.";
    if (activeDataset && (activeDataset.source === "grvt-perp-trade-public" || activeDataset.source === "binance-spot-public" || activeDataset.source === "mexc-spot-public")) {
      var form = root.querySelector("#lab-download");
      form.querySelector('[name="provider"]').value = activeDataset.source === "grvt-perp-trade-public" ? "grvt" : activeDataset.source === "mexc-spot-public" ? "mexc" : "binance";
      form.querySelector('[name="symbol"]').value = activeDataset.symbol;
      form.querySelector('[name="timeframe"]').value = activeDataset.timeframe;
      updateDownloadMarket();
    }
  }
  function updateResearchSource() {
    var editor = root.querySelector('#lab-research-form [name="source"]').value === "editor";
    var period = root.querySelector("#lab-research-period");
    period.hidden = editor;
    period.querySelector("input").disabled = editor;
  }
  function executeResearch(event) {
    event.preventDefault();
    if (!activeDataset) { notice("Selecciona un histórico antes de investigar.", true); return; }
    var form = event.currentTarget, button = form.querySelector('button[type="submit"]');
    var count = Number(form.elements.period.value);
    var rule = form.elements.source.value === "editor" ?
      readRuleList("#lab-rule-list", root.querySelector('#lab-strategy-form [name="op"]').value) :
      {op:"AND", children:[{kind:"BEARISH_STREAK",period:count},{kind:"VOLUME_ASCENDING_BARS",period:count}]};
    button.disabled = true;
    api("/research/runs", {method:"POST", body:JSON.stringify({dataset_id:activeDataset.id, rule:rule})})
      .then(function (run) {
        notice("Investigación guardada: " + run.summary.cases + " casos; ninguna orden creada.", false);
        activeResearch = run;
        return reload(null, run.id).then(function () { if (root) showResearch(run); });
      }).catch(function (error) { notice(error.message, true); })
      .finally(function () { if (root) button.disabled = false; });
  }
  function selectResearch() {
    var id = root.querySelector("#lab-research-select").value;
    if (!id) {
      activeResearch = null;
      researchCandles = [];
      clearResearchChart();
      root.querySelector("#lab-research-report").textContent = "Elige un histórico y busca una estructura.";
      return;
    }
    api("/research/runs/" + encodeURIComponent(id)).then(function (run) {
      if (root && root.querySelector("#lab-research-select").value === id) showResearch(run);
    }).catch(function (error) { notice(error.message, true); });
  }
  function utc(t) { return new Date(Number(t) * 1000).toISOString().slice(0,16).replace("T", " "); }
  function historyControls(container, currentChart, candles, storageKey) {
    if (!container || !currentChart || candles.length < 2 || !window.OrbisChartRange) return;
    container.hidden = false;
    window.OrbisChartRange.mount(container, currentChart, candles, {
      selected:"1", storageKey:storageKey
    });
  }
  function showResearch(run) {
    if (!root) return;
    var existingReport = root.querySelector("#lab-research-report");
    if (activeResearch && activeResearch.id === run.id &&
        existingReport && existingReport.dataset.runId === run.id) return;
    activeResearch = run;
    researchPage = 0;
    researchCandles = [];
    clearResearchChart();
    root.querySelector("#lab-research-select").value = run.id;
    var summary = run.summary, horizons = [["3600","1 h"],["14400","4 h"],["86400","1 d"],["604800","7 d"]];
    var smallNonOverlap = horizons.some(function (h) {
      var d = summary.horizons[h[0]];
      return d.non_overlapping && d.count >= 30 && d.non_overlapping.count < 30;
    });
    var rows = horizons.map(function (h) {
      var d = summary.horizons[h[0]];
      return '<tr><th scope="row">' + h[1] + '</th><td class="num">' + esc(d.count) + '</td>' +
        '<td class="num" title="' + (d.non_overlapping ? 'Primer caso elegible; siguiente cierre al menos ' + h[1] + ' después. No garantiza independencia.' : 'No calculado por esta versión del motor') + '">' +
        (d.non_overlapping ? esc(d.non_overlapping.count) : '—') + '</td><td class="num" title="' + esc(d.mean_pct) + '">' + displayPercent(d.mean_pct, 2) + '</td>' +
        '<td class="num" title="' + esc(d.median_pct) + '">' + displayPercent(d.median_pct, 2) + '</td><td class="num" title="' + esc(d.p25_pct) + ' / ' + esc(d.p75_pct) + '">' + displayPercent(d.p25_pct, 2) + ' / ' + displayPercent(d.p75_pct, 2) + '</td>' +
        '<td class="num">' + (d.positive_rate == null ? "—" : displayDecimal(String(Number(d.positive_rate) * 100), 1) + '%') + '</td></tr>';
    }).join("");
    var report = root.querySelector("#lab-research-report");
    report.className = "";
    report.innerHTML = '<div class="lab-research-head"><div class="lab-metrics"><div><small>Casos</small><strong>' + esc(summary.cases) + '</strong></div>' +
      '<div><small>Histórico</small><strong>' + esc(run.dataset_id.slice(0,8)) + '</strong></div>' +
      '<div><small>Motor</small><strong>' + esc(run.engine_version) + '</strong></div></div>' +
      '<div class="lab-research-actions"><select id="lab-research-export-format" class="lab-export-format" aria-label="Formato de exportación de casos" data-help="Evidencia conserva todo el histórico. 70/30 añade particiones cronológicas y purga etiquetas que cruzan la reserva; no entrena una IA ni crea una validación independiente."><option value="v2">Evidencia</option><option value="v3">70/30 · IA</option></select>' +
      '<button type="button" id="lab-research-export" class="ghost" data-help="Descarga los casos LAB verificados en este equipo. No envía datos a una IA.">Exportar casos</button>' +
      '<button type="button" id="lab-research-to-strategy" class="ghost" data-help="Copia estas condiciones a un borrador. Revisa dirección, capital, stops y costes antes de guardar o probar.">Usar como estrategia</button>' +
      '<button type="button" id="lab-ai-preview-action" class="ghost" data-help="Analiza con OpenAI sólo estadísticas LAB verificadas. Primero muestra exactamente la evidencia que saldrá del equipo.">Analizar con IA</button></div></div>' +
      experimentVerifyMarkup() +
      '<p class="muted lab-research-context">' + esc(run.created_at.slice(0,19)) + ' UTC · SHA-256 ' + esc(run.dataset_hash.slice(0,16)) + '… · Precio bruto, sin órdenes ni costes.</p>' +
      (summary.coverage && summary.coverage.missing_bars ? '<p class="amber" role="status">Histórico discontinuo: ' + esc(summary.coverage.missing_bars) + (summary.coverage.missing_bars === 1 ? ' vela ausente entre ' : ' velas ausentes entre ') + esc(summary.coverage.segments) + ' tramos. Las reglas y los resultados futuros no cruzan huecos.</p>' : '') +
      (summary.cases < 30 ? '<p class="amber" role="status">Muestra pequeña; los casos pueden solaparse.</p>' : '<p class="muted">Casos solapados: el recuento bruto no equivale a observaciones independientes.</p>') +
      (smallNonOverlap ? '<p class="amber" role="status">Algún horizonte queda con menos de 30 casos sin solape.</p>' : '') +
      '<div class="tablewrap"><table class="lab-research-distribution"><thead><tr><th>Después</th><th>Casos completos</th><th data-help="Subconjunto cronológico sin ventanas futuras solapadas. Se elige el primer caso elegible y después el siguiente al menos un horizonte más tarde. No implica independencia estadística.">Sin solape</th><th data-help="Media de todos los casos completos, incluidos los solapados.">Media</th><th data-help="Mediana de todos los casos completos, incluidos los solapados.">Mediana</th><th>P25 / P75</th><th data-help="Porcentaje positivo de todos los casos completos, incluidos los solapados.">Positivos</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<details id="lab-regime-panel"><summary data-help="Clasifica cada señal por la volatilidad ya conocida al cierre; las etiquetas posteriores sólo se usan para resumir resultados.">Volatilidad previa</summary>' +
      '<form id="lab-regime-form" class="row"><label class="field" data-help="Retornos cierre a cierre anteriores a cada señal; los huecos reinician la ventana.">Ventana<input name="lookback" type="number" min="2" max="300" value="20" required></label>' +
      '<label class="field" data-help="Desviación típica porcentual por vela. El umbral no se optimiza con los resultados futuros.">Umbral %<input name="threshold" type="text" inputmode="decimal" value="2" required></label>' +
      '<label class="field">Después<select name="horizon"><option value="3600">1 h</option><option value="14400">4 h</option><option value="86400" selected>1 d</option><option value="604800">7 d</option></select></label>' +
      '<label class="field" data-help="La reserva empieza en una vela cronológica fija. Las etiquetas de desarrollo que llegan hasta ella quedan excluidas.">Desarrollo / reserva<select name="train_fraction"><option value="0.6">60 / 40</option><option value="0.7" selected>70 / 30</option><option value="0.8">80 / 20</option></select></label>' +
      '<button type="submit" class="ghost">Analizar</button></form><div id="lab-regime-result" class="state muted" role="status">Elige ventana, umbral y reserva.</div></details>' +
      '<p class="muted lab-chart-legend" data-help="Cada círculo marca el cierre de una estructura detectada. No indica entrada ni compra.">● Estructura detectada al cierre · Sin operaciones</p>' +
      '<div id="lab-research-chart" role="img" aria-label="Velas con estructuras detectadas al cierre"></div>' +
      '<div id="lab-research-history" class="lab-history-controls" hidden></div>' +
      '<p id="lab-research-chart-detail" class="muted" role="status">Mueve el cursor sobre un marcador para ver sus condiciones.</p>' +
      '<div class="lab-research-events-head"><h4>Casos individuales</h4><div><button type="button" id="lab-research-prev" class="ghost">←</button>' +
      '<span id="lab-research-page"></span><button type="button" id="lab-research-next" class="ghost">→</button></div></div>' +
      '<div class="tablewrap"><table class="lab-research-events"><thead><tr><th>#</th><th>Cierre UTC</th><th>Precio</th><th>1 h</th><th>4 h</th><th>1 d</th><th>7 d</th><th>Máx ↑</th><th>Máx ↓</th></tr></thead><tbody id="lab-research-event-rows"></tbody></table></div>' +
      '<details><summary>Regla y límites de la medición</summary><pre class="lab-events">' + esc(JSON.stringify(run.rule, null, 2)) + '</pre><p class="muted">' + esc(run.assumptions.join(" · ")) + '</p></details>' +
      '<div id="lab-ai-panel" class="lab-ai-panel" hidden></div>';
    attachExperimentVerifier(report, run.id, "/research/runs/", function () {
      clearResearchChart();
      researchCandles = [];
      report.querySelector("#lab-research-chart").textContent = "Gráfico bloqueado: la investigación guardada no coincide con la repetición.";
      var history = report.querySelector("#lab-research-history");
      if (history) { history.hidden = true; history.replaceChildren(); }
      report.querySelector("#lab-research-to-strategy").disabled = true;
      report.querySelector("#lab-research-export").disabled = true;
      report.querySelector("#lab-ai-preview-action").disabled = true;
      report.querySelector('#lab-regime-form button[type="submit"]').disabled = true;
    });
    report.querySelector("#lab-research-export").addEventListener("click", function () { exportResearch(run.id); });
    report.querySelector("#lab-research-to-strategy").addEventListener("click", researchToStrategy);
    report.querySelector("#lab-ai-preview-action").addEventListener("click", function () { showAiPreview(run.id, "research"); });
    report.querySelector("#lab-regime-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = event.currentTarget, output = report.querySelector("#lab-regime-result");
      if (!form.reportValidity() || report.dataset.integrity === "mismatch") return;
      var lookback = Number(form.elements.lookback.value);
      var threshold = form.elements.threshold.value.trim().replace(",", ".");
      if (!Number.isInteger(lookback) || lookback < 2 || lookback > 300 ||
          !Number.isFinite(Number(threshold)) || Number(threshold) <= 0 || Number(threshold) > 1000) {
        output.className = "state error"; output.textContent = "Ventana o umbral inválido."; return;
      }
      var button = form.querySelector('button[type="submit"]'), horizon = form.elements.horizon.value;
      var trainFraction = form.elements.train_fraction.value;
      button.disabled = true;
      output.className = "state loading";
      output.textContent = "Verificando y segmentando casos…";
      api("/research/runs/" + encodeURIComponent(run.id) + "/volatility-regimes?lookback=" +
        encodeURIComponent(lookback) + "&threshold_pct=" + encodeURIComponent(threshold) +
        "&train_fraction=" + encodeURIComponent(trainFraction), {cache:"no-store"})
        .then(function (analysis) {
          if (!root || activeResearch !== run || !output.isConnected || report.dataset.integrity === "mismatch") return;
          var labels = [["BELOW", "Baja"], ["AT_OR_ABOVE", "Alta"], ["INSUFFICIENT_HISTORY", "Sin historia"]];
          var reserve = analysis.chronological_reserve;
          var cutoff = new Date(reserve.cutoff_t * 1000).toISOString().slice(0, 16).replace("T", " ");
          output.className = "state";
          output.innerHTML = '<p class="muted">Volatilidad por vela al cierre · umbral ' + esc(analysis.threshold_pct_per_bar) +
            '% · ' + esc(analysis.lookback_returns) + ' retornos · reserva desde ' + esc(cutoff) + ' UTC.</p>' +
            '<div class="tablewrap"><table><thead><tr><th>Régimen</th><th data-help="Todos los casos con futuro completo, incluidos los solapados y los excluidos por la frontera cronológica.">Total</th>' +
            '<th data-help="Casos de desarrollo con resultado posterior terminado antes de la reserva. Recuento y media porcentual.">Desarrollo · n / media</th>' +
            '<th data-help="Casos cuya señal ocurre desde el inicio de reserva. Recuento y media porcentual. Reserva exploratoria, no sellada.">Reserva · n / media</th>' +
            '<th data-help="Subconjunto cronológico global entre regímenes para el horizonte elegido. No garantiza independencia.">Sin solape · n</th></tr></thead><tbody>' +
            labels.map(function (item) {
              var group = analysis.regimes[item[0]], stats = group.horizons[horizon];
              return '<tr><th scope="row">' + item[1] + (group.small_sample ? ' · muestra pequeña' : '') +
                '</th><td class="num">' + esc(stats.count) +
                '</td><td class="num">' + esc(stats.development.count) + ' / ' + displayPercent(stats.development.mean_pct, 2) +
                '</td><td class="num">' + esc(stats.out_of_sample.count) + ' / ' + displayPercent(stats.out_of_sample.mean_pct, 2) +
                '</td><td class="num">' + esc(stats.non_overlapping.count) + '</td></tr>';
            }).join("") + '</tbody></table></div>' +
            '<p class="muted" data-help="Una etiqueta de desarrollo se excluye si toca o cruza el inicio de reserva. Cambiar parámetros después de ver la reserva invalida una pretensión fuera de muestra.">Frontera: ' +
            esc(reserve.embargoed_cases_by_horizon[horizon]) + ' casos excluidos · Reserva exploratoria, no validación sellada.</p>';
        }).catch(function (error) {
          if (!output.isConnected || activeResearch !== run) return;
          output.className = "state error"; output.textContent = error.message;
        }).finally(function () { if (button.isConnected) button.disabled = false; });
    });
    report.querySelector("#lab-research-prev").addEventListener("click", function () { researchPage -= 1; renderResearchPage(); });
    report.querySelector("#lab-research-next").addEventListener("click", function () { researchPage += 1; renderResearchPage(); });
    renderResearchPage();
    api("/datasets/" + encodeURIComponent(run.dataset_id)).then(function (dataset) {
      if (!root || !activeResearch || activeResearch.id !== run.id || report.dataset.integrity === "mismatch") return;
      if (dataset.hash !== run.dataset_hash) { notice("El histórico ya no coincide con la investigación guardada.", true); return; }
      researchCandles = dataset.candles;
      drawResearch(dataset.candles, run.events);
    }).catch(function (error) { notice("No se pudo dibujar la investigación: " + error.message, true); });
  }
  function renderResearchPage() {
    if (!root || !activeResearch) return;
    var events = activeResearch.events, pageSize = 50, pages = Math.max(1, Math.ceil(events.length / pageSize));
    researchPage = Math.min(Math.max(researchPage, 0), pages - 1);
    var start = researchPage * pageSize;
    root.querySelector("#lab-research-page").textContent = events.length ? (start + 1) + "–" + Math.min(start + pageSize, events.length) + " / " + events.length : "0 casos";
    root.querySelector("#lab-research-prev").disabled = researchPage === 0;
    root.querySelector("#lab-research-next").disabled = researchPage === pages - 1;
    root.querySelector("#lab-research-event-rows").innerHTML = events.slice(start, start + pageSize).map(function (item, offset) {
      function outcome(key) { var exact = item.outcomes_pct[key]; return '<td class="num" title="' + esc(exact) + '">' + displayPercent(exact, 2) + '</td>'; }
      return '<tr data-signal-time="' + esc(item.signal_time) + '" tabindex="0" title="Centrar este caso en el gráfico"><td>' + (start + offset + 1) + '</td><td>' + utc(item.signal_time) + '</td>' +
        '<td class="num" title="' + esc(item.close) + '">' + displayDecimal(item.close, 4) + '</td>' +
        outcome("3600") + outcome("14400") + outcome("86400") + outcome("604800") +
        '<td class="num" title="' + esc(item.max_up_pct) + '">' + displayPercent(item.max_up_pct, 2) + '</td>' +
        '<td class="num" title="' + esc(item.max_down_pct) + '">' + displayPercent(item.max_down_pct, 2) + '</td></tr>';
    }).join("");
    root.querySelectorAll("#lab-research-event-rows tr").forEach(function (row) {
      function focus() {
        if (!researchChart || !researchCandles.length) return;
        var time = Number(row.dataset.signalTime), index = researchCandles.findIndex(function (bar) { return bar.t === time; });
        if (index < 0) return;
        var event = activeResearch.events.find(function (item) { return item.signal_time === time; });
        if (!event) return;
        var pivots = matchedDivergencePivots(event.explanation), firstIndex = index;
        pivots.forEach(function (pivot) {
          var at = researchCandles.findIndex(function (bar) { return bar.t === pivot.time; });
          if (at >= 0) firstIndex = Math.min(firstIndex, at);
        });
        researchChart.timeScale().setVisibleRange({from:researchCandles[Math.max(0,firstIndex-12)].t,
          to:researchCandles[Math.min(researchCandles.length-1,index+12)].t});
        if (focusResearchPivots) focusResearchPivots(pivots);
        root.querySelector("#lab-research-chart-detail").textContent = utc(time) + " UTC · " + ruleSummary(event.explanation);
      }
      row.addEventListener("click", focus);
      row.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); focus(); } });
    });
  }
  function matchedDivergencePivots(evidence) {
    if (!evidence || !evidence.matched) return [];
    if (evidence.children) return evidence.children.flatMap(matchedDivergencePivots);
    if (!divergence(evidence.kind) || !evidence.pivots) return [];
    return evidence.pivots.map(function (pivot, index) {
      return {time:pivot.t, text:"G" + (index + 1),
        position:evidence.kind === "RSI_BULLISH_DIVERGENCE" ? "belowBar" : "aboveBar"};
    });
  }
  function drawResearch(candles, events) {
    clearResearchChart();
    var target = root.querySelector("#lab-research-chart"), lw = window.LightweightCharts;
    if (!target || !lw) { notice("La biblioteca de gráficos no está disponible.", true); return; }
    researchChart = lw.createChart(target, {autoSize:true, height:460,
      layout:{background:{type:"solid",color:"#101b18"},textColor:"#c9d6ce"},
      grid:{vertLines:{color:"#24362f"},horzLines:{color:"#24362f"}},timeScale:{timeVisible:true,minBarSpacing:0.1}});
    var series = researchChart.addSeries(lw.CandlestickSeries,
      {upColor:"#42c88a",downColor:"#ff7b78",borderVisible:false,wickUpColor:"#42c88a",wickDownColor:"#ff7b78"});
    series.setData(candles.map(function (bar) { return {time:bar.t,open:Number(bar.o),high:Number(bar.h),low:Number(bar.l),close:Number(bar.c)}; }));
    var signals = events.map(function (event) {
      return {time:event.signal_time,position:"aboveBar",color:"#efbd63",shape:"circle",text:events.length <= 40 ? "Estructura" : ""};
    }).sort(function (a,b) { return a.time - b.time; });
    var markers = lw.createSeriesMarkers(series, signals);
    focusResearchPivots = function (pivots) {
      var byPivotTime = new Map(pivots.map(function (pivot) {
        return [pivot.time, Object.assign({color:"#88b8ff",shape:"square"}, pivot)];
      }));
      markers.setMarkers(signals.concat(Array.from(byPivotTime.values())).sort(function (a,b) { return a.time - b.time; }));
    };
    target.dataset.signalCount = String(events.length);
    var byTime = new Map(events.map(function (event) { return [event.signal_time, event]; }));
    researchChart.subscribeCrosshairMove(function (param) {
      var detail = root && root.querySelector("#lab-research-chart-detail");
      if (!detail) return;
      var event = byTime.get(param.time);
      detail.textContent = event ? utc(event.signal_time) + " UTC · " + ruleSummary(event.explanation) :
        "Mueve el cursor sobre un marcador para ver sus condiciones.";
    });
    historyControls(root.querySelector("#lab-research-history"), researchChart, candles,
      "orbis.chartRange.labResearch");
  }
  function researchToStrategy() {
    if (!activeResearch || !editableRuleGroup(activeResearch.rule, 0, {count:0})) {
      notice("Esta regla no cabe en el editor de estrategias.", true); return;
    }
    root.querySelector("#lab-strategy").value = "";
    selectStrategy();
    var form = root.querySelector("#lab-strategy-form"), rule = activeResearch.rule;
    form.elements.name.value = "Hipótesis " + activeResearch.created_at.slice(0,10);
    form.elements.op.value = rule.op || "AND";
    root.querySelector("#lab-rule-list").replaceChildren();
    (rule.children || [rule]).forEach(function (child) { renderChild(child, root.querySelector("#lab-rule-list"), false); });
    notice("Reglas copiadas al borrador. Revisa dirección, capital, costes y salidas antes de guardar.", false);
    form.scrollIntoView({block:"start",behavior:"smooth"});
  }
  function selectStrategy() {
    var epoch = ++strategySelectionEpoch;
    versionSelectionEpoch += 1;
    var item = strategies.find(function (row) { return row.id === root.querySelector("#lab-strategy").value; });
    var selector = root.querySelector("#lab-version"), runButton = root.querySelector("#lab-run");
    selector.dataset.strategyId = "";
    selector.dataset.ready = "false";
    selector.disabled = true;
    runButton.disabled = true;
    root.querySelector("#lab-export-strategy").disabled = true;
    if (item) {
      selector.innerHTML = '<option value="">Cargando versiones…</option>';
      fillDefinition(item);
      return api("/strategies/" + encodeURIComponent(item.id) + "/versions").then(function (versions) {
        if (!selector.isConnected || epoch !== strategySelectionEpoch ||
            root.querySelector("#lab-strategy").value !== item.id) return;
        selector.innerHTML = versions.map(function (version) { return '<option value="' + esc(version.id) + '">v' + version.version + ' · ' + esc(version.created_at.slice(0,19)) + '</option>'; }).join("");
        selector.value = item.version_id;
        selector.dataset.strategyId = item.id;
        selector.dataset.ready = selector.value ? "true" : "false";
        selector.disabled = !selector.value;
        runButton.disabled = !selector.value || runButton.dataset.running === "true";
        root.querySelector("#lab-export-strategy").disabled = !selector.value;
      }).catch(function (error) {
        if (selector.isConnected && epoch === strategySelectionEpoch) {
          selector.innerHTML = '<option value="">Versiones no disponibles</option>';
          notice(error.message, true);
        }
      });
    }
    selector.innerHTML = '<option value="">Guarda una estrategia</option>';
    root.querySelector("#lab-strategy-form").reset(); root.querySelector("#lab-save-strategy").textContent = "Guardar estrategia";
    root.querySelector("#lab-save-strategy").disabled = false;
    root.querySelector("#lab-add-rule").disabled = false;
    root.querySelector("#lab-add-group").disabled = false;
    root.querySelector("#lab-rule-list").replaceChildren(); addRule();
    root.querySelector("#lab-exit-rule-list").replaceChildren(); addRule("CLOSE_BELOW_EMA", 20, null, true);
    updateExitEditor();
  }
  function selectVersion() {
    var strategy = strategies.find(function (row) { return row.id === root.querySelector("#lab-strategy").value; });
    if (!strategy) return;
    var selector = root.querySelector("#lab-version"), versionId = selector.value;
    var epoch = ++versionSelectionEpoch;
    selector.dataset.ready = "false";
    root.querySelector("#lab-run").disabled = true;
    root.querySelector("#lab-export-strategy").disabled = true;
    return api("/strategies/" + encodeURIComponent(strategy.id) + "/versions").then(function (versions) {
      if (!selector.isConnected || epoch !== versionSelectionEpoch ||
          root.querySelector("#lab-strategy").value !== strategy.id || selector.value !== versionId) return;
      var selected = versions.find(function (item) { return item.id === versionId; });
      if (!selected) { notice("Esta versión ya no está disponible.", true); return; }
      fillDefinition({name:strategy.name, definition:selected.definition});
      selector.dataset.ready = "true";
      root.querySelector("#lab-run").disabled = root.querySelector("#lab-run").dataset.running === "true";
      root.querySelector("#lab-export-strategy").disabled = false;
    }).catch(function (error) {
      if (selector.isConnected && epoch === versionSelectionEpoch) notice(error.message, true);
    });
  }
  function download(event) {
    event.preventDefault();
    var form = event.currentTarget, f = new FormData(form), button = form.querySelector("button");
    var start = Date.parse(String(f.get("start")) + "Z") / 1000, end = Date.parse(String(f.get("end")) + "Z") / 1000;
    button.disabled = true;
    api("/datasets/download", {method:"POST", body:JSON.stringify({symbol:f.get("symbol"), timeframe:f.get("timeframe"), provider:f.get("provider"), start:start, end:end})})
      .then(function (data) { notice("Histórico conservado: " + data.count + " velas.", false); reload(); })
      .catch(function (error) { notice(error.message, true); }).finally(function () { button.disabled = false; });
  }
  function importBtcArchive() {
    var button = root.querySelector("#lab-btc-archive");
    button.disabled = true;
    notice("Descargando y validando el archivo BTC/USD…", false);
    api("/datasets/btc-archive", {method:"POST"}).then(function (data) {
      return reload().then(function () {
        if (!root || !root.isConnected) return;
        if (!datasets.some(function (item) { return item.id === data.id; })) throw new Error("El histórico se guardó, pero no se pudo recuperar de LAB. Recarga la vista.");
        root.querySelector("#lab-dataset").value = data.id;
        selectDataset();
        notice("Histórico BTC/USD conservado: " + data.count + " velas.", false);
      });
    }).catch(function (error) { notice(error.message, true); }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function updateDownloadMarket() {
    var form = root.querySelector("#lab-download"), provider = form.querySelector('[name="provider"]').value;
    var symbol = form.querySelector('[name="symbol"]');
    var market = symbol.value.trim().toUpperCase();
    if (provider === "grvt" && /^[A-Z0-9]{2,12}\/USDT$/.test(market)) symbol.value = market + ":PERP";
    if (provider !== "grvt" && /^[A-Z0-9]{2,12}\/USDT:PERP$/.test(market)) symbol.value = market.slice(0, -5);
    root.querySelector("#lab-download-market-note").textContent = provider === "grvt" ?
      "PERP: velas TRADE de GRVT. Backtest teórico sin financiación ni liquidación." :
      (provider === "mexc" ? "MEXC Spot: velas públicas; diario 1d compatible con DCA PAPER." :
      "Spot: mercado al contado. No se mezclará con PERP.");
  }
  function parseCsv(text) {
    var lines = text.trim().split(/\r?\n/);
    var headers = lines.shift().toLowerCase().split(",").map(function (x) { return x.trim(); });
    var names = ["time","open","high","low","close","volume"];
    if (names.some(function (name) { return headers.indexOf(name) < 0; })) throw new Error("CSV: faltan columnas time,open,high,low,close,volume");
    if (lines.length > 10000) throw new Error("CSV: máximo 10000 velas");
    return lines.filter(Boolean).map(function (line) {
      var cells = line.split(","), raw = cells[headers.indexOf("time")].trim();
      var t = /^\d+$/.test(raw) ? Number(raw) : Date.parse(raw) / 1000;
      if (t > 1000000000000) t = Math.floor(t / 1000);
      return {t:t, o:cells[headers.indexOf("open")].trim(), h:cells[headers.indexOf("high")].trim(),
        l:cells[headers.indexOf("low")].trim(), c:cells[headers.indexOf("close")].trim(), v:cells[headers.indexOf("volume")].trim()};
    });
  }
  function importCsv(event) {
    event.preventDefault();
    var form = event.currentTarget, f = new FormData(form), file = f.get("file"), button = form.querySelector("button");
    if (!file || !file.name) { notice("Selecciona un CSV.", true); return; }
    button.disabled = true;
    file.text().then(function (text) {
      return api("/datasets", {method:"POST", body:JSON.stringify({symbol:f.get("symbol"), timeframe:f.get("timeframe"), source:"csv:" + file.name.slice(0,80), candles:parseCsv(text)})});
    }).then(function (data) { notice("CSV conservado: " + data.count + " velas.", false); reload(); })
      .catch(function (error) { notice(error.message, true); }).finally(function () { button.disabled = false; });
  }
  function saveStrategy(event) {
    event.preventDefault();
    var form = event.currentTarget, selected = strategies.find(function (row) { return row.id === root.querySelector("#lab-strategy").value; });
    var path = selected ? "/strategies/" + encodeURIComponent(selected.id) + "/versions" : "/strategies";
    var body = selected ? {definition:readDefinition(form)} : {name:form.elements.name.value.trim(), definition:readDefinition(form)};
    var button = root.querySelector("#lab-save-strategy"); button.disabled = true;
    api(path, {method:"POST", body:JSON.stringify(body)}).then(function (saved) {
      notice("Estrategia guardada en versión " + saved.version + ".", false);
      return reload(saved.id);
    }).catch(function (error) { notice(error.message, true); }).finally(function () { button.disabled = false; });
  }
  function exportSelectedStrategy() {
    var strategy = strategies.find(function (row) { return row.id === root.querySelector("#lab-strategy").value; });
    var versionId = root.querySelector("#lab-version").value;
    if (!strategy || !versionId) { notice("Selecciona una versión guardada.", true); return; }
    var button = root.querySelector("#lab-export-strategy"); button.disabled = true;
    api("/strategies/" + encodeURIComponent(strategy.id) + "/versions/" +
      encodeURIComponent(versionId) + "/export", {cache:"no-store"}).then(function (bundle) {
      downloadEvidence(bundle, "orbis-estrategia-" + versionId.replace(/[^A-Za-z0-9-]/g, "") + ".json");
      notice("Versión LAB exportada en este equipo.", false);
    }).catch(function (error) { notice(error.message, true); }).finally(function () {
      if (button.isConnected) button.disabled = false;
    });
  }
  function importStrategyFile(event) {
    var input = event.currentTarget, file = input.files && input.files[0];
    if (!file) return;
    if (file.size > 100000) { notice("Archivo de estrategia demasiado grande.", true); input.value = ""; return; }
    var button = root.querySelector("#lab-import-strategy"); button.disabled = true;
    file.text().then(function (raw) {
      return api("/strategies/import", {method:"POST", body:JSON.stringify(JSON.parse(raw))});
    }).then(function (saved) {
      return reload(saved.id).then(function () {
        notice("Estrategia LAB importada como versión nueva independiente.", false);
      });
    }).catch(function (error) {
      notice(error instanceof SyntaxError ? "El archivo no contiene JSON válido." : error.message, true);
    }).finally(function () {
      input.value = "";
      if (button.isConnected) button.disabled = false;
    });
  }
  function execute() {
    var strategy = strategies.find(function (row) { return row.id === root.querySelector("#lab-strategy").value; });
    if (!activeDataset || !strategy) { notice("Selecciona un dataset y guarda una estrategia antes de ejecutar.", true); return; }
    var button = root.querySelector("#lab-run"), selector = root.querySelector("#lab-version");
    var versionId = selector.value;
    if (!versionId || selector.dataset.strategyId !== strategy.id || selector.dataset.ready !== "true") {
      notice("Espera a que se cargue la versión de la estrategia.", true); return;
    }
    button.dataset.running = "true"; button.disabled = true;
    api("/runs?view=chart&markers=window&curves=window", {method:"POST", body:JSON.stringify({dataset_id:activeDataset.id, version_id:versionId})})
      .then(function (run) {
        notice("Prueba guardada. Verificando el resultado antes de mostrarlo…", false);
        return reload().then(function () {
          if (!root || !root.isConnected) return;
          root.querySelector("#lab-run-select").value = run.id;
          selectRun();
        });
      })
      .catch(function (error) { notice(error.message, true); }).finally(function () {
        delete button.dataset.running;
        if (button.isConnected) button.disabled = !selector.isConnected || !selector.value || selector.dataset.ready !== "true";
      });
  }
  function compareSelectedRuns() {
    var leftId = root.querySelector("#lab-compare-left").value;
    var rightId = root.querySelector("#lab-compare-right").value;
    if (!leftId || !rightId || leftId === rightId) {
      notice("Elige dos pruebas distintas.", true); return;
    }
    var mounted = root, button = root.querySelector("#lab-compare-action"), target = root.querySelector("#lab-comparison");
    var selection = {left_id:leftId, right_id:rightId};
    function samePair() {
      return root === mounted && target.isConnected &&
        mounted.querySelector("#lab-compare-left").value === leftId &&
        mounted.querySelector("#lab-compare-right").value === rightId;
    }
    button.disabled = true; target.textContent = "Verificando y repitiendo ambas pruebas…";
    api("/runs/compare", {method:"POST", body:JSON.stringify(selection)}).then(function (result) {
      if (!samePair()) return;
      if (!result.comparable) {
        target.innerHTML = '<p class="amber">' + esc(result.reason) + '</p>' +
          '<small>Base: ' + esc(result.left.dataset.hash.slice(0,16)) + '… · Alternativa: ' +
          esc(result.right.dataset.hash.slice(0,16)) + '…</small>';
        attachComparisonAi(target, selection, samePair);
        return;
      }
      var a = result.left, b = result.right, delta = result.deltas;
      function assumptions(item) {
        var x = item.assumptions;
        return "Dirección " + x.direction + " · Capital " + x.initial_capital +
          " · Asignación " + x.allocation + " · Comisión " + x.fee_rate +
          " · Slippage " + x.slippage_rate + " · Stop " + x.stop_loss +
          " · Objetivo " + x.take_profit +
          (x.trailing_stop ? " · Stop móvil " + x.trailing_stop : "") +
          (x.max_holding_bars ? " · Máx. velas " + x.max_holding_bars : "");
      }
      target.innerHTML = '<p class="muted">' + esc(a.dataset.symbol) + ' · ' + esc(a.dataset.timeframe) +
        ' · SHA-256 ' + esc(a.dataset.hash.slice(0,16)) + '… · ' + esc(a.engine_version) + ' · VERIFICADOS</p>' +
        '<div class="tablewrap"><table><thead><tr><th>Métrica</th><th data-help="' + esc(assumptions(a)) + '">Base · ' + esc(a.strategy_name) +
        '</th><th data-help="' + esc(assumptions(b)) + '">Alternativa · ' + esc(b.strategy_name) + '</th><th>Δ alt. − base</th></tr></thead><tbody>' +
        '<tr><td>Rentabilidad</td><td>' + displayPercent(a.metrics.return_pct, 2) + '</td><td>' +
          displayPercent(b.metrics.return_pct, 2) + '</td><td>' + displayDecimal(delta.return_percentage_points, 2) + ' pp</td></tr>' +
        '<tr><td>Drawdown máx.</td><td>' + displayPercent(a.metrics.max_drawdown_pct, 2) + '</td><td>' +
          displayPercent(b.metrics.max_drawdown_pct, 2) + '</td><td>' + displayDecimal(delta.drawdown_percentage_points, 2) + ' pp</td></tr>' +
        '<tr><td>Operaciones</td><td>' + esc(a.metrics.trades) + '</td><td>' + esc(b.metrics.trades) +
          '</td><td>' + esc(delta.trades) + '</td></tr></tbody></table></div>' +
        (result.assumptions_equal ? '' : '<p class="amber">Supuestos distintos: revisa capital, costes y riesgo de cada versión.</p>') +
        '<small>' + esc(result.note) + '</small>';
      attachComparisonAi(target, selection, samePair);
    }).catch(function (error) {
      if (!samePair()) return;
      target.textContent = "Comparación no disponible: " + error.message;
      notice(error.message, true);
    }).finally(function () { if (button.isConnected) button.disabled = runs.length < 2; });
  }
  function attachComparisonAi(target, selection, samePair) {
    target.insertAdjacentHTML("beforeend", '<div class="row"><button type="button" id="lab-compare-ai" class="ghost">Analizar comparación con IA</button></div>' +
      '<div id="lab-compare-ai-panel" class="lab-ai-panel" hidden></div>');
    var panel = target.querySelector("#lab-compare-ai-panel"), button = target.querySelector("#lab-compare-ai");
    button.addEventListener("click", function () {
      mountAiPreview(panel, button, function () { return panel.isConnected && samePair(); }, "/runs/compare", {
        selection:selection,
        archive:null
      });
    });
  }
  function executeValidation() {
    var versionId = root.querySelector("#lab-version").value;
    if (!activeDataset || !versionId) { notice("Selecciona un histórico y una versión guardada.", true); return; }
    var button = root.querySelector("#lab-validate"); button.disabled = true;
    api("/validations", {method:"POST", body:JSON.stringify({
      dataset_id:activeDataset.id, version_id:versionId,
      train_fraction:root.querySelector("#lab-validation-fraction").value
    })}).then(function (run) {
      notice("Validación cronológica guardada; revisa ambos tramos y su muestra.", false);
      return reload().then(function () { if (root) showValidation(run); });
    }).catch(function (error) { notice(error.message, true); })
      .finally(function () { if (root) button.disabled = false; });
  }
  function executeRollingValidation() {
    var versionId = root.querySelector("#lab-version").value;
    if (!activeDataset || !versionId) { notice("Selecciona un histórico y una versión guardada.", true); return; }
    var button = root.querySelector("#lab-rolling"); button.disabled = true;
    api("/rolling-validations", {method:"POST", body:JSON.stringify({
      dataset_id:activeDataset.id, version_id:versionId,
      folds:Number(root.querySelector("#lab-rolling-folds").value)
    })}).then(function (run) {
      notice("Ventanas guardadas; revisa cada reserva antes de sacar conclusiones.", false);
      return reload().then(function () { if (root) showValidation(run); });
    }).catch(function (error) { notice(error.message, true); })
      .finally(function () { if (root) button.disabled = false; });
  }
  function selectValidation() {
    var renderedRoot = root, id = root.querySelector("#lab-validation-select").value;
    var target = root.querySelector("#lab-validation-report");
    var generation = Number(target.dataset.loadGeneration || 0) + 1;
    target.dataset.loadGeneration = String(generation);
    target.dataset.integrity = id ? "checking" : "empty";
    target.className = "state";
    target.textContent = id ? "Cargando validación…" : "Selecciona una validación.";
    function current() {
      return root === renderedRoot && target.isConnected &&
        root.querySelector("#lab-validation-select").value === id && Number(target.dataset.loadGeneration) === generation;
    }
    if (id) api("/validations/" + encodeURIComponent(id)).then(function (run) {
      if (current()) showValidation(run);
    }).catch(function (error) {
      if (!current()) return;
      target.className = "state error"; target.dataset.integrity = "error"; target.textContent = error.message;
    });
  }
  function experimentVerifyMarkup() {
    return '<div class="lab-verify" data-state="unchecked"><span class="lab-verify-badge">SIN COMPROBAR</span>' +
      '<button class="ghost" type="button" data-help="Repite el experimento con el histórico, versión y parámetros guardados. No garantiza rendimientos futuros.">Verificar</button>' +
      '<span class="lab-verify-detail" role="status" aria-live="polite"></span></div>';
  }
  function reserveReuseMarkup(run) {
    var reuse = run.reserve_reuse || {}, status = reuse.status || "ARCHIVE_UNVERIFIABLE";
    var count = Number(reuse.overlapping_run_count) || 0;
    var labels = {
      SHARED_WITH_SAVED_EXPERIMENTS: ["RESERVA COMPARTIDA", count +
        (count === 1 ? " experimento guardado comparte velas reservadas." :
          " experimentos guardados comparten velas reservadas.")],
      NO_SHARED_SAVED_RESERVE: ["SIN SOLAPE REGISTRADO", "Sólo se comprueba este mismo histórico guardado."],
      NO_RESERVE_EVALUATED: ["RESERVA SIN EVALUAR", "No hubo resultado fuera de muestra en esta ejecución."],
      ARCHIVE_UNVERIFIABLE: ["SOLAPE NO VERIFICABLE", "Revisa la integridad de los experimentos guardados."]
    };
    if (!Object.prototype.hasOwnProperty.call(labels, status)) status = "ARCHIVE_UNVERIFIABLE";
    return '<div class="lab-reserve-reuse" data-status="' + status + '" role="status" ' +
      'data-help="Coincidencia temporal de reservas sobre el mismo SHA-256 de histórico LAB. No detecta datos vistos fuera de ORBIS ni copias reimportadas.">' +
      '<strong>' + labels[status][0] + '</strong><span>' + esc(labels[status][1]) + '</span></div>';
  }
  function attachExperimentVerifier(target, id, path, onUnverified) {
    var panel = target.querySelector(".lab-verify");
    var button = panel.querySelector("button");
    var badge = panel.querySelector(".lab-verify-badge");
    var detail = panel.querySelector(".lab-verify-detail");
    var generation = Number(target.dataset.verifyGeneration || 0) + 1;
    target.dataset.verifyGeneration = String(generation);
    target.dataset.runId = id;
    target.dataset.integrity = "unchecked";
    var renderedRoot = root, reference = null;
    if (path === "/validations/") {
      var referenceHost = document.createElement("span");
      panel.insertBefore(referenceHost, detail);
      reference = window.OrbisResearchReference.mount(referenceHost, {
        url:"/lab" + path + encodeURIComponent(id) + "/reference",
        fileName:"orbis-referencia-" + id + ".json",
        isCurrent:function () {
          return root === renderedRoot && target.dataset.runId === id && target.dataset.integrity === "verified" &&
            root.querySelector("#lab-validation-select").value === id;
        },
        onError:function (error) {
          target.dataset.integrity = "error"; panel.dataset.state = "error";
          badge.textContent = "NO VERIFICADO"; detail.textContent = error.message;
          if (onUnverified) onUnverified();
        }
      });
    }
    button.addEventListener("click", function () {
      var requestGeneration = Number(target.dataset.verifyGeneration) + 1;
      target.dataset.verifyGeneration = String(requestGeneration);
      button.disabled = true;
      target.dataset.integrity = "checking";
      if (reference) reference.setEnabled(false);
      panel.dataset.state = "checking";
      badge.textContent = "COMPROBANDO";
      detail.textContent = "";
      api(path + encodeURIComponent(id) + "/verify").then(function (result) {
        if (!target.isConnected || target.dataset.runId !== id ||
            Number(target.dataset.verifyGeneration) !== requestGeneration) return;
        var labels = {verified:"REPRODUCIBLE", mismatch:"DIFERENCIA DETECTADA",
          engine_unavailable:"MOTOR NO DISPONIBLE", legacy_unpinned:"LEGADO SIN HUELLAS"};
        var status = Object.prototype.hasOwnProperty.call(labels, result.status) ? result.status : "error";
        panel.dataset.state = status;
        badge.textContent = labels[status] || "NO VERIFICABLE";
        detail.textContent = result.reason || "No se pudo comprobar el experimento.";
        target.dataset.integrity = status;
        if (reference) reference.setEnabled(status === "verified");
        if (status !== "verified" && onUnverified) onUnverified();
      }).catch(function (error) {
        if (!target.isConnected || target.dataset.runId !== id ||
            Number(target.dataset.verifyGeneration) !== requestGeneration) return;
        panel.dataset.state = "error";
        target.dataset.integrity = "error";
        badge.textContent = "NO VERIFICADO";
        detail.textContent = error.message;
      }).finally(function () {
        if (target.isConnected && target.dataset.runId === id &&
            Number(target.dataset.verifyGeneration) === requestGeneration) button.disabled = false;
      });
    });
  }
  function showValidation(run) {
    if (run.method === "ROLLING_FIXED_VERSION") { showRollingValidation(run); return; }
    var target = root && root.querySelector("#lab-validation-report");
    if (!target) return;
    root.querySelector("#lab-validation-select").value = run.id;
    var train = run.train.metrics, test = run.out_of_sample.metrics;
    var trainRisk = run.risk && run.risk.train, testRisk = run.risk && run.risk.out_of_sample;
    function metricRow(label, key, percent) {
      return '<tr><th scope="row">' + label + '</th><td class="num" title="' + esc(train[key]) + '">' +
        (key === "trades" ? esc(train[key]) : percent ? displayPercent(train[key], 2) : displayDecimal(train[key], 2)) +
        '</td><td class="num" title="' + esc(test[key]) + '">' +
        (key === "trades" ? esc(test[key]) : percent ? displayPercent(test[key], 2) : displayDecimal(test[key], 2)) + '</td></tr>';
    }
    target.className = "";
    target.innerHTML = '<div class="lab-validation-split" role="img" aria-label="Desarrollo ' + esc(run.train_bars) +
      ' velas, reserva ' + esc(run.test_bars) + ' velas"><span style="flex:' + esc(run.train_bars) + '">DESARROLLO · ' +
      esc(run.train_bars) + '</span><span style="flex:' + esc(run.test_bars) + '">RESERVA · ' + esc(run.test_bars) + '</span></div>' +
      experimentVerifyMarkup() + reserveReuseMarkup(run) +
      '<div class="tablewrap"><table class="lab-validation-table"><thead><tr><th>Métrica</th><th>Desarrollo</th><th>Reserva</th></tr></thead><tbody>' +
      metricRow("Operaciones", "trades", false) + metricRow("Resultado neto", "net_profit", false) +
      metricRow("Rentabilidad", "return_pct", true) + metricRow("Drawdown máx.", "max_drawdown_pct", true) +
      metricRow("Exposición", "exposure_pct", true) +
      '<tr><th scope="row" data-help="Desviación típica muestral de los retornos del capital entre velas; no anualizada.">Volatilidad/vela</th>' +
      '<td class="num">' + displayPercent(trainRisk && trainRisk.bar_volatility_pct, 2) + '</td>' +
      '<td class="num">' + displayPercent(testRisk && testRisk.bar_volatility_pct, 2) + '</td></tr></tbody></table></div>' +
      '<p class="muted">Corte ' + esc(utc(run.test_start_t)) + ' UTC · Dataset ' + esc(run.dataset_hash.slice(0,16)) +
      '… · Motor ' + esc(run.engine_version) + '</p>' +
      '<p class="amber" role="status">' + (train.trades < 30 || test.trades < 30 ?
        'Muestra pequeña; ' : '') + 'reserva cronológica no garantiza resultados futuros.</p>' +
      '<details><summary>Método y límites</summary><ul>' + run.warnings.map(function (warning) {
        return '<li>' + esc(warning) + '</li>'; }).join("") + '</ul></details>' +
      '<details><summary>Operaciones de reserva y cifras exactas</summary><pre class="lab-events">' +
      esc(JSON.stringify({metrics:test, trades:run.out_of_sample.trades,
        events:run.out_of_sample.events}, null, 2)) + '</pre></details>';
    attachExperimentVerifier(target, run.id, "/validations/");
  }
  function showRollingValidation(run) {
    var target = root && root.querySelector("#lab-validation-report");
    if (!target) return;
    root.querySelector("#lab-validation-select").value = run.id;
    target.className = "";
    target.innerHTML = '<div class="lab-rolling-head"><strong>' + esc(run.positive_folds) + '/' + esc(run.folds) +
      ' reservas positivas</strong><span>' + esc(run.tested_trades) + ' operaciones en reservas disjuntas</span></div>' +
      experimentVerifyMarkup() + reserveReuseMarkup(run) +
      '<div class="lab-rolling-sensitive"><div class="tablewrap"><table class="lab-validation-table"><thead><tr>' +
      '<th>Ventana</th><th>Reserva UTC</th><th>Velas</th><th>Operaciones</th><th>Neto</th><th>Drawdown</th></tr></thead><tbody>' +
      run.windows.map(function (window) { var metrics = window.out_of_sample.metrics;
        return '<tr><th scope="row">' + esc(window.index) + '</th><td>' + esc(utc(window.test_start_t)) +
          ' → ' + esc(utc(window.test_end_t)) + '</td><td class="num">' + esc(window.test_bars) +
          '</td><td class="num">' + esc(metrics.trades) + '</td><td class="num" title="' + esc(metrics.net_profit) +
          '">' + displayDecimal(metrics.net_profit, 2) + '</td><td class="num">' +
          displayPercent(metrics.max_drawdown_pct, 2) + '</td></tr>';
      }).join("") + '</tbody></table></div>' +
      '<details><summary>Operaciones por ventana</summary><pre class="lab-events">' +
      esc(JSON.stringify(run.windows.map(function (window) { return {window:window.index,
        metrics:window.out_of_sample.metrics, trades:window.out_of_sample.trades}; }), null, 2)) +
      '</pre></details></div>' +
      '<p class="muted">Dataset ' + esc(run.dataset_hash.slice(0,16)) + '… · Motor ' + esc(run.engine_version) + '</p>' +
      '<details><summary>Método y límites</summary><ul>' + run.warnings.map(function (warning) {
        return '<li>' + esc(warning) + '</li>'; }).join("") + '</ul></details>';
    attachExperimentVerifier(target, run.id, "/validations/", function () {
      var sensitive = target.querySelector(".lab-rolling-sensitive");
      if (sensitive) sensitive.hidden = true;
      var head = target.querySelector(".lab-rolling-head");
      if (head) head.hidden = true;
    });
  }
  function executeOptimization() {
    var versionId = root.querySelector("#lab-version").value;
    if (!activeDataset || !versionId) { notice("Selecciona un histórico y una versión guardada.", true); return; }
    function rates(selector) { return root.querySelector(selector).value.trim().split(/[;,\s]+/).filter(Boolean); }
    var button = root.querySelector("#lab-optimize"); button.disabled = true;
    var walkForward = root.querySelector("#lab-opt-method").value === "walk-forward";
    var payload = {
      dataset_id:activeDataset.id, version_id:versionId,
      stop_losses:rates("#lab-opt-stops"), take_profits:rates("#lab-opt-targets")
    };
    if (walkForward) {
      payload.folds = Number(root.querySelector("#lab-opt-fold-count").value);
      payload.final_holdout_fraction = root.querySelector("#lab-opt-final-fraction").value;
    }
    else payload.train_fraction = root.querySelector("#lab-opt-fraction").value;
    api(walkForward ? "/optimizations/walk-forward" : "/optimizations",
      {method:"POST", body:JSON.stringify(payload)}).then(function (run) {
      notice("Exploración guardada: " + (walkForward ? run.folds + " ventanas" : run.candidates.length + " combinaciones") + ".", false);
      return reload().then(function () { if (root) showOptimization(run); });
    }).catch(function (error) { notice(error.message, true); })
      .finally(function () { if (root) button.disabled = false; });
  }
  function updateOptimizationMethod() {
    if (!root) return;
    var walkForward = root.querySelector("#lab-opt-method").value === "walk-forward";
    root.querySelector(".lab-opt-split").hidden = walkForward;
    root.querySelectorAll(".lab-opt-folds").forEach(function (field) { field.hidden = !walkForward; });
  }
  function selectOptimization() {
    var id = root.querySelector("#lab-opt-select").value;
    if (id) api("/optimizations/" + encodeURIComponent(id)).then(showOptimization)
      .catch(function (error) { notice(error.message, true); });
  }
  function showParameterMap(host, run) {
    function label(value) {
      return String(value).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "").replace(".", ",");
    }
    window.OrbisLabParameterMap.render(host, run.candidates.map(function (candidate) {
      var selected = run.selected && candidate.stop_loss === run.selected.stop_loss &&
        candidate.take_profit === run.selected.take_profit;
      return {stop:label(candidate.stop_loss_pct), target:label(candidate.take_profit_pct),
        net:candidate.train_metrics.net_profit, eligible:candidate.eligible, selected:selected};
    }));
  }
  function showOptimization(run) {
    if (run.method === "WALK_FORWARD_GRID_STOP_TARGET") { showWalkForwardOptimization(run); return; }
    var target = root && root.querySelector("#lab-opt-report");
    if (!target) return;
    root.querySelector("#lab-opt-select").value = run.id;
    root.querySelector("#lab-opt-method").value = "holdout";
    updateOptimizationMethod();
    var selected = run.selected, test = run.out_of_sample && run.out_of_sample.metrics;
    var sensitivity = run.sensitivity;
    var sensitivityText = '';
    if (selected && sensitivity) {
      sensitivityText = sensitivity.neighbor_count ?
        '<p class="lab-opt-sensitivity" data-help="Vecinos inmediatos del stop y objetivo elegidos; se calculan sólo en desarrollo. No predicen la reserva ni resultados futuros.">' +
        '<strong>Parámetros vecinos</strong><span>' + esc(sensitivity.positive_neighbors) + ' / ' +
        esc(sensitivity.eligible_neighbors) + ' rentables · ' + esc(sensitivity.neighbor_count) +
        ' evaluados</span></p>' :
        '<p class="lab-opt-sensitivity"><strong>Parámetros vecinos</strong><span>Sin comparaciones disponibles</span></p>';
    }
    target.className = "";
    target.innerHTML = '<p class="muted">Desarrollo ' + esc(run.train_bars) + ' velas · reserva ' + esc(run.test_bars) +
      ' · SHA-256 ' + esc(run.dataset_hash.slice(0,16)) + '… · ' + esc(run.optimizer_version) + '</p>' +
      experimentVerifyMarkup() + reserveReuseMarkup(run) + '<div class="lab-parameter-map"></div>' +
      '<div class="tablewrap"><table class="lab-opt-table"><thead><tr><th>Stop</th><th>Objetivo</th><th>Operaciones · desarrollo</th>' +
      '<th>Neto · desarrollo</th><th>Drawdown · desarrollo</th><th>Estado</th></tr></thead><tbody>' +
      run.candidates.map(function (candidate) {
        var winner = selected && candidate.stop_loss === selected.stop_loss && candidate.take_profit === selected.take_profit;
        return '<tr' + (winner ? ' class="lab-opt-winner"' : '') + '><td>' + displayPercent(candidate.stop_loss_pct, 1) +
          '</td><td>' + displayPercent(candidate.take_profit_pct, 1) + '</td><td class="num">' +
          esc(candidate.train_metrics.trades) + '</td><td class="num" title="' + esc(candidate.train_metrics.net_profit) + '">' +
          displayDecimal(candidate.train_metrics.net_profit, 2) + '</td><td class="num">' +
          displayPercent(candidate.train_metrics.max_drawdown_pct, 2) + '</td><td>' +
          (winner ? 'Elegido' : candidate.eligible ? 'Apto' : 'Muestra escasa') + '</td></tr>';
      }).join("") + '</tbody></table></div>' + sensitivityText +
      (selected ? '<p class="lab-opt-reserve"><strong>Reserva · sólo elegido:</strong> ' +
        esc(test.trades) + ' operaciones · neto ' + displayDecimal(test.net_profit, 2) +
        ' · drawdown ' + displayPercent(test.max_drawdown_pct, 2) + '</p>' :
        '<p class="amber" role="status">Sin candidato con muestra mínima; la reserva no se evaluó.</p>') +
      (test && test.trades < 30 ? '<p class="amber" role="status">Reserva con menos de 30 operaciones: muestra inestable.</p>' : '') +
      '<p class="amber" role="status">El mejor histórico no garantiza resultados futuros.</p>' +
      '<details><summary>Método y límites</summary><ul>' + run.warnings.map(function (warning) {
        return '<li>' + esc(warning) + '</li>'; }).join("") + '</ul></details>';
    var heatmap = target.querySelector(".lab-parameter-map");
    showParameterMap(heatmap, run);
    attachExperimentVerifier(target, run.id, "/optimizations/", function () { heatmap.hidden = true; });
  }
  function showWalkForwardOptimization(run) {
    var target = root && root.querySelector("#lab-opt-report");
    if (!target) return;
    root.querySelector("#lab-opt-select").value = run.id;
    root.querySelector("#lab-opt-method").value = "walk-forward";
    root.querySelector("#lab-opt-fold-count").value = String(run.folds);
    if (run.final_fraction) root.querySelector("#lab-opt-final-fraction").value = run.final_fraction;
    updateOptimizationMethod();
    var final = run.final_holdout, finalMetrics = final && final.out_of_sample && final.out_of_sample.metrics;
    target.className = "";
    target.innerHTML = '<div class="lab-rolling-head"><strong>' + esc(run.positive_folds) + ' / ' + esc(run.eligible_folds) +
      ' reservas positivas</strong><span>' + esc(run.tested_trades) + ' operaciones · ' + esc(run.folds) +
      ' ventanas</span></div>' + experimentVerifyMarkup() + reserveReuseMarkup(run) +
      '<div class="lab-opt-windows"><div class="tablewrap"><table class="lab-opt-table"><thead><tr><th>Ventana</th><th>Reserva UTC</th>' +
      '<th>Elegido · stop / objetivo</th><th>Operaciones</th><th>Neto · reserva</th><th>Vecinos rentables / aptos</th></tr></thead><tbody>' +
      run.windows.map(function (window) {
        var selected = window.selected, test = window.out_of_sample && window.out_of_sample.metrics;
        var sensitivity = window.sensitivity;
        return '<tr><th scope="row">' + esc(window.index) + '</th><td>' + esc(utc(window.test_start_t)) +
          ' → ' + esc(utc(window.test_end_t)) + '</td><td>' +
          (selected ? displayPercent(selected.stop_loss_pct, 1) + ' / ' + displayPercent(selected.take_profit_pct, 1) :
            'Sin muestra mínima') + '</td><td class="num">' + (test ? esc(test.trades) : '—') +
          '</td><td class="num">' + (test ? displayDecimal(test.net_profit, 2) : '—') + '</td><td class="num">' +
          (sensitivity ? esc(sensitivity.positive_neighbors) + ' / ' + esc(sensitivity.eligible_neighbors) : '—') +
          '</td></tr>';
      }).join('') + '</tbody></table></div>' +
      run.windows.map(function (window) {
        return '<details><summary>Ventana ' + esc(window.index) + ' · candidatos y operaciones</summary>' +
          '<div class="lab-parameter-map" data-map-window></div>' +
          '<div class="tablewrap"><table class="lab-opt-table"><thead><tr><th>Stop</th><th>Objetivo</th><th>Operaciones · desarrollo</th>' +
          '<th>Neto · desarrollo</th><th>Estado</th></tr></thead><tbody>' + window.candidates.map(function (candidate) {
            var selected = window.selected && candidate.stop_loss === window.selected.stop_loss &&
              candidate.take_profit === window.selected.take_profit;
            return '<tr' + (selected ? ' class="lab-opt-winner"' : '') + '><td>' + displayPercent(candidate.stop_loss_pct, 1) +
              '</td><td>' + displayPercent(candidate.take_profit_pct, 1) + '</td><td class="num">' +
              esc(candidate.train_metrics.trades) + '</td><td class="num" title="' + esc(candidate.train_metrics.net_profit) + '">' +
              displayDecimal(candidate.train_metrics.net_profit, 2) + '</td><td>' +
              (selected ? 'Elegido' : candidate.eligible ? 'Apto' : 'Muestra escasa') + '</td></tr>';
          }).join('') + '</tbody></table></div>' +
          (window.out_of_sample ? '<pre class="lab-events">' +
            esc(JSON.stringify({metrics:window.out_of_sample.metrics, trades:window.out_of_sample.trades}, null, 2)) +
            '</pre>' : '<p class="amber">Reserva no evaluada.</p>') + '</details>';
      }).join('') +
      (final ? '<div class="lab-parameter-map" data-map-final></div>' : '') + '</div>' +
      (run.windows.some(function (window) { return window.out_of_sample && window.out_of_sample.metrics.trades < 30; }) ?
        '<p class="amber" role="status">Alguna reserva tiene menos de 30 operaciones: muestra inestable.</p>' : '') +
      (final ? '<p class="lab-opt-reserve"><strong>Reserva final intacta · ' + esc(final.bars) + ' velas:</strong> ' +
        esc(utc(final.start_t)) + ' → ' + esc(utc(final.end_t)) +
        (finalMetrics ? ' · ' + esc(finalMetrics.trades) + ' operaciones · neto ' +
          displayDecimal(finalMetrics.net_profit, 2) + ' · drawdown ' +
          displayPercent(finalMetrics.max_drawdown_pct, 2) : ' · no evaluada: sin candidato apto') + '</p>' +
        (final.selected ? '<p class="muted">Elegido con todo el desarrollo, antes de abrir la reserva: stop ' +
          displayPercent(final.selected.stop_loss_pct, 1) + ' · objetivo ' +
          displayPercent(final.selected.take_profit_pct, 1) + '.</p>' : '') +
        (finalMetrics ? '<details><summary>Operaciones de la reserva final</summary><pre class="lab-events">' +
          esc(JSON.stringify({metrics:finalMetrics, trades:final.out_of_sample.trades}, null, 2)) +
          '</pre></details>' : '') :
        '<p class="amber" role="status">Informe anterior: no hay una reserva final intacta.</p>') +
      '<p class="amber" role="status">Las reservas anteriores pasan al desarrollo posterior; ningún resultado garantiza rendimiento futuro.</p>' +
      '<p class="muted">Dataset ' + esc(run.dataset_hash.slice(0,16)) + '… · Motor ' + esc(run.engine_version) + '</p>' +
      '<details><summary>Método y límites</summary><ul>' + run.warnings.map(function (warning) {
        return '<li>' + esc(warning) + '</li>'; }).join('') + '</ul></details>';
    target.querySelectorAll("[data-map-window]").forEach(function (host, index) {
      showParameterMap(host, run.windows[index]);
    });
    if (final) showParameterMap(target.querySelector("[data-map-final]"), final);
    attachExperimentVerifier(target, run.id, "/optimizations/", function () {
      var sensitive = target.querySelector(".lab-opt-windows");
      if (sensitive) sensitive.hidden = true;
    });
  }
  function selectRun() {
    var id = root.querySelector("#lab-run-select").value;
    var epoch = ++runSelectionEpoch, currentRoot = root;
    activeRun = null;
    runVerifyEpoch += 1;
    verifiedPaperTarget = null;
    if (chart) { chart.remove(); chart = null; }
    if (equityChart) { equityChart.remove(); equityChart = null; }
    var target = root.querySelector("#lab-report");
    target.className = "state loading";
    target.dataset.integrity = id ? "checking" : "none";
    target.textContent = id ? "Verificando y repitiendo la prueba guardada…" : "Selecciona una prueba o ejecuta una.";
    if (!id) return;
    api("/runs/" + encodeURIComponent(id) + "?view=chart&markers=window&curves=window&require_verified=true")
      .then(function (run) {
        if (root !== currentRoot || epoch !== runSelectionEpoch ||
            root.querySelector("#lab-run-select").value !== id) return;
        if (run.verified !== true) throw new Error("La prueba no pudo verificarse.");
        showRun(run);
      }).catch(function (error) {
        if (root !== currentRoot || epoch !== runSelectionEpoch ||
            root.querySelector("#lab-run-select").value !== id) return;
        target.className = "state error";
        target.dataset.integrity = "mismatch";
        target.textContent = "Prueba no reproducible o no disponible: " + error.message;
        if (error.status === 409) {
          var replay = document.createElement("button");
          replay.type = "button";
          replay.className = "ghost";
          replay.textContent = "Repetir como prueba nueva";
          replay.setAttribute("data-help", "Usa el histórico y la versión guardados con el motor actual. Conserva la prueba anterior; no crea órdenes PAPER ni reales.");
          replay.addEventListener("click", function () {
            replay.disabled = true;
            target.className = "state loading";
            target.firstChild.textContent = "Creando una prueba nueva con las entradas conservadas…";
            api("/runs/" + encodeURIComponent(id) + "/replay-as-new", {method:"POST"}).then(function (created) {
              if (!target.isConnected || root !== currentRoot) return;
              return reload().then(function () {
                if (!target.isConnected || root !== currentRoot) return;
                if (!runs.some(function (row) { return row.id === created.run_id; })) {
                  throw new Error("La nueva prueba no aparece en el historial; recarga LAB.");
                }
                root.querySelector("#lab-run-select").value = created.run_id;
                selectRun();
                notice("Nueva prueba conservada con el motor actual; la anterior sigue intacta.", false);
              });
            }).catch(function (failure) {
              if (!target.isConnected || root !== currentRoot) return;
              target.className = "state error";
              target.firstChild.textContent = "No se pudo repetir la prueba: " + failure.message;
              replay.disabled = false;
            });
          });
          target.appendChild(document.createTextNode(" "));
          target.appendChild(replay);
        }
      });
  }
  function applyPaperTarget(run, target) {
    verifiedPaperTarget = target && target.run_id === run.id && target.dataset_id === run.dataset_id &&
      target.version_id === run.version_id && target.strategy_id &&
      (target.venue === "SPOT" || target.venue === "GRVT") ? target : null;
    var action = root.querySelector("#lab-paper-action");
    if (!action) return;
    action.hidden = !verifiedPaperTarget;
    action.disabled = !verifiedPaperTarget;
    if (verifiedPaperTarget) action.textContent = target.venue === "GRVT" ? "Seguir en GRVT PAPER" : "Seguir en Spot PAPER";
  }
  function verifyRun(id) {
    if (!activeRun || activeRun.id !== id) return;
    var epoch = ++runVerifyEpoch;
    var panel = root.querySelector("#lab-run-verify");
    var button = root.querySelector("#lab-verify-action");
    var badge = panel.querySelector(".lab-verify-badge");
    var detail = root.querySelector("#lab-verify-detail");
    button.disabled = true;
    panel.dataset.state = "checking";
    badge.textContent = "COMPROBANDO";
    detail.textContent = "";
    verifiedPaperTarget = null;
    var pendingPaperAction = root.querySelector("#lab-paper-action");
    if (pendingPaperAction) { pendingPaperAction.hidden = true; pendingPaperAction.disabled = true; }
    var pendingCoverage = root.querySelector("#lab-run-coverage");
    if (pendingCoverage) pendingCoverage.hidden = true;
    api("/runs/" + encodeURIComponent(id) + "/verify").then(function (result) {
      if (!activeRun || activeRun.id !== id || epoch !== runVerifyEpoch || !root.querySelector("#lab-run-verify")) return;
      var labels = {verified:"REPRODUCIBLE", mismatch:"DIFERENCIA DETECTADA",
        engine_unavailable:"MOTOR NO DISPONIBLE", legacy_unpinned:"LEGADO SIN HUELLAS"};
      var status = Object.prototype.hasOwnProperty.call(labels, result.status) ? result.status : "error";
      panel.dataset.state = status;
      badge.textContent = labels[status] || "NO VERIFICABLE";
      detail.textContent = result.reason || "No se pudo comprobar la prueba.";
      root.querySelector("#lab-report").dataset.integrity = status;
      var coverageBox = root.querySelector("#lab-run-coverage");
      if (coverageBox) {
        var rules = result.coverage && Array.isArray(result.coverage.rules) ? result.coverage.rules : [];
        coverageBox.hidden = status !== "verified" || !rules.length;
        if (!coverageBox.hidden) {
          coverageBox.innerHTML = rules.map(function (row) {
            var label = row.timeframe + " · " + row.complete_bars +
              (row.minimum_bars == null ? " velas completas" : "/" + row.minimum_bars + " velas") +
              " · " + row.kind.replace(/_/g, " ");
            var help = row.status === "INSUFFICIENT" ? "No había suficientes velas superiores completas para calcular este indicador. Una ausencia de señales no valida la regla." :
              row.status === "COVERAGE_ONLY" ? "Cobertura de velas completas; esta estructura no tiene un calentamiento mínimo suficiente por sí solo." :
              "Indicador calculable desde " + new Date(row.first_evaluable_at * 1000).toISOString() + "; no garantiza que la condición se cumpla.";
            return '<span class="lab-coverage-chip" data-status="' + esc(row.status) + '" data-help="' + esc(help) + '">' + esc(label) + '</span>';
          }).join("");
        }
      }
      if (status === "mismatch") {
        if (chart) { chart.remove(); chart = null; }
        if (equityChart) { equityChart.remove(); equityChart = null; }
        var markerStatus = root.querySelector("#lab-marker-status");
        if (markerStatus) { markerStatus.hidden = true; markerStatus.replaceChildren(); }
        root.querySelector("#lab-chart").textContent = "Gráfico bloqueado: prueba no reproducible.";
        root.querySelector("#lab-equity-chart").textContent = "Curva bloqueada: prueba no reproducible.";
        ["#lab-run-history", "#lab-equity-range"].forEach(function (selector) {
          var history = root.querySelector(selector);
          if (history) { history.hidden = true; history.replaceChildren(); }
        });
      }
      var aiAction = root.querySelector("#lab-run-ai-action");
      if (aiAction) aiAction.disabled = status !== "verified";
      applyPaperTarget(activeRun, status === "verified" ? result.paper_target : null);
      if (status !== "verified") {
        var aiPanel = root.querySelector("#lab-run-ai-panel");
        if (aiPanel) { aiPanel.hidden = true; aiPanel.replaceChildren(); }
      }
    }).catch(function (error) {
      if (!activeRun || activeRun.id !== id || epoch !== runVerifyEpoch || !root.querySelector("#lab-run-verify")) return;
      panel.dataset.state = "error";
      badge.textContent = "NO VERIFICADO";
      detail.textContent = error.message;
      verifiedPaperTarget = null;
      var paperAction = root.querySelector("#lab-paper-action");
      if (paperAction) { paperAction.hidden = true; paperAction.disabled = true; }
    }).finally(function () {
      if (activeRun && activeRun.id === id && epoch === runVerifyEpoch && root.querySelector("#lab-verify-action")) button.disabled = false;
    });
  }
  function downloadEvidence(bundle, filename, textType) {
    var file = textType ? new Blob([bundle], {type:textType}) :
      new Blob([JSON.stringify(bundle, null, 2)], {type:"application/json"});
    var url = URL.createObjectURL(file);
    var link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  function updateCohortButton(form) {
    var selected = form.querySelectorAll('input[name="run"]:checked').length;
    form.querySelector('button[type="submit"]').disabled =
      form.dataset.exporting === "true" || selected < 2 || selected > 8 || !form.elements.boundary.value;
  }
  function renderResearchCohort() {
    var panel = root.querySelector("#lab-cohort");
    panel.hidden = researchRuns.length < 2;
    var form = panel.querySelector("form"), options = form.querySelector("#lab-cohort-options");
    var checked = new Set(Array.from(options.querySelectorAll("input:checked"), function (input) { return input.value; }));
    var byDataset = new Map(datasets.map(function (dataset) { return [dataset.id, dataset]; }));
    options.innerHTML = researchRuns.map(function (run) {
      var dataset = byDataset.get(run.dataset_id);
      var market = dataset ? dataset.symbol + " · " + dataset.timeframe : "Histórico " + run.dataset_id.slice(0,8);
      return '<label><input type="checkbox" name="run" value="' + esc(run.id) + '"' +
        (checked.has(run.id) ? ' checked' : '') + '> <span>' + esc(market) + ' · ' +
        esc(run.created_at.slice(0,16).replace("T", " ")) + ' · ' + esc(run.summary.cases) +
        ' casos · ' + esc(run.id.slice(0,8)) + '</span></label>';
    }).join("");
    updateCohortButton(form);
  }
  function exportResearchCohort(event) {
    event.preventDefault();
    var form = event.currentTarget, result = form.querySelector("#lab-cohort-result");
    var runIds = Array.from(form.querySelectorAll('input[name="run"]:checked'), function (input) { return input.value; });
    if (!form.reportValidity() || form.dataset.exporting === "true" || runIds.length < 2 || runIds.length > 8) return;
    var boundary = Date.parse(form.elements.boundary.value + "T00:00:00Z") / 1000;
    if (!Number.isSafeInteger(boundary)) return;
    form.dataset.exporting = "true";
    result.textContent = "Verificando consultas…";
    updateCohortButton(form);
    api("/research/export-cohort", {method:"POST", cache:"no-store",
      body:JSON.stringify({run_ids:runIds, boundary_open_t:boundary})}).then(function (bundle) {
      if (!form.isConnected || !root || !root.contains(form)) return;
      downloadEvidence(bundle, "orbis-conjunto-" + bundle.content_sha256.slice(0,12) + ".json");
      result.textContent = bundle.counts.development + " desarrollo · " + bundle.counts.validation + " reserva";
    }).catch(function (error) {
      if (form.isConnected) result.textContent = error.message;
    }).finally(function () {
      delete form.dataset.exporting;
      if (form.isConnected) updateCohortButton(form);
    });
  }
  function exportResearch(id) {
    if (!activeResearch || activeResearch.id !== id) return;
    var button = root.querySelector("#lab-research-export");
    var format = root.querySelector("#lab-research-export-format").value;
    button.disabled = true;
    api("/research/runs/" + encodeURIComponent(id) + "/export" + (format === "v3" ? "?schema_version=v3" : ""), {cache:"no-store"}).then(function (bundle) {
      downloadEvidence(bundle, "orbis-casos-" + id.replace(/[^A-Za-z0-9-]/g, "") + (format === "v3" ? "-70-30" : "") + ".json");
      notice("Casos LAB verificados descargados en este equipo.", false);
    }).catch(function (error) {
      notice(error.message, true);
    }).finally(function () {
      if (root && root.querySelector("#lab-research-export") === button &&
          root.querySelector("#lab-research-report").dataset.integrity !== "mismatch") button.disabled = false;
    });
  }
  function showAiPreview(id, kind) {
    var backtest = kind === "backtest";
    var panelSelector = backtest ? "#lab-run-ai-panel" : "#lab-ai-panel";
    var buttonSelector = backtest ? "#lab-run-ai-action" : "#lab-ai-preview-action";
    var reportSelector = backtest ? "#lab-report" : "#lab-research-report";
    var apiPrefix = backtest ? "/runs/" : "/research/runs/";
    var current = backtest ? activeRun : activeResearch;
    if (!root || !current || current.id !== id) return;
    var panel = root.querySelector(panelSelector), button = root.querySelector(buttonSelector);
    if (!panel || !button || root.querySelector(reportSelector).dataset.integrity === "mismatch") return;
    function stillCurrent() {
      var active = backtest ? activeRun : activeResearch;
      return root && active && active.id === id && root.querySelector(panelSelector) === panel;
    }
    mountAiPreview(panel, button, stillCurrent, apiPrefix + encodeURIComponent(id), {
      selection:null, archive:apiPrefix + encodeURIComponent(id), archiveId:id,
      mayEnable:function () { return root.querySelector(reportSelector).dataset.integrity !== "mismatch"; }
    });
  }
  function mountAiPreview(panel, button, stillCurrent, path, options) {
    panel.hidden = false;
    panel.className = "lab-ai-panel";
    panel.textContent = "Verificando la evidencia…";
    button.disabled = true;
    var previewOptions = {cache:"no-store"};
    if (options.selection) Object.assign(previewOptions, {method:"POST", body:JSON.stringify(options.selection)});
    api(path + "/ai-preview", previewOptions).then(function (preview) {
      if (!stillCurrent()) return;
      panel.innerHTML = '<div class="lab-ai-head"><strong>Consulta externa · ' + esc(preview.model) + '</strong>' +
        '<span>LAB · sin órdenes</span></div>' +
        (options.selection ? '<p class="muted">OpenAI recibirá las estadísticas verificadas de ambas pruebas y tu pregunta. La consulta puede tener coste.</p>' :
          '<p class="muted">Solo se enviarán estos datos agregados y tu pregunta a OpenAI. No se envían velas, señales, casos u operaciones individuales ni cartera. La API puede tener coste y retención de datos; consulta sus condiciones antes de continuar.</p>') +
        '<details><summary>Ver datos exactos a enviar</summary><pre class="lab-events"></pre></details>' +
        '<form class="lab-ai-form"><label class="field">Pregunta<textarea name="question" minlength="3" maxlength="1000" rows="2" required placeholder="¿Qué limitaciones tiene esta muestra?"></textarea></label>' +
        '<label class="field">Clave API OpenAI<input name="api_key" type="password" autocomplete="off" minlength="10" maxlength="1024" required></label>' +
        '<label class="field lab-ai-consent"><input name="consent" type="checkbox" required> Confirmo el envío externo de la evidencia y mi pregunta</label>' +
        (preview.archive_available ? '<label class="field lab-ai-consent"><input name="save_locally" type="checkbox"> Guardar respuesta en LAB cifrado</label>' : '') +
        '<button type="submit">Enviar análisis</button><span class="muted">La clave nunca se guarda. El archivo local es optativo.</span></form>' +
        (preview.archive_available && options.archive ? '<div class="lab-ai-archive-actions"><button type="button" class="ghost lab-ai-history-action">Ver archivo</button>' +
          '<button type="button" class="ghost lab-ai-export-action">Exportar JSON</button></div><div class="lab-ai-history state" hidden></div>' : '') +
        '<div class="lab-ai-result state" role="status" hidden></div>';
      panel.querySelector("pre").textContent = JSON.stringify(preview.evidence, null, 2);
      function loadAiHistory() {
        var history = panel.querySelector(".lab-ai-history");
        history.hidden = false; history.textContent = "Cargando archivo local…";
        api(options.archive + "/ai-analyses", {cache:"no-store"}).then(function (items) {
          if (!stillCurrent()) return;
          history.replaceChildren();
          if (!items.length) { history.textContent = "No hay análisis guardados para esta ejecución."; return; }
          items.forEach(function (item) {
            var article = document.createElement("article"), detail = document.createElement("pre");
            detail.textContent = item.created_at + " · " + item.model + "\n" + item.question +
              "\n" + item.answer + "\nHuella " + item.hash.slice(0, 12) + "…";
            if (item.evidence && item.evidence.schema === "orbis.lab.ai-backtest-comparison.v1") {
              detail.textContent = "Comparación · " + detail.textContent;
            }
            var remove = document.createElement("button");
            remove.type = "button"; remove.className = "ghost"; remove.textContent = "Borrar";
            remove.addEventListener("click", function () {
              if (!window.confirm("¿Borrar este análisis del LAB activo? No afecta a la estrategia ni al backtest. Las copias anteriores pueden conservarlo.")) return;
              remove.disabled = true;
              api(options.archive + "/ai-analyses/" + encodeURIComponent(item.id), {
                method:"DELETE", cache:"no-store", body:JSON.stringify({
                  expected_hash:item.hash, confirmation:"BORRAR ANÁLISIS"
                })
              }).then(function () { if (stillCurrent()) loadAiHistory(); })
                .catch(function (error) { notice(error.message, true); remove.disabled = false; });
            });
            article.appendChild(detail); article.appendChild(remove); history.appendChild(article);
          });
        }).catch(function (error) { history.textContent = error.message; });
      }
      if (preview.archive_available && options.archive) {
        panel.querySelector(".lab-ai-history-action").addEventListener("click", loadAiHistory);
        panel.querySelector(".lab-ai-export-action").addEventListener("click", function () {
          if (!window.confirm("El JSON descargado no estará cifrado. Incluye preguntas, respuestas y evidencia LAB. ¿Exportarlo?")) return;
          api(options.archive + "/ai-archive", {cache:"no-store"}).then(function (bundle) {
            if (stillCurrent()) {
              downloadEvidence(bundle, "orbis-ia-" + options.archiveId.replace(/[^A-Za-z0-9-]/g, "") + ".json");
              notice("Archivo de IA exportado sin cifrar en este equipo.", false);
            }
          }).catch(function (error) { notice(error.message, true); });
        });
      }
      panel.querySelector("form").addEventListener("submit", function (event) {
        event.preventDefault();
        var form = event.currentTarget, submit = form.querySelector('button[type="submit"]');
        if (!stillCurrent() || submit.disabled || !form.elements.consent.checked) return;
        var key = form.elements.api_key.value;
        var question = form.elements.question.value.trim();
        var saveLocally = Boolean(form.elements.save_locally && form.elements.save_locally.checked);
        form.elements.api_key.value = "";
        form.elements.consent.checked = false;
        var output = panel.querySelector(".lab-ai-result");
        output.hidden = false; output.className = "lab-ai-result state"; output.textContent = "Analizando…";
        submit.disabled = true;
        var body = {evidence_sha256:preview.evidence_sha256, question:question, api_key:key, save_locally:saveLocally};
        if (options.selection) Object.assign(body, options.selection, {confirmation:"ENVIAR COMPARACIÓN A OPENAI"});
        api(path + "/ai-analyze", {method:"POST", cache:"no-store", body:JSON.stringify(body)}).then(function (result) {
          if (stillCurrent()) {
            output.className = "lab-ai-result state ok";
            output.textContent = result.answer + "\n\n" + result.model + " · evidencia " +
              result.evidence_sha256.slice(0, 12) + "…" +
              (result.saved_locally ? " · guardado en LAB cifrado" : " · no guardado") +
              (result.archive_warning ? "\n" + result.archive_warning : "");
            if (result.saved_locally && options.archive && !panel.querySelector(".lab-ai-history").hidden) loadAiHistory();
          }
        }).catch(function (error) {
          output.className = "lab-ai-result state error"; output.textContent = error.message;
        }).finally(function () { submit.disabled = false; });
      });
    }).catch(function (error) {
      if (!stillCurrent()) return;
      panel.className = "lab-ai-panel state error";
      panel.textContent = error.message;
    }).finally(function () {
      if (stillCurrent() && (!options.mayEnable || options.mayEnable())) button.disabled = false;
    });
  }
  function exportRun(id) {
    if (!activeRun || activeRun.id !== id) return;
    var button = root.querySelector("#lab-export-action");
    var format = root.querySelector("#lab-export-format").value;
    button.disabled = true;
    api("/runs/" + encodeURIComponent(id) + "/export" + (format === "v3" ? "?schema_version=v3" : ""), {cache:"no-store"}).then(function (bundle) {
      downloadEvidence(bundle, "orbis-lab-" + id.replace(/[^A-Za-z0-9-]/g, "") + (format === "v3" ? "-70-30" : "") + ".json");
      notice("Evidencia LAB verificada descargada en este equipo.", false);
    }).catch(function (error) {
      notice(error.message, true);
    }).finally(function () {
      if (root && root.querySelector("#lab-export-action") === button) button.disabled = false;
    });
  }
  function showRun(run) {
    runVerifyEpoch += 1;
    activeRun = run;
    verifiedPaperTarget = null;
    var target = root.querySelector("#lab-report"); if (!target) return;
    if (chart) { chart.remove(); chart = null; }
    if (equityChart) { equityChart.remove(); equityChart = null; }
    root.querySelector("#lab-run-select").value = run.id;
    var m = run.metrics;
    var risk = run.risk || {};
    // Los runs antiguos no tienen esta métrica; sus trades guardados sí conservan el motivo.
    var ambiguousExits = run.ambiguous_exits == null ? run.trades.filter(function (trade) {
      return trade.reason === "STOP_AND_TARGET_AMBIGUOUS" || trade.reason === "TRAILING_STOP_AND_TARGET_AMBIGUOUS";
    }).length : run.ambiguous_exits;
    target.className = "";
    var initiallyVerified = run.verified === true;
    target.dataset.integrity = initiallyVerified ? "verified" : "unchecked";
    target.innerHTML = '<div class="lab-metrics"><div><small>Resultado neto</small><strong title="' + esc(m.net_profit) + '">' + displayDecimal(m.net_profit, 2) + '</strong></div><div><small>Operaciones</small><strong>' + esc(m.trades) +
      '</strong></div><div><small>Win rate</small><strong>' + (m.win_rate === null ? "—" : displayDecimal(String(Number(m.win_rate)*100), 1) + '%') + '</strong></div><div><small>Drawdown máximo</small><strong title="' + esc(m.max_drawdown_pct) + '">' + displayDecimal(m.max_drawdown_pct, 2) + '%</strong></div>' +
      '<div><small data-help="Desviación típica muestral de los retornos del capital entre velas; no anualizada ni comparable entre intervalos distintos.">Volatilidad/vela</small><strong title="' + esc(risk.bar_volatility_pct) + '">' + displayPercent(risk.bar_volatility_pct, 2) + '</strong></div>' +
      (risk.annualized_return_pct == null ? '' : '<div><small data-help="CAGR descriptivo del capital ficticio sin aportaciones, calculado sólo sobre un histórico de al menos 365 días. No pronostica resultados futuros.">Anualizada</small><strong title="' + esc(risk.annualized_return_pct) + '">' + displayPercent(risk.annualized_return_pct, 2) + '</strong></div>') + '</div>' +
      (ambiguousExits ? '<p class="amber" role="status" data-help="Con velas OHLC se desconoce qué precio se alcanzó primero. El motor atribuye la salida al stop para no favorecer artificialmente el resultado.">' +
        esc(ambiguousExits) + (ambiguousExits === 1 ? ' salida ambigua' : ' salidas ambiguas') +
        ': stop y objetivo en la misma vela; se asumió stop.</p>' : '') +
      (m.trades < 30 ? '<p class="amber" role="status">Muestra pequeña: ' + esc(m.trades) + ' operaciones. No extrapoles este resultado.</p>' : '') +
      '<div id="lab-run-verify" class="lab-verify" data-state="' + (initiallyVerified ? 'verified' : 'unchecked') + '"><span class="lab-verify-badge">' + (initiallyVerified ? 'REPRODUCIBLE' : 'SIN COMPROBAR') + '</span>' +
      '<button id="lab-verify-action" class="ghost" type="button" data-help="Repite el backtest con el histórico y la versión guardados y compara sus huellas y resultados. No demuestra rentabilidad futura.">Verificar prueba</button>' +
      '<select id="lab-export-format" class="lab-export-format" aria-label="Formato de exportación del backtest" data-help="Evidencia conserva todo el histórico. 70/30 añade particiones cronológicas y purga etiquetas que cruzan la reserva; no entrena una IA ni crea una validación independiente."><option value="v2">Evidencia</option><option value="v3">70/30 · IA</option></select>' +
      '<button id="lab-export-action" class="ghost" type="button" data-help="Descarga sólo datos LAB tras repetir y verificar la prueba. Separa condiciones conocidas de resultados futuros; no envía nada a una IA.">Exportar datos</button>' +
      '<button id="lab-run-ai-action" class="ghost" type="button" data-help="Verifica y muestra las estadísticas exactas antes de pedir un análisis externo. Sin velas, operaciones individuales ni acceso a cartera.">Analizar con IA</button>' +
      '<button id="lab-paper-action" class="ghost" type="button" hidden disabled data-help="Abre PAPER con esta versión y este histórico preseleccionados. No activa la estrategia ni mueve dinero.">Seguir en PAPER</button>' +
      '<span id="lab-verify-detail" role="status" aria-live="polite">' + (initiallyVerified ? 'Dataset, versión y resultado repetidos al abrir.' : '') + '</span></div>' +
      '<details class="lab-sequence-panel"><summary data-help="Permuta el P/L neto de operaciones cerradas de una prueba verificada. No recalcula tamaño, señales ni pérdidas dentro de la operación.">Sensibilidad al orden de operaciones</summary>' +
      (m.trades < 20 ? '<p class="muted">Requiere al menos 20 operaciones cerradas.</p>' :
        '<div class="row"><label class="field">Reordenaciones<select id="lab-sequence-paths"><option value="100">100</option><option value="250" selected>250</option><option value="500">500</option></select></label>' +
        '<button id="lab-sequence-action" class="ghost" type="button">Calcular</button></div>' +
        '<div id="lab-sequence-result" role="status" aria-live="polite"></div>') +
      '<p class="muted">Sólo P/L cerrados: no es un pronóstico ni reproduce el riesgo dentro de cada posición.</p></details>' +
      '<div id="lab-run-ai-panel" class="lab-ai-panel" hidden></div>' +
      '<div id="lab-run-coverage" class="lab-coverage" role="status" aria-label="Cobertura de temporalidades superiores" hidden></div>' +
      '<p class="muted">Motor ' + esc(run.engine_version) + ' · dataset ' + esc(run.dataset_id) +
      (run.dataset_source ? ' · ' + esc(run.dataset_source) : '') +
      (run.dataset_hash ? ' · SHA-256 ' + esc(run.dataset_hash.slice(0,16)) + '…' : '') +
      ' · versión ' + esc(run.version_id) + '</p>' +
      '<p class="muted lab-chart-legend" data-help="Una señal detectada no es una orden: sólo la flecha de entrada representa la ejecución simulada en la vela siguiente.">● Señal al cierre · ↑ Entrada simulada · ↓ Salida simulada</p>' +
      '<p id="lab-chart-load-status" class="state error" role="status" hidden></p><div id="lab-run-history" class="lab-history-controls" hidden></div><p id="lab-candle-status" class="state loading" role="status" hidden></p><div id="lab-chart" role="img" aria-label="Velas con señales, entradas y salidas simuladas"></div><p id="lab-marker-status" class="amber" role="status" hidden></p><p id="lab-chart-detail" class="muted" role="status">Mueve el cursor sobre una señal para ver sus condiciones.</p>' +
      '<h4>Capital ficticio y drawdown</h4><div id="lab-equity-range" class="lab-history-controls"></div><p id="lab-curve-status" class="state loading" role="status" hidden></p><div id="lab-equity-chart" role="img" aria-label="Curva de capital y drawdown simulados"></div>' +
      '<div class="lab-research-events-head"><h4>Operaciones</h4><div><button type="button" id="lab-trade-prev" class="ghost" aria-label="Operaciones anteriores">←</button><span id="lab-trade-page" role="status"></span><button type="button" id="lab-trade-next" class="ghost" aria-label="Operaciones siguientes">→</button></div></div>' +
      '<div class="tablewrap"><table><thead><tr><th>#</th><th>Dirección</th><th>Entrada UTC</th><th>Precio entrada</th><th>Precio salida</th><th>P/L</th><th>Motivo</th></tr></thead><tbody id="lab-trade-rows"></tbody></table></div>' +
      '<div class="lab-research-events-head"><h4>Señales detectadas</h4><div><button type="button" id="lab-event-prev" class="ghost" aria-label="Señales anteriores">←</button><span id="lab-event-page" role="status"></span><button type="button" id="lab-event-next" class="ghost" aria-label="Señales siguientes">→</button></div></div>' +
      '<div class="tablewrap"><table><thead><tr><th>#</th><th>Cierre UTC</th><th>Acción</th><th>Condición</th></tr></thead><tbody id="lab-event-rows"></tbody></table></div>' +
      '<details><summary>Hipótesis y cifras exactas</summary><p>' + esc(run.assumptions.join(" · ")) + '</p><pre class="lab-events">' + esc(JSON.stringify({metrics:run.metrics, exit_event_count:run.exit_event_count == null ? (run.exit_events || []).length : run.exit_event_count}, null, 2)) + '</pre><p class="muted">Señales y operaciones por páginas; la exportación verificada conserva el conjunto completo.</p></details>';
    var runCandles = [];
    function loadRunPage(kind, page) {
      var prefix = kind === "trades" ? "trade" : "event", offset = page * 50;
      var previous = target.querySelector("#lab-" + prefix + "-prev"), next = target.querySelector("#lab-" + prefix + "-next");
      previous.disabled = true; next.disabled = true;
      target.querySelector("#lab-" + prefix + "-page").textContent = "Cargando…";
      api("/runs/" + encodeURIComponent(run.id) + "/" + kind + "?offset=" + offset + "&limit=50").then(function (result) {
        if (!root || !activeRun || activeRun.id !== run.id || !previous.isConnected) return;
        target.querySelector("#lab-" + prefix + "-page").textContent = result.total ?
          (offset + 1) + "–" + (offset + result.items.length) + " / " + result.total : "0 / 0";
        previous.disabled = page === 0; next.disabled = offset + result.items.length >= result.total;
        target.querySelector("#lab-" + prefix + "-rows").innerHTML = kind === "trades" ? result.items.map(function (trade, index) {
          return '<tr data-entry-time="' + esc(trade.entry_time) + '" tabindex="0" title="Centrar entrada en el gráfico"><td>' + (offset + index + 1) + '</td><td>' + esc(trade.direction) + '</td><td>' + esc(new Date(trade.entry_time*1000).toISOString().slice(0,16)) +
            '</td><td class="num" title="' + esc(trade.entry_price) + '">' + displayDecimal(trade.entry_price, 4) + '</td><td class="num" title="' + esc(trade.exit_price) + '">' + displayDecimal(trade.exit_price, 4) +
            '</td><td class="num" title="' + esc(trade.pnl) + '">' + displayDecimal(trade.pnl, 2) + '</td><td>' + exitReason(trade.reason) + '</td></tr>';
        }).join("") : result.items.map(function (item, index) {
          return '<tr><td>' + (offset + index + 1) + '</td><td>' + esc(utc(item.signal_time)) + '</td><td>' + esc(item.action) +
            '</td><td><details><summary>' + esc(ruleSummary(item.explanation)) + '</summary><pre class="lab-events">' +
            esc(JSON.stringify(item, null, 2)) + '</pre></details></td></tr>';
        }).join("");
      }).catch(function (error) {
        if (!root || !activeRun || activeRun.id !== run.id || !previous.isConnected) return;
        var status = target.querySelector("#lab-" + prefix + "-page");
        status.textContent = "Error: " + error.message + " ";
        var retry = document.createElement("button");
        retry.type = "button"; retry.className = "ghost"; retry.textContent = "Reintentar";
        retry.addEventListener("click", function () { loadRunPage(kind, page); });
        status.appendChild(retry);
      });
    }
    ["trades", "events"].forEach(function (kind) {
      var prefix = kind === "trades" ? "trade" : "event", page = 0;
      target.querySelector("#lab-" + prefix + "-prev").addEventListener("click", function () { if (page > 0) loadRunPage(kind, --page); });
      target.querySelector("#lab-" + prefix + "-next").addEventListener("click", function () { loadRunPage(kind, ++page); });
      loadRunPage(kind, page);
    });
    function focusTrade(event) {
      var row = event.target.closest("tr[data-entry-time]");
      if (!row || !chart || !runCandles.length) return;
      var time = Number(row.dataset.entryTime), index = runCandles.findIndex(function (bar) { return bar.t === time; });
      if (index < 0) return;
      chart.timeScale().setVisibleLogicalRange({from:Math.max(0,index-20),to:Math.min(runCandles.length-1,index+20)});
      target.querySelector("#lab-chart-detail").textContent = utc(time) + " UTC · Entrada simulada";
    }
    target.querySelector("#lab-trade-rows").addEventListener("click", focusTrade);
    target.querySelector("#lab-trade-rows").addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); focusTrade(event); }
    });
    root.querySelector("#lab-verify-action").addEventListener("click", function () { verifyRun(run.id); });
    var sequenceButton = root.querySelector("#lab-sequence-action");
    if (sequenceButton) sequenceButton.addEventListener("click", function () {
      var output = root.querySelector("#lab-sequence-result");
      var paths = root.querySelector("#lab-sequence-paths").value;
      sequenceButton.disabled = true;
      output.className = "state loading";
      output.textContent = "Verificando y calculando…";
      api("/runs/" + encodeURIComponent(run.id) + "/sequence-risk?paths=" + paths, {cache:"no-store"}).then(function (report) {
        if (!sequenceButton.isConnected || !activeRun || activeRun.id !== run.id) return;
        var dd = report.drawdown_pct;
        output.className = "";
        output.innerHTML = '<div class="lab-sequence-stats">' +
          '<div><small>Orden observado</small><strong>' + displayPercent(report.observed_closed_trade_drawdown_pct, 2) + '</strong></div>' +
          '<div><small>Mediana</small><strong>' + displayPercent(dd.median, 2) + '</strong></div>' +
          '<div><small>Percentil 95</small><strong>' + displayPercent(dd.p95, 2) + '</strong></div>' +
          '<div><small>Peor reordenación</small><strong>' + displayPercent(dd.worst, 2) + '</strong></div></div>' +
          '<p class="muted">' + esc(report.trades) + ' cierres · ' + esc(report.paths) +
          ' reordenaciones · capital agotado en ' + esc(report.ruin_paths) +
          ' · P/L final invariable. Drawdown medido sólo tras cierres.</p>';
      }).catch(function (error) {
        if (!sequenceButton.isConnected || !activeRun || activeRun.id !== run.id) return;
        output.className = "state error";
        output.textContent = error.status === 404
          ? "Análisis no disponible en este servidor. Reinicia Orbis para cargar la versión actual."
          : error.message;
      }).finally(function () { if (sequenceButton.isConnected) sequenceButton.disabled = false; });
    });
    root.querySelector("#lab-export-action").addEventListener("click", function () { exportRun(run.id); });
    root.querySelector("#lab-run-ai-action").addEventListener("click", function () { showAiPreview(run.id, "backtest"); });
    applyPaperTarget(run, initiallyVerified ? run.paper_target : null);
    root.querySelector("#lab-paper-action").addEventListener("click", function () {
      if (!verifiedPaperTarget || !activeRun || activeRun.id !== run.id) return;
      var target = verifiedPaperTarget;
      var route = target.venue === "GRVT" ? "#/perpetuos" : "#/spot-paper";
      location.hash = route + "?dataset=" + encodeURIComponent(target.dataset_id) +
        "&strategy=" + encodeURIComponent(target.strategy_id) +
        "&version=" + encodeURIComponent(target.version_id) +
        "&run=" + encodeURIComponent(target.run_id);
    });
    var chartHost = root.querySelector("#lab-chart");
    var chartLoadStatus = root.querySelector("#lab-chart-load-status");
    function stillShowingRun() {
      return root && activeRun && activeRun.id === run.id && chartHost.isConnected &&
        root.querySelector("#lab-chart") === chartHost;
    }
    function loadChartDataset() {
      chartHost.hidden = true;
      chartLoadStatus.hidden = false;
      chartLoadStatus.className = "state loading";
      chartLoadStatus.textContent = "Cargando histórico…";
      api("/datasets/" + encodeURIComponent(run.dataset_id) + "?view=chart", {cache:"no-store"}).then(function (dataset) {
        if (!stillShowingRun() || target.dataset.integrity === "mismatch") return;
        if (run.dataset_hash && dataset.hash !== run.dataset_hash) {
          if (chart) { chart.remove(); chart = null; }
          if (equityChart) { equityChart.remove(); equityChart = null; }
          chartHost.hidden = false;
          chartHost.textContent = "Histórico distinto: gráfico bloqueado.";
          ["#lab-run-history", "#lab-equity-range"].forEach(function (selector) {
            var history = root.querySelector(selector);
            if (history) { history.hidden = true; history.replaceChildren(); }
          });
          root.querySelector("#lab-run-ai-action").disabled = true;
          verifiedPaperTarget = null;
          var paperAction = root.querySelector("#lab-paper-action");
          if (paperAction) { paperAction.hidden = true; paperAction.disabled = true; }
          root.querySelector("#lab-run-ai-panel").hidden = true;
          chartLoadStatus.hidden = true;
          notice("La huella del dataset no coincide con la prueba guardada.", true);
          return;
        }
        if (dataset.candles_windowed && (!Array.isArray(dataset.times) || dataset.times.length !== dataset.count ||
            dataset.times.some(function (time, index) { return !Number.isInteger(time) ||
              (index > 0 && time <= dataset.times[index - 1]); }))) {
          throw new Error("El índice temporal del histórico está incompleto");
        }
        runCandles = dataset.candles_windowed ? dataset.times.map(function (time) { return {t:time}; }) : dataset.candles;
        chartLoadStatus.hidden = true;
        chartLoadStatus.replaceChildren();
        chartHost.hidden = false;
        draw(runCandles, run.events || [], run.trades || [], run.equity, run.drawdown, run.id,
          !!run.markers_windowed, run.signal_count, run.trade_count, !!run.curves_windowed,
          !!dataset.candles_windowed, dataset.id, dataset.hash);
      }).catch(function (error) {
        if (!stillShowingRun() || target.dataset.integrity === "mismatch") return;
        chartLoadStatus.className = "state error";
        chartLoadStatus.textContent = "No se pudo cargar el histórico: " + error.message + " ";
        var retry = document.createElement("button");
        retry.type = "button"; retry.className = "ghost"; retry.textContent = "Reintentar gráfico";
        retry.addEventListener("click", loadChartDataset);
        chartLoadStatus.appendChild(retry);
      });
    }
    loadChartDataset();
  }
  function ruleSummary(node) {
    if (!node) return "Condiciones no disponibles";
    if (node.children) return node.children.map(ruleSummary).join(node.op === "OR" ? " O " : " Y ");
    if (node.kind && node.kind.indexOf("EMA_CROSS_") === 0) return "EMA " + node.period + "/" + node.slow_period +
      (node.kind === "EMA_CROSS_ABOVE_EMA" ? " cruza ↑" : " cruza ↓") +
      " · rápida " + (node.observed == null ? "—" : node.observed) + " · lenta " + (node.threshold == null ? "—" : node.threshold);
    if (node.kind && node.kind.indexOf("MACD_") === 0) return "MACD " + node.period + "/" + node.slow_period + "/" + node.signal_period +
      (node.kind === "MACD_CROSS_ABOVE_SIGNAL" ? " cruza ↑" : " cruza ↓") +
      " · línea " + (node.observed == null ? "—" : node.observed) + " · señal " + (node.threshold == null ? "—" : node.threshold);
    if (divergence(node.kind)) {
      var name = node.kind === "RSI_BULLISH_DIVERGENCE" ? "Divergencia RSI alcista" : "Divergencia RSI bajista";
      return name + (node.timeframe ? " · " + node.timeframe : "") + (node.pivots ?
        " · Confirma " + utc(node.confirmation_close_t) + " UTC · " + node.pivots.map(function (pivot, index) {
          return "G" + (index + 1) + " " + utc(pivot.t) + " UTC · Precio " + pivot.price +
            " · RSI " + displayDecimal(pivot.rsi, 2);
        }).join(" → ") : " · Sin confirmación");
    }
    if (node.kind && node.kind.indexOf("RETEST_") === 0) return (node.kind === "RETEST_ABOVE_RANGE" ? "Ruptura ↑" : "Ruptura ↓") +
      " · rango " + node.period + " · nivel " + (node.level || "—") + " · retesteo " + (node.retest_extreme || "—") +
      " · cierre " + (node.observed || "—") + " · " + (node.reason || "sin ruptura");
    return node.kind + " (" + node.period + "): " + (node.observed == null ? "—" : node.observed) + " / umbral " + (node.threshold == null ? "—" : node.threshold);
  }
  function draw(candles, events, trades, equity, drawdown, runId, windowed, signalCount, tradeCount,
                curvesWindowed, candlesWindowed, datasetId, datasetHash) {
    if (chart) { chart.remove(); chart = null; }
    if (equityChart) { equityChart.remove(); equityChart = null; }
    var target = root.querySelector("#lab-chart"), lw = window.LightweightCharts;
    if (!target || !lw) { notice("La biblioteca de gráficos no está disponible.", true); return; }
    chart = lw.createChart(target, {autoSize:true, height:500, layout:{background:{type:"solid",color:"#101b18"},textColor:"#c9d6ce"},
      grid:{vertLines:{color:"#24362f"},horzLines:{color:"#24362f"}},
      timeScale:{timeVisible:true,minBarSpacing:0.001,lockVisibleTimeRangeOnResize:true}});
    var series = chart.addSeries(lw.CandlestickSeries, {upColor:"#42c88a",downColor:"#ff7b78",borderVisible:false,wickUpColor:"#42c88a",wickDownColor:"#ff7b78"});
    if (candlesWindowed) {
      var priceTimeline = chart.addSeries(lw.LineSeries, {color:"rgba(0,0,0,0)",
        priceScaleId:"lab-time-axis",priceLineVisible:false,lastValueVisible:false,
        crosshairMarkerVisible:false});
      priceTimeline.setData(candles.map(function (bar) { return {time:bar.t,value:0}; }));
      target.style.visibility = "hidden";
      target.dataset.candleVisibleCount = "0";
    }
    series.setData(candlesWindowed ? [] : candles.map(function (bar) { return {time:bar.t,open:Number(bar.o),high:Number(bar.h),low:Number(bar.l),close:Number(bar.c)}; }));
    var markerApi = lw.createSeriesMarkers(series, []);
    var markers = [], byTime = new Map(), signalIndices = new Map();
    var markerTimer = null, markerChart = chart, requestedWindow = null, failedWindow = null;
    var loadedCandleWindow = null;
    var suppressCandleRange = false;
    var markerStatus = root.querySelector("#lab-marker-status");
    target.dataset.signalCount = String(signalCount == null ? events.length : signalCount);
    target.dataset.entryCount = String(tradeCount == null ? trades.length : tradeCount);
    function applyMarkers(windowEvents, windowTrades, fromTime, toTime) {
      markers = [];
      byTime = new Map();
      signalIndices = new Map();
      var signals = windowEvents.filter(function (event) { return event.action === "ENTER_NEXT_OPEN"; });
      signals.forEach(function (event) {
        if (event.signal_time < fromTime || event.signal_time > toTime) return;
        markers.push({time:event.signal_time,position:"aboveBar",color:"#efbd63",shape:"circle",
          text:Number(target.dataset.signalCount) <= 100 ? "Señal" : ""});
        var items = byTime.get(event.signal_time) || [];
        items.push("Señal al cierre: " + (event.explanation ? ruleSummary(event.explanation) : "consultando condición…"));
        byTime.set(event.signal_time, items);
        if (event.index != null && !event.explanation) signalIndices.set(event.signal_time, event.index);
      });
      windowTrades.forEach(function (trade) {
        if (trade.entry_time >= fromTime && trade.entry_time <= toTime) {
          markers.push({time:trade.entry_time,position:trade.direction === "LONG" ? "belowBar" : "aboveBar",
            color:"#88b8ff",shape:trade.direction === "LONG" ? "arrowUp" : "arrowDown",text:"Entrada"});
          var entryItems = byTime.get(trade.entry_time) || [];
          entryItems.push("Entrada simulada: " + trade.entry_price + " · " + trade.direction);
          byTime.set(trade.entry_time, entryItems);
        }
        if (trade.exit_time >= fromTime && trade.exit_time <= toTime) {
          markers.push({time:trade.exit_time,position:trade.direction === "LONG" ? "aboveBar" : "belowBar",
            color:trade.pnl[0] === "-" ? "#ff7b78" : "#9ce5c0",
            shape:trade.direction === "LONG" ? "arrowDown" : "arrowUp",text:"Salida"});
          var exitItems = byTime.get(trade.exit_time) || [];
          exitItems.push("Salida simulada: " + trade.exit_price + " · " + trade.reason);
          byTime.set(trade.exit_time, exitItems);
        }
      });
      markers.sort(function (a,b) { return a.time - b.time; });
      if (windowed || markers.length <= 1500) markerApi.setMarkers(markers);
      target.dataset.markerVisibleCount = String(markers.length);
    }
    function markerLowerBound(time) {
      var left = 0, right = markers.length;
      while (left < right) {
        var middle = (left + right) >> 1;
        if (markers[middle].time < time) left = middle + 1;
        else right = middle;
      }
      return left;
    }
    function markerBounds() {
      var range = chart.timeScale().getVisibleLogicalRange();
      var first = range ? Math.max(0, Math.floor((range.from - 40) / 200) * 200) : Math.max(0, candles.length - 300);
      var last = range ? Math.min(candles.length - 1, Math.ceil((range.to + 41) / 200) * 200 - 1) : candles.length - 1;
      return last < first ? null : {first:first, last:last,
        fromTime:candles[first].t, toTime:candles[last].t,
        key:candles[first].t + ":" + candles[last].t};
    }
    function updateVisibleMarkers() {
      if (markerTimer) clearTimeout(markerTimer);
      markerTimer = null;
      if (chart !== markerChart || !target.isConnected) return;
      var bounds = markerBounds();
      if (candlesWindowed && (!bounds || bounds.key !== loadedCandleWindow)) return;
      if (windowed) {
        if (!bounds) {
          markerApi.setMarkers([]); target.dataset.markerVisibleCount = "0";
          markerStatus.hidden = true;
          return;
        }
        var fromTime = bounds.fromTime, toTime = bounds.toTime, key = bounds.key;
        if (failedWindow === key) return;
        if (requestedWindow === key) return;
        requestedWindow = key;
        markerStatus.hidden = false;
        markerStatus.textContent = "Cargando marcas del tramo…";
        api("/runs/" + encodeURIComponent(runId) + "/markers?from_t=" + fromTime + "&to_t=" + toTime).then(function (windowData) {
          if (chart !== markerChart || !activeRun || activeRun.id !== runId || requestedWindow !== key) return;
          if (windowData.run_id !== runId || windowData.from_t !== fromTime || windowData.to_t !== toTime) {
            throw new Error("La ventana de marcas no coincide con la solicitada");
          }
          applyMarkers(windowData.events, windowData.trades, fromTime, toTime);
          failedWindow = null;
          markerStatus.hidden = true;
          markerStatus.replaceChildren();
        }).catch(function (error) {
          if (chart !== markerChart || requestedWindow !== key) return;
          requestedWindow = null;
          failedWindow = key;
          markerApi.setMarkers([]);
          target.dataset.markerVisibleCount = "0";
          markerStatus.hidden = false;
          markerStatus.textContent = "No se pudieron cargar las marcas del tramo. ";
          var retry = document.createElement("button");
          retry.type = "button"; retry.className = "ghost"; retry.textContent = "Reintentar";
          retry.addEventListener("click", function () { failedWindow = null; updateVisibleMarkers(); });
          markerStatus.appendChild(retry);
        });
        return;
      }
      if (markers.length <= 1500 && !candlesWindowed) {
        target.dataset.markerVisibleCount = String(markers.length);
        return;
      }
      if (!bounds) {
        markerApi.setMarkers([]);
        target.dataset.markerVisibleCount = "0";
        return;
      }
      var visible = markers.slice(markerLowerBound(bounds.fromTime), markerLowerBound(bounds.toTime + 1));
      markerApi.setMarkers(visible);
      target.dataset.markerVisibleCount = String(visible.length);
    }
    if (!windowed) applyMarkers(events, trades, -Infinity, Infinity);
    if (windowed || markers.length > 1500) chart.timeScale().subscribeVisibleLogicalRangeChange(function () {
      if (suppressCandleRange) return;
      if (windowed && requestedWindow !== null) {
        var next = markerBounds();
        if (!next || next.key !== requestedWindow) {
          requestedWindow = null;
          markerApi.setMarkers([]);
          target.dataset.markerVisibleCount = "0";
        }
      }
      if (markerTimer) clearTimeout(markerTimer);
      markerTimer = setTimeout(updateVisibleMarkers, windowed ? 80 : 40);
    });
    var drawnChart = chart, hoverTime = null, lookupTimer = null, loadingSignals = new Set();
    chart.subscribeCrosshairMove(function (param) {
      hoverTime = param.time;
      if (lookupTimer) clearTimeout(lookupTimer);
      var detail = root && root.querySelector("#lab-chart-detail");
      if (detail) detail.textContent = param.time === undefined || !byTime.has(param.time) ?
        "Mueve el cursor sobre una señal para ver sus condiciones." : byTime.get(param.time).join(" · ");
      if (param.time === undefined || !signalIndices.has(param.time) || loadingSignals.has(param.time)) return;
      var signalTime = param.time, index = signalIndices.get(signalTime);
      lookupTimer = setTimeout(function () {
        loadingSignals.add(signalTime);
        api("/runs/" + encodeURIComponent(runId) + "/events?offset=" + index + "&limit=1").then(function (page) {
          if (!root || chart !== drawnChart || !activeRun || activeRun.id !== runId ||
              !page.items.length || page.items[0].signal_time !== signalTime) return;
          var items = byTime.get(signalTime);
          if (!items) return;
          items[0] = "Señal al cierre: " + ruleSummary(page.items[0].explanation);
          signalIndices.delete(signalTime);
          if (hoverTime === signalTime) root.querySelector("#lab-chart-detail").textContent = items.join(" · ");
        }).catch(function () {
          if (!root || chart !== drawnChart || !activeRun || activeRun.id !== runId) return;
          var items = byTime.get(signalTime);
          if (!items) return;
          items[0] = "Señal al cierre: no se pudo cargar la condición; mueve el cursor para reintentar";
          if (hoverTime === signalTime) root.querySelector("#lab-chart-detail").textContent = items.join(" · ");
        }).finally(function () { loadingSignals.delete(signalTime); });
      }, 120);
    });
    historyControls(root.querySelector("#lab-run-history"), chart, candles,
      "orbis.chartRange.labPrice");
    if (candlesWindowed) {
      var candleStatus = root.querySelector("#lab-candle-status");
      var candleTimer = null, wantedCandles = null, failedCandles = null, drawnPriceChart = chart;
      function candleCurrent() {
        return root && chart === drawnPriceChart && activeRun && activeRun.id === runId && target.isConnected;
      }
      function scheduleCandles() {
        if (suppressCandleRange) return;
        if (candleTimer) clearTimeout(candleTimer);
        candleTimer = setTimeout(updateCandles, 80);
      }
      function updateCandles() {
        candleTimer = null;
        if (!candleCurrent()) return;
        var bounds = markerBounds();
        if (!bounds || bounds.key === wantedCandles || bounds.key === failedCandles) return;
        var key = bounds.key, fromTime = bounds.fromTime, toTime = bounds.toTime;
        wantedCandles = key; failedCandles = null;
        loadedCandleWindow = requestedWindow = failedWindow = null;
        markerStatus.hidden = true;
        if (windowed) { byTime.clear(); signalIndices.clear(); }
        target.style.visibility = "hidden";
        target.dataset.candleVisibleCount = "0";
        markerApi.setMarkers([]);
        target.dataset.markerVisibleCount = "0";
        candleStatus.hidden = false; candleStatus.className = "state loading";
        candleStatus.textContent = "Cargando velas del tramo…";
        var count = bounds.last - bounds.first + 1;
        var url = "/datasets/" + encodeURIComponent(datasetId) + "/candles?from_t=" + fromTime +
          "&to_t=" + toTime + "&view=plot&limit=" + count;
        api(url, {cache:"no-store"}).then(function (page) {
          if (!candleCurrent() || wantedCandles !== key) return;
          if (page.dataset_id !== datasetId || page.dataset_hash !== datasetHash ||
              page.from_t !== fromTime || page.to_t !== toTime || page.has_more !== false ||
              !Array.isArray(page.candles) || page.candles.length !== count) {
            throw new Error("La ventana de velas no coincide con el histórico guardado");
          }
          var points = page.candles.map(function (bar, index) {
            if (bar.t !== candles[bounds.first + index].t || ![bar.o, bar.h, bar.l, bar.c].every(Number.isFinite)) {
              throw new Error("Velas incompletas o desordenadas");
            }
            return {time:bar.t,open:bar.o,high:bar.h,low:bar.l,close:bar.c};
          });
          var visibleRange = drawnPriceChart.timeScale().getVisibleLogicalRange();
          suppressCandleRange = true;
          try {
            series.setData(points);
            if (visibleRange) drawnPriceChart.timeScale().setVisibleLogicalRange(visibleRange);
          } finally {
            setTimeout(function () {
              suppressCandleRange = false;
              if (candleCurrent()) { scheduleCandles(); updateVisibleMarkers(); }
            }, 120);
          }
          failedCandles = null;
          loadedCandleWindow = key;
          target.style.visibility = "";
          target.dataset.candleVisibleCount = String(points.length);
          candleStatus.hidden = true; candleStatus.replaceChildren();
          updateVisibleMarkers();
        }).catch(function (error) {
          if (!candleCurrent() || wantedCandles !== key) return;
          wantedCandles = null; failedCandles = key;
          candleStatus.hidden = false; candleStatus.className = "state error";
          candleStatus.textContent = "No se pudieron cargar las velas del tramo: " + error.message + " ";
          var retry = document.createElement("button");
          retry.type = "button"; retry.className = "ghost"; retry.textContent = "Reintentar velas";
          retry.addEventListener("click", function () { failedCandles = null; updateCandles(); });
          candleStatus.appendChild(retry);
        });
      }
      chart.timeScale().subscribeVisibleLogicalRangeChange(scheduleCandles);
      scheduleCandles();
    } else if (windowed || markers.length > 1500) updateVisibleMarkers();
    var capitalTarget = root.querySelector("#lab-equity-chart");
    if (capitalTarget && equity && drawdown) {
      equityChart = lw.createChart(capitalTarget, {autoSize:true, height:240,
        layout:{background:{type:"solid",color:"#101b18"},textColor:"#c9d6ce"},
        grid:{vertLines:{color:"#24362f"},horzLines:{color:"#24362f"}},
        timeScale:{timeVisible:true,minBarSpacing:0.001,lockVisibleTimeRangeOnResize:true}});
      var capital = equityChart.addSeries(lw.LineSeries, {color:"#88b8ff",lineWidth:2,priceLineVisible:false}, 0);
      capital.setData(curvesWindowed ? [] : equity.map(function (point) { return {time:point.t,value:Number(point.equity)}; }));
      if (curvesWindowed) {
        // Un eje temporal completo evita que setData de un tramo convierta
        // «Todo disponible» en «sólo el tramo que se acaba de cargar».
        var timeline = equityChart.addSeries(lw.LineSeries, {
          color:"rgba(0,0,0,0)",priceLineVisible:false,lastValueVisible:false,
          crosshairMarkerVisible:false}, 1);
        timeline.setData(candles.map(function (bar) { return {time:bar.t,value:0}; }));
      }
      var dd = equityChart.addSeries(lw.LineSeries, {color:"#ff7b78",lineWidth:1,priceLineVisible:false,title:"Drawdown %"}, 1);
      dd.setData(curvesWindowed ? [] : drawdown.map(function (point) { return {time:point.t,value:Number(point.drawdown_pct)}; }));
      equityChart.panes()[1].setHeight(70);
      equityChart.timeScale().fitContent();
      if (curvesWindowed) {
        var drawnEquityChart = equityChart, curveStatus = root.querySelector("#lab-curve-status");
        var curveTimer = null, wantedCurve = null, failedCurve = null, suppressCurveRange = false;
        function curveCurrent() {
          return root && equityChart === drawnEquityChart && activeRun && activeRun.id === runId &&
            capitalTarget.isConnected;
        }
        function curveBounds() {
          var range = drawnEquityChart.timeScale().getVisibleLogicalRange();
          if (!range || !candles.length) return null;
          var first = Math.max(0, Math.floor((range.from - 40) / 200) * 200);
          var last = Math.min(candles.length - 1, Math.ceil((range.to + 41) / 200) * 200 - 1);
          return last < first ? null : {first:first,fromTime:candles[first].t,toTime:candles[last].t,
            expectedCount:last - first + 1, key:candles[first].t + ":" + candles[last].t};
        }
        function scheduleCurves() {
          if (suppressCurveRange) return;
          if (curveTimer) clearTimeout(curveTimer);
          curveTimer = setTimeout(updateCurves, 80);
        }
        function updateCurves() {
          curveTimer = null;
          if (!curveCurrent()) return;
          var bounds = curveBounds();
          if (!bounds || bounds.key === wantedCurve || bounds.key === failedCurve) return;
          var key = bounds.key, fromTime = bounds.fromTime, toTime = bounds.toTime;
          wantedCurve = key; failedCurve = null;
          capitalTarget.style.visibility = "hidden";
          capitalTarget.dataset.curveVisibleCount = "0";
          curveStatus.hidden = false; curveStatus.className = "state loading";
          curveStatus.textContent = "Cargando curva del tramo…";
          var url = "/runs/" + encodeURIComponent(runId) + "/curves?from_t=" + fromTime +
            "&to_t=" + toTime + "&view=plot&limit=" + bounds.expectedCount;
          api(url, {cache:"no-store"}).then(function (page) {
            if (!curveCurrent() || wantedCurve !== key) return;
            if (page.run_id !== runId || page.from_t !== fromTime || page.to_t !== toTime || page.has_more !== false ||
                !Array.isArray(page.equity) || !Array.isArray(page.drawdown) ||
                page.equity.length !== bounds.expectedCount || page.drawdown.length !== bounds.expectedCount) {
              throw new Error("La ventana de capital no coincide con la solicitada");
            }
            var capitalPoints = [], declinePoints = [];
            for (var index = 0; index < bounds.expectedCount; index++) {
              var equityPoint = page.equity[index], declinePoint = page.drawdown[index];
              var expected = candles[bounds.first + index].t;
              if (equityPoint.t !== expected || declinePoint.t !== expected ||
                  !Number.isFinite(equityPoint.equity) || !Number.isFinite(declinePoint.drawdown_pct)) {
                throw new Error("Puntos de capital incompletos o desordenados");
              }
              capitalPoints.push({time:expected,value:equityPoint.equity});
              declinePoints.push({time:expected,value:declinePoint.drawdown_pct});
            }
            failedCurve = null;
            var visibleRange = drawnEquityChart.timeScale().getVisibleLogicalRange();
            suppressCurveRange = true;
            try {
              capital.setData(capitalPoints);
              dd.setData(declinePoints);
              if (visibleRange) drawnEquityChart.timeScale().setVisibleLogicalRange(visibleRange);
            } finally {
              setTimeout(function () {
                suppressCurveRange = false;
                if (curveCurrent()) scheduleCurves();
              }, 120);
            }
            capitalTarget.style.visibility = "";
            capitalTarget.dataset.curveVisibleCount = String(capitalPoints.length);
            curveStatus.hidden = true; curveStatus.replaceChildren();
          }).catch(function (error) {
            if (!curveCurrent() || wantedCurve !== key) return;
            wantedCurve = null; failedCurve = key;
            curveStatus.hidden = false; curveStatus.className = "state error";
            curveStatus.textContent = "No se pudo cargar la curva del tramo: " + error.message + " ";
            var retry = document.createElement("button");
            retry.type = "button"; retry.className = "ghost"; retry.textContent = "Reintentar curva";
            retry.addEventListener("click", function () { failedCurve = null; updateCurves(); });
            curveStatus.appendChild(retry);
          });
        }
        drawnEquityChart.timeScale().subscribeVisibleLogicalRangeChange(scheduleCurves);
      }
      if (window.OrbisChartRange) window.OrbisChartRange.mount(root.querySelector("#lab-equity-range"), equityChart,
        curvesWindowed ? candles : equity, {
        storageKey:"orbis.chartRange.labEquity", selected:curvesWindowed ? "1" : "all"
      });
      if (curvesWindowed) scheduleCurves();
    }
  }
  function cleanup() {
    if (perpOptimizationWidget) { perpOptimizationWidget.dispose(); perpOptimizationWidget = null; }
    if (aiArchiveWidget) { aiArchiveWidget.dispose(); aiArchiveWidget = null; }
    if (chart) { chart.remove(); chart = null; }
    if (equityChart) { equityChart.remove(); equityChart = null; }
    clearResearchChart();
    activeDataset = null;
    activeRun = null;
    activeResearch = null;
    researchCandles = [];
    researchPage = 0;
    root = null;
  }
  window.OrbisLab = { render:render, cleanup:cleanup };
})();
