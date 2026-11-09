/* Bots MAINNET: revisión de un uso, autoridad acotada e historial cifrado. */
(function () {
  "use strict";
  var root = null, api = null, epoch = 0, timer = null, busy = false, reading = false;
  var reviewed = null, items = [], uncertain = new Set(), offset = 0, hasMore = false, rendered = null;
  var sourceItems = [], sourceOffset = 0, sourceMore = false, perpEnabled = false;
  var handoffId = null, sourceSequence = 0;
  var base = "/exchanges/grvt/mainnet/bots";
  function node(tag, text, cls) { var el = document.createElement(tag); el.textContent = text; if (cls) el.className = cls; return el; }
  function field(name) { return root.querySelector('[name="bot_' + name + '"]'); }
  function perpMode() { return !!root && field("model").value === "PERPETUAL"; }
  function supervisionLabel(value) {
    return {POSITION_OPEN:"Posición abierta",AWAITING_CONFIRMED_QUOTE:"Esperando cotización",EXIT_RULE:"Regla de salida",TIME_LIMIT:"Permanencia",
      STOP_OR_TRAILING:"Stop / trailing",TAKE_PROFIT:"Objetivo",DATA_GAP:"Datos interrumpidos",DATA_STALE:"Datos antiguos",
      SOURCE_ERROR:"Fuente interrumpida",ARCHIVE_ERROR:"Registro sin confirmar",ARCHIVE_DELAY:"Registro demorado",
      LEVERAGE_MISMATCH:"Apalancamiento cambiado",LOSS_LIMIT:"Umbral de pérdida",FEE_CEILING_EXCEEDED:"Comisión superior al techo"}[value] || value || "Esperando entrada";
  }
  function valid(host, issued, context) { return root === host && host.isConnected && epoch === issued && (context === undefined || api.context() === context); }
  function say(text, error) { if (!root) return; var el = root.querySelector('[data-bot-notice]'); el.textContent = text; el.className = "state " + (error ? "error" : "muted"); }
  function clear() {
    epoch += 1; if (timer !== null) clearTimeout(timer);
    sourceSequence += 1;
    root = null; api = null; timer = null; busy = false; reading = false; reviewed = null; items = []; sourceItems = []; sourceOffset = 0; sourceMore = false; perpEnabled = false; uncertain = new Set(); offset = 0; hasMore = false; rendered = null;
    handoffId = null;
  }
  function invalidate() {
    reviewed = null;
    if (root) {
      root.querySelector('[data-bot-review]').hidden = true;
      root.querySelector('[data-bot-evidence]').replaceChildren();
      field("authorize_bot").checked = false;
      guards();
    }
  }
  function changedSub() { epoch += 1; busy = false; reading = false; items = []; uncertain = new Set(); offset = 0; rendered = null; invalidate(); if (root) { field("dedicated").checked = false; render(); guards(); } }
  function cancel() { epoch += 1; busy = false; invalidate(); }
  function scope() { return {sub_account_id:api.sub(),permission_revision:api.revision()}; }
  function label(status) {
    return {WAITING_SIGNAL:"Esperando señal",PREPARING_ENTRY:"Preparando entrada",RECOVERING_ENTRY:"Comprobando entrada",WATCHING:"Siguiendo posición",
      CANCELLING:"Cancelando protecciones",AUDITING:"Verificando saldo",REDUCING:"Cerrando residuo",VERIFYING:"Conciliando",RECOVERING:"Consultando cierre",
      ENTRY_LIMIT:"Límite de entradas",BUDGET_LIMIT:"Presupuesto agotado",LOSS_LIMIT:"Límite de pérdida",COMPLETE:"Completado",STOPPED:"Detenido · revisar posición",
      REVIEW:"Revisión necesaria",EXPIRED:"Permiso vencido",INTERRUPTED:"Interrumpido · revisar registro",UNCONFIRMED:"Activación sin confirmar"}[status] || "Registrado";
  }
  function date(ns) { return new Date(Number(BigInt(ns) / 1000000n)).toLocaleString(); }
  function guards() {
    if (!root) return;
    var allowed = api.canSend(), pending = items.some(function (item) { return item.active; }) || uncertain.size > 0;
    root.querySelectorAll('[data-bot-form] input,[data-bot-form] select').forEach(function (el) {
      el.disabled = busy || !allowed || !!el.closest(perpMode() ? '[data-bot-price-only]' : '[data-bot-perp-only]');
    });
    field("model").querySelector('[value="PERPETUAL"]').disabled = !perpEnabled;
    root.querySelector('[data-bot-prepare]').disabled = busy || api.isBusy() || !allowed || !api.canSign() || !field("session_id").value ||
      !!(handoffId && (!perpEnabled || !perpMode() || field("session_id").value !== handoffId)) ||
      (perpMode() ? !field("dedicated").checked : !field("research").value) || pending;
    root.querySelector('[data-bot-sources]').disabled = busy || !allowed;
    root.querySelector('[data-bot-handoff]').hidden = !handoffId;
    root.querySelector('[data-bot-discard-source]').disabled = busy || api.isBusy();
    root.querySelector('[data-bot-sources]').textContent = handoffId ? "Verificar sesión seleccionada" : "Sesiones PAPER";
    root.querySelector('[data-bot-refresh]').disabled = busy || !allowed || !api.sub();
    root.querySelector('[data-bot-activate]').disabled = busy || api.isBusy() || !allowed || !api.canSign() || !reviewed ||
      !field("authorize_bot").checked || BigInt(reviewed.review.expires_at_ns) <= BigInt(Date.now()) * 1000000n;
    root.querySelector('[data-bot-prev]').disabled = busy || offset === 0;
    root.querySelector('[data-bot-next]').disabled = busy || !hasMore;
    root.querySelector('[data-bot-source-prev]').hidden = !perpMode() || sourceOffset === 0;
    root.querySelector('[data-bot-source-next]').hidden = !perpMode() || !sourceMore;
    root.querySelector('[data-bot-source-prev]').disabled = busy;
    root.querySelector('[data-bot-source-next]').disabled = busy;
    root.querySelectorAll('[data-bot-run-stop]').forEach(function (button) { button.disabled = !allowed; });
    root.querySelectorAll('[data-bot-market]').forEach(function (el) {
      var item = items.find(function (row) { return row.id === el.dataset.botMarket; });
      el.textContent = item && item.supervision ? (item.supervision.halt_reason ? supervisionLabel(item.supervision.halt_reason) :
        !item.active && item.financial_complete ? "Posición conciliada" : supervisionLabel(item.supervision.exit && item.supervision.exit.reason)) :
        item && item.last_decision ? "Mark · " + item.last_decision.mark_price : "";
    });
  }
  function render() {
    if (!root) return;
    var signature = JSON.stringify({sub:api.sub(),visible:api.canSend(),unknown:Array.from(uncertain),items:items.map(function (item) {
      return {id:item.id,status:item.status,active:item.active,policy:item.policy,source:item.source,research:item.research_binding,entries:item.entry_count,completed:item.completed_entries,
        net:item.net_realized_pnl_usdt,reserved:item.entry_budget_reserved,parent:item.parent_client_order_id,deadline:item.deadline_ns,
        archive_error:item.archive_error,supervision:item.supervision,entry:item.last_decision && item.last_decision.entry_price,
        stop:item.last_decision && item.last_decision.stop_price,target:item.last_decision && item.last_decision.target_price,selections:item.selections.map(function (row) {
          return {parent:row.parent_client_order_id,settled:row.settlement};
        })};
    })});
    if (rendered === signature) { guards(); return; } rendered = signature;
    var list = root.querySelector('[data-bot-items]'); list.replaceChildren();
    if (!api.canSend()) { list.append(node("p", "Importes ocultos", "muted")); return; }
    if (!items.length) list.append(node("p", uncertain.size ? "Consultando activación…" : "Sin bots registrados", "muted"));
    items.forEach(function (item) {
      var card = node("article", "", "grvt-mainnet-group"); card.dataset.mainnetBotRun = item.id;
      var head = node("div", "", "real-mexc-head");
      var statusText = label(item.status);
      if (item.status === "STOPPED") statusText = item.financial_complete ? "Detenido · conciliado" : item.entry_count ? statusText : "Detenido";
      head.append(node("strong", item.source.symbol + " · " + item.source.definition.direction),node("span", statusText, "statusbadge off")); card.append(head);
      var metrics = node("div", "", "grvt-mainnet-metrics");
      [["Entradas",item.entry_count + "/" + item.policy.max_entries],["Presupuesto reservado",item.entry_budget_reserved + " USDT"],
        ["Neto conciliado",item.completed_entries ? item.net_realized_pnl_usdt + " USDT" : "Pendiente"]].forEach(function (pair) {
        var cell = node("div", ""); cell.append(node("small", pair[0], "muted"),node("strong", pair[1])); metrics.append(cell);
      }); card.append(metrics);
      if (item.external_closures) card.append(node("small", "Cierre conciliado fuera del bot", "muted"));
      if (item.terms) card.append(node("small", item.terms.profile.margin_mode + " · " + item.terms.profile.leverage + "× · " + item.source.timeframe, "grvt-oco-id"));
      if (item.last_decision && !item.supervision) card.append(node("small", "Entrada media · " + item.last_decision.entry_price + " · SL " + item.last_decision.stop_price + " · TP " + item.last_decision.target_price, "grvt-oco-id"));
      var mark = node("small", "", "muted"); mark.dataset.botMarket = item.id; card.append(mark);
      var details = document.createElement("details"); details.append(node("summary", "Registro"));
      details.append(node("small", "ID · " + item.id, "grvt-oco-id"),node("small", "Versión · " + item.source.version_id, "grvt-oco-id"),
        node("small", "Permiso hasta · " + date(item.deadline_ns), "grvt-oco-id"));
      var research = item.source.research || item.research_binding && item.research_binding.research;
      var evidence = node("small",research ? "Reserva · " + research.artifact_id : "Legado · sin reserva","grvt-oco-id");
      evidence.dataset.help = item.source.research ? "Validación perpetua experimental con reglas actuales y un prefijo PAPER observado. El capital REAL se autoriza por separado." : research ? "Validación de reglas de precio y señales PAPER comprobadas al autorizar. El histórico no modela margen, liquidación ni financiación de derivados." :
        "Autorización anterior al vínculo de investigación; se conserva su interpretación original.";
      details.append(evidence);
      item.selections.forEach(function (selection) { details.append(node("small", "Entrada · " + selection.parent_client_order_id + (selection.settlement ? " · conciliada" : " · pendiente"), "grvt-oco-id")); });
      card.append(details);
      if (item.archive_error) card.append(node("p", "Registro pendiente · revisar tras desbloquear", "state error"));
      var controls = node("div", "", "row");
      if (item.parent_client_order_id) {
        var inspect = node("button", "Ver operación", "ghost"); inspect.type = "button";
        inspect.addEventListener("click", function () { api.inspect(item.parent_client_order_id); }); controls.append(inspect);
      }
      if (item.active || item.archive_error || item.status === "INTERRUPTED") {
        var stop = node("button", "Detener bot", "ghost"); stop.type = "button"; stop.dataset.botRunStop = item.id;
        stop.addEventListener("click", stopSub); controls.append(stop);
      }
      card.append(controls); list.append(card);
    }); guards();
  }
  function merge(item) { if (!item) return; uncertain.delete(item.id); var at = items.findIndex(function (row) { return row.id === item.id; }); if (at < 0) items.unshift(item); else items[at] = item; render(); }
  async function fetchRun(id) {
    var host = root, issued = epoch, context = api.context();
    var data = await api.request(base + "/" + id, {}, true);
    if (valid(host, issued, context) && data.item) merge(data.item);
  }
  async function refresh() {
    if (!api.canSend() || !/^[1-9][0-9]{0,19}$/.test(api.sub())) return;
    var host = root, issued = epoch, context = api.context();
    var data = await api.request(base + "?sub_account_id=" + encodeURIComponent(api.sub()) + "&offset=" + offset, {}, true);
    if (!valid(host, issued, context)) return;
    if (data.history_pending) data.items.forEach(merge);
    else { items = data.items; hasMore = data.has_more; items.forEach(function (item) { uncertain.delete(item.id); }); render(); }
    for (var id of Array.from(uncertain)) { try { await fetchRun(id); } catch (_) { /* No se reenvía una activación incierta. */ } }
    guards();
  }
  async function sources() {
    var host = root, issued = epoch, context = api.context(), selected = handoffId;
    var serial = ++sourceSequence;
    function currentSource() {
      if (serial !== sourceSequence || selected !== handoffId) return false;
      // Source identity is independent of the GRVT subaccount and its Trade revision.
      return selected ? root === host && host.isConnected && api.canSend() : valid(host,issued,context);
    }
    if (selected) {
      sourceItems = [];
      field("session_id").replaceChildren(new Option("Elegir sesión PAPER", ""));
      invalidate();
    }
    var data, perpetual;
    try {
      var capabilities = await api.request(base + "/capabilities", {}, true).catch(function (error) {
        if (selected) throw error;
        return null;
      });
      if (!currentSource()) return;
      if (capabilities) perpEnabled = capabilities.supervised_perpetual === true;
      perpetual = perpMode();
      if (selected && (!perpEnabled || !perpetual)) throw new Error("Preparación MARKET · no disponible");
      var path = selected ? "/perp/sources?session_id=" + encodeURIComponent(selected) :
        perpetual ? "/perp/sources?offset=" + sourceOffset : "/sources";
      data = await api.request(base + path, {}, true);
      if (!currentSource()) return;
      if (selected && (data.execution_authority !== "NONE" || !Array.isArray(data.items) || data.items.length > 1 ||
          data.items.some(function (item) { return !item || item.session_id !== selected; })))
        throw new Error("No se pudo verificar la sesión seleccionada");
      if (!Array.isArray(data.items) || !data.items.every(function (source) { return source && typeof source.session_id === "string" &&
        (perpetual ? source.verification === "ON_PREVIEW_AND_START" && ["LONG","SHORT"].includes(source.direction) : Array.isArray(source.research_options) && source.research_options.every(function (option) {
          return option && typeof option.id === "string" && typeof option.created_at === "string" && Number.isInteger(option.test_bars) && option.verification === "ON_PREVIEW_AND_START";
        })); })) throw new Error("Fuentes de investigación no verificables");
    } catch (error) {
      if (!selected) throw error;
      if (currentSource()) say(error.message,true);
      return;
    }
    sourceMore = perpetual && data.has_more === true;
    sourceItems = data.items;
    var select = field("session_id"), previous = select.value; select.replaceChildren();
    var first = node("option", "Elegir sesión PAPER"); first.value = ""; select.append(first);
    data.items.forEach(function (source) {
      var option = node("option", source.symbol + " · " + source.direction + " · " + source.timeframe +
        (perpetual ? " · " + source.margin_mode + " " + source.leverage + "×" : source.paper_ready ? "" : " · revisar PAPER"));
      option.value = source.session_id; option.title = source.version_id; select.append(option);
    });
    var wanted = selected || previous;
    if (data.items.some(function (source) { return source.session_id === wanted; })) select.value = wanted;
    researchOptions();
    say(data.items.length ? (selected ? "Sesión PAPER seleccionada" : "Sesiones disponibles") :
      selected ? "Sesión PAPER MARKET · no disponible" :
      perpetual ? "Primero completa una prueba PAPER observada" : "No hay sesiones AUTO PAPER activas"); guards();
  }
  function researchOptions() {
    var source = sourceItems.find(function (item) { return item.session_id === field("session_id").value; });
    var select = field("research"), previous = select.value;
    select.replaceChildren(new Option("Elegir reserva", ""));
    (source && source.research_options || []).forEach(function (item) {
      select.add(new Option(item.created_at.slice(0,10) + " · " + item.test_bars + " velas · " + item.id.slice(0,8),item.id));
    });
    if (previous && [].some.call(select.options,function (option) { return option.value === previous; })) select.value = previous;
    else if (select.options.length === 2) select.selectedIndex = 1;
    invalidate();
  }
  function paintModel() {
    sourceOffset = 0; sourceMore = false; sourceItems = []; field("session_id").replaceChildren(new Option("Elegir sesión PAPER", ""));
    field("research").replaceChildren(new Option("Elegir reserva", "")); field("dedicated").checked = false;
    root.querySelectorAll('[data-bot-perp-only]').forEach(function (el) { el.hidden = !perpMode(); });
    root.querySelectorAll('[data-bot-price-only]').forEach(function (el) { el.hidden = perpMode(); });
    root.querySelector('[data-bot-entry-label]').firstChild.nodeValue = perpMode() ? "Nominal entrada · USDT" : "Tope entrada · USDT";
    root.querySelector('[data-bot-total-label]').firstChild.nodeValue = perpMode() ? "Presupuesto nominal · USDT" : "Presupuesto · USDT";
    field("loss_cap").closest("label").title = perpMode() ? "Umbral de vigilancia para detener nuevas entradas y solicitar cierre; no garantiza una pérdida máxima" :
      "Detiene nuevas entradas al alcanzar esta pérdida neta conciliada";
    invalidate();
  }
  function modelChanged() {
    handoffId = null;
    paintModel(); run(sources);
  }
  async function run(task) {
    if (busy || !api.canSend() || api.isBusy()) return;
    var host = root, issued = ++epoch, context = api.context(); busy = true; guards(); api.guard();
    try { await task(host, issued, context); }
    catch (error) { if (valid(host, issued, context)) say(error.message, true); }
    finally { if (valid(host, issued)) { busy = false; guards(); api.guard(); } }
  }
  async function stopSub() {
    if (!api.canSend()) return;
    var host = root, issued = ++epoch; busy = true; invalidate(); api.invalidateManual(); guards(); api.guard();
    try {
      await api.request(base + "/stop", {method:"POST",body:JSON.stringify(Object.assign(scope(), {confirmation:"DETENER_BOT_GRVT_MAINNET"}))});
      if (!valid(host, issued)) return;
      uncertain.clear(); await api.refreshParent(); await refresh(); say("Bot detenido · revisa la posición existente");
    } catch (error) { if (valid(host, issued)) say(error.message, true); }
    finally { if (valid(host, issued)) { busy = false; guards(); api.guard(); } }
  }
  function schedule() {
    timer = setTimeout(async function () {
      if (!root || !root.isConnected) { clear(); return; }
      var host = root;
      if (!busy && !reading && !api.isBusy() && api.canSend() && root.open) {
        reading = true;
        try { await refresh(); } catch (_) { if (root === host) say("Seguimiento interrumpido · consultar registro", true); }
        finally { if (root === host) reading = false; }
      }
      if (root) schedule();
    }, 3000);
  }
  function mount(host, services) {
    clear(); if (!host) return; root = host; api = services;
    root.innerHTML = '<summary>Bot · MAINNET</summary><div class="grvt-mainnet-body">' +
      '<div class="row" data-bot-handoff hidden><span class="statusbadge off">PAPER → REAL · MARKET</span>'+
      '<button type="button" class="ghost" data-bot-discard-source>Quitar selección</button></div>'+
      '<div class="row"><button type="button" class="ghost" data-bot-sources>Sesiones PAPER</button><button type="button" class="ghost" data-bot-refresh>Consultar bots</button></div>' +
      '<form data-bot-form><div class="grvt-mainnet-fields"><label class="field">Modelo<select name="bot_model"><option value="PRICE_RULES">Reglas de precio</option><option value="PERPETUAL" disabled>Perpetuo · margen</option></select></label>' +
      '<label class="field">Sesión PAPER<select name="bot_session_id" required><option value="">Elegir sesión PAPER</option></select></label>' +
      '<label class="field" data-bot-price-only data-help="Reserva de la misma versión y mercado público GRVT, anterior a PAPER. Se repiten backtest, reserva y señales observadas al revisar y activar.">Reserva<select name="bot_research" required><option value="">Elegir reserva</option></select></label>' +
      '<a href="#/laboratorio" data-bot-price-only data-help="Guarda una validación fuera de muestra con datos públicos GRVT.">Validar</a>' +
      '<label class="field" data-bot-perp-only hidden data-help="Límite de todo el colateral USDT de la subcuenta dedicada. En CROSS también respalda la posición.">Colateral · USDT<input name="bot_collateral_limit" inputmode="decimal" value="100" required></label>' +
      '<label class="field" data-bot-perp-only hidden data-help="Máximo de efectivo para margen, colchón adverso y comisiones por entrada. El tamaño se calcula con equity REAL, asignación y apalancamiento.">Efectivo por entrada · USDT<input name="bot_entry_cash_limit" inputmode="decimal" value="50" required></label>' +
      '<label class="field" data-bot-entry-label>Tope entrada · USDT<input name="bot_entry_cap" inputmode="decimal" value="150" required></label>' +
      '<label class="field">Tope salida · USDT<input name="bot_exit_cap" inputmode="decimal" value="200" required></label>' +
      '<label class="field" data-bot-total-label>Presupuesto · USDT<input name="bot_total_entry_cap" inputmode="decimal" value="450" required></label>' +
      '<label class="field" title="Detiene nuevas entradas al alcanzar esta pérdida neta conciliada">Umbral de pérdida · USDT<input name="bot_loss_cap" inputmode="decimal" value="15" required></label>' +
      '<label class="field">Entradas máximas<input name="bot_max_entries" type="number" min="1" max="20" value="3" required></label>' +
      '<label class="field">Esperar señales · min<input name="bot_wait_minutes" type="number" min="1" max="1400" value="60" required></label>' +
      '<label class="field">Vigencia SL/TP · min<input name="bot_exit_minutes" type="number" min="2" max="1400" value="60" required></label></div>' +
      '<div class="row"><button type="button" class="ghost" data-bot-source-prev hidden>Pruebas anteriores</button><button type="button" class="ghost" data-bot-source-next hidden>Más pruebas</button></div>' +
      '<label class="grvt-mainnet-check" data-bot-perp-only hidden><input name="bot_dedicated" type="checkbox" required>Subcuenta dedicada · todo su colateral</label>' +
      '<details><summary>Ejecución</summary><div class="grvt-mainnet-fields"><label class="field" data-bot-perp-only hidden data-help="Fracción por fill, estimada: 0,001 = 0,1 %. Si las comisiones reales la superan, se detienen nuevas entradas; no fija la tarifa del exchange.">Techo de comisión<input name="bot_fee_rate_ceiling" inputmode="decimal" value="0.001" required></label>' +
      '<label class="field">Recuperación · segundos<input name="bot_recovery_seconds" type="number" min="30" max="300" value="180" required></label>' +
      '<label class="field">Intentos de cierre<input name="bot_max_attempts" type="number" min="1" max="3" value="3" required></label>' +
      '<label class="field">Spread máximo · bps<input name="bot_max_spread_bps" type="number" min="0" max="100" value="25" required></label>' +
      '<label class="field">Deslizamiento máximo · bps<input name="bot_max_slippage_bps" type="number" min="0" max="100" value="25" required></label></div></details>' +
      '<label class="grvt-mainnet-check"><input name="bot_keep_operating" type="checkbox" checked>Continuar con vista protegida</label>' +
      '<button type="submit" data-bot-prepare>Revisar bot</button></form>' +
      '<div data-bot-review class="grvt-mainnet-review" hidden><strong>Autorizar bot · MAINNET</strong><div data-bot-review-values class="grvt-mainnet-metrics"></div><div data-bot-evidence></div>' +
      '<label class="grvt-mainnet-check"><input name="bot_authorize_bot" type="checkbox">Autorizar operaciones reales con estos límites</label>' +
      '<div class="row"><button type="button" data-bot-activate>Activar bot</button><button type="button" class="ghost" data-bot-discard>Descartar</button></div></div>' +
      '<div data-bot-notice class="state muted" role="status">Conecta Trade y elige una sesión PAPER</div><div data-bot-items></div>' +
      '<div class="row"><button type="button" class="ghost" data-bot-prev>Anterior</button><button type="button" class="ghost" data-bot-next>Siguiente</button></div></div>';
    root.querySelector('[data-bot-form]').addEventListener("input", function () { invalidate(); api.invalidateManual(); });
    field("session_id").addEventListener("change", researchOptions);
    field("model").addEventListener("change", modelChanged);
    root.querySelector('[data-bot-discard-source]').addEventListener("click", function () {
      if (!handoffId || busy) return;
      handoffId = null; paintModel(); say("Elige una sesión PAPER");
    });
    ["prev","next"].forEach(function (name) { root.querySelector('[data-bot-source-' + name + ']').addEventListener("click", function () {
      sourceOffset = Math.max(0,sourceOffset + (name === "next" ? 25 : -25)); run(async function () { await sources(); });
    }); });
    field("authorize_bot").addEventListener("change", guards);
    root.querySelector('[data-bot-sources]').addEventListener("click", function () { run(async function () { await sources(); }); });
    root.querySelector('[data-bot-refresh]').addEventListener("click", function () { run(async function () { await refresh(); }); });
    root.querySelector('[data-bot-form]').addEventListener("submit", function (event) {
      event.preventDefault();
      if (root.querySelector('[data-bot-prepare]').disabled || !event.currentTarget.reportValidity()) return;
      run(async function (host, issued, context) {
        invalidate(); api.invalidateManual(); var body = scope();
        body.session_id = field("session_id").value;
        ["entry_cap","exit_cap","total_entry_cap","loss_cap"].forEach(function (name) { body[name] = field(name).value.trim().replace(",", "."); });
        ["max_entries","recovery_seconds","max_attempts","max_spread_bps","max_slippage_bps"].forEach(function (name) { body[name] = Number(field(name).value); });
        body.wait_seconds = Number(field("wait_minutes").value) * 60; body.exit_seconds = Number(field("exit_minutes").value) * 60; body.keep_operating = field("keep_operating").checked;
        var perpetual = perpMode();
        if (perpetual) {
          body.entry_notional_limit = body.entry_cap; body.exit_notional_limit = body.exit_cap; delete body.entry_cap; delete body.exit_cap;
          ["collateral_limit","entry_cash_limit","fee_rate_ceiling"].forEach(function (name) { body[name] = field(name).value.trim().replace(",", "."); });
          body.dedicated_account_confirmed = field("dedicated").checked;
        } else body.research_validation_id = field("research").value;
        var data = await api.request(base + (perpetual ? "/perp" : "") + "/preview", {method:"POST",body:JSON.stringify(body)});
        if (!valid(host, issued, context)) return;
        if (perpetual ? !data.review || data.review.schema !== "orbis.grvt.mainnet.perp.bot.review.v1" ||
            !data.review.terms || !data.review.source || data.review.source.session_id !== body.session_id ||
            data.review.sub_account_id !== body.sub_account_id || !data.review.sample_plan || data.review.execution_authority !== "NONE" :
            !data.review || data.review.schema !== "orbis.grvt.mainnet.bot.review.v2" || !data.review.research_binding ||
            data.review.research_binding.schema !== "orbis.grvt.bot.research.v1" ||
            !data.review.research_binding.research || data.review.research_binding.research.execution_authority !== "NONE") {
          throw new Error("Revisión de investigación no verificable");
        }
        var review = data.review, values = root.querySelector('[data-bot-review-values]');
        var evidence = perpetual ? window.OrbisGrvtPerpReviewEvidence.create(
          review.lab_origin, review.source.research, review.source.paper) : null;
        values.replaceChildren();
        var pairs = perpetual ? [
          ["Mercado",review.source.symbol + " · " + review.source.definition.direction],
          ["Perfil",review.terms.profile.margin_mode + " · " + review.terms.profile.leverage + "×"],
          ["Validación","Experimental · reglas actuales"],
          ["Colateral máximo",review.terms.budget.collateral_limit + " USDT · subcuenta completa"],
          ["Efectivo por entrada",review.terms.budget.entry_cash_limit + " USDT"],
          ["Equity consultado",review.sample_plan.result.account_equity + " USDT"],
          ["Nominal entrada / salida",review.policy.entry_cap + " / " + review.policy.exit_cap + " USDT"],
          ["Presupuesto nominal",review.policy.total_entry_cap + " USDT"],["Umbral de pérdida",review.policy.loss_cap + " USDT"],
          ["Entradas",String(review.policy.max_entries)],["Esperar señales",review.policy.wait_seconds + " s"],
          ["Vigencia SL/TP",review.policy.exit_seconds + " s"],["Techo de comisión",review.terms.budget.fee_rate_ceiling + " · estimado"],
          ["Protección nativa","LAST · límite firmado"],["Seguimiento software","Bid LONG / ask SHORT · fills propios"],
          ["STOP / desconexión","Revoca envíos · revisar posición"],
          ["Vista protegida",review.keep_operating ? "Continúa" : "Se detiene"],["Activación","Siguiente señal nueva"]
        ] : [["Mercado",review.source.symbol + " · " + review.source.direction],
          ["Reserva",review.research_binding.research.artifact_id.slice(0,8)],["Histórico","Sin margen/funding"],
          ["Entrada",review.policy.entry_cap + " USDT"],
          ["Salida",review.policy.exit_cap + " USDT"],["Presupuesto",review.policy.total_entry_cap + " USDT"],
          ["Umbral de pérdida",review.policy.loss_cap + " USDT"],["Entradas",String(review.policy.max_entries)],
          ["Esperar señales",review.policy.wait_seconds + " s"],["Vigencia SL/TP",review.policy.exit_seconds + " s"],
          ["Vista protegida",review.keep_operating ? "Continúa" : "Se detiene"],["Activación","Siguiente señal"]];
        pairs.forEach(function (pair) {
          var cell = node("div", ""); cell.append(node("small", pair[0], "muted"),node("strong", pair[1])); values.append(cell);
        });
        if (evidence) root.querySelector('[data-bot-evidence]').replaceChildren(evidence);
        reviewed = data;
        root.querySelector('[data-bot-review]').hidden = false;
        say("Revisión válida durante 120 s");
      });
    });
    root.querySelector('[data-bot-activate]').addEventListener("click", function () { run(async function (host, issued, context) {
      if (!reviewed || !field("authorize_bot").checked) return;
      var value = reviewed, id = value.review.run_id, revision = api.revision(); invalidate(); uncertain.add(id);
      try {
        var perpetual = value.review.schema === "orbis.grvt.mainnet.perp.bot.review.v1";
        var item = await api.request(base + (perpetual ? "/perp" : "") + "/start", {method:"POST",body:JSON.stringify(Object.assign({}, value, {permission_revision:revision,
          confirmation:perpetual ? "ACTIVAR_BOT_PERPETUO_GRVT_MAINNET" : "ACTIVAR_BOT_GRVT_MAINNET"}))});
        if (valid(host, issued, context)) { merge(item); say("Bot autorizado · esperando señal nueva"); }
      } catch (_) {
        if (!valid(host, issued, context)) return;
        say("Activación sin confirmar · consulta el registro; no la repitas", true);
        try { await fetchRun(id); } catch (_) { /* El ID conocido permite recuperar sin reactivar. */ }
      }
    }); });
    root.querySelector('[data-bot-discard]').addEventListener("click", invalidate);
    ["prev","next"].forEach(function (name) { root.querySelector('[data-bot-' + name + ']').addEventListener("click", function () { run(async function () {
      offset = Math.max(0, offset + (name === "next" ? 20 : -20)); await refresh();
    }); }); });
    render(); guards(); schedule();
    var capHost = root, capEpoch = epoch, capContext = api.context();
    if (api.canSend()) api.request(base + "/capabilities", {}, true).then(function (data) {
      if (valid(capHost,capEpoch,capContext)) { perpEnabled = data.supervised_perpetual === true; guards(); }
    }).catch(function () { /* La opción queda indisponible sin capacidad confirmada. */ });
  }
  function prepare(id) {
    handoffId = id;
    field("model").value = "PERPETUAL";
    paintModel(); say("Verificando sesión PAPER…"); run(sources);
  }
  window.OrbisGrvtMainnetBots = {mount:mount,prepare:prepare,clear:clear,invalidate:invalidate,cancel:cancel,changedSub:changedSub,isBusy:function () { return busy; },guard:guards,ackStop:function () { uncertain.clear(); invalidate(); }};
}());
