(function () {
  "use strict";

  const KEY = "orbis.market.alerts.v1";
  const LEGACY = "orbis.estudio.__al|all|x|alerts";
  const LEGACY_FIRED = "orbis.estudio.__al_fired|all|x|alerts";
  const SOUND = "orbis.alert.sound";
  const CHANGE = "orbis:market-alerts";
  const PROVIDERS = {
    auto: "Automática", kraken: "Kraken", binance: "Binance", coinbase: "Coinbase",
    okx: "OKX", bybit: "Bybit", mexc: "MEXC", kucoin: "KuCoin", gate: "Gate",
    bitget: "Bitget", grvt: "GRVT", bitstamp: "Bitstamp"
  };
  const INTERVAL = 20000;
  const MAX_AGE = 60000;
  let timer, audio;
  let running = false, refreshRequested = false;
  let storageError = false;
  let soundEnabled = true;
  let visibleEvents = new Set();
  let U, stopViewUpdates;

  function positiveDecimal(raw) {
    if (typeof raw !== "string" || raw.length > 170) return null;
    const value = raw.trim().replace(",", ".");
    const scientific = /^\+?(\d*)(?:\.(\d*))?e([+-]?\d{1,3})$/i.exec(value);
    let result = value.replace(/^\+?\./, "0.");
    if (scientific) {
      const digits = scientific[1] + (scientific[2] || "");
      if (!digits) return null;
      const point = scientific[1].length + Number(scientific[3]);
      if (point < -80 || point > 80) return null;
      result = point <= 0 ? "0." + "0".repeat(-point) + digits :
        point >= digits.length ? digits.padEnd(point, "0") :
        digits.slice(0, point) + "." + digits.slice(point);
    }
    return window.OrbisDecimal.compareStrings(result, "0") === 1 ? result : null;
  }

  function definition(symbol, provider, op, price) {
    const pair = String(symbol || "").trim().toUpperCase().replace(/[-_]/g, "/")
      .replace(/^(?:XXBT|XBT)\//, "BTC/").replace(/\/EURO$/, "/EUR");
    if (!/^[A-Z0-9]{1,24}\/[A-Z0-9]{1,12}(?::PERP)?$/.test(pair)) {
      throw new Error("Usa un par como BTC/EUR o BTC/USDT:PERP.");
    }
    const source = String(provider || "auto").toLowerCase();
    if (!Object.hasOwn(PROVIDERS, source)) throw new Error("Elige una fuente de precios.");
    if (op !== "<" && op !== ">") throw new Error("Elige una condición.");
    const threshold = positiveDecimal(String(price));
    if (!threshold) throw new Error("Introduce un precio superior a cero.");
    return { symbol: pair, provider: source, op, price: threshold };
  }

  function read() {
    const raw = localStorage.getItem(KEY);
    if (raw !== null) {
      const state = JSON.parse(raw);
      if (!state || !Array.isArray(state.rules) || !Array.isArray(state.events) ||
          !state.rules.every(item => item && typeof item.id === "string" && typeof item.symbol === "string") ||
          !state.events.every(item => item && typeof item.id === "string" && typeof item.symbol === "string")) {
        throw new Error("El historial local de alertas no está disponible.");
      }
      return state;
    }
    const legacy = JSON.parse(localStorage.getItem(LEGACY) || "[]");
    const fired = JSON.parse(localStorage.getItem(LEGACY_FIRED) || "{}") || {};
    const rules = Array.isArray(legacy) ? legacy.flatMap(function (rule) {
      try {
        return [{
          ...definition(rule.symbol, rule.provider, rule.op, rule.price),
          id: String(rule.id), active: Object.hasOwn(fired, rule.id) && Boolean(fired[rule.id])
        }];
      } catch (error) { return []; }
    }) : [];
    return { rules, events: [] };
  }

  function state() {
    try { return { ...read(), storageError }; }
    catch (error) { return { rules: [], events: [], storageError: true }; }
  }

  function lock(name, options, run) {
    return navigator.locks ? navigator.locks.request(name, options, run) : run(true);
  }

  function notify(events) {
    if (document.visibilityState !== "visible" || !U) return;
    for (const event of events.slice().reverse()) {
      U.showToast("alert", event.symbol + " · " + event.op + " " + event.threshold +
        " · Precio " + event.price + " · " + PROVIDERS[event.provider]);
      if (soundEnabled) playBell().catch(function () {});
    }
  }

  function announce(events) {
    const unseen = events.filter(event => !visibleEvents.has(event.id));
    visibleEvents = new Set(events.map(event => event.id));
    notify(unseen);
    window.dispatchEvent(new Event(CHANGE));
  }

  async function update(mutator) {
    return lock(KEY, {}, function () {
      let current;
      try { current = read(); }
      catch (error) { throw new Error("No se pueden leer las alertas locales."); }
      if (mutator(current)) {
        try { localStorage.setItem(KEY, JSON.stringify(current)); }
        catch (error) { throw new Error("No se pueden guardar las alertas locales."); }
      }
      storageError = false;
      announce(current.events);
    });
  }

  function group(rule) { return rule.symbol + "|" + rule.provider; }

  async function add(symbol, provider, op, price) {
    const data = definition(symbol, provider, op, price);
    await update(function (current) {
      if (current.rules.length >= 40) throw new Error("Límite de 40 alertas.");
      if (current.rules.some(rule => group(rule) === group(data) && rule.op === data.op &&
          window.OrbisDecimal.compareStrings(rule.price, data.price) === 0)) {
        throw new Error("Ya existe esta alerta.");
      }
      current.rules.push({ ...data, id: U.newRequestId(), active: false });
      return true;
    });
    scheduleSoon();
  }

  async function remove(id) {
    await update(function (current) {
      const next = current.rules.filter(rule => rule.id !== id);
      if (next.length === current.rules.length) return false;
      current.rules = next;
      return true;
    });
  }

  async function quote(rule) {
    try {
      const response = await U.api("/market/snapshot?symbol=" + encodeURIComponent(rule.symbol) +
        "&provider=" + encodeURIComponent(rule.provider), { cache: "no-store", signal: AbortSignal.timeout(6000) });
      if (!response.ok || !response.data) return null;
      const data = response.data;
      const provider = data.resolved_provider;
      if (data.available !== true || data.symbol !== rule.symbol || data.requested_provider !== rule.provider ||
          !Object.hasOwn(PROVIDERS, provider) || provider === "auto" ||
          (rule.provider !== "auto" && provider !== rule.provider)) return null;
      const price = positiveDecimal(data.price);
      const received = Date.parse(data.received_at);
      const now = Date.now();
      if (!price || !Number.isFinite(received) || now - received > MAX_AGE || received > now + 5000) return null;

      const hasMarketTime = data.market_time_status === "fresh";
      const market = hasMarketTime ? Date.parse(data.market_time_at) : null;
      if (hasMarketTime ? !Number.isFinite(market) || now - market > MAX_AGE || market > now + 5000 :
          data.market_time_status !== "unknown") return null;
      const eventNs = typeof data.event_time_ns === "string" && /^\d{1,22}$/.test(data.event_time_ns) ?
        data.event_time_ns : null;
      return { price, received, market, eventNs: hasMarketTime ? eventNs : null, provider };
    } catch (error) { return null; }
  }

  function apply(current, quotes, requestedIds) {
    let changed = false;
    const observedAt = new Date().toISOString();
    for (const rule of current.rules) {
      if (!requestedIds.has(rule.id)) continue;
      const key = group(rule);
      if (!quotes.has(key)) continue;
      const observation = quotes.get(key);
      if (!observation) {
        if (!rule.unavailable) { rule.unavailable = true; changed = true; }
        continue;
      }
      if (observation.received < (rule.received || 0)) continue;
      if (observation.market !== null && rule.source === observation.provider && rule.marketTime != null) {
        if (observation.eventNs && rule.eventNs ?
            BigInt(observation.eventNs) <= BigInt(rule.eventNs) : observation.market < rule.marketTime) continue;
      }
      if (rule.unavailable) { rule.unavailable = false; changed = true; }
      if (observation.received === rule.received) continue;
      const comparison = window.OrbisDecimal.compareStrings(observation.price, rule.price);
      if (comparison === null) continue;
      const hit = rule.op === ">" ? comparison > 0 : comparison < 0;
      if (hit && !rule.active) {
        current.events.unshift({
          id: U.newRequestId(), ruleId: rule.id, symbol: rule.symbol, provider: observation.provider,
          op: rule.op, threshold: rule.price, price: observation.price, observedAt,
          quoteAt: new Date(observation.market ?? observation.received).toISOString(),
          quoteTimeKind: observation.market === null ? "received" : "market"
        });
      }
      rule.active = hit;
      rule.received = observation.received;
      rule.marketTime = observation.market;
      rule.eventNs = observation.eventNs;
      rule.source = observation.provider;
      changed = true;
    }
    if (current.events.length > 200) current.events.length = 200;
    return changed;
  }

  async function observe() {
    running = true;
    try {
      await lock(KEY + ".observer", { ifAvailable: true }, async function (leader) {
        if (!leader) return;
        const { rules } = read();
        if (!rules.length) return;
        const groups = Array.from(new Map(rules.map(rule => [group(rule), rule])));
        const ids = new Set(rules.map(rule => rule.id));
        for (let index = 0; index < groups.length; index += 4) {
          const pairs = await Promise.all(groups.slice(index, index + 4).map(async function ([key, rule]) {
            return [key, await quote(rule)];
          }));
          await update(current => apply(current, new Map(pairs), ids));
        }
      });
    } catch (error) {
      storageError = true;
      window.dispatchEvent(new Event(CHANGE));
    } finally {
      running = false;
      const wait = refreshRequested ? 0 : INTERVAL;
      refreshRequested = false;
      timer = setTimeout(observe, wait);
    }
  }

  function scheduleSoon() {
    if (running) { refreshRequested = true; return; }
    clearTimeout(timer);
    timer = setTimeout(observe, 0);
  }

  function armAudio() {
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio) return Promise.reject(new Error("Audio no disponible."));
    audio ||= new Audio();
    return audio.state === "running" ? Promise.resolve() : audio.resume();
  }

  async function playBell() {
    await armAudio();
    const now = audio.currentTime;
    const gain = audio.createGain();
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.35, now + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.6);
    gain.connect(audio.destination);
    [880, 660].forEach(function (frequency, index) {
      const tone = audio.createOscillator();
      tone.frequency.value = frequency;
      tone.connect(gain);
      tone.start(now + index * 0.08);
      tone.stop(now + 0.6 + index * 0.08);
      if (index === 1) tone.onended = function () { gain.disconnect(); };
    });
  }

  function onChange(listener) {
    window.addEventListener(CHANGE, listener);
    return function () { window.removeEventListener(CHANGE, listener); };
  }

  function attach(listener) {
    cleanupView();
    stopViewUpdates = onChange(listener);
    listener();
  }

  function cleanupView() {
    if (stopViewUpdates) stopViewUpdates();
    stopViewUpdates = null;
  }

  function message(error) { U.showToast("blocked", error.message); }

  function render(root) {
    const sourceOptions = Object.entries(PROVIDERS).map(([id, label]) => '<option value="' + id + '">' + label + "</option>").join("");
    root.innerHTML = '<section class="card"><div class="pagina-cabecera"><h2>Alertas</h2>' +
      '<span class="muted" data-help="ORBIS consulta precios públicos mientras esta aplicación está abierta. No reconstruye avisos de los periodos sin conexión.">Con ORBIS abierto</span></div>' +
      '<form class="row" id="alerts-create"><label class="field">Mercado<input name="symbol" placeholder="BTC/EUR" maxlength="42" required></label>' +
      '<label class="field">Fuente<select name="provider">' + sourceOptions + '</select></label>' +
      '<label class="field">Condición<select name="op"><option value="&gt;">Por encima de</option>' +
      '<option value="&lt;">Por debajo de</option></select></label>' +
      '<label class="field">Precio<input name="price" inputmode="decimal" maxlength="170" required></label>' +
      '<button type="submit">Crear alerta</button></form></section>' +
      '<section class="card"><h3>Vigilancia</h3><div id="alerts-rules"></div></section>' +
      '<section class="card"><h3>Historial</h3><div id="alerts-events"></div></section>';
    const form = root.querySelector("#alerts-create");
    const ruleList = root.querySelector("#alerts-rules");
    const history = root.querySelector("#alerts-events");
    const submit = form.querySelector("button");
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const fields = new FormData(form);
      submit.disabled = true;
      try {
        await add(fields.get("symbol"), fields.get("provider"), fields.get("op"), fields.get("price"));
        form.elements.price.value = "";
        U.showToast("ok", "Alerta creada.");
      } catch (error) { message(error); }
      finally { submit.disabled = false; }
    });
    ruleList.addEventListener("click", function (event) {
      const button = event.target.closest("[data-remove-alert]");
      if (button) remove(button.dataset.removeAlert).catch(message);
    });

    let previousRules, previousHistory;
    attach(function () {
      const current = state();
      const rules = current.storageError ? '<p role="alert">Alertas locales no disponibles.</p>' :
        current.rules.length ? table("Vigilancia", ["Mercado", "Fuente", "Condición", "Estado", ""],
          current.rules.map(function (rule) {
            const status = rule.unavailable ? "Sin precio vigente" : !rule.received ? "Pendiente" :
              rule.active ? "Condición cumplida" : "Vigilando";
            const removeLabel = rule.symbol + " " + rule.op + " " + rule.price + " · " + PROVIDERS[rule.provider];
            const badge = '<span class="badge ' + (rule.unavailable ? "stale" : rule.active ? "fresh" : "") +
              '">' + status + "</span>";
            const button = '<button type="button" class="ghost" data-remove-alert="' + U.esc(rule.id) +
              '" aria-label="Quitar alerta ' + U.esc(removeLabel) + '">Quitar</button>';
            return '<tr><th scope="row">' + U.esc(rule.symbol) + "</th>" +
              cell("Fuente", U.esc(PROVIDERS[rule.provider])) +
              cell("Condición", U.esc(rule.op + " " + rule.price)) +
              cell("Estado", badge) + cell("", button) + "</tr>";
          }).join("")) : '<p class="muted">Sin alertas.</p>';
      if (rules !== previousRules) {
        const focused = document.activeElement && document.activeElement.dataset.removeAlert;
        ruleList.innerHTML = rules;
        if (focused) {
          const replacement = Array.from(ruleList.querySelectorAll("[data-remove-alert]")).find(button => button.dataset.removeAlert === focused);
          (replacement || form.elements.symbol).focus({ preventScroll: true });
        }
        previousRules = rules;
      }
      const events = current.events.length ? table("Historial de alertas", ["Mercado", "Detección", "Condición", "Precio observado", "Fuente"],
        current.events.map(function (event) {
          const quoteClock = (event.quoteTimeKind === "market" ? "Hora de mercado " : "Hora de recepción ") + U.fmtDateTime(event.quoteAt);
          const observed = '<time datetime="' + U.esc(event.observedAt) + '">' +
            U.esc(U.fmtDateTime(event.observedAt)) + "</time>";
          const price = '<span title="' + U.esc(quoteClock) + '">' + U.esc(event.price) + "</span>";
          return '<tr><th scope="row">' + U.esc(event.symbol) + "</th>" + cell("Detección", observed) +
            cell("Condición", U.esc(event.op + " " + event.threshold)) +
            cell("Precio observado", price) + cell("Fuente", U.esc(PROVIDERS[event.provider])) + "</tr>";
        }).join("")) : '<p class="muted">Sin avisos registrados.</p>';
      if (events !== previousHistory) { history.innerHTML = events; previousHistory = events; }
    });
  }

  function cell(label, value) {
    return '<td data-label="' + label + '">' + value + "</td>";
  }

  function table(caption, columns, rows) {
    return '<div class="tablewrap alert-table"><table class="resp" role="table"><caption class="sr-only">' + caption + "</caption><thead><tr>" +
      columns.map(name => '<th scope="col">' + name + "</th>").join("") +
      "</tr></thead><tbody>" + rows + "</tbody></table></div>";
  }

  function home(node) {
    attach(function () {
      const current = state();
      const last = current.events[0];
      const count = current.rules.length;
      node.textContent = current.storageError ? "Alertas locales no disponibles" :
        count + (count === 1 ? " alerta en vigilancia" : " alertas en vigilancia") +
        (last ? " · Último aviso " + last.symbol + " · " + U.fmtDateTime(last.observedAt) : "");
    });
  }

  function start(ui) {
    U = ui;
    visibleEvents = new Set(state().events.map(event => event.id));
    try { soundEnabled = localStorage.getItem(SOUND) !== "false"; } catch (error) {}
    function armFromGesture() {
      if (!soundEnabled) return;
      try { armAudio().catch(function () {}); } catch (error) {}
    }
    window.addEventListener("pointerdown", armFromGesture, { passive: true });
    window.addEventListener("keydown", armFromGesture);
    window.addEventListener("storage", function (event) {
      if (event.key === SOUND) soundEnabled = event.newValue !== "false";
      if (event.key === KEY || event.key === null) announce(state().events);
    });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") scheduleSoon();
    });
    scheduleSoon();
  }

  window.OrbisMarketAlerts = {
    start, add, remove, state, onChange, render, home, playBell, cleanupView,
    setSound: function (enabled) { soundEnabled = enabled; }
  };
})();
