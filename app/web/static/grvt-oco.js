/* Protecciones nativas GRVT TESTNET. Sin autostart, reenvío ni secretos persistidos. */
(function () {
  "use strict";
  var root = null, api = null, epoch = 0, busy = false, timer = null;
  var prepared = null, sources = [], offset = 0, attempted = new Set();
  var base = "/exchanges/grvt/testnet/oco";
  function secrets(host) {
    if (host) host.querySelectorAll('input[type="password"], [name="expected_signer"]').forEach(function (input) { input.value = ""; });
  }
  function clear() {
    epoch += 1; secrets(root);
    if (timer !== null) clearTimeout(timer);
    timer = null; root = null; api = null; busy = false; prepared = null;
    sources = []; offset = 0; attempted = new Set();
  }
  function current(host, issued) { return root === host && host.isConnected && epoch === issued && api; }
  function node(tag, text, className) {
    var element = document.createElement(tag); element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function notice(text, error) {
    var target = root.querySelector("[data-oco-state]");
    target.textContent = text; target.className = "state " + (error ? "error" : "muted");
  }
  function eligiblePreview() {
    return prepared && prepared.preview.expires_at_ms > Date.now() && !attempted.has(prepared.preview.group_id);
  }
  function guards() {
    if (!root) return;
    root.querySelector("[data-oco-controls]").disabled = busy || !api.canSend();
    root.querySelector("[data-oco-send]").disabled = busy || !api.canSend() || !eligiblePreview();
    var source = sources.find(function (s) { return s.parent_client_order_id === root.querySelector('[name="source"]').value; });
    root.querySelector("[data-oco-prepare]").disabled = busy || !api.canSend() || !source || !source.eligible;
    if (prepared) root.querySelector("[data-oco-expiry]").textContent = eligiblePreview() ?
      "Vigente · " + Math.max(0, Math.ceil((prepared.preview.expires_at_ms - Date.now()) / 1000)) + " s" : "Caducada · prepara de nuevo";
  }
  function invalidate() {
    prepared = null;
    if (!root) return;
    root.querySelector("[data-oco-review]").hidden = true;
    root.querySelector("[data-oco-preview]").replaceChildren();
    root.querySelector('[name="dedicated_private_key"]').value = "";
    root.querySelector('[name="expected_signer"]').value = "";
    guards();
  }
  function subaccount() { return root.querySelector('[name="sub_account_id"]').value.trim(); }
  function groupButton(host, title, action, id, disabled, help) {
    var button = node("button", title, "ghost"); button.type = "button";
    button.dataset.ocoAction = action; button.dataset.groupId = id; button.disabled = disabled;
    if (help) button.dataset.help = help;
    host.append(button);
  }
  function renderGroups(data) {
    var select = root.querySelector('[name="source"]'), previous = select.value;
    sources = data.sources; select.replaceChildren(new Option("Entrada conciliada", ""));
    sources.forEach(function (source) {
      var option = new Option(source.instrument + " · cierre " + source.side + " · " + source.size +
        (source.eligible ? "" : " · bloqueada"),source.parent_client_order_id);
      option.disabled = !source.eligible; select.add(option);
    });
    if (sources.some(function (s) { return s.parent_client_order_id === previous; })) select.value = previous;
    else if (sources.length === 1 && sources[0].eligible) select.value = sources[0].parent_client_order_id;
    var list = root.querySelector("[data-oco-groups]"); list.replaceChildren();
    data.groups.forEach(function (group) {
      var card = node("article", "", "grvt-oco-group"); card.dataset.ocoGroup = group.group_id;
      var receipt = group.reconciliation, posted = ["ACKNOWLEDGED", "POST_CLAIMED"].includes(group.status);
      var label = receipt ? "Conciliada" : group.status === "ACKNOWLEDGED" ? "Acuse recibido" :
        group.status === "POST_CLAIMED" ? "Envío incierto" : group.status === "ABANDONED" ? "Descartada" : "Preparada · sin envío";
      var head = node("div", "", "real-mexc-head");
      head.append(node("strong",group.instrument + " · cierre " + group.side + " · " + group.size),node("span",label,"statusbadge off"));
      card.append(head,node("small","Grupo " + group.group_id,"muted grvt-oco-id"));
      var observed = group.last_observation && group.last_observation.orders || [];
      group.legs.forEach(function (leg) {
        var state = observed.find(function (o) { return o.client_order_id === leg.client_order_id; });
        card.append(node("div",(leg.role === "STOP_LOSS" ? "SL" : "TP") + " · " + leg.trigger_price +
          " → límite " + leg.limit_price + (state ? " · " + state.status + " · ejecutado " + state.traded_size : " · sin observar"),"grvt-oco-leg"));
        card.append(node("small","ID " + leg.client_order_id,"muted grvt-oco-id"));
      });
      card.append(node("small","Auditorías " + group.financial_audit_count +
        (group.cancel_attempt ? " · Cancelación " + group.cancel_attempt.status : ""),"muted"));
      if (receipt) {
        var net = node("p","Neto de salidas · " + receipt.result.net_realized_pnl_usdt + " USDT","grvt-oco-net");
        net.dataset.help = "Resultado de estas salidas respecto al recibo de entrada. Los costes de apertura ya descontados no se incluyen otra vez; no es el neto del ciclo completo ni un cálculo fiscal.";
        card.append(net,node("small","Posición restante · " + (receipt.result.positions.length ?
          receipt.result.positions.map(function (position) { return position[0] + " · " + position[1]; }).join(" / ") : "plana"),"muted"));
      }
      var actions = node("div", "", "row");
      var terminal = observed.length === 2 && observed.every(function (o) { return ["FILLED","CANCELLED","REJECTED","EXPIRED"].includes(o.status); });
      if (posted && !receipt) {
        groupButton(actions,"Consultar","observe",group.group_id,false,"Consulta ambos IDs sin crear ni cancelar órdenes.");
        groupButton(actions,"Auditar","audit",group.group_id,!terminal || !group.preflight_available,
          "Exige ambas salidas terminales. Dos auditorías estables separadas al menos 5 s; la última de hasta 30 s.");
        groupButton(actions,"Conciliar","reconcile",group.group_id,!group.can_reconcile,
          "Guarda el recibo local verificado. No rearma la estrategia ni registra patrimonio REAL.");
        groupButton(actions,"Cancelar salidas","cancel",group.group_id,!!group.cancel_attempt || terminal,
          "Aplica STOP y pide retirar SL/TP. No cierra la posición; puede dejarla sin protección.");
      }
      if (group.status === "PREPARED") groupButton(actions,"Descartar","abandon",group.group_id,false,
        "Sólo antes del intento de envío. Los IDs quedan reservados y no se reutilizan.");
      card.append(actions); list.append(card);
    });
    if (!data.groups.length) list.append(node("p","Sin grupos OCO.","muted"));
    root.querySelector("[data-oco-previous]").disabled = offset === 0;
    root.querySelector("[data-oco-next]").disabled = !data.has_more;
    root.querySelector("[data-oco-page]").textContent = "Página " + (offset / 50 + 1) + (data.stopped ? " · STOP activo" : "");
    guards();
  }
  async function readPanel(localApi, host, issued) {
    var sub = subaccount();
    if (!/^[0-9]{1,30}$/.test(sub)) throw new Error("Introduce la subcuenta TESTNET.");
    var data = await localApi.request(base + "?sub_account_id=" + encodeURIComponent(sub) + "&offset=" + offset,null,true);
    if (!current(host,issued) || subaccount() !== sub || !api.canSend()) return;
    if (data.network !== "TESTNET" || data.authorization !== false || !Array.isArray(data.groups) || !Array.isArray(data.sources))
      throw new Error("Registro OCO no verificable");
    renderGroups(data);
  }
  async function refresh() {
    if (!root || busy || !api.canSend()) return;
    var host = root, issued = epoch, localApi = api; busy = true; guards();
    try { await readPanel(localApi,host,issued); if (current(host,issued)) notice("Registro local · no envía órdenes.",false); }
    catch (error) { if (current(host,issued)) { invalidate(); notice(error.message === "Not Found" ?
      "Servidor anterior · reinicio pendiente para habilitar OCO." : error.message || "Registro no disponible",true); } }
    finally { if (current(host,issued)) { busy = false; guards(); } }
  }
  async function prepare(event) {
    event.preventDefault(); if (busy || !api.canSend()) return;
    var form = event.currentTarget, fields = new FormData(form);
    var host = root, issued = epoch, localApi = api;
    var body = {sub_account_id:subaccount(),parent_client_order_id:String(fields.get("source")),
      stop_loss:String(fields.get("stop_loss")),take_profit:String(fields.get("take_profit")),
      max_order_notional:String(fields.get("max_order_notional")),max_slippage_bps:Number(fields.get("max_slippage_bps")),
      ttl_minutes:Number(fields.get("ttl_minutes")),api_key:String(fields.get("api_key")).trim()};
    invalidate(); fields = null; secrets(host); busy = true; guards(); notice("Comprobando posición y LAST…",false);
    try {
      var pending = localApi.request(base + "/preview",{method:"POST",body:JSON.stringify(body)});
      body.api_key = ""; body = null;
      var data = await pending;
      if (!current(host,issued) || !api.canSend()) return;
      if (data.network !== "TESTNET" || data.can_submit !== false || !data.preview || !data.preview_mac) throw new Error("Preparación no verificable");
      prepared = data;
      var preview = data.preview, box = host.querySelector("[data-oco-preview]"); box.replaceChildren();
      box.append(node("strong",preview.instrument + " · cierre " + preview.side + " · " + preview.size),
        node("small","LAST " + preview.last_price + " · tope " + preview.max_order_notional + " USDT","muted"));
      preview.legs.forEach(function (leg) { box.append(node("div",(leg.role === "STOP_LOSS" ? "SL" : "TP") +
        " · " + leg.trigger_price + " → límite " + leg.limit_price)); });
      host.querySelector("[data-oco-review]").hidden = false;
      notice("Preparación revisable · todavía sin firma ni órdenes.",false);
    } catch (error) { if (current(host,issued)) notice(error.message || "Preparación no disponible",true); }
    finally { if (body) body.api_key = ""; secrets(host); if (current(host,issued)) { busy = false; guards(); } }
  }
  async function send(event) {
    event.preventDefault(); if (busy || !api.canSend() || !eligiblePreview()) return;
    var keyInput = root.querySelector('[name="api_key"]');
    if (!keyInput.reportValidity()) return;
    var preview = prepared.preview;
    if (!confirm("GRVT TESTNET · cierre " + preview.side + " · " + preview.size + " " + preview.instrument +
        "\nSL " + preview.legs[0].trigger_price + " · TP " + preview.legs[1].trigger_price +
        "\nUn único grupo OCO reduce-only. Un límite puede quedar sin ejecución tras un gap. ¿Firmar y enviar?")) return;
    if (!eligiblePreview()) { secrets(root); guards(); return; }
    var host = root, issued = epoch, localApi = api;
    var fields = new FormData(event.currentTarget);
    var body = {preview:preview,preview_mac:prepared.preview_mac,api_key:keyInput.value.trim(),
      dedicated_private_key:String(fields.get("dedicated_private_key")).trim(),
      expected_signer:String(fields.get("expected_signer")).trim(),confirmation:"FIRMAR_OCO_GRVT_TESTNET"};
    attempted.add(preview.group_id); fields = null; invalidate(); secrets(host); busy = true; guards();
    notice("Firmando y enviando · " + preview.group_id,false);
    try {
      var pending = localApi.request(base + "/send",{method:"POST",body:JSON.stringify(body)});
      body.api_key = ""; body.dedicated_private_key = ""; body.expected_signer = ""; body = null;
      var result = await pending;
      if (!current(host,issued)) return;
      if (result.network !== "TESTNET" || !result.group || result.group.group_id !== preview.group_id) throw new Error("Respuesta incierta");
      notice("Acuse registrado · consulta ambos IDs; todavía sin conciliación.",false);
    } catch (_) { if (current(host,issued)) notice("Envío bloqueado o incierto · " + preview.group_id + ". Consulta el registro; no reenvíes.",true); }
    finally {
      if (body) { body.api_key = ""; body.dedicated_private_key = ""; body.expected_signer = ""; }
      secrets(host);
      if (current(host,issued)) {
        try { offset = 0; await readPanel(localApi,host,issued); }
        catch (_) { if (current(host,issued)) notice("Recuperación pendiente · consulta el registro antes de continuar.",true); }
        if (current(host,issued)) { busy = false; guards(); }
      }
    }
  }
  async function act(event) {
    var button = event.target.closest("button[data-oco-action]");
    if (!button || !root.contains(button) || button.disabled || busy || !api.canSend()) return;
    var action = button.dataset.ocoAction, id = button.dataset.groupId, body = {};
    if (action === "cancel" && !confirm("Retirar SL y TP aplica STOP, pero NO cierra la posición. Puede quedar sin protección. ¿Cancelar ambas salidas TESTNET?")) return;
    if (action === "abandon" && !confirm("¿Descartar esta preparación sin envío? Los IDs no se reutilizarán.")) return;
    if (action === "reconcile" && !confirm("¿Guardar el recibo financiero TESTNET? No registra patrimonio REAL ni rearma la estrategia.")) return;
    if (["observe","audit","cancel"].includes(action)) {
      var keyInput = root.querySelector('[name="api_key"]');
      if (!keyInput.reportValidity()) return;
      body.api_key = keyInput.value.trim();
    }
    if (action === "cancel") { body.confirmation = "RETIRAR_PROTECCIONES_OCO_TESTNET"; body.acknowledge_unprotected_position = true; }
    if (action === "abandon") body.confirmation = "DESCARTAR_OCO_GRVT_TESTNET";
    if (action === "reconcile") body.confirmation = "CONCILIAR_OCO_GRVT_TESTNET";
    var host = root, issued = epoch, localApi = api; secrets(host); busy = true; guards();
    try {
      var pending = localApi.request(base + "/" + encodeURIComponent(id) + "/" + action,{method:"POST",body:JSON.stringify(body)});
      body.api_key = ""; body = null;
      var data = await pending;
      if (!current(host,issued)) return;
      if (data.network !== "TESTNET" || !data.group || data.group.group_id !== id) throw new Error("Respuesta no verificable");
      notice(action === "audit" ? "Auditoría guardada · repite tras al menos 5 s antes de conciliar." :
        action === "cancel" ? "Cancelación solicitada · STOP permanece activo. No cierra posición." :
        action === "reconcile" ? "Recibo conciliado · no se reactiva la estrategia." : "Registro actualizado.",false);
    } catch (error) { if (current(host,issued)) notice((error.message || "Resultado incierto") + " · Consulta el registro.",true); }
    finally {
      if (body) body.api_key = ""; secrets(host);
      if (current(host,issued)) {
        try { await readPanel(localApi,host,issued); } catch (_) { if (current(host,issued)) notice("Registro no disponible · conserva el bloqueo.",true); }
        if (current(host,issued)) { busy = false; guards(); }
      }
    }
  }
  function schedule() {
    var issued = epoch;
    timer = setTimeout(function () {
      timer = null; if (epoch !== issued) return;
      if (!root || !root.isConnected) { clear(); return; }
      guards(); schedule();
    },1000);
  }
  function setSubaccount(value) {
    if (!root || root.querySelector('[name="sub_account_id"]').value) return;
    root.querySelector('[name="sub_account_id"]').value = value;
    if (root.open) refresh();
  }
  function mount(host, options) {
    clear(); if (!host) return; root = host; api = options;
    host.innerHTML = '<summary data-help="SL y TP nativos sobre una entrada propia ya conciliada. Sólo TESTNET: no activa bots GRVT ni opera mainnet.">Protecciones SL / TP · TESTNET</summary>' +
      '<fieldset data-oco-controls class="grvt-oco-controls"><form data-oco-form autocomplete="off" class="grvt-testnet-send-form">' +
      '<div class="real-form"><label class="field">Subcuenta TESTNET<input name="sub_account_id" list="real-grvt-testnet-subaccounts" inputmode="numeric" pattern="[0-9]{1,30}" maxlength="30" required></label>' +
      '<label class="field" data-help="La cantidad completa y el lado de cierre se derivan de la posición conciliada y se comprueban con GRVT al preparar y enviar.">Entrada<select name="source" required><option value="">Entrada conciliada</option></select></label>' +
      '<label class="field">Stop loss<input name="stop_loss" inputmode="decimal" pattern="[0-9]{1,18}(\\.[0-9]{1,18})?" required></label>' +
      '<label class="field">Take profit<input name="take_profit" inputmode="decimal" pattern="[0-9]{1,18}(\\.[0-9]{1,18})?" required></label>' +
      '<label class="field" data-help="Límite nocional de estas salidas reduce-only, no margen ni fondos PAPER. Máximo 1000 USDT; no amplía el tope de entradas.">Tope USDT<input name="max_order_notional" value="1000" inputmode="decimal" pattern="[0-9]{1,18}(\\.[0-9]{1,18})?" required></label>' +
      '<label class="field" data-help="Clave exclusiva de GRVT TESTNET. Se usa sólo para la acción elegida y se borra del formulario; la clave View mainnet no sirve.">API key · TESTNET<input name="api_key" type="password" maxlength="512" autocomplete="off" spellcheck="false" required></label></div>' +
      '<details><summary>Límites</summary><div class="real-form"><label class="field">Deslizamiento · bps<input name="max_slippage_bps" type="number" min="0" max="100" step="1" value="25" required></label>' +
      '<label class="field">Vigencia · minutos<input name="ttl_minutes" type="number" min="1" max="1440" step="1" value="60" required></label></div></details>' +
      '<div class="row"><button type="button" class="ghost" data-oco-refresh>Consultar registro</button><button type="submit" data-oco-prepare disabled>Preparar</button></div></form>' +
      '<section data-oco-review hidden><div data-oco-preview class="grvt-oco-preview"></div><small class="muted" data-oco-expiry></small>' +
      '<form data-oco-sign class="grvt-testnet-send-form" autocomplete="off"><div class="real-form">' +
      '<label class="field" data-help="Clave de firma dedicada a la API Trade TESTNET. Nunca la clave privada de una wallet ni una clave mainnet. No se conserva.">Firma de API · TESTNET<input name="dedicated_private_key" type="password" pattern="(0x)?[a-fA-F0-9]{64}" maxlength="66" required autocomplete="off" spellcheck="false"></label>' +
      '<label class="field">Firmante de API<input name="expected_signer" pattern="0x[a-fA-F0-9]{40}" maxlength="42" required autocomplete="off" spellcheck="false"></label></div>' +
      '<div class="row"><button type="submit" data-oco-send disabled>Enviar · TESTNET</button><button type="button" class="ghost" data-oco-invalidate>Descartar revisión</button></div></form></section>' +
      '<div data-oco-groups class="grvt-oco-groups"></div><div class="row"><button type="button" class="ghost" data-oco-previous disabled>Más recientes</button>' +
      '<small class="muted" data-oco-page></small><button type="button" class="ghost" data-oco-next disabled>Anteriores</button></div></fieldset>' +
      '<div data-oco-state class="state muted" role="status">Consulta una subcuenta para preparar sus salidas.</div>';
    host.querySelector('[data-oco-form]').addEventListener("submit",prepare);
    host.querySelector('[data-oco-sign]').addEventListener("submit",send);
    host.querySelector('[data-oco-refresh]').addEventListener("click",refresh);
    host.querySelector('[data-oco-invalidate]').addEventListener("click",invalidate);
    host.querySelector('[data-oco-groups]').addEventListener("click",act);
    host.querySelector('[data-oco-previous]').addEventListener("click",function () { offset = Math.max(0,offset-50); refresh(); });
    host.querySelector('[data-oco-next]').addEventListener("click",function () { offset += 50; refresh(); });
    host.querySelector('[data-oco-form]').addEventListener("input",function (event) { if (event.target.name !== "api_key") invalidate(); });
    host.querySelector('[name="source"]').addEventListener("change",invalidate);
    host.querySelector('[name="sub_account_id"]').addEventListener("input",function () {
      offset = 0; sources = []; host.querySelector('[data-oco-groups]').replaceChildren();
      host.querySelector('[name="source"]').replaceChildren(new Option("Entrada conciliada","")); guards();
    });
    host.addEventListener("toggle",function (event) {
      if (event.target !== host) return;
      if (!host.open) { secrets(host); invalidate(); } else if (api.canSend() && subaccount()) refresh();
    });
    if (!api.canSend()) notice("Importes ocultos · muestra los importes para revisar y operar.",false);
    guards(); schedule();
  }
  window.OrbisGrvtOco = {mount:mount,clear:clear,setSubaccount:setSubaccount};
})();
