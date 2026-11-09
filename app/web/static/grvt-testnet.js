/* Envío manual, único y efímero PAPER → GRVT TESTNET. Nunca mainnet. */
(function () {
  "use strict";
  var root = null, api = null, epoch = 0, timer = null, busy = false, reading = false;
  var items = [], attempted = new Set();
  function secrets(host) {
    if (host) host.querySelectorAll('input[type="password"]').forEach(function (input) { input.value = ""; });
  }
  function clear() {
    epoch += 1; secrets(root);
    if (timer !== null) clearTimeout(timer);
    timer = null; root = null; api = null; busy = false; reading = false; items = []; attempted = new Set();
  }
  function current(host, issued) { return root === host && host.isConnected && epoch === issued && api; }
  function node(tag, text, className) {
    var element = document.createElement(tag); element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function notice(message, error) {
    var target = root.querySelector('[data-testnet-state]');
    target.textContent = message; target.className = "state " + (error ? "error" : "muted");
  }
  function selected() {
    var value = root.querySelector('[name="source"]').value;
    return items.find(function (item) { return item.client_order_id === value; });
  }
  function eligible(item) { return item && item.candidate === true && item.expires_at_ms > Date.now() && !attempted.has(item.client_order_id); }
  function status() {
    if (!root) return;
    var item = selected(), detail = root.querySelector('[data-testnet-source]');
    detail.replaceChildren();
    if (item) {
      detail.append(node("strong", item.direction + " · " + (item.action === "OPEN" ? "Entrada" : "Cierre") + " · " + item.side));
      detail.append(node("small", "ID " + item.client_order_id + " · " +
        (attempted.has(item.client_order_id) ? "Intento realizado · revisar registro" : eligible(item) ?
          "Vigente " + Math.max(0, Math.ceil((item.expires_at_ms - Date.now()) / 1000)) + " s · se revalida al enviar" : "No enviable · espera una señal nueva"), "muted"));
    } else detail.append(node("span", "Selecciona una acción PAPER reciente.", "muted"));
    root.querySelector('[data-testnet-send]').disabled = busy || !eligible(item) || !api.canSend();
  }
  function renderSources() {
    var select = root.querySelector('[name="source"]'), previous = select.value;
    select.replaceChildren(new Option("Selecciona señal PAPER", ""));
    items.forEach(function (item) {
      var option = new Option(item.account_name + " · " + item.symbol + " · " + item.direction + " · " +
        (item.action === "OPEN" ? "Entrada" : "Cierre") + (eligible(item) ? "" : " · no enviable"), item.client_order_id);
      option.disabled = !eligible(item); select.add(option);
    });
    if (items.some(function (item) { return item.client_order_id === previous; })) select.value = previous;
    status();
  }
  async function refresh() {
    if (!root || busy || reading) return;
    var host = root, issued = epoch, localApi = api;
    reading = true;
    try {
      var data = await localApi.request("/exchanges/grvt/testnet/signals/candidates", null, true);
      if (!current(host, issued)) return;
      if (data.network !== "TESTNET" || data.authorization !== false || !Array.isArray(data.items)) throw new Error("Origen testnet no verificable");
      items = data.items; renderSources();
      if (!attempted.size) notice(items.length ? "Sólo señales observadas · sin envío automático." : "Sin acciones. Activa antes una estrategia AUTO PAPER en GRVT.", false);
    } catch (error) {
      if (current(host, issued)) { items = []; renderSources(); notice(error.message || "Señales no disponibles", true); }
    } finally { if (current(host, issued)) reading = false; }
  }
  function schedule() {
    var issued = epoch;
    timer = setTimeout(async function () {
      timer = null;
      if (epoch !== issued) return;
      if (!root || !root.isConnected) { clear(); return; }
      if (root.open) { status(); await refresh(); }
      if (root && epoch === issued) schedule();
    }, 1500);
  }
  async function submit(event) {
    event.preventDefault();
    if (busy || !api.canSend()) return;
    var item = selected();
    if (!eligible(item)) { status(); return; }
    var form = event.currentTarget, fields = new FormData(form);
    if (!confirm("GRVT TESTNET · " + item.direction + " · " + (item.action === "OPEN" ? "entrada" : "cierre") +
        " " + item.side + " · " + item.instrument + "\n" + fields.get("size") + " × " + fields.get("limit_price") +
        " USDT · tope " + fields.get("max_order_notional") + " USDT\nUn único intento IOC. No es un bot automático ni usa fondos PAPER. ¿Firmar y enviar?")) return;
    if (!eligible(item)) { secrets(root); status(); notice("La señal ha caducado. Espera una nueva.", true); return; }
    var host = root, issued = epoch, localApi = api, subaccount = String(fields.get("sub_account_id")).trim();
    var body = {session_id:item.session_id, action_request_id:item.action_request_id, instrument:item.instrument,
      side:item.side, reduce_only:item.reduce_only, sub_account_id:subaccount,
      api_key:String(fields.get("api_key")).trim(), dedicated_private_key:String(fields.get("dedicated_private_key")).trim(),
      expected_signer:String(fields.get("expected_signer")).trim(), builder:String(fields.get("builder")).trim(),
      max_order_notional:String(fields.get("max_order_notional")), size:String(fields.get("size")),
      limit_price:String(fields.get("limit_price")), max_spread_bps:Number(fields.get("max_spread_bps")),
      max_slippage_bps:Number(fields.get("max_slippage_bps")), confirmation:"FIRMAR_Y_ENVIAR_GRVT_TESTNET"};
    secrets(host); fields = null; busy = true; attempted.add(item.client_order_id); status();
    host.querySelectorAll("input,select,button").forEach(function (input) { input.disabled = true; });
    notice("Firmando y enviando · ID " + item.client_order_id, false);
    try {
      var pending = localApi.request("/exchanges/grvt/testnet/signals/sign-and-submit", {method:"POST", body:JSON.stringify(body)});
      body.api_key = ""; body.dedicated_private_key = ""; body = null;
      var result = await pending;
      if (!current(host, issued)) return;
      if (result.network !== "TESTNET" || result.mainnet_order_sent !== false || result.client_order_id !== item.client_order_id)
        throw new Error("Respuesta no verificable");
      notice("Intento registrado · " + (result.external_status || result.journal_status) + " · no confirma fills. Audita el ID.", false);
    } catch (_) {
      if (current(host, issued)) notice("Envío bloqueado o respuesta incierta · ID " + item.client_order_id + ". Consulta el registro; no se reenvía.", true);
    } finally {
      if (body) { body.api_key = ""; body.dedicated_private_key = ""; body = null; }
      secrets(host);
      if (current(host, issued)) {
        busy = false;
        host.querySelectorAll("input,select,button").forEach(function (input) { input.disabled = false; });
        status(); localApi.onAttempt(subaccount, item.client_order_id);
      }
    }
  }
  function mount(host, options) {
    clear(); if (!host) return;
    root = host; api = options;
    host.innerHTML = '<summary data-help="Envía una sola acción reciente de GRVT AUTO PAPER a una subcuenta TESTNET. No activa bots ni opera mainnet.">Enviar señal · TESTNET</summary>' +
      '<form class="grvt-testnet-send-form" autocomplete="off"><div class="real-form">' +
      '<label class="field">Subcuenta TESTNET<input name="sub_account_id" inputmode="numeric" pattern="[0-9]{1,30}" maxlength="30" required></label>' +
      '<label class="field" data-help="Lectura local cada 1,5 s. La acción seleccionada no se reemplaza automáticamente. La validez máxima es 20 s y se comprueba de nuevo al enviar.">Acción PAPER<select name="source" required><option value="">Selecciona señal PAPER</option></select></label></div>' +
      '<div data-testnet-source class="grvt-testnet-source" aria-live="polite"></div>' +
      '<div class="real-form"><label class="field" data-help="Tope nocional por intento en USDT, no margen ni fondos ficticios. Máximo de esta fase: 100 USDT.">Tope USDT<input name="max_order_notional" inputmode="decimal" pattern="[0-9]{1,6}(\\.[0-9]{1,6})?" value="100" required></label>' +
      '<label class="field">Unidades<input name="size" inputmode="decimal" pattern="[0-9]{1,30}(\\.[0-9]{1,18})?" required></label>' +
      '<label class="field" data-help="Orden limitada IOC: ejecuta lo disponible al límite y cancela el resto. No garantiza ejecución.">Precio límite<input name="limit_price" inputmode="decimal" pattern="[0-9]{1,30}(\\.[0-9]{1,18})?" required></label>' +
      '<label class="field" data-help="Spread máximo admitido del ticker testnet. 100 puntos básicos = 1 %.">Spread · bps<input name="max_spread_bps" type="number" min="0" max="100" step="1" value="25" required></label>' +
      '<label class="field" data-help="Desviación máxima del precio límite frente al bid/ask testnet. Se vuelve a comprobar antes del envío.">Deslizamiento · bps<input name="max_slippage_bps" type="number" min="0" max="100" step="1" value="25" required></label></div>' +
      '<details class="grvt-testnet-credentials"><summary>Credenciales dedicadas · TESTNET</summary>' +
      '<p class="amber">Nunca uses la clave privada de tu wallet ni claves mainnet.</p><div class="real-form">' +
      '<label class="field">API key Trade · TESTNET<input name="api_key" type="password" maxlength="256" autocomplete="off" spellcheck="false" required></label>' +
      '<label class="field" data-help="Clave de firma generada para esta API Trade testnet, no la wallet. Uso efímero; no se guarda ni se reutiliza.">Clave de firma Trade · TESTNET<input name="dedicated_private_key" type="password" pattern="0x[a-fA-F0-9]{64}" maxlength="66" autocomplete="off" spellcheck="false" required></label>' +
      '<label class="field" data-help="Dirección pública del firmante de la API. Debe coincidir con la clave Trade dedicada.">Firmante<input name="expected_signer" pattern="0x[a-fA-F0-9]{40}" maxlength="42" spellcheck="false" required></label>' +
      '<label class="field" data-help="Dirección builder requerida por GRVT; comisión builder fijada a cero. No se inventa ni se toma de una wallet conectada.">Builder<input name="builder" pattern="0x[a-fA-F0-9]{40}" maxlength="42" spellcheck="false" required></label></div></details>' +
      '<div class="row"><button type="button" class="ghost" data-testnet-refresh>Consultar señales</button>' +
      '<button type="submit" data-testnet-send disabled>Firmar y enviar · TESTNET</button></div></form>' +
      '<div class="state muted" data-testnet-state role="status">Configura límites y credenciales antes de seleccionar la señal.</div>';
    host.querySelector("form").addEventListener("submit", submit);
    host.querySelector("form").addEventListener("invalid", function (event) {
      if (event.target.closest(".grvt-testnet-credentials")) host.querySelector(".grvt-testnet-credentials").open = true;
    }, true);
    host.querySelector('[name="source"]').addEventListener("change", status);
    host.querySelector('[data-testnet-refresh]').addEventListener("click", refresh);
    host.addEventListener("toggle", function (event) { if (event.target === host) { if (host.open) refresh(); else secrets(host); } });
    status(); schedule();
  }
  window.OrbisGrvtTestnet = {mount:mount, clear:clear};
})();
