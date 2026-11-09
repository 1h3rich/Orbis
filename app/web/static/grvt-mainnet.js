/* GRVT MAINNET: revisión de un uso, envío manual protegido y cierre acotado.
 * Secretos y revisiones sólo en RAM. La consulta periódica nunca envía órdenes. */
(function () {
  "use strict";
  var root = null, api = null, epoch = 0, serial = 0, timer = null, suggestionTimer = null;
  var busy = false, reading = false, state = null, reviewed = null, offset = 0, closing = null;
  var renderedGroups = null;
  var renewing = null;
  var reads = new Set();
  var base = "/exchanges/grvt/mainnet";
  function node(tag, text, cls) {
    var el = document.createElement(tag); el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }
  function field(name) { return root.querySelector('[name="' + name + '"]'); }
  function sub() { return field("sub_account_id").value.trim(); }
  function secrets(host) {
    if (host) host.querySelectorAll('[data-mainnet-secret]').forEach(function (el) { el.value = ""; });
  }
  function moduleBusy(module) { return !!(module && module.isBusy()); }
  function clear() {
    reads.forEach(function (controller) { controller.abort(); }); reads.clear();
    if (window.OrbisGrvtMainnetChart) window.OrbisGrvtMainnetChart.clear();
    if (window.OrbisGrvtMainnetBots) window.OrbisGrvtMainnetBots.clear();
    if (window.OrbisGrvtMainnetLimit) window.OrbisGrvtMainnetLimit.clear();
    epoch += 1; serial += 1; secrets(root);
    if (timer !== null) clearTimeout(timer);
    if (suggestionTimer !== null) clearTimeout(suggestionTimer);
    root = null; api = null; timer = null; suggestionTimer = null; busy = false; reading = false; state = null; reviewed = null; closing = null; renewing = null; offset = 0; renderedGroups = null;
  }
  function current(host, issued) { return root === host && host.isConnected && epoch === issued; }
  function notice(text, error) {
    if (!root) return;
    var el = root.querySelector('[data-mainnet-state]'); el.textContent = text; el.className = "state " + (error ? "error" : "muted");
  }
  function invalidate() {
    reviewed = null;
    if (window.OrbisGrvtMainnetBots) window.OrbisGrvtMainnetBots.invalidate();
    if (window.OrbisGrvtMainnetLimit) window.OrbisGrvtMainnetLimit.invalidate();
    if (!root) return;
    root.querySelector('[data-mainnet-review]').hidden = true;
    field("authorize_entry").checked = false;
    if (renewing) renewing.review = null;
    root.querySelector('[data-mainnet-renew-review]').hidden = true;
    field("authorize_renewal").checked = false;
    guards();
  }
  function revision() { if (!state) throw new Error("Consulta la subcuenta"); return state.permission_revision.slice(); }
  function scope() { return {sub_account_id:sub(), permission_revision:revision()}; }
  async function readOnly(path, passive) {
    // Una lectura sin respuesta no debe retener busy/reading indefinidamente.
    // Cancelar el GET permite al sondeo recuperar el diario; nunca repite POST.
    var controller = new AbortController(); reads.add(controller);
    var deadline = setTimeout(function () { controller.abort(); }, 15000);
    try { return await api.request(path, {signal:controller.signal}, !!passive); }
    catch (error) {
      if (controller.signal.aborted) throw new Error("Consulta sin respuesta · seguimiento pendiente");
      throw error;
    } finally { clearTimeout(deadline); reads.delete(controller); }
  }
  function guards() {
    if (!root) return;
    var acting = busy || moduleBusy(window.OrbisGrvtMainnetBots) || moduleBusy(window.OrbisGrvtMainnetLimit);
    var allowed = api.canSend(), connected = !!(state && state.connected), sign = !!(state && state.can_sign);
    var stopped = !!(state && state.stop && state.stop.stopped);
    root.querySelectorAll('[data-mainnet-order] input,[data-mainnet-order] select').forEach(function (el) { el.disabled = acting || !allowed; });
    root.querySelectorAll('[data-mainnet-renew-form] input').forEach(function (el) { el.disabled = acting || !allowed || !sign; });
    root.querySelectorAll('[data-mainnet-group]').forEach(function (card) {
      var group = state && state.groups.find(function (item) { return item.parent_client_order_id === card.dataset.mainnetGroup; });
      var consult = card.querySelector('[data-mainnet-consult]'), close = card.querySelector('[data-mainnet-close-position]');
      if (consult) consult.disabled = acting || !allowed || !connected || group.job.active;
      if (close) close.disabled = acting || !allowed || !sign || group.job.active;
      var limits = card.querySelector('[data-mainnet-limit]');
      if (limits) limits.disabled = acting || !allowed || !sign || group.job.active;
    });
    root.querySelector('[data-mainnet-connect]').disabled = acting || !allowed;
    root.querySelector('[data-mainnet-connect-view]').disabled = acting || !allowed || sign;
    root.querySelector('[data-mainnet-refresh]').disabled = acting || !allowed;
    root.querySelector('[data-mainnet-disconnect]').disabled = acting || !allowed || !connected;
    root.querySelector('[data-mainnet-stop]').disabled = !allowed; // STOP también durante una petición en curso.
    root.querySelector('[data-mainnet-size]').disabled = acting || !allowed || !connected;
    root.querySelector('[data-mainnet-preview]').disabled = acting || !allowed || !sign || stopped;
    root.querySelector('[data-mainnet-send]').disabled = acting || !allowed || !sign || stopped || !reviewed ||
      !field("authorize_entry").checked || BigInt(reviewed.review.expires_at_ns) <= BigInt(Date.now()) * 1000000n;
    if (reviewed && BigInt(reviewed.review.expires_at_ns) <= BigInt(Date.now()) * 1000000n) notice("Revisión caducada · vuelve a revisar");
    root.querySelector('[data-mainnet-rearm]').hidden = !stopped;
    root.querySelector('[data-mainnet-rearm]').disabled = acting || !allowed || !sign || !stopped ||
      state.groups.some(function (group) { return !group.result; });
    root.querySelector('[data-mainnet-connection]').textContent = !allowed ? "Importes ocultos" : stopped ? "STOP activo" :
      sign ? "Firma conectada" : connected ? "Sólo lectura" : "Desconectado";
    root.querySelector('[data-mainnet-confirm-close]').disabled = acting || !allowed || !sign || !field("authorize_close").checked;
    root.querySelector('[data-mainnet-renew-prepare]').disabled = acting || !allowed || !sign || !renewing;
    root.querySelector('[data-mainnet-renew-send]').disabled = acting || !allowed || !sign || !renewing || !renewing.review ||
      !field("authorize_renewal").checked || BigInt(renewing.review.review.expires_at_ns) <= BigInt(Date.now()) * 1000000n;
    root.querySelector('[data-mainnet-prev]').disabled = acting || offset === 0;
    root.querySelector('[data-mainnet-next]').disabled = acting || !state || !state.has_more;
    if (window.OrbisGrvtMainnetBots) window.OrbisGrvtMainnetBots.guard();
    if (window.OrbisGrvtMainnetLimit) window.OrbisGrvtMainnetLimit.guard();
  }
  async function run(task) {
    if (busy || !api.canSend() || moduleBusy(window.OrbisGrvtMainnetBots) || moduleBusy(window.OrbisGrvtMainnetLimit)) return;
    var host = root, issued = ++epoch, op = ++serial; busy = true; guards();
    try { await task(host, issued); }
    catch (error) { if (current(host, issued) && op === serial) notice(error.message, true); }
    finally { if (current(host, issued) && op === serial) { busy = false; guards(); } }
  }
  function label(job) {
    return {WAITING_SIGNAL:"Esperando señal",PREPARING_ENTRY:"Preparando entrada",RECOVERING_ENTRY:"Comprobando entrada",WATCHING:"Siguiendo posición",
      PREPARING:"Preparando cierre",CANCELLING:"Cancelando protecciones",RECOVERING:"Consultando cierre",
      AUDITING:"Verificando saldo",VERIFYING:"Conciliando",REDUCING:"Cerrando residuo",COMPLETE:"Completado",
      REVIEW:"Revisión necesaria",EXPIRED:"Permiso vencido",STOPPED:"Detenido · revisar posición",ATTEMPT_LIMIT:"Límite alcanzado · revisar residuo"}[job.status] || "";
  }
  function openRenewal(group) {
    renewing = {parent:group.parent_client_order_id,review:null}; invalidate(); closing = null;
    field("renew_exit_cap").value = group.recovery && group.recovery.exit_cap || group.exit_cap;
    root.querySelector('[data-mainnet-close]').hidden = true;
    root.querySelector('[data-mainnet-renew]').hidden = false;
    root.querySelector('[data-mainnet-renew-prepare]').focus();
  }
  function renderGroups() {
    var presentation = JSON.stringify({visible:api.canSend(),sub:sub(),pending:!!(state && state.history_pending && !state.groups.length),
      groups:state ? state.groups : null,expired:state ? state.groups.map(function (group) {
        return BigInt(group.exit_expiration_ns) <= BigInt(Date.now()) * 1000000n;
      }) : []}, function (key, value) { return ["observed_at_ns","create_time_ns","update_time_ns"].includes(key) ? undefined : value; });
    if (presentation === renderedGroups) { guards(); return; }
    renderedGroups = presentation;
    var list = root.querySelector('[data-mainnet-groups]'); list.replaceChildren();
    if (!api.canSend()) { list.append(node("p", "Importes ocultos", "muted")); return; }
    if (!state || !state.groups.length) { list.append(node("p", state && state.history_pending ? "Consultando registro…" : "Sin operaciones registradas", "muted")); return; }
    state.groups.forEach(function (group) {
      var card = node("article", "", "grvt-mainnet-group"); card.dataset.mainnetGroup = group.parent_client_order_id;
      var head = node("div", "", "real-mexc-head");
      head.append(node("strong", group.instrument.replace("_USDT_Perp", "/USDT") + " · " + group.direction),
        node("span", group.result ? "Conciliada" : group.job.active ? label(group.job) : group.status === "ACKNOWLEDGED" ? "Acuse recibido" :
          group.status === "POST_CLAIMED" ? "Envío sin confirmar" : "Observada", "statusbadge off"));
      card.append(head);
      var values = node("div", "", "grvt-mainnet-metrics");
      [["Cantidad",group.size],["Entrada límite",group.entry_price],["SL",group.stop_loss],["TP",group.take_profit]].forEach(function (item) {
        var cell = node("div", ""); cell.append(node("small", item[0], "muted"), node("strong", item[1])); values.append(cell);
      }); card.append(values);
      if (group.state) {
        var statuses = node("div", "", "grvt-mainnet-order-states");
        group.state.orders.forEach(function (order, index) {
          statuses.append(node("span", ["Entrada","SL","TP"][index] + " · " + order.status + " · " + order.traded_size));
        }); card.append(statuses);
      }
      (group.reductions || []).forEach(function (reduction) {
        card.append(node("small", "Cierre IOC · " + (reduction.state ? reduction.state.status + " · " + reduction.state.traded_size : "Ejecución sin confirmar"), "grvt-oco-id"));
      });
      if (group.result) card.append(node("p", "Neto conciliado · " + group.result.net_realized_pnl_usdt + " USDT · Posición cerrada", "grvt-oco-net"));
      else if (group.recovery && typeof group.recovery.remaining_size_from_orders === "string") {
        var remaining = group.recovery.remaining_size_from_orders;
        card.append(node("p", remaining === "0" ? "Cierre según órdenes · conciliación pendiente" :
          "Residuo según órdenes · " + remaining, "state"));
      }
      else if (BigInt(group.exit_expiration_ns) <= BigInt(Date.now()) * 1000000n) card.append(node("p", "Protecciones caducadas · revisar posición", "state error"));
      else if (group.state && group.state.needs_review) card.append(node("p", "Revisión de ejecución necesaria", "state error"));
      if (!group.job.active && group.job.status !== "IDLE") card.append(node("small", label(group.job), "muted"));
      var detail = document.createElement("details"); detail.append(node("summary", "Registro"));
      detail.append(node("small", "ID · " + group.parent_client_order_id, "grvt-oco-id"),
        node("small", "Auditorías · " + group.financial_audit_count, "grvt-oco-id"));
      detail.append(node("small", "Vencimiento SL/TP · " + new Date(Number(BigInt(group.exit_expiration_ns) / 1000000n)).toLocaleString(), "grvt-oco-id"));
      detail.append(node("small", "Tope salida · " + group.exit_cap + " USDT", "grvt-oco-id"));
      if (group.recovery && group.recovery.exit_cap !== group.exit_cap) detail.append(node("small", "Tope de cierre revisado · " + group.recovery.exit_cap + " USDT", "grvt-oco-id"));
      (group.reductions || []).forEach(function (reduction) {
        detail.append(node("small", "IOC · " + reduction.client_order_id + " · " + reduction.size + " · límite " + reduction.limit_price, "grvt-oco-id"));
      });
      if (group.recovery) detail.append(node("small", "Cierre · " + group.recovery.attempts + "/" + group.recovery.max_attempts + " intentos", "grvt-oco-id"));
      card.append(detail);
      var graph = node("button", "Ver gráfico", "ghost"); graph.type = "button";
      graph.addEventListener("click", function () {
        if (api.canSend() && window.OrbisGrvtMainnetChart) window.OrbisGrvtMainnetChart.show({id:group.parent_client_order_id,symbol:group.instrument.replace("_USDT_Perp","/USDT:PERP")});
      }); card.append(graph);
      if (!group.result) {
        var actions = node("div", "", "row");
        var consult = node("button", "Consultar", "ghost"); consult.type = "button";
        consult.dataset.mainnetConsult = "";
        consult.disabled = busy || !state.connected || group.job.active;
        consult.addEventListener("click", function () { run(async function (host, issued) {
          var data = await api.request(base + "/groups/" + group.parent_client_order_id + "/live", {method:"POST",body:JSON.stringify(scope())});
          if (!current(host, issued)) return;
          replaceGroup(data.group); notice(data.group.result ? "Operación conciliada" : "Estado consultado");
        }); });
        var needsRenewal = group.recovery && (BigInt(group.recovery.deadline_ns) <= BigInt(Date.now()) * 1000000n || group.recovery.attempts >= group.recovery.max_attempts);
        var close = node("button", needsRenewal ? "Renovar cierre" : "Cerrar posición", "ghost"); close.type = "button";
        close.dataset.mainnetClosePosition = "";
        close.disabled = busy || !state.can_sign || group.job.active;
        close.addEventListener("click", function () {
          if (needsRenewal) {
            openRenewal(group); return;
          }
          closing = group.parent_client_order_id; field("authorize_close").checked = false;
          root.querySelector('[data-mainnet-close-title]').textContent = "Cerrar · " + group.instrument.replace("_USDT_Perp", "/USDT") + " · " + group.direction;
          root.querySelector('[data-mainnet-close-limits]').textContent = group.recovery ?
            "Cancelar protecciones y cerrar sólo el residuo. Permiso original · " + group.recovery.attempts + "/" + group.recovery.max_attempts +
            " intentos · tope " + group.recovery.exit_cap + " USDT · vence " + new Date(Number(BigInt(group.recovery.deadline_ns) / 1000000n)).toLocaleString() :
            "Cancelar protecciones y cerrar sólo el residuo. Hasta 3 intentos · 180 s · tope " + group.exit_cap + " USDT.";
          root.querySelector('[data-mainnet-close]').hidden = false; guards(); field("authorize_close").focus();
        }); actions.append(consult, close);
        if (group.recovery && !needsRenewal) {
          var limits = node("button", "Límites de cierre", "ghost"); limits.type = "button"; limits.dataset.mainnetLimit = "";
          limits.addEventListener("click", function () { openRenewal(group); }); actions.append(limits);
        }
        card.append(actions);
      }
      list.append(card);
    });
  }
  function replaceGroup(group) {
    if (!state || group.sub_account_id !== sub()) return;
    var index = state.groups.findIndex(function (item) { return item.parent_client_order_id === group.parent_client_order_id; });
    if (index >= 0) state.groups[index] = group; else state.groups.unshift(group);
    renderGroups(); guards();
  }
  async function readGroup(parent, passive) {
    var host = root, issued = epoch, op = serial;
    var data = await readOnly(base + "/groups/" + parent, passive);
    if (!current(host, issued) || op !== serial) return;
    if (data.group) replaceGroup(data.group);
    else if (data.pending && data.job && state) {
      var previous = state.groups.find(function (group) { return group.parent_client_order_id === parent; });
      if (previous) replaceGroup(Object.assign({}, previous, {job:data.job}));
    }
  }
  async function refresh(passive) {
    if (!root || !api.canSend() || !/^[1-9][0-9]{0,19}$/.test(sub())) return;
    var host = root, issued = epoch, selected = sub(), op = serial;
    var data = await readOnly(base + "?sub_account_id=" + encodeURIComponent(selected) + "&offset=" + offset, passive);
    if (!current(host, issued) || selected !== sub() || op !== serial) return;
    if (data.history_pending) {
      // El estado de sesión se obtiene de RAM aunque el historial esté ocupado.
      // Conserva registros y STOP conocidos; una lectura pendiente no los borra.
      var previous = state;
      state = Object.assign({}, data, {groups:previous ? previous.groups : [],
        stop:previous ? previous.stop : null,has_more:previous ? previous.has_more : false});
      if (previous && JSON.stringify(previous.permission_revision) !== JSON.stringify(data.permission_revision)) invalidate();
      renderGroups(); guards(); notice("Registro ocupado · consultando"); return;
    }
    if (host.querySelector('[data-mainnet-state]').textContent === "Registro ocupado · consultando") notice("Registro actualizado");
    if (state && JSON.stringify(data.permission_revision) !== JSON.stringify(state.permission_revision)) invalidate();
    state = data; renderGroups(); guards();
  }
  function schedule() {
    timer = setTimeout(async function () {
      if (!root || !root.isConnected) { clear(); return; }
      if (!busy && !reading && api.canSend() && root.open) {
        var host = root, issued = epoch; reading = true;
        try {
          await refresh(true);
          var pending = current(host, issued) && state && state.connected && state.groups.find(function (group) { return !group.result; });
          if (pending) {
            var op = serial, selected = sub();
            if (pending.job.active) await readGroup(pending.parent_client_order_id, true);
            else {
              var data = await api.request(base + "/groups/" + pending.parent_client_order_id + "/live", {method:"POST",body:JSON.stringify(scope())}, true);
              if (current(host, issued) && op === serial && selected === sub()) replaceGroup(data.group);
            }
          }
        } catch (_) { if (current(host, issued)) notice("Seguimiento interrumpido · consultar estado", true); }
        finally { if (root === host) reading = false; }
      }
      if (root) { guards(); schedule(); }
    }, 5000);
  }
  function mount(host, services) {
    clear(); if (!host) return; root = host; api = services;
    host.classList.add("grvt-mainnet");
    host.innerHTML = '<summary>Operar GRVT <span class="statusbadge off">MAINNET</span></summary>' +
      '<div class="grvt-mainnet-body"><div class="real-mexc-head"><strong>Entrada protegida</strong><span data-mainnet-connection class="muted">Desconectado</span></div>' +
      '<div class="row"><label class="field">Subcuenta<input name="sub_account_id" list="grvt-mainnet-subaccounts" inputmode="numeric" pattern="[1-9][0-9]{0,19}" maxlength="20" autocomplete="off"></label><datalist id="grvt-mainnet-subaccounts"></datalist>' +
      '<button type="button" class="ghost" data-mainnet-refresh>Consultar</button><button type="button" class="ghost" data-mainnet-stop data-help="Detiene nuevos envíos locales y retira la firma. Las órdenes existentes requieren cerrar la posición.">STOP</button>' +
      '<button type="button" class="ghost" data-mainnet-disconnect>Desconectar</button><button type="button" class="ghost" data-mainnet-rearm hidden>Rearmar</button></div>' +
      '<details data-mainnet-view-credentials><summary>Conexión View</summary><form data-mainnet-view-form class="grvt-mainnet-fields">' +
      '<label class="field" data-help="Consulta los grupos de esta subcuenta registrada. La clave queda en memoria durante la sesión.">Clave API View<input data-mainnet-secret name="view_api_key" type="password" autocomplete="off" maxlength="256" required></label>' +
      '<button type="submit" data-mainnet-connect-view>Conectar View</button></form></details>' +
      '<details data-mainnet-credentials><summary>Conexión Trade</summary><form data-mainnet-connect-form class="grvt-mainnet-fields">' +
      '<label class="field" data-help="Clave API de la Trading Account de GRVT MAINNET, con permiso Trade y ligada a esta subcuenta.">API key<input data-mainnet-secret name="api_key" type="password" autocomplete="off" maxlength="256" required></label>' +
      '<label class="field" data-help="Clave de firma dedicada de GRVT. No uses la clave privada de tu wallet ni su frase semilla. Sólo permanece en memoria durante la sesión.">Firma dedicada<input data-mainnet-secret name="dedicated_private_key" type="password" autocomplete="off" maxlength="66" spellcheck="false" required></label>' +
      '<label class="field">Firmante GRVT<input data-mainnet-secret name="expected_signer" autocomplete="off" maxlength="42" spellcheck="false" required></label>' +
      '<button type="submit" data-mainnet-connect>Conectar</button></form></details>' +
      '<details data-mainnet-bots></details>' +
      '<details data-mainnet-limit class="grvt-limit"></details>' +
      '<form data-mainnet-order><div class="grvt-mainnet-fields">' +
      '<label class="field">Mercado<select name="instrument"><option value="BTC_USDT_Perp">BTC/USDT</option><option value="ETH_USDT_Perp">ETH/USDT</option></select></label>' +
      '<label class="field">Dirección<select name="direction"><option>LONG</option><option>SHORT</option></select></label>' +
      '<label class="field" data-help="Tope de nocional, no margen. Máximo inicial de ORBIS: 1000 USDT.">Tope entrada · USDT<input name="entry_cap" inputmode="decimal" value="150" required></label>' +
      '<label class="field">Tope salida · USDT<input name="exit_cap" inputmode="decimal" value="200" required></label>' +
      '<label class="field">SL · %<input name="stop_percent" inputmode="decimal" value="3" required></label>' +
      '<label class="field">TP · %<input name="take_percent" inputmode="decimal" value="8" required></label></div>' +
      '<div class="row"><button type="button" class="ghost" data-mainnet-size>Calcular orden</button></div>' +
      '<div class="grvt-mainnet-fields"><label class="field">Cantidad<input name="size" inputmode="decimal" required></label>' +
      '<label class="field">Entrada límite<input name="entry_price" inputmode="decimal" required></label>' +
      '<label class="field">Stop loss<input name="stop_loss" inputmode="decimal" required></label>' +
      '<label class="field">Take profit<input name="take_profit" inputmode="decimal" required></label></div>' +
      '<details><summary>Condiciones de ejecución</summary><div class="grvt-mainnet-fields">' +
      '<label class="field" data-help="Después caducan las órdenes de protección. No equivale a un cierre automático de posición.">Vigencia SL/TP · segundos<input name="exit_seconds" type="number" min="120" max="86400" value="3600" required></label>' +
      '<label class="field">Spread máximo · bps<input name="max_spread_bps" type="number" min="0" max="100" value="25" required></label>' +
      '<label class="field">Deslizamiento máximo · bps<input name="max_slippage_bps" type="number" min="0" max="100" value="25" required></label></div></details>' +
      '<button type="submit" data-mainnet-preview>Revisar entrada</button></form>' +
      '<div data-mainnet-review class="grvt-mainnet-review" hidden><strong>Autorizar entrada · MAINNET</strong><div data-mainnet-preview-values class="grvt-mainnet-metrics"></div>' +
      '<label class="grvt-mainnet-check"><input type="checkbox" name="authorize_entry">Operar con dinero real según esta revisión</label>' +
      '<div class="row"><button type="button" data-mainnet-send>Enviar entrada protegida</button><button type="button" class="ghost" data-mainnet-discard>Descartar</button></div></div>' +
      '<div data-mainnet-close class="grvt-mainnet-review" hidden><strong data-mainnet-close-title>Cerrar posición</strong>' +
      '<p data-mainnet-close-limits></p>' +
      '<label class="grvt-mainnet-check"><input type="checkbox" name="authorize_close">Autorizar este cierre real</label>' +
      '<div class="row"><button type="button" data-mainnet-confirm-close>Confirmar cierre</button><button type="button" class="ghost" data-mainnet-dismiss-close>Volver</button></div></div>' +
      '<div data-mainnet-renew class="grvt-mainnet-review" hidden><strong>Renovar cierre · MAINNET</strong>' +
      '<form data-mainnet-renew-form><div class="grvt-mainnet-fields"><label class="field">Nuevo plazo · segundos<input name="renew_ttl" type="number" min="30" max="300" value="180" required></label>' +
      '<label class="field">Nuevos intentos<input name="renew_attempts" type="number" min="1" max="3" value="3" required></label>' +
      '<label class="field">Tope de salida · USDT<input name="renew_exit_cap" inputmode="decimal" required></label></div>' +
      '<button type="submit" data-mainnet-renew-prepare>Revisar cierre</button></form>' +
      '<div data-mainnet-renew-review hidden><p data-mainnet-renew-terms></p><label class="grvt-mainnet-check"><input type="checkbox" name="authorize_renewal">Autorizar otro cierre real con estos límites</label>' +
      '<button type="button" data-mainnet-renew-send>Autorizar cierre</button></div><button type="button" class="ghost" data-mainnet-renew-dismiss>Volver</button></div>' +
      '<div data-mainnet-state class="state muted" role="status">Consulta tu subcuenta</div><div data-mainnet-chart hidden></div><div data-mainnet-groups></div>' +
      '<div class="row"><button type="button" class="ghost" data-mainnet-prev>Anterior</button><button type="button" class="ghost" data-mainnet-next>Siguiente</button></div></div>';
    field("sub_account_id").addEventListener("input", function () { if (window.OrbisGrvtMainnetBots) window.OrbisGrvtMainnetBots.changedSub(); if (window.OrbisGrvtMainnetLimit) window.OrbisGrvtMainnetLimit.changedSub(); if (window.OrbisGrvtMainnetChart) window.OrbisGrvtMainnetChart.hide(); epoch += 1; serial += 1; busy = false; reading = false; state = null; offset = 0; closing = null; renewing = null; secrets(root); invalidate(); renderGroups(); guards(); root.querySelector('[data-mainnet-close]').hidden = true; root.querySelector('[data-mainnet-renew]').hidden = true; });
    root.querySelector('[data-mainnet-order]').addEventListener("input", invalidate);
    field("authorize_entry").addEventListener("change", guards); field("authorize_close").addEventListener("change", guards);
    field("authorize_renewal").addEventListener("change", guards);
    root.querySelector('[data-mainnet-renew-form]').addEventListener("input", invalidate);
    root.querySelector('[data-mainnet-renew-dismiss]').addEventListener("click", function () { renewing = null; invalidate(); root.querySelector('[data-mainnet-renew]').hidden = true; });
    root.querySelector('[data-mainnet-renew-form]').addEventListener("submit", function (event) {
      event.preventDefault(); run(async function (host, issued) {
        if (!renewing) return; var parent = renewing.parent; invalidate();
        var body = Object.assign(scope(), {ttl_seconds:Number(field("renew_ttl").value),max_attempts:Number(field("renew_attempts").value),exit_cap:field("renew_exit_cap").value.trim().replace(",", ".")});
        var data = await api.request(base + "/groups/" + parent + "/renew-preview", {method:"POST",body:JSON.stringify(body)});
        if (!current(host, issued) || !renewing || renewing.parent !== parent) return;
        renewing.review = data; var value = data.review;
        root.querySelector('[data-mainnet-renew-terms]').textContent = "Sólo residuo · " + value.ttl_seconds + " s · intentos nuevos " + value.max_attempts +
          " · tope " + value.exit_cap + " USDT · spread " + value.max_spread_bps + " bps · deslizamiento " + value.max_slippage_bps + " bps";
        if (value.previous_exit_cap !== value.exit_cap) root.querySelector('[data-mainnet-renew-terms]').textContent += " · tope anterior " + value.previous_exit_cap + " USDT";
        root.querySelector('[data-mainnet-renew-review]').hidden = false; notice("Revisión válida durante 120 s");
      });
    });
    root.querySelector('[data-mainnet-renew-send]').addEventListener("click", function () { run(async function (host, issued) {
      if (!renewing || !renewing.review || !field("authorize_renewal").checked) return;
      var parent = renewing.parent, value = renewing.review, body = Object.assign({}, value, {permission_revision:revision(),confirmation:"RENOVAR_CIERRE_GRVT_MAINNET"});
      renewing = null; invalidate(); host.querySelector('[data-mainnet-renew]').hidden = true;
      try { await api.request(base + "/groups/" + parent + "/renew", {method:"POST",body:JSON.stringify(body)}); }
      catch (_) { if (current(host, issued)) notice("Cierre sin confirmar · consulta el registro; no lo repitas", true); }
      if (current(host, issued)) await readGroup(parent, true);
    }); });
    root.querySelector('[data-mainnet-refresh]').addEventListener("click", function () { run(async function () { await refresh(false); }); });
    root.querySelector('[data-mainnet-view-form]').addEventListener("submit", function (event) {
      event.preventDefault();
      var key = field("view_api_key").value;
      secrets(root);
      run(async function (host, issued) {
        try {
          await refresh(false);
          if (!current(host, issued)) return;
          await api.request(base + "/connect-view", {method:"POST",body:JSON.stringify(Object.assign(scope(), {api_key:key}))});
        } finally { key = ""; }
        if (!current(host, issued)) return;
        await refresh(false);
        if (current(host, issued)) {
          host.querySelector('[data-mainnet-view-credentials]').open = false;
          notice("View conectada");
        }
      });
    });
    root.querySelector('[data-mainnet-connect-form]').addEventListener("submit", function (event) {
      event.preventDefault();
      var credentials = {api_key:field("api_key").value,dedicated_private_key:field("dedicated_private_key").value,
        expected_signer:field("expected_signer").value.trim(),confirmation:"CONECTAR_GRVT_MAINNET"};
      secrets(root); invalidate(); run(async function (host, issued) {
        try {
          await refresh(false); if (!current(host, issued)) return;
          await api.request(base + "/connect", {method:"POST",body:JSON.stringify(Object.assign(scope(), credentials))});
        } finally { credentials.api_key = ""; credentials.dedicated_private_key = ""; credentials.expected_signer = ""; }
        if (!current(host, issued)) return;
        await refresh(false); if (current(host, issued)) { host.querySelector('[data-mainnet-credentials]').open = false; notice("Conexión preparada"); }
      });
    });
    root.querySelector('[data-mainnet-size]').addEventListener("click", function () { run(async function (host, issued) {
      invalidate(); var body = scope(); ["instrument","direction","entry_cap","exit_cap","stop_percent","take_percent"].forEach(function (name) { body[name] = field(name).value.trim(); });
      var data = await api.request(base + "/sizing", {method:"POST",body:JSON.stringify(body)});
      if (!current(host, issued)) return;
      ["size","entry_price","stop_loss","take_profit"].forEach(function (name) { field(name).value = data.draft[name]; }); notice("Orden calculada · revisa precios y límites");
    }); });
    root.querySelector('[data-mainnet-order]').addEventListener("submit", function (event) {
      event.preventDefault(); run(async function (host, issued) {
        invalidate(); var body = scope();
        ["instrument","direction","size","entry_price","stop_loss","take_profit","entry_cap","exit_cap"].forEach(function (name) { body[name] = field(name).value.trim(); });
        ["exit_seconds","max_spread_bps","max_slippage_bps"].forEach(function (name) { body[name] = Number(field(name).value); });
        var data = await api.request(base + "/preview", {method:"POST",body:JSON.stringify(body)});
        if (!current(host, issued)) return;
        reviewed = data; var list = host.querySelector('[data-mainnet-preview-values]'); list.replaceChildren();
        [["Dirección",body.direction],["Cantidad",body.size],["Entrada",body.entry_price],["SL",body.stop_loss],["TP",body.take_profit],
          ["Tope entrada",body.entry_cap + " USDT"],["Tope salida",body.exit_cap + " USDT"],["Vigencia SL/TP",body.exit_seconds + " s"]].forEach(function (item) {
          var el = node("div", ""); el.append(node("small", item[0], "muted"), node("strong", item[1])); list.append(el);
        }); host.querySelector('[data-mainnet-review]').hidden = false; notice("Revisión válida durante 120 s");
      });
    });
    root.querySelector('[data-mainnet-send]').addEventListener("click", function () { run(async function (host, issued) {
      if (!reviewed || !field("authorize_entry").checked) return;
      var draft = reviewed, selected = sub(), parent = draft.review.parent_client_order_id, rev = revision(); offset = 0; invalidate();
      try {
        var data = await api.request(base + "/send", {method:"POST",body:JSON.stringify(Object.assign({}, draft, {permission_revision:rev,confirmation:"ENVIAR_ENTRADA_PROTEGIDA_GRVT_MAINNET"}))});
        if (current(host, issued) && selected === sub()) { replaceGroup(data.group); notice("Acuse recibido · ejecución pendiente de comprobar"); }
      } catch (_) {
        if (!current(host, issued) || selected !== sub()) return;
        notice("Envío sin confirmar · no lo repitas · consulta el registro", true);
        try {
          await readGroup(parent, true);
        } catch (_) { /* El ID revisado sigue siendo la identidad del único intento. */ }
      }
    }); });
    root.querySelector('[data-mainnet-discard]').addEventListener("click", invalidate);
    root.querySelector('[data-mainnet-dismiss-close]').addEventListener("click", function () { closing = null; root.querySelector('[data-mainnet-close]').hidden = true; field("authorize_close").checked = false; });
    root.querySelector('[data-mainnet-confirm-close]').addEventListener("click", function () { run(async function (host, issued) {
      if (!closing || !field("authorize_close").checked) return;
      var parent = closing, body = Object.assign(scope(), {ttl_seconds:180,max_attempts:3,confirmation:"RECUPERAR_POSICION_GRVT_MAINNET"});
      closing = null; host.querySelector('[data-mainnet-close]').hidden = true; field("authorize_close").checked = false; invalidate();
      try {
        var started = await api.request(base + "/groups/" + parent + "/recover", {method:"POST",body:JSON.stringify(body)});
        if (current(host, issued) && state) {
          var previous = state.groups.find(function (group) { return group.parent_client_order_id === parent; });
          if (previous) replaceGroup(Object.assign({}, previous, {job:started.job}));
        }
      }
      catch (_) { if (current(host, issued)) notice("Cierre sin confirmar · consultando el registro", true); }
      if (current(host, issued)) await readGroup(parent, true);
    }); });
    root.querySelector('[data-mainnet-stop]').addEventListener("click", async function () {
      if (!api.canSend()) return;
      if (window.OrbisGrvtMainnetBots) window.OrbisGrvtMainnetBots.cancel();
      if (window.OrbisGrvtMainnetLimit) window.OrbisGrvtMainnetLimit.cancel();
      var host = root, selected = sub(), issued = ++epoch; ++serial; busy = true; secrets(host); invalidate(); closing = null; renewing = null; host.querySelector('[data-mainnet-close]').hidden = true; host.querySelector('[data-mainnet-renew]').hidden = true; guards();
      try {
        var stopped = await api.request(base + "/stop", {method:"POST",body:JSON.stringify({sub_account_id:selected,confirmation:"DETENER_GRVT_MAINNET"})});
        if (current(host, issued) && selected === sub()) {
          if (stopped.network !== "MAINNET" || stopped.stopped !== true || !Number.isSafeInteger(stopped.revision)) {
            throw new Error("STOP sin confirmar · comprueba la subcuenta");
          }
          state = Object.assign({}, state || {groups:[]}, {can_sign:false, stop:{stopped:true, revision:stopped.revision}});
          guards();
          if (window.OrbisGrvtMainnetBots) window.OrbisGrvtMainnetBots.ackStop();
          if (window.OrbisGrvtMainnetLimit) window.OrbisGrvtMainnetLimit.ackStop();
          await refresh(true);
          if (current(host, issued)) notice("STOP activo · revisa las posiciones existentes");
        }
      } catch (error) { if (current(host, issued)) notice(error.message, true); }
      finally { if (current(host, issued)) { busy = false; guards(); renderGroups(); } }
    });
    root.querySelector('[data-mainnet-disconnect]').addEventListener("click", function () { run(async function (host, issued) {
      var body = Object.assign(scope(), {confirmation:"DESCONECTAR_GRVT_MAINNET"}); secrets(host); invalidate();
      await api.request(base + "/disconnect", {method:"POST",body:JSON.stringify(body)});
      if (current(host, issued)) { await refresh(false); notice("Desconectado"); }
    }); });
    root.querySelector('[data-mainnet-rearm]').addEventListener("click", function () { run(async function (host, issued) {
      if (!window.confirm("¿Retirar STOP? Sólo habilita nuevas revisiones; no envía órdenes.")) return;
      var body = Object.assign(scope(), {stop_revision:state.stop.revision,confirmation:"REARMAR_GRVT_MAINNET"}); invalidate();
      await api.request(base + "/rearm", {method:"POST",body:JSON.stringify(body)});
      if (current(host, issued)) { await refresh(false); notice("Preparado para una nueva revisión"); }
    }); });
    ["prev","next"].forEach(function (name) { root.querySelector('[data-mainnet-' + name + ']').addEventListener("click", function () { run(async function () {
      offset = Math.max(0, offset + (name === "next" ? 25 : -25)); await refresh(false);
    }); }); });
    if (window.OrbisGrvtMainnetBots) window.OrbisGrvtMainnetBots.mount(root.querySelector('[data-mainnet-bots]'), {
      request:api.request,sub:sub,revision:revision,context:function () { return sub() + ":" + epoch; },
      canSend:function () { return !!root && api.canSend(); },canSign:function () { return !!(state && state.can_sign && !(state.stop && state.stop.stopped)); },
      isBusy:function () { return busy || moduleBusy(window.OrbisGrvtMainnetLimit); },guard:guards,invalidateManual:invalidate,refreshParent:function () { return refresh(true); },
      inspect:function (parent) {
        run(async function (host, issued) { await readGroup(parent, false); if (!current(host, issued)) return;
          var card = host.querySelector('[data-mainnet-group="' + parent + '"]'); if (card) card.scrollIntoView({block:"center",behavior:"smooth"});
        });
      }
    });
    if (window.OrbisGrvtMainnetLimit) window.OrbisGrvtMainnetLimit.mount(root.querySelector('[data-mainnet-limit]'), {
      request:api.request,read:readOnly,sub:sub,revision:revision,context:function () { return sub() + ":" + epoch; },
      canSend:function () { return !!root && api.canSend(); },canSign:function () { return !!(state && state.can_sign && !(state.stop && state.stop.stopped)); },
      isBusy:function () { return busy || moduleBusy(window.OrbisGrvtMainnetBots); },guard:guards,invalidateManual:invalidate,
      showChart:function (item) {
        if (window.OrbisGrvtMainnetChart) window.OrbisGrvtMainnetChart.show({id:item.client_order_id,symbol:item.instrument,limit:true});
      }
    });
    if (window.OrbisGrvtMainnetChart) window.OrbisGrvtMainnetChart.mount(root.querySelector('[data-mainnet-chart]'), {
      request:api.request,getScopeId:sub,exact:api.exact,canSend:api.canSend
    });
    guards(); renderGroups(); schedule();
    if (api.canSend()) {
      var issued = epoch;
      function suggest() {
        if (!current(host, issued) || !api.canSend()) return;
        readOnly(base + "/subaccounts", true).then(function (data) {
          if (!current(host, issued) || !api.canSend()) return;
          if (data.pending) { suggestionTimer = setTimeout(suggest, 400); return; }
          var list = root.querySelector('#grvt-mainnet-subaccounts');
          data.sub_account_ids.forEach(function (id) { var option = node("option", ""); option.value = id; list.append(option); });
          if (!sub() && data.sub_account_ids.length === 1) { field("sub_account_id").value = data.sub_account_ids[0]; refresh(true).catch(function () {}); }
        }).catch(function () { /* El ID manual sigue disponible sin sugerencias. */ });
      }
      suggest();
    }
  }
  function prepare(source) {
    if (!root || !api.canSend()) return false;
    var limit = source.kind === "LIMIT";
    var panel = root.querySelector(limit ? '[data-mainnet-limit]' : '[data-mainnet-bots]');
    var child = limit ? window.OrbisGrvtMainnetLimit : window.OrbisGrvtMainnetBots;
    if (!panel || !child) return false;
    root.open = true;
    panel.open = true;
    child.prepare(source.id);
    panel.querySelector("summary").focus({preventScroll:true});
    panel.scrollIntoView({block:"center"});
    return true;
  }
  window.OrbisGrvtMainnet = {mount:mount,clear:clear,prepare:prepare};
}());
