/* Orbis PAPER: hash router, mismo origen, sin framework ni CDN.
 * La UI envía importes como cadenas. Los saldos y resultados proceden del
 * servidor; los estimados previos usan decimal.js, nunca Number para dinero.
 * La compra/venta usa cotización pública PAPER validada por el servidor.
 * El historial del Ledger PAPER se lee de /paper/operations y su resumen
 * agregado de /paper/operations/summary. Sin POST manual de operaciones.
 */
/* Constantes de refresco por vista (ms) */
var PORTFOLIO_REFRESH_MS = 86400000; // 24h
var HOME_REFRESH_MS = 60000;         // 1 min: la cotización pública se cachea 30 s
var OPERATIVA_REFRESH_MS = 15000;    // 15s
var ESTUDIO_REFRESH_MS = 30000;      // 30s
(function () {
  "use strict";

  var view = document.getElementById("view");
  var baseInput = document.getElementById("api-base");
  var homeTimer = null;
  var mktTimer = null;
  var mktLast = null;
  var operativaTimer = null;
  var portfolioTimer = null;
  var estudioTimer = null;

  /* Pares principales (BTC, ETH, SOL, ADA, DOT, MATIC, AVAX, LINK en
   * EUR/USD). El precio siempre llega del servidor (/market/price). */
  var SUPPORTED_ASSETS = ["BTC", "ETH", "SOL", "ADA", "DOT", "MATIC", "AVAX", "LINK"];
  var SUPPORTED_QUOTES = ["EUR", "USD", "USDT"];
  function homeQuotePreference() {
    try {
      var saved = localStorage.getItem("orbis.home.quote");
      if (SUPPORTED_QUOTES.indexOf(saved) >= 0) return saved;
    } catch (e) {}
    return "EUR";
  }

  function apiBase() {
    var w = (window.ORBIS_API_BASE || "").trim();
    if (w) return w.replace(/\/$/, "");
    try {
      var s = (localStorage.getItem("orbis_api_base") || "").trim();
      if (s) return s.replace(/\/$/, "");
      if (baseInput && baseInput.value.trim()) return baseInput.value.trim().replace(/\/$/, "");
    } catch (e) { /* almacenamiento no disponible */ }
    return "";
  }

  if (baseInput) {
    try { baseInput.value = localStorage.getItem("orbis_api_base") || ""; } catch (e) {}
    baseInput.addEventListener("change", function () {
      try { localStorage.setItem("orbis_api_base", baseInput.value.trim()); } catch (e) {}
      checkRuntimeStatus();
    });
  }

  function api(path, options) {
    var opts = options || {};
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    return fetch(apiBase() + path, opts).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
        return { status: resp.status, ok: resp.ok, data: data };
      });
    });
  }

  function paintRuntimeStatus(label, explanation) {
    var badge = document.getElementById("runtime-status");
    if (!badge) return;
    if (!label) { badge.hidden = true; badge.textContent = ""; return; }
    if (badge.textContent !== label) badge.textContent = label;
    badge.title = explanation;
    badge.hidden = false;
  }
  function checkRuntimeStatus() {
    api("/system/runtime", {cache: "no-store"}).then(function (res) {
      if (res.ok && res.data && res.data.restart_required === true) {
        paintRuntimeStatus("Reinicio pendiente",
          "El código local cambió desde el arranque. Reinicia ORBIS cuando no haya operaciones en curso; puede pedir las contraseñas de tus almacenes cifrados.");
      } else if (res.ok && res.data && res.data.restart_required === false) {
        paintRuntimeStatus("", "");
      } else if (res.status === 404) {
        paintRuntimeStatus("Servidor sin verificar",
          "La interfaz no puede comprobar la versión del servidor. Puede ser anterior o distinto; reinicia ORBIS para cargar el código nuevo.");
      } else {
        paintRuntimeStatus("", "");
      }
    }).catch(function () { paintRuntimeStatus("", ""); });
  }

  /* UUIDv4 propio sobre crypto.getRandomValues (el helper nativo del
   * navegador falla en http:// no seguro como 10.0.2.2). */
  function newRequestId() {
    var b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    function h(i) { return b[i].toString(16).padStart(2, "0"); }
    return h(0) + h(1) + h(2) + h(3) + "-" + h(4) + h(5) + "-"
      + h(6) + h(7) + "-" + h(8) + h(9) + "-" + h(10) + h(11)
      + h(12) + h(13) + h(14) + h(15);
  }

  /* ------------------------- FORMATO CENTRALIZADO ------------------------ */
  var eurFmt = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var usdFmt = new Intl.NumberFormat("es-ES", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var tokenFmt = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 8 });
  var qtyFmt = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 8, minimumFractionDigits: 0 });
  var numFmt = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 12 });
  var pctFmt = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero" });

  function fmtExactDecimal(x, minDigits, maxDigits) {
    /* El servidor envía Decimal como texto: no convertirlo a Number antes
     * de separar/grupar sus dígitos, porque >15 cifras pierden precisión. */
    if (typeof x !== "string") return null;
    var match = /^\s*([+-]?)(\d+)(?:\.(\d+))?\s*$/.exec(x);
    if (!match) return null;
    var rawInteger = match[2].replace(/^0+(?=\d)/, "");
    var rawFraction = match[3] || "";
    var units = BigInt(rawInteger);
    var fraction = rawFraction.slice(0, maxDigits).padEnd(maxDigits, "0");
    if (rawFraction.length > maxDigits && rawFraction.charCodeAt(maxDigits) >= 53) {
      var scale = 10n ** BigInt(maxDigits);
      var rounded = BigInt(fraction || "0") + 1n;
      if (rounded >= scale) { units += 1n; rounded = 0n; }
      fraction = maxDigits ? String(rounded).padStart(maxDigits, "0") : "";
    }
    while (fraction.length > minDigits && fraction.endsWith("0")) fraction = fraction.slice(0, -1);
    var whole = String(units).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    var negative = match[1] === "-" && (units !== 0n || /[1-9]/.test(fraction));
    return (negative ? "-" : "") + whole + (fraction ? "," + fraction : "");
  }

  function fmtEUR(x) {
    if (x === null || x === undefined || x === "") return "—";
    var exact = fmtExactDecimal(x, 2, 2);
    if (exact !== null) return exact + "\u00a0€";
    var n = Number(x);
    if (!isFinite(n)) return "—";
    return eurFmt.format(n);
  }
  function fmtMoney(x, ccy) {
    if (x === null || x === undefined || x === "") return "—";
    var code = String(ccy || "").toUpperCase();
    var exact = fmtExactDecimal(x, 2, code === "EUR" || code === "USD" ? 2 : 8);
    if (exact !== null) {
      if (code === "EUR") return exact + "\u00a0€";
      if (code === "USD") return exact + "\u00a0US$";
      if (/^[A-Z0-9]{2,12}$/.test(code)) return exact + " " + code;
      return "—";
    }
    var n = Number(x);
    if (!isFinite(n)) return "—";
    if (code === "EUR") return eurFmt.format(n);
    if (code === "USD") return usdFmt.format(n);
    if (/^[A-Z0-9]{2,12}$/.test(code)) return tokenFmt.format(n) + " " + code;
    return "—";
  }
  /* Cantidad hasta 8 decimales, recorta ceros, con sufijo del activo. */
  function fmtQty(x, suffix) {
    if (x === null || x === undefined || x === "") return "—";
    var t = fmtExactDecimal(x, 0, 8);
    if (t === null) {
      var n = Number(x);
      if (!isFinite(n)) return "—";
      t = qtyFmt.format(n);
    }
    return suffix ? t + " " + suffix : t;
  }
  /* Precisión de precio coherente con Estudio (estudio.js precision):
   * BTC/ETH 2 decimales, resto 8. Solo presentación; el cálculo sigue en
   * Decimal en el servidor y fmtMoney (fiat a 2dec, tokens hasta 8)/fmtQty (hasta 8) no
   * cambian: el precio se muestra con 2dec y las cantidades con hasta 8. */
  function pricePrecision(symbol) {
    var s = String(symbol === null || symbol === undefined ? "" : symbol)
      .trim().toUpperCase().replace("-", "/").replace("_", "/");
    var base = s.split("/")[0];
    if (base === "BTC" || base === "ETH") return 2;
    return 8;
  }
  function fmtPriceFor(x, symbol) {
    if (x === null || x === undefined || x === "") return "—";
    var d = pricePrecision(symbol);
    var exact = fmtExactDecimal(x, d, d);
    if (exact !== null) return exact;
    var n = Number(x);
    if (!isFinite(n)) return "—";
    return new Intl.NumberFormat("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d }).format(n);
  }
  function fmtNum(x) {
    if (x === null || x === undefined || x === "") return "—";
    var exact = fmtExactDecimal(x, 0, 12);
    if (exact !== null) return exact;
    var n = Number(x);
    if (!isFinite(n)) return "—";
    return numFmt.format(n);
  }
  function fmtPct(x) {
    if (x === null || x === undefined || x === "") return "—";
    var n = Number(x);
    if (!isFinite(n)) return "—";
    return pctFmt.format(n) + "%";
  }
  function fmtPctShown(x) {
    return fmtPct(x);
  }
  /* Porcentaje sin signo para pesos y distribución (93,86%). */
  var shareFmt = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function fmtShare(x) {
    if (x === null || x === undefined || x === "") return "—";
    var n = Number(x);
    if (!isFinite(n)) return "—";
    return shareFmt.format(n) + "%";
  }
  /* Edad del precio: segundos, minutos u horas, siempre humano. */
  function fmtAge(seconds) {
    if (seconds === null || seconds === undefined || seconds === "") return "actualización pendiente";
    var s = Math.max(0, Math.round(Number(seconds)));
    if (!isFinite(s)) return "actualización pendiente";
    if (s < 60) return "hace " + s + " s";
    var m = Math.floor(s / 60);
    if (m < 60) return "hace " + m + " min";
    return "hace " + Math.floor(m / 60) + " h";
  }
  /* Timestamps naive UTC del servidor: añadir Z antes de formatear. */
  function fmtDateTime(s) {
    if (!s) return "—";
    var iso = /Z|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + "Z";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString("es-ES");
  }
  /* Etiqueta humana de la fuente del precio. Nunca se muestra el
   * identificador interno de cache tal cual. */
  function sourceLabel(s) {
    var v = String(s === null || s === undefined ? "" : s).toLowerCase();
    if (v.indexOf("kraken") === 0) return "Datos de Kraken";
    if (v === "stale-cache") return "Kraken (sin actualizar)";
    if (v === "coingecko") return "CoinGecko";
    if (v === "manual") return "precio manual";
    if (v === "static") return "prueba";
    var names = {binance:"Binance", coinbase:"Coinbase", okx:"OKX", bybit:"Bybit",
      mexc:"MEXC", kucoin:"KuCoin", gate:"Gate", bitget:"Bitget",
      grvt:"GRVT", bitstamp:"Bitstamp"};
    if (names[v]) return "Datos de " + names[v];
    return "sin fuente";
  }
  function marketSourcesMatch(priceSource, chartSource) {
    var price = String(priceSource || "").trim().toLowerCase();
    var chart = String(chartSource || "").trim().toLowerCase();
    if (price.indexOf("kraken") === 0) price = "kraken";
    if (chart.indexOf("kraken") === 0) chart = "kraken";
    return !!price && !!chart && price !== "none" && chart !== "none" && price === chart;
  }
  function marketSourceNote(priceSource, chartSource) {
    if (!priceSource || !chartSource || priceSource === "none" || chartSource === "none" ||
        marketSourcesMatch(priceSource, chartSource)) return "";
    return "Precio: " + sourceLabel(priceSource) + " · gráfico: " + sourceLabel(chartSource) +
      ". Fuentes distintas; el cambio 24h no se combina.";
  }
  function withMarketSourceNote(note, priceSource, chartSource) {
    return [note, marketSourceNote(priceSource, chartSource)].filter(Boolean).join(" ") || null;
  }

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  /* Caja de estado: kind empty|loading|error|blocked|ok.
   * role="alert" solo para errores; el resto role="status" u off. */
  function stateBox(kind, html) {
    var role = kind === "error" ? "alert" : "status";
    return '<div class="state ' + kind + '" role="' + role + '">' + html + "</div>";
  }
  function detailBlock(summary, text) {
    return '<details class="det"><summary>' + esc(summary || "Ver detalle") +
      "</summary><p class=\"mono\">" + esc(text) + "</p></details>";
  }
  /* Toasts flotantes en capa fija (#toasts): no mueven la página.
   * 4s éxito, 6s error/bloqueo Guard, cierre manual X, apilado máx 3. */
  var TOAST_OK_MS = 4000;
  var TOAST_ERR_MS = 6000;
  function showToast(tipo, mensaje) {
    var box = document.getElementById("toasts");
    if (!box) {
      box = document.createElement("div");
      box.id = "toasts";
      box.setAttribute("aria-live", "polite");
      document.body.appendChild(box);
    }
    while (box.children.length >= 3) box.removeChild(box.firstChild);
    var t = String(tipo || "").toLowerCase();
    var cls = "toast--ok", ms = TOAST_OK_MS;
    if (t === "alert") { cls = "toast--alert"; ms = 7000; }
    else if (t === "error" || t === "err") { cls = "toast--error"; ms = TOAST_ERR_MS; }
    else if (t === "blocked" || t === "warn" || t === "warning" || t === "amber") { cls = "toast--blocked"; ms = TOAST_ERR_MS; }
    else if (t === "ok" || t === "success" || t === "exito") { cls = "toast--ok"; ms = TOAST_OK_MS; }
    else { cls = "toast--ok"; ms = TOAST_OK_MS; }
    var el = document.createElement("div");
    el.className = "toast " + cls;
    el.setAttribute("role", "status");
    var msg = document.createElement("span");
    msg.className = "msg";
    msg.textContent = String(mensaje === null || mensaje === undefined ? "" : mensaje);
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "close";
    btn.setAttribute("aria-label", "Cerrar aviso");
    btn.textContent = "×";
    var timer = null;
    function close() {
      if (timer) { clearTimeout(timer); timer = null; }
      if (el.parentNode) el.parentNode.removeChild(el);
    }
    btn.addEventListener("click", close);
    el.appendChild(msg);
    el.appendChild(btn);
    box.appendChild(el);
    timer = setTimeout(close, ms);
    return el;
  }
  try { window.showToast = showToast; } catch (e) { /* sin window expuesto */ }

  function upQuote(v) { return (v || "").trim().toUpperCase(); }
  function pairOptions(list, selected) {
    return list.map(function (v) {
      return '<option value="' + esc(v) + '"' + (v === selected ? " selected" : "") + ">" + esc(v) + "</option>";
    }).join("");
  }
  function techDetail(res) {
    /* El detalle interno puede contener claves o códigos útiles para
     * desarrollo pero no para la persona usuaria. La interfaz solo comunica
     * una causa accionable y mantiene el contrato de datos fuera de pantalla. */
    if (res && res.status >= 500) return "El servicio no pudo completar la operación.";
    return "Revisa los datos indicados y vuelve a intentarlo.";
  }

  /* ------------------- FRASES HUMANAS DE GUARD (16+) ------------------- */
  /* Cada motivo exacto del servidor tiene su frase con importes. */
  function guardPhrase(reason, ctx) {
    var c = ctx || {};
    var a = c.asset || "ese activo";
    switch (String(reason)) {
      case "Asset price must be greater than zero":
        return "El precio tiene que ser mayor que 0.";
      case "No open position for asset":
        return "Aún no tienes " + a + ". Compra primero para poder vender.";
      case "Sell quantity must be greater than zero":
        return "La cantidad a vender tiene que ser mayor que 0.";
      case "Sell quantity exceeds available position":
        return "Quieres vender " + (c.qty || "esa cantidad") + " pero solo tienes " + (c.avail || "menos") + ".";
      case "Position inventory cannot go negative":
        return "Esa venta dejaría tu saldo en negativo. Revisa la cantidad.";
      case "Order amount must be greater than zero":
        return "Revisa el importe: tiene que ser mayor que 0.";
      case "Insufficient budget":
        return "No tienes suficiente: quieres usar " + (c.amount || "ese importe") + " y dispones de " + (c.cash || "menos") + ".";
      case "Order exceeds maximum allowed amount":
        return "Esa orden supera tu máximo por orden (" + (c.max || "configurado") + "). Prueba con un importe menor.";
      case "Estimated fee cannot be negative":
        return "La comisión no puede ser negativa.";
      case "Estimated fee must be lower than order amount":
        return "La comisión (" + (c.fee || "indicada") + ") tiene que ser menor que el importe (" + (c.amount || "indicado") + ").";
      case "Estimated fee exceeds allowed percentage":
        return "La comisión supera el máximo permitido (" + (c.maxFee || "configurado") + ").";
      case "Daily spending limit exceeded":
        return "Con esa compra superarías tu límite diario. Prueba mañana o con menos importe.";
      case "Monthly spending limit exceeded":
        return "Con esa compra superarías tu límite mensual. Prueba con menos importe.";
      case "Daily spent cannot be negative":
      case "Monthly spent cannot be negative":
      case "Daily limit cannot be negative":
      case "Monthly limit cannot be negative":
        return "Hay un límite mal configurado en tu saldo. Revísalo en Operativa.";
      case "Kill switch is active":
        return "La operativa está pausada por seguridad. Vuelve a intentarlo más tarde.";
      case "Strategy not found or disabled":
        return "Esa estrategia ya no está activa. Elige otra o créala de nuevo.";
      case "Paper scenario closed":
        return "Ese saldo PAPER ya está cerrado. Abre una nueva sesión para operar.";
      case "Order quantity below ledger precision":
      case "Order proceeds below ledger precision":
        return "Importe demasiado pequeño para registrarlo. Aumenta la cantidad.";
      case "Order rounding loss exceeds limit":
        return "Se perdería demasiado por redondeo. Aumenta el importe.";
      case "Order input outside ledger precision":
      case "Order quantity outside ledger precision":
      case "Order proceeds outside ledger precision":
        return "El importe excede la precisión del registro PAPER. Revísalo.";
      default:
        return null;
    }
  }

  /* Errores HTTP humanos con acción. El literal técnico vive en <details>. */
  function httpErrorBox(res, ctx) {
    var c = ctx || {};
    if (res.data && res.data.code === "market_quote_unavailable") {
      return stateBox("blocked", "<strong>Cotización no disponible.</strong> " +
        "Reintenta con los mismos datos para confirmar esta orden.");
    }
    if (res.status === 404) {
      var q = c.quote || "esa moneda";
      return stateBox("empty",
        "<strong>Todavía no tienes saldo en " + esc(q) + ".</strong> " +
        "Empieza añadiendo saldo de prueba. " +
        '<a class="cta" href="#/operativa">Añadir saldo PAPER</a>');
    }
    if (res.status === 409) {
      return stateBox("error",
        "<strong>Esa operación ya se registró.</strong> Revisa tu historial antes de reintentarlo. " +
        detailBlock("Ver detalle", techDetail(res)));
    }
    if (res.status === 422) {
      return stateBox("error",
        "<strong>Revisa el importe: tiene que ser mayor que 0.</strong> " +
        detailBlock("Ver detalle", techDetail(res)));
    }
    if (res.status >= 500) {
      var rid = nextRetryId("srv");
      return '<div class="state error" role="alert"><strong>No se ha podido guardar.</strong> ' +
        "Vuelve a intentarlo. " +
        '<button type="button" class="ghost" data-retry="' + rid + '">Reintentar</button>' +
        detailBlock("Ver detalle", techDetail(res)) + "</div>";
    }
    return stateBox("error",
      "<strong>Algo no ha salido bien.</strong> Vuelve a intentarlo. " +
      detailBlock("Ver detalle", techDetail(res)));
  }
  function networkBox(retryId) {
    return '<div class="state error" role="alert"><strong>No hemos podido conectar.</strong> ' +
      "¿Está el servidor arrancado? " +
      '<button type="button" class="ghost" data-retry="' + esc(retryId || "retry") + '">Reintentar</button></div>';
  }
  var retrySeq = 0;
  function nextRetryId(prefix) {
    retrySeq += 1;
    return (prefix || "r") + "-" + retrySeq;
  }
  function armRetry(root, id, fn) {
    if (!root || !id) return;
    var b = root.querySelector('[data-retry="' + id + '"]');
    if (b) b.addEventListener("click", fn);
  }
  /* Arma el primer botón Reintentar que encuentre en el bloque. */
  function armAnyRetry(root, fn) {
    if (!root) return;
    var b = root.querySelector("[data-retry]");
    if (b) b.addEventListener("click", fn);
  }
  /* Resultado de buy/sell: ejecutado -> ok; bloqueado -> frase Guard o
   * genérico con literal solo en <details>. */
  function tradeResultBox(verb, res, ctx) {
    if (!res.ok) return httpErrorBox(res, ctx);
    var r = res.data || {};
    if (r.executed) {
      var evidence = r.quote || null;
      var provenance = evidence && r.price
        ? " Precio aplicado PAPER " + esc(fmtMoney(r.price, String(evidence.symbol || "BTC/EUR").split("/")[1] || "EUR")) +
          " · " + esc(String(evidence.provider || "").toUpperCase()) +
          (evidence.approximate ? " · aproximado" : "") + "."
        : "";
      return stateBox("ok",
        "<strong>" + esc(verb) + " lista.</strong>" + provenance + " Ya aparece en tus movimientos.");
    }
    var phrase = guardPhrase(r.reason, ctx);
    if (phrase) {
      return '<div class="amber" role="status"><strong>' + esc(verb) +
        " no se ha podido hacer.</strong> " + esc(phrase) + " " +
        "</div>";
    }
    return '<div class="amber" role="status"><strong>' + esc(verb) +
      " no se ha podido hacer.</strong> Motivo no reconocido; revisa tu saldo e inténtalo de nuevo. " +
      "</div>";
  }

  /* ------------------------------ COMPONENTES ------------------------------ */
  function statCard(label, value, caption) {
    return '<div class="stat"><p class="stat-label">' + esc(label) + "</p>" +
      '<p class="stat-value num">' + value + "</p>" +
      (caption ? '<p class="stat-cap">' + esc(caption) + "</p>" : "") + "</div>";
  }
  function portfolioEstimate(p) {
    return p && p.valuation_status === "ESTIMATED";
  }
  function priceReferenceHelp(pos) {
    var source = {
      kraken: "Kraken", coingecko: "CoinGecko", manual: "precio manual",
      static: "precio fijo", last_trade: "última operación PAPER",
      average_cost: "coste medio", provider: "proveedor sin fecha verificada",
      "stale-cache": "última cotización guardada"
    }[pos.price_source] || "proveedor de precios";
    var detail = "Referencia: " + source + ".";
    if (pos.price_as_of) detail += " Fecha: " + pos.price_as_of + ".";
    if (pos.price_stale) detail += " No confirma un precio negociable actual.";
    return detail;
  }
  function emptyState(title, stepsHtml, ctaHtml) {
    return '<div class="state empty" role="status"><strong>' + esc(title) + "</strong>" +
      '<ol class="steps">' + stepsHtml + "</ol>" + (ctaHtml || "") + "</div>";
  }
  function statusBadge(enabled) {
    return enabled
      ? '<span class="statusbadge on">Activa</span>'
      : '<span class="statusbadge off">Pausada</span>';
  }
  function priceTicker(asset, quote, m, changePct, periodLabel) {
    var stale = !!m.stale;
    var approximate = !stale && !!(m.buy_approximate || m.sell_approximate);
    var badge = stale
      ? '<span class="badge stale">Puede estar desactualizado</span>'
      : approximate
        ? '<span class="badge approx" data-help="El proveedor no publicó bid/ask o una hora verificable del evento. Referencia PAPER aproximada; una orden obtiene otra cotización.">Precio aprox.</span>'
      : '<span class="badge fresh">Actualizado</span>';
    var label = esc(periodLabel || "24h");
    var ch = (changePct === null || changePct === undefined)
      ? '<span class="muted">' + label + ': —</span>'
      : '<span class="' + (changePct >= 0 ? "up" : "down") + '">' + label + ': ' + esc(fmtPctShown(changePct)) + "</span>";
    return '<div class="ticker" aria-live="off">' +
      '<p class="ticker-pair">' + esc(asset) + "/" + esc(quote) + "</p>" +
      '<p class="ticker-price num"><strong>' + esc(fmtMoney(m.price, quote)) + "</strong></p>" +
      '<p class="ticker-sub">' + ch + " " + badge + "</p>" +
      '<p class="muted">' + esc(fmtAge(m.age_seconds)) + " · " + esc(sourceLabel(m.source)) + "</p></div>";
  }
  /* Gráfico canvas propio sin dependencias + <details> con sus datos. */
  function drawChart(canvas, pts) {
    if (!canvas || !canvas.getContext) return;
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || (canvas.parentNode && canvas.parentNode.clientWidth) || 600;
    var h = canvas.clientHeight || 400;
    canvas.width = Math.max(1, Math.round(w * dpr));
    canvas.height = Math.round(h * dpr);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#0e1116";
    ctx.fillRect(0, 0, w, h);
    var padL = 8, padR = 64, padT = 10, padB = 16;
    var plotW = Math.max(10, w - padL - padR);
    var plotH = Math.max(10, h - padT - padB);
    function numOf(v) { var n = Number(v); return isFinite(n) ? n : null; }
    function val(p, k) { return numOf(p[k] !== undefined ? p[k] : p.close); }
    var hi = null, lo = null, hasOHLC = false;
    pts.forEach(function (p) {
      if (p.open !== undefined && p.high !== undefined && p.low !== undefined) hasOHLC = true;
      var a = val(p, "high"), b = val(p, "low");
      if (a !== null) hi = (hi === null || a > hi) ? a : hi;
      if (b !== null) lo = (lo === null || b < lo) ? b : lo;
    });
    if (hi === null || lo === null) return;
    if (hi === lo) { hi += 1; lo -= 1; }
    function y(v) { return padT + (1 - (v - lo) / (hi - lo)) * plotH; }
    ctx.strokeStyle = "#2a3342";
    ctx.fillStyle = "#9aa7b8";
    ctx.font = "10px system-ui, sans-serif";
    ctx.lineWidth = 1;
    [hi, (hi + lo) / 2, lo].forEach(function (g) {
      var gy = Math.round(y(g)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padL, gy);
      ctx.lineTo(padL + plotW, gy);
      ctx.stroke();
      ctx.fillText(fmtNum(String(g)), padL + plotW + 4, gy + 3);
    });
    var n = pts.length;
    var step = plotW / n;
    if (hasOHLC) {
      var bodyW = Math.max(1, Math.min(14, step * 0.6));
      pts.forEach(function (p, i) {
        var o = val(p, "open"), hh = val(p, "high"),
          ll = val(p, "low"), c = val(p, "close");
        if (o === null || hh === null || ll === null || c === null) return;
        var x = padL + i * step + step / 2;
        var up = c >= o;
        ctx.strokeStyle = up ? "#3fb950" : "#f85149";
        ctx.fillStyle = up ? "#3fb950" : "#f85149";
        ctx.beginPath();
        ctx.moveTo(x, y(hh));
        ctx.lineTo(x, y(ll));
        ctx.stroke();
        var yO = y(o), yC = y(c);
        var top = Math.min(yO, yC);
        var bh = Math.max(1, Math.abs(yC - yO));
        ctx.fillRect(x - bodyW / 2, top, bodyW, bh);
      });
    } else {
      ctx.strokeStyle = "#4da3ff";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      pts.forEach(function (p, i) {
        var c = numOf(p.close);
        if (c === null) return;
        var x = padL + (n === 1 ? plotW / 2 : i / (n - 1) * plotW);
        var yy = y(c);
        if (i === 0) ctx.moveTo(x, yy);
        else ctx.lineTo(x, yy);
      });
      ctx.stroke();
    }
  }
  function chartBlock(id, pts, tf, source, stale, note) {
    if (!pts || pts.length < 2) {
      return stateBox("empty", "Sin gráfico suficiente para este periodo: no inventamos datos. Prueba con otro periodo.") +
        (note ? '<p class="muted">' + esc(note) + '</p>' : '');
    }
    var max = null, min = null;
    pts.forEach(function (p) {
      var c = Number(p.close);
      if (!isFinite(c)) return;
      if (max === null || c > max) max = c;
      if (min === null || c < min) min = c;
    });
    function marketDate(seconds) {
      var value = Number(seconds);
      if (!isFinite(value)) return "fecha desconocida";
      return new Date(value * 1000).toLocaleString("es-ES", {
        day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit"
      });
    }
    var firstDate = marketDate(pts[0].t), lastDate = marketDate(pts[pts.length - 1].t);
    var openLast = pts[pts.length - 1].closed === false;
    return (stale ? '<p class="amber" role="status">Histórico desactualizado. ' + esc(note || "Revisa las fechas del gráfico.") + '</p>' : '') +
      (!stale && note ? '<p class="amber" role="status">' + esc(note) + '</p>' : '') +
      '<canvas id="' + esc(id) + '" class="market-mini-chart" role="img" aria-label="Gráfico de precio (' + esc(tf) + ')"></canvas>' +
      '<p class="muted" data-chart-range="' + esc(id) + '" data-open-candle="' + (openLast ? 'true' : 'false') + '">' +
      esc(firstDate) + ' → ' + esc(lastDate) +
      (openLast ? ' <span class="market-open-badge" data-help="La vela actual aún cambia; no es un cierre confirmado.">Vela abierta</span>' : '') +
      '</p>' +
      '<details class="det"><summary>Datos del gráfico</summary><p>' + pts.length +
      " puntos · periodo " + esc(tf) + " · " + esc(sourceLabel(source)) +
      " · máx. " + esc(fmtNum(max)) + " · mín. " + esc(fmtNum(min)) + ".</p></details>";
  }
  function paintChart(id, pts, viewKey) {
    if (paintLwcMini(id, pts, viewKey)) return;
    var cv = document.getElementById(id);
    if (cv && pts && pts.length >= 2) {
      try { drawChart(cv, pts); } catch (e) {
        try { if (window.console && console.warn) console.warn("No se pudo dibujar el gráfico."); } catch (e2) {}
        try { showToast("blocked", "No se pudo dibujar el gráfico. Prueba de nuevo."); } catch (e3) {}
      }
    }
  }
  var MARKET_RANGE_OPTIONS = '<option value="1h">1 h</option><option value="24h">24 h</option>' +
    '<option value="7d">7 días</option><option value="1y">1 año</option>' +
    '<option value="5y">5 años</option><option value="10y">10 años</option><option value="all">Todo disponible</option>' +
    '<option value="custom">Fechas…</option>';
  var marketLongRangeCache = {};
  var marketLastCandleTime = {};
  function chartRangePref(key, fallback) {
    try {
      var value = localStorage.getItem("orbis.chartRange." + key);
      return ["live", "1h", "24h", "7d", "1y", "5y", "10y", "all", "custom"].includes(value) ? value : fallback;
    } catch (e) { return fallback; }
  }
  function saveChartRangePref(key, value) {
    try { localStorage.setItem("orbis.chartRange." + key, value); } catch (e) {}
  }
  function marketRangePicker(id) {
    return '<div class="market-range-control"><label class="field" data-help="Rango visible; los años y las fechas usan velas diarias y respetan la cobertura real del proveedor.">Rango <select id="' +
      esc(id) + '" aria-label="Rango del gráfico">' + MARKET_RANGE_OPTIONS + '</select></label>' +
      '<button type="button" class="ghost market-range-edit" id="' + esc(id) + '-edit" hidden aria-controls="' + esc(id) + '-dates" aria-expanded="false">Cambiar fechas</button>' +
      '<div class="market-range-dates" id="' + esc(id) + '-dates" hidden role="group" aria-label="Elegir fechas del gráfico">' +
      '<div class="market-range-fields"><label class="field">Desde<input type="date" id="' + esc(id) + '-from" aria-label="Desde fecha del gráfico"></label>' +
      '<label class="field">Hasta<input type="date" id="' + esc(id) + '-to" aria-label="Hasta fecha del gráfico"></label></div>' +
      '<span class="amber" id="' + esc(id) + '-error" role="status" hidden></span>' +
      '<div class="market-range-actions"><button type="button" class="ghost" id="' + esc(id) + '-cancel">Cancelar</button>' +
      '<button type="button" id="' + esc(id) + '-apply">Aplicar</button></div></div></div>' +
      '<span class="muted market-range-note" id="' + esc(id) + '-note" role="status"></span>';
  }
  function bindMarketRange(id, key, reload) {
    var select = document.getElementById(id);
    var dates = document.getElementById(id + "-dates");
    var from = document.getElementById(id + "-from");
    var to = document.getElementById(id + "-to");
    var edit = document.getElementById(id + "-edit");
    var error = document.getElementById(id + "-error");
    var note = document.getElementById(id + "-note");
    function storedDate(suffix) {
      try {
        var value = localStorage.getItem("orbis.chartRange." + key + "." + suffix);
        return /^\d{4}-\d{2}-\d{2}$/.test(value || "") ? value : "";
      } catch (e) { return ""; }
    }
    var applied = chartRangePref(key, "24h");
    var committedFrom = storedDate("from"), committedTo = storedDate("to");
    if (applied === "custom" && (!committedFrom || !committedTo || committedFrom > committedTo)) {
      applied = "24h";
      note.textContent = "Fechas guardadas incompletas; se muestran 24 h.";
    }
    select.value = applied;
    from.value = committedFrom;
    to.value = committedTo;
    dates.hidden = true;
    edit.hidden = applied !== "custom";
    function chosen() {
      return applied === "custom" ? {from:committedFrom, to:committedTo} : null;
    }
    function close() {
      dates.hidden = true;
      edit.setAttribute("aria-expanded", "false");
    }
    function open() {
      if (!from.value || !to.value) {
        var end = new Date((marketLastCandleTime[id] || Math.floor(Date.now() / 1000)) * 1000);
        var start = new Date(Date.UTC(end.getUTCFullYear() - 1, end.getUTCMonth(), end.getUTCDate()));
        if (!from.value) from.value = start.toISOString().slice(0, 10);
        if (!to.value) to.value = end.toISOString().slice(0, 10);
      }
      error.hidden = true;
      dates.hidden = false;
      edit.hidden = false;
      edit.textContent = applied === "custom" ? "Cambiar fechas" : "Elegir fechas";
      edit.setAttribute("aria-expanded", "true");
      from.focus();
    }
    function cancel() {
      close();
      select.value = applied;
      edit.hidden = applied !== "custom";
      from.value = committedFrom;
      to.value = committedTo;
    }
    select.addEventListener("change", function () {
      if (select.value === "custom") {
        select.value = applied;
        open();
        return;
      }
      applied = select.value;
      close();
      edit.hidden = true;
      note.textContent = "";
      saveChartRangePref(key, applied);
      reload();
    });
    edit.addEventListener("click", open);
    document.getElementById(id + "-cancel").addEventListener("click", cancel);
    document.getElementById(id + "-apply").addEventListener("click", function () {
      if (!from.value || !to.value || from.value > to.value) {
        error.textContent = "Elige dos fechas en orden.";
        error.hidden = false;
        return;
      }
      committedFrom = from.value;
      committedTo = to.value;
      applied = "custom";
      select.value = applied;
      try {
        localStorage.setItem("orbis.chartRange." + key + ".from", committedFrom);
        localStorage.setItem("orbis.chartRange." + key + ".to", committedTo);
      } catch (e) {}
      saveChartRangePref(key, applied);
      note.textContent = "";
      edit.textContent = "Cambiar fechas";
      close();
      reload();
    });
    dates.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        cancel();
        select.focus();
      }
    });
    return chosen;
  }
  function longRangeStart(range, end) {
    if (range === "all") return null;
    var years = Number(range.slice(0, -1));
    if (![1, 5, 10].includes(years)) return null;
    return window.OrbisMarketHistory.yearsBefore(end, years);
  }
  function loadMarketChart(asset, quote, range, force, dates) {
    if (range === "custom" && (!dates || !dates.from || !dates.to || dates.from > dates.to))
      return Promise.resolve({ok:false, data:{reason:"Elige dos fechas en orden."}});
    var selectedProvider = orbisProvider();
    if (selectedProvider === "auto" && ["1h", "24h", "7d"].includes(range)) {
      return api("/market/history?asset=" + encodeURIComponent(asset) +
        "&quote_currency=" + encodeURIComponent(quote) + "&timeframe=" + encodeURIComponent(range));
    }
    var archive = (selectedProvider === "auto" || selectedProvider === "bitstamp") &&
      asset === "BTC" && ["USD", "EUR"].includes(quote) && ["5y", "10y", "all", "custom"].includes(range);
    /* N intervalos completos requieren N+1 cierres (incluido el ancla). */
    var shortRange = {"1h": {timeframe:"1m", limit:61},
      "24h": {timeframe:"1h", limit:25}, "7d": {timeframe:"1h", limit:169}}[range];
    var cacheKey = [selectedProvider, asset, quote, range,
      dates && dates.from || "", dates && dates.to || ""].join("|");
    var cached = marketLongRangeCache[cacheKey];
    if (!force && !shortRange && cached && Date.now() - cached.at < 60000) return Promise.resolve(cached.result);
    var url = archive ? "/market/btc-daily-archive?quote_currency=" + encodeURIComponent(quote) : "/market/candles?symbol=" +
      encodeURIComponent(asset + "/" + quote) + "&provider=" + encodeURIComponent(selectedProvider) +
      "&timeframe=" + (shortRange ? shortRange.timeframe : "1d") +
      "&limit=" + (shortRange ? shortRange.limit : 1000);
    return api(url).then(function (res) {
      if (!res.ok || !res.data) return res;
      var data = res.data;
      var raw = Array.isArray(data.candles) ? data.candles : [];
      var end = raw.length ? Number(raw[raw.length - 1].t) : 0;
      var start = range === "custom" ? Date.parse(dates.from + "T00:00:00Z") / 1000 :
        shortRange ? end - ({"1h":3600, "24h":86400, "7d":604800}[range]) : longRangeStart(range, end);
      var stop = range === "custom" ? Date.parse(dates.to + "T23:59:59Z") / 1000 : null;
      var exhausted = !!data.history_exhausted, paginationNote = "", gapFound = !!data.gap_count;
      function finish() {
        var points = raw.filter(function (c) {
          return (start === null || Number(c.t) >= start) && (stop === null || Number(c.t) <= stop);
        }).map(function (c) {
          return {t:Number(c.t), open:c.o, high:c.h, low:c.l, close:c.c, closed:c.closed};
        });
        var requested = start !== null ? start : 0;
        var limited = !shortRange && raw.length &&
          (range === "all" ? !archive && !exhausted :
            Number(raw[0].t) > requested + (range === "custom" ? 86400 : 7 * 86400) ||
            (stop !== null && end < stop - 86400));
        var coverageNote = limited ?
          (range === "all" ? "Cobertura cargada desde " : "Cobertura limitada: esta fuente sólo entregó desde ") +
          new Date(Number(raw[0].t) * 1000).toISOString().slice(0, 10) +
          (range === "all" ? "; puede existir más historia." : ".") : "";
        if (range === "custom" && !points.length && raw.length)
          coverageNote += " No hay velas cargadas entre esas fechas.";
        var maxAge = shortRange ? ({"1h":300, "24h":7200, "7d":10800}[range]) : 3 * 86400;
        var staleLast = raw.length && end < Date.now() / 1000 - maxAge;
        var freshnessNote = staleLast ? "La última vela es del " + new Date(end * 1000).toISOString().slice(0, 10) +
          "; faltan datos recientes." : "";
        var result = {ok:true, data:{points:points, source:data.resolved_provider || "none",
          stale:!data.available || !!data.stale || !!staleLast, note:!data.available ? data.reason :
            [data.note, coverageNote, paginationNote, gapFound ? "Hay intervalos sin velas." : "", freshnessNote]
              .filter(Boolean).join(" ") || null}};
        if (!shortRange && data.available && raw.length && !paginationNote)
          marketLongRangeCache[cacheKey] = {at:Date.now(), result:result};
        return result;
      }
      if (shortRange || archive || !data.available || !raw.length || !data.older_pagination_supported ||
          (start !== null && Number(raw[0].t) <= start + 86400)) return finish();
      var pinnedProvider = data.resolved_provider;
      var symbol = asset + "/" + quote;
      return window.OrbisMarketHistory.collect(data, function (before) {
        return api("/market/candles?symbol=" + encodeURIComponent(symbol) +
          "&provider=" + encodeURIComponent(pinnedProvider) + "&timeframe=1d&limit=1000&before=" + before);
      }, {symbol:symbol, timeframe:"1d", reached:function (oldest) { return start !== null && oldest <= start; }})
        .then(function (history) {
          raw = history.rows;
          exhausted = history.exhausted;
          gapFound = history.gaps;
          paginationNote = {
            limit: "Se alcanzó el límite de carga automática.",
            source: "No se pudo completar el histórico anterior con la misma fuente.",
            cursor: "La fuente no avanzó de forma coherente."
          }[history.problem] || "";
          return finish();
        });
    });
  }
  /* M4: envío PAPER compartido Operativa + Estudio (único constructor del
   * body). Genera un request_id NUEVO por llamada con newRequestId() (UUIDv4
   * propio sobre getRandomValues: el helper nativo del navegador falla en
   * http no seguro; ver test_js_uses_safe_uuid_and_same_origin_base): nunca
   * se reutiliza —cada envío, incluido el Reintentar que reenvía el
   * formulario, pasa por aquí y crea su propio id—. Nunca envía strategy_id
   * (el esquema lo prohíbe). */
  function postPaperTrade(side, fields) {
    var f = fields || {};
    var body = {
      request_id: newRequestId(),
      asset: String(f.asset === null || f.asset === undefined ? "" : f.asset).trim().toUpperCase(),
      quote_currency: String(f.quote_currency === null || f.quote_currency === undefined ? "" : f.quote_currency).trim().toUpperCase(),
      price: String(f.price)
    };
    if (side === "sell") {
      body.quantity = String(f.quantity);
      body.fee = String(f.fee === undefined || f.fee === null || f.fee === "" ? "0" : f.fee);
    } else {
      body.order_amount = String(f.order_amount);
      body.estimated_fee = String(f.estimated_fee === undefined || f.estimated_fee === null || f.estimated_fee === "" ? "0" : f.estimated_fee);
    }
    /* Provenance F4: manda exchange=<provider> (campo ya existe). */
    if (f.exchange) {
      body.exchange = String(f.exchange);
    } else {
      try {
        var mp = orbisProvider();
        body.exchange = (mp === "auto") ? String(f.resolved_provider || "kraken") : mp;
      } catch (e) { body.exchange = "kraken"; }
    }
    return api(side === "sell" ? "/paper/sell" : "/paper/buy", { method: "POST", body: JSON.stringify(body) });
  }
  /* Ruta PAPER pública: el precio nunca viaja en el body. El request_id es
   * estable entre reintentos de una misma intención para impedir duplicados. */
  function postMarketPaperTrade(side, fields) {
    var f = fields || {};
    var body = {
      request_id: String(f.request_id || newRequestId()),
      asset: String(f.asset || "").trim().toUpperCase(),
      quote_currency: String(f.quote_currency || "").trim().toUpperCase(),
      provider: String(f.provider || "auto").trim().toLowerCase()
    };
    if (side === "sell") {
      body.quantity = String(f.quantity);
      body.fee = String(f.fee === undefined || f.fee === null || f.fee === "" ? "0" : f.fee);
    } else {
      body.order_amount = String(f.order_amount);
      body.estimated_fee = String(f.estimated_fee === undefined || f.estimated_fee === null || f.estimated_fee === "" ? "0" : f.estimated_fee);
    }
    return api(side === "sell" ? "/paper/market/sell" : "/paper/market/buy",
      { method: "POST", body: JSON.stringify(body) });
  }
  /* M4: par visible del mini-gráfico (Operativa lo fija en refreshMarket y
   * Portfolio en marketBox antes de pintar: el chart se recrea en cada tick,
   * así que el contexto viaja en esta variable y no en el DOM). */
  var miniMarketCtx = null;
  var miniCharts = {};
  var miniChartViews = {};
  function releaseMiniChart(id) {
    var active = miniCharts[id];
    if (!active) return;
    if (active.resizeObserver) active.resizeObserver.disconnect();
    if (active.resizeHandler) window.removeEventListener("resize", active.resizeHandler);
    try {
      var scale = active.chart.timeScale();
      var logical = scale.getVisibleLogicalRange();
      if (logical && isFinite(logical.from) && isFinite(logical.to) && logical.to > logical.from) {
        miniChartViews[id] = {
          key: active.key, logical: logical, count: active.count,
          time: scale.getVisibleRange(),
          atRight: logical.to >= active.count - 1 - 0.5
        };
      }
    } catch (e) { /* mantener el gráfico usable si la escala no responde */ }
    /* El vendor puede tener pintura y resize en cola al desmontar. Retener el
     * chart brevemente, con su observer ya desconectado, permite vaciar esos
     * frames antes de liberar sus canvas. */
    var retiringChart = active.chart;
    setTimeout(function () {
      try { retiringChart.remove(); } catch (e) {}
    }, 500);
    delete miniCharts[id];
  }
  function releaseMiniCharts() {
    Object.keys(miniCharts).forEach(function (id) {
      releaseMiniChart(id);
    });
  }
  /* Constructor puro operación->marcador con la MISMA forma que M1 en
   * estudio.js (círculo compra / cuadrado venta, size 2, texto con cantidad,
   * sin title porque LWC v5.0.9 lo ignora): coherencia entre vistas. */
  function miniOpEpoch(o) {
    try {
      var raw = String((o && o.timestamp) || "").trim().replace(" ", "T");
      var t = Math.floor(Date.parse(/[Zz]$|[+-]\d{2}:\d{2}$/.test(raw) ? raw : raw + "Z") / 1000);
      return isFinite(t) ? t : null;
    } catch (e) { return null; }
  }
  function miniPaperMarks(ops, mk, bars) {
    var out = [];
    var barTimes = Array.isArray(bars) ? bars.map(function (bar) { return Number(bar && bar.t); })
      .filter(function (value) { return isFinite(value); }).sort(function (a, b) { return a - b; }) : [];
    var interval = Infinity;
    for (var i = 1; i < barTimes.length; i++) {
      var delta = barTimes[i] - barTimes[i - 1];
      if (delta > 0 && delta < interval) interval = delta;
    }
    function containingBar(epoch) {
      if (!barTimes.length) return epoch;
      if (!isFinite(interval) || epoch < barTimes[0]) return null;
      var lo = 0, hi = barTimes.length;
      while (lo < hi) {
        var mid = Math.floor((lo + hi) / 2);
        if (barTimes[mid] <= epoch) lo = mid + 1;
        else hi = mid;
      }
      var start = barTimes[lo - 1];
      return epoch < start + interval ? start : null;
    }
    (ops || []).forEach(function (o) {
      if (!o) return;
      if ((o.asset + "/" + o.quote_currency) !== mk) return;
      var t = miniOpEpoch(o);
      if (t === null) return;
      t = containingBar(t);
      if (t === null) return;
      var isBuy = o.operation_type === "BUY";
      var qty = null;
      try { qty = fmtQty(o.asset_received, o.asset); } catch (e) { qty = String(o.asset_received); }
      out.push({
        time: t,
        position: isBuy ? "belowBar" : "aboveBar",
        shape: isBuy ? "circle" : "square",
        color: isBuy ? "#3fb950" : "#f85149",
        size: 2,
        text: isBuy ? ("P · " + qty) : "S"
      });
    });
    return out;
  }
  /* Aplica los marcadores PAPER del par visible sobre la serie recién creada.
   * Instancia única por serie (guardada en la propia serie): setMarkers
   * reemplaza, nunca se adjuntan primitivos en bucle. Sin operaciones deja la
   * serie limpia sin ruido. */
  function refreshMiniMarks(series, mk, bars) {
    api("/paper/operations?limit=200&order=asc").then(function (r) {
      if (!series) return;
      var ops = (r.ok && r.data) ? r.data : [];
      var marks = miniPaperMarks(ops.filter(function (o) {
        return o && (o.asset + "/" + o.quote_currency) === mk;
      }), mk, bars);
      try {
        if (!series._miniMarks) { series._miniMarks = window.LightweightCharts.createSeriesMarkers(series, []); }
        series._miniMarks.setMarkers(marks);
      } catch (e) { /* motor ausente: el canvas propio ya pintó */ }
    }).catch(function () { /* secundario: el gráfico queda sin marcas */ });
  }
  /* Mejora progresiva F4: gráfico LWC embebido (vendor propio) si existe;
   * si no, se conserva el canvas propio. Devuelve true si pintó con LWC. */
  function paintLwcMini(id, pts, viewKey) {
    var el, parent, host, ch, resizeObserver, resizeHandler;
    try {
      var LW = window.LightweightCharts;
      el = document.getElementById(id);
      if (!LW || !el || !pts || pts.length < 2) return false;
      parent = el.parentNode;
      host = document.createElement("div");
      releaseMiniChart(id);
      host.className = el.className;
      host.setAttribute("role", "img");
      host.setAttribute("aria-label", "Gráfico de precio");
      parent.replaceChild(host, el);
      host.id = id;
      /* Tanda C7: el mini vive sobre UI oscura: mismos fondo/grid/texto que
       * el Estudio (fondo #08100f, texto #9caaa4, rejilla #172521) en vez
       * del tema claro por defecto del vendor. Solo opciones del chart del
       * mini; el Estudio no se toca. */
      ch = LW.createChart(host, {
        /* Controlar el observer nosotros permite desconectarlo ANTES de
         * remove(); el autoSize interno puede dejar un frame sobre canvas
         * disposed durante navegación rápida. */
        autoSize: false,
        width: Math.max(1, host.clientWidth), height: Math.max(1, host.clientHeight),
        layout: { background: { color: "#08100f" }, textColor: "#9caaa4" },
        grid: { vertLines: { color: "#172521" }, horzLines: { color: "#172521" } },
        timeScale: {minBarSpacing: 0.1, lockVisibleTimeRangeOnResize: id === "home-chart-cv"},
        localization: { locale: "es-ES" }
      });
      var hasOHLC = pts.some(function (p) { return p.open !== undefined; });
      var s;
      if (hasOHLC) {
        s = ch.addSeries(LW.CandlestickSeries, { upColor: "#26a69a", downColor: "#ef5350", borderUpColor: "#26a69a", borderDownColor: "#ef5350", wickUpColor: "#26a69a", wickDownColor: "#ef5350" });
        s.setData(pts.map(function (p) {
          return { time: Math.floor(numOf(p.t)) , open: Number(p.open), high: Number(p.high), low: Number(p.low), close: Number(p.close) };
        }));
      } else {
        s = ch.addSeries(LW.LineSeries, { color: "#4da3ff", lineWidth: 1, priceLineVisible: false });
        s.setData(pts.map(function (p, i) {
          return { time: Math.floor(numOf(p.t) || (Date.now() / 1000 - (pts.length - i) * 60)), value: Number(p.close) };
        }));
      }
      ch.timeScale().fitContent();
      var previous = miniChartViews[id];
      if (previous && previous.key === viewKey) {
        try {
          if (previous.atRight) {
            var width = previous.logical.to - previous.logical.from;
            var rightGap = previous.logical.to - (previous.count - 1);
            var right = pts.length - 1 + rightGap;
            ch.timeScale().setVisibleLogicalRange({ from: right - width, to: right });
          } else if (previous.time) {
            ch.timeScale().setVisibleRange(previous.time);
          }
        } catch (e) { ch.timeScale().fitContent(); }
      }
      /* M4: el chart se recrea en cada tick: repintar las marcas PAPER del
       * par visible en cada creación (no solo al cambiar de par). Solo PAPER:
       * en LIVE no hay operativa simulada que marcar. */
      try {
        if (orbisMode() === "PAPER" && miniMarketCtx) refreshMiniMarks(s, miniMarketCtx, pts);
      } catch (e) { /* sin marcas: el gráfico sigue válido */ }
      resizeHandler = function () {
        if (!miniCharts[id] || miniCharts[id].chart !== ch || !host.isConnected) return;
        if (!host.clientWidth || !host.clientHeight) return;
        try { ch.resize(host.clientWidth, host.clientHeight); } catch (e) {}
      };
      miniCharts[id] = {chart: ch, key: viewKey, count: pts.length};
      if (window.ResizeObserver) {
        resizeObserver = new ResizeObserver(resizeHandler);
        resizeObserver.observe(host);
        miniCharts[id].resizeObserver = resizeObserver;
      } else {
        window.addEventListener("resize", resizeHandler);
        miniCharts[id].resizeHandler = resizeHandler;
      }
      return true;
    } catch (e) {
      if (resizeObserver) resizeObserver.disconnect();
      if (resizeHandler) window.removeEventListener("resize", resizeHandler);
      if (ch) { try { ch.remove(); } catch (ignored) {} }
      if (parent && host && el && host.parentNode === parent) parent.replaceChild(el, host);
      return false;
    }
  }
  function numOf(v) { var n = Number(v); return isFinite(n) ? n : 0; }
  /* Cambio 24h solo presentación desde cierres OHLC (último vs primero). */
  function change24h(pts) {
    if (!pts || pts.length < 2) return null;
    var first = Number(pts[0].close), last = Number(pts[pts.length - 1].close);
    if (!isFinite(first) || !isFinite(last) || first === 0) return null;
    return (last - first) / Math.abs(first) * 100;
  }
  /* Un porcentaje con etiqueta 24h/7d exige la ventana completa y
   * sin una vela ausente entre las observadas. Frescura y fuente se comprueban en
   * cada vista por separado; esta función sólo valida cobertura temporal. */
  function marketWindowChange(pts, period) {
    var windowSeconds = period === "24h" ? 86400 : period === "7d" ? 604800 : 0;
    var stepSeconds = period === "24h" ? 3600 : period === "7d" ? 86400 : 0;
    if (!windowSeconds || !pts || pts.length < 2) return null;
    var first = Number(pts[0].t), last = Number(pts[pts.length - 1].t);
    if (!isFinite(first) || !isFinite(last) || last - first < windowSeconds ||
        last - first > windowSeconds + stepSeconds) return null;
    for (var i = 1; i < pts.length; i++) {
      var previous = Number(pts[i - 1].t), current = Number(pts[i].t);
      if (!isFinite(previous) || !isFinite(current) || current <= previous ||
          current - previous > 1.5 * stepSeconds) return null;
    }
    return change24h(pts);
  }

  /* ---------------------- ESTADO GLOBAL DEL MERCADO ---------------------- */
  function paintMarketStatus() {
    var el = document.getElementById("mkt-status");
    if (!el) return;
    if (!mktLast || mktLast.price === null || mktLast.price === undefined) {
      el.textContent = "Mercado: precio no disponible";
      return;
    }
    var age = (mktLast.age_seconds === null || mktLast.age_seconds === undefined)
      ? 0 : Math.round(Number(mktLast.age_seconds)) + Math.round((Date.now() - mktLast.at) / 1000);
    el.textContent = "BTC/EUR " + fmtMoney(mktLast.price, "EUR") +
      " · Actualizado " + fmtAge(age) + " · " + sourceLabel(mktLast.source);
  }
  function fetchMarketStatus() {
    api("/market/price?asset=BTC&quote_currency=EUR").then(function (res) {
      if (res.ok && res.data) {
        mktLast = { price: res.data.price, age_seconds: res.data.age_seconds, source: res.data.source, at: Date.now() };
      } else {
        mktLast = null;
      }
      paintMarketStatus();
    }).catch(function () { mktLast = null; paintMarketStatus(); });
  }
  function armMarketStatus() {
    if (mktTimer) return;
    fetchMarketStatus();
    mktTimer = setInterval(function () {
      if (document.hidden) return;
      paintMarketStatus();
      var elapsed = mktLast ? (Date.now() - mktLast.at) / 1000 : 9999;
      if (elapsed > 30) fetchMarketStatus();
    }, 5000);
  }

  /* -------------------------------- INICIO -------------------------------- */
  function stopHomeTimer() {
    if (homeTimer) { clearInterval(homeTimer); homeTimer = null; }
  }
  function renderInicio() {
    stopHomeTimer();
    armMarketStatus();
    var initialHomeQuote = homeQuotePreference();
    view.innerHTML =
      '<section class="card pagina-inicio" aria-live="off"><div class="pagina-cabecera"><div><h2 data-help="Resumen de PAPER, cartera REAL desbloqueada, mercados y actividad; cada entorno conserva sus cifras.">Inicio</h2>' +
      '<span class="muted" id="home-live"></span></div>' +
      '<div class="home-workspace-tools"><label class="field">Espacio<select id="home-workspace">' +
      '<option value="general">General</option><option value="trading">Trading</option>' +
      '<option value="research">Investigación</option><option value="portfolio">Cartera</option></select></label>' +
      '<label class="field" data-help="Elige la divisa del resumen PAPER y del gráfico BTC. EUR, USD y USDT mantienen saldos separados; no se convierten entre sí.">Divisa<select id="home-quote">' +
      pairOptions(SUPPORTED_QUOTES, initialHomeQuote) + '</select></label></div></div>' + modeTag() +
      '<details id="home-workspace-editor"><summary>Paneles</summary><ul data-workspace-rows></ul>' +
      '<div class="home-workspace-footer"><button type="button" class="ghost" data-workspace-reset>Restablecer espacio</button>' +
      '<span class="muted" role="status" data-workspace-status></span></div></details>' +
      '<div class="inicio-rejilla home-panel-grid" id="home-panel-grid">' +
      '<section class="home-paper-summary" data-home-panel="paper" data-home-width="full" aria-labelledby="home-paper-title"><div class="home-paper-head">' +
      '<h3 id="home-paper-title" data-help="Capital y operaciones ficticias; no incluye patrimonio registrado ni órdenes reales.">Simulación <span>PAPER</span></h3>' +
      '<a href="#/operativa">Operar</a></div>' +
      '<div id="home-stats" class="stats">' + stateBox("loading", "Cargando tu resumen…") + "</div></section>" +
      '<section class="home-market-panel" id="home-market-panel" data-home-panel="market" data-home-width="full"><div class="pagina-cabecera"><h3 id="home-market-heading">BTC/' + initialHomeQuote + '</h3>' +
      marketRangePicker("home-range") +
      '<button type="button" id="home-market-refresh" class="ghost" data-help="Consulta de nuevo el precio y el histórico públicos.">Actualizar</button></div>' +
      '<div id="home-market" aria-live="off">' + stateBox("loading", "Cargando mercado…") + '</div></section>' +
      '<section class="bloque-inicio" data-home-panel="real"><h3 data-help="Libro manual cifrado; no habilita compras ni ventas reales.">Patrimonio registrado</h3><div id="home-real">' + stateBox("loading", "Comprobando bóveda local…") + '</div></section>' +
      '<section class="bloque-inicio" data-home-panel="watch"><h3>Mercados seguidos</h3><div id="home-wl">' + stateBox("loading", "Cargando mercados…") + "</div></section>" +
      '<section class="bloque-inicio" data-home-panel="strategies"><h3>Planes PAPER</h3><div id="home-strat">' + stateBox("loading", "Cargando…") + "</div>" +
      '<p><a class="cta" href="#/estrategias">Ver estrategias</a></p></section>' +
      '<section class="bloque-inicio" data-home-panel="activity"><h3>Movimientos PAPER</h3><div id="home-ops">' + stateBox("loading", "Cargando…") + "</div>" +
      '<p><a class="cta" href="#/ledger">Ver todos</a></p></section>' +
      '<section class="bloque-inicio" data-home-panel="dca"><h3>Planes de aportación</h3><div id="home-plans">' + stateBox("loading", "Cargando…") + "</div></section>" +
      '<section class="bloque-inicio" data-home-panel="alerts"><h3>Alertas</h3><p id="home-alerts" class="muted"></p>' +
      '<p><a class="cta" href="#/alertas">Ver alertas</a></p></section></div></section>';

    window.OrbisMarketAlerts.home(document.getElementById("home-alerts"));
    var homeMarketSeq = 0;
    var marketPanel = document.getElementById("home-market-panel");
    var marketDirty = true;
    var homeRequestSeq = 0;
    var homeAppliedSeq = Object.create(null);
    function homeQuote() { return document.getElementById("home-quote").value; }
    function homeResponseMayApply(seq, id, box) {
      return seq >= (homeAppliedSeq[id] || 0) && box.isConnected &&
        document.getElementById(id) === box &&
        (!location.hash || location.hash === "#/inicio");
    }
    function homeResponseApplied(seq, id) {
      homeAppliedSeq[id] = seq;
    }
    var homeChosenDates = bindMarketRange("home-range", "home", loadHomeMarket);
    function loadHomeMarket(force) {
      var seq = ++homeMarketSeq;
      if (!marketPanel.isConnected || marketPanel.hidden) { marketDirty = true; return; }
      var box = document.getElementById("home-market");
      var quote = homeQuote();
      var range = document.getElementById("home-range").value;
      var dates = homeChosenDates();
      if (dates === false) return;
      if (!box) return;
      marketDirty = false;
      releaseMiniChart("home-chart-cv");
      box.innerHTML = stateBox("loading", "Cargando BTC/" + quote + "…");
      return Promise.all([
        api("/market/price?asset=BTC&quote_currency=" + encodeURIComponent(quote)),
        loadMarketChart("BTC", quote, range, force === true, dates)
      ]).then(function (responses) {
        if (!box.isConnected || seq !== homeMarketSeq) return;
        if (marketPanel.hidden) { marketDirty = true; return; }
        var price = responses[0], history = responses[1];
        var data = history.ok && history.data ? history.data : {};
        var points = Array.isArray(data.points) ? data.points : [];
        if (points.length) marketLastCandleTime["home-range"] = Number(points[points.length - 1].t);
        var priceAvailable = price.ok && price.data && price.data.price !== null && price.data.price !== undefined;
        var homePriceSource = priceAvailable ? price.data.source : null;
        var homeChange = range === "24h" && !data.stale && priceAvailable && !price.data.stale &&
          marketSourcesMatch(homePriceSource, data.source) ? marketWindowChange(points, "24h") : null;
        box.innerHTML = (priceAvailable ? priceTicker("BTC", quote, price.data, homeChange) :
          stateBox("error", "Precio BTC/" + quote + " no disponible. El histórico se muestra por separado.")) +
          chartBlock("home-chart-cv", points, range, data.source, data.stale,
            withMarketSourceNote(data.note, homePriceSource, data.source));
        miniMarketCtx = "BTC/" + quote;
        paintChart("home-chart-cv", points, "BTC/" + quote + "|" + range + "|" + (dates ? dates.from + "|" + dates.to : ""));
      }).catch(function () {
        if (!box.isConnected || seq !== homeMarketSeq) return;
        if (marketPanel.hidden) { marketDirty = true; return; }
        box.innerHTML = stateBox("error", "No se pudo cargar el mercado. Pulsa Actualizar.");
      });
    }
    document.getElementById("home-quote").addEventListener("change", function () {
      var quote = homeQuote();
      try { localStorage.setItem("orbis.home.quote", quote); } catch (e) {}
      document.getElementById("home-market-heading").textContent = "BTC/" + quote;
      delete marketLastCandleTime["home-range"];
      loadHomeMarket(false);
      loadHome(false);
    });
    document.getElementById("home-market-refresh").addEventListener("click", function () { loadHomeMarket(true); });
    if (window.OrbisHomeWorkspaces) {
      window.OrbisHomeWorkspaces.mount(document.getElementById("home-panel-grid"),
        document.getElementById("home-workspace"), document.getElementById("home-workspace-editor"), function () {
          if (marketDirty && !marketPanel.hidden) loadHomeMarket(false);
        });
    } else {
      document.getElementById("home-workspace").disabled = true;
      document.getElementById("home-workspace-editor").hidden = true;
      loadHomeMarket();
    }

    function loadHome(silent) {
      var live = document.getElementById("home-live");
      if (location.hash && location.hash !== "" && location.hash !== "#/inicio") return;
      if (document.hidden) return;
      if (live) live.textContent = silent ? "Refrescando…" : "";
      var statsBox = document.getElementById("home-stats");
      var opsBox = document.getElementById("home-ops");
      if (!statsBox || !opsBox) return;
      var seq = ++homeRequestSeq;
      var quote = homeQuote();
      if (window.OrbisReal) window.OrbisReal.renderDashboard(document.getElementById("home-real"));
      api("/paper/portfolio?quote_currency=" + encodeURIComponent(quote)).then(function (res) {
        if (!homeResponseMayApply(seq, "home-stats", statsBox)) return;
        homeResponseApplied(seq, "home-stats");
        if (res.status === 404) {
          statsBox.innerHTML = emptyState("Empieza con 2 pasos", "<li>Añade saldo PAPER</li><li>Haz tu primera compra</li>",
            '<a class="cta" href="#/operativa">Añadir saldo PAPER</a>');
          opsBox.innerHTML = "";
          if (live) live.textContent = "";
          return;
        }
        if (!res.ok) {
          statsBox.innerHTML = httpErrorBox(res, { quote: quote });
          armAnyRetry(statsBox, function () { loadHome(false); });
          if (live) live.textContent = "";
          return;
        }
        var p = res.data || {};
        var positions = Array.isArray(p.positions) ? p.positions : [];
        var btc = null;
        positions.forEach(function (pos) { if (pos.asset === "BTC") btc = pos; });
        var invested = (p.positions_value !== undefined && p.positions_value !== null) ? p.positions_value : "—";
        var estimated = portfolioEstimate(p);
        statsBox.innerHTML =
          statCard(estimated ? "Valor PAPER estimado" : "Valor PAPER", esc(fmtMoney(p.equity, p.quote_currency)), estimated ? "Incluye precios sin cotización reciente" : "Disponible + posiciones a precio de referencia") +
          statCard(estimated ? "Resultado PAPER estimado" : "Resultado PAPER", esc(fmtMoney(p.total_pnl, p.quote_currency)) + " · " + esc(fmtPctShown(p.return_percentage)), estimated ? "La valoración puede cambiar al actualizar precios" : "Realizado + no realizado") +
          statCard("Disponible", esc(fmtMoney(p.cash_balance, p.quote_currency)), "Lo que puedes gastar") +
          statCard("Invertido", esc(fmtMoney(invested, p.quote_currency)), estimated ? "Valor estimado de tus monedas" : "Valor de referencia de tus monedas") +
          statCard("BTC", btc ? esc(fmtQty(btc.quantity, "BTC")) : "—", btc ? (estimated ? "Valor estimado " : "Valor de referencia ") + fmtMoney(btc.value, p.quote_currency) : "Aún no tienes BTC");
        if (live) live.textContent = "";
      }).catch(function () {
        if (!homeResponseMayApply(seq, "home-stats", statsBox)) return;
        homeResponseApplied(seq, "home-stats");
        statsBox.innerHTML = networkBox("home");
        armRetry(statsBox, "home", function () { loadHome(false); });
        if (live) live.textContent = "";
      });
      api("/paper/operations?limit=5&order=desc").then(function (res) {
        if (!homeResponseMayApply(seq, "home-ops", opsBox)) return;
        homeResponseApplied(seq, "home-ops");
        if (!res.ok) { opsBox.innerHTML = ""; return; }
        var items = res.data || [];
        if (!items.length) { opsBox.innerHTML = ""; return; }
        var html = '<ul class="miniops">';
        items.forEach(function (o) {
          var isBuy = o.operation_type === "BUY";
          html += '<li><span class="pillop ' + (isBuy ? "buy" : "sell") + '">' + (isBuy ? "Compra" : "Venta") + "</span> " +
            esc(o.asset) + "/" + esc(o.quote_currency) + " · " + esc(fmtMoney(o.amount_spent, o.quote_currency)) +
            ' <span class="muted">' + esc(fmtDateTime(o.timestamp)) + "</span></li>";
        });
        opsBox.innerHTML = html + "</ul>";
      }).catch(function () { /* secundario: se deja vacío sin ruido */ });
      loadHomeWatchlist(seq);
      loadHomeStrategies(seq);
    }
    function loadHomeWatchlist(seq) {
      var box = document.getElementById("home-wl");
      if (!box) return;
      var local = [];
      try { local = JSON.parse(localStorage.getItem("orbis.watchlist") || "[]"); } catch (e) { local = []; }
      api("/ux/watchlist").then(function (res) {
        if (!homeResponseMayApply(seq, "home-wl", box)) return;
        homeResponseApplied(seq, "home-wl");
        var server = (res.ok && Array.isArray(res.data)) ? res.data : [];
        var merged = server.length ? server : local;
        if (!merged.length) {
          box.innerHTML = '<p class="muted">Aún no sigues ningún mercado. Añádelo desde el <a href="#/estudio">Estudio</a>.</p>';
          return;
        }
        var html = '<ul class="wl">';
        merged.slice(0, 6).forEach(function (w) {
          var mk = (typeof w === "string") ? w : (w.market || "");
          var pv = (w && w.provider) ? w.provider : orbisProvider();
          html += '<li data-wl="' + esc(mk) + '" data-pv="' + esc(pv) + '">' +
            "<span><strong>" + esc(mk) + '</strong> <span class="muted">' + esc(pv) + '</span><br><span class="muted" data-wlprice>…</span></span>' +
             '<a class="cta" href="#/estudio">Abrir gráfico</a></li>';
        });
        box.innerHTML = html + "</ul>";
        box.querySelectorAll("[data-wl]").forEach(function (li) {
          var sym = li.getAttribute("data-wl"), pv = li.getAttribute("data-pv");
          api("/market/snapshot?symbol=" + encodeURIComponent(sym) + "&provider=" + encodeURIComponent(pv === "auto" ? "auto" : pv)).then(function (r) {
            if (!homeResponseMayApply(seq, "home-wl", box) ||
                homeAppliedSeq["home-wl"] !== seq || !li.isConnected) return;
            var cell = li.querySelector("[data-wlprice]");
            if (!cell) return;
            if (r.ok && r.data && r.data.available) {
              cell.textContent = fmtMoney(r.data.price, sym.split("/")[1]) +
                (r.data.change_pct !== null && r.data.change_pct !== undefined ? " · 24h " + fmtPctShown(r.data.change_pct) : "") +
                " · " + (r.data.resolved_provider || pv);
            } else { cell.textContent = "precio no disponible"; }
          }).catch(function () {});
        });
      }).catch(function () {
        if (!homeResponseMayApply(seq, "home-wl", box)) return;
        homeResponseApplied(seq, "home-wl");
        box.innerHTML = '<p class="muted">No se han podido cargar los mercados seguidos.</p>';
      });
    }
    function loadHomeStrategies(seq) {
      var box = document.getElementById("home-strat");
      var plans = document.getElementById("home-plans");
      if (!box) return;
      api("/strategies").then(function (res) {
        if (!homeResponseMayApply(seq, "home-strat", box)) return;
        homeResponseApplied(seq, "home-strat");
        if (!res.ok) { box.innerHTML = ""; if (plans) plans.innerHTML = ""; return; }
        var items = res.data || [];
        if (!items.length) {
          box.innerHTML = '<p class="muted">Sin estrategias. <a href="#/estrategias">Crear una</a>.</p>';
          if (plans) plans.innerHTML = '<p class="muted">Sin planes.</p>';
          return;
        }
        var html = '<ul class="miniops">';
        items.slice(0, 3).forEach(function (s) {
          html += "<li><strong>" + esc(s.name) + "</strong> " + statusBadge(!!s.enabled) + "</li>";
        });
        box.innerHTML = html + "</ul>";
        if (plans) {
          var ph = '<ul class="miniops">';
          items.slice(0, 3).forEach(function (s) {
            var cfg = (s.config) || {};
            ph += "<li>Plan " + esc(s.name) + " · " + esc(String(cfg.asset || cfg.base || "—")) +
              "/" + esc(String(cfg.quote_currency || cfg.quote || "—")) + ' · <a href="#/estudio">Abrir en Estudio</a></li>';
          });
          plans.innerHTML = ph + "</ul>";
        }
      }).catch(function () { /* secundario */ });
    }
    loadHome(false);
    homeTimer = setInterval(function () { loadHome(true); }, HOME_REFRESH_MS);
  }

  /* ------------------------------- OPERATIVA ------------------------------ */
  function renderOperativa() {
    stopHomeTimer();
    view.innerHTML =
      '<section class="card pagina-operativa"><div class="pagina-cabecera operativa-cabecera"><div class="operativa-title"><h2 data-help="Compra y venta con saldo ficticio. No envía órdenes a exchanges.">Operativa simulada</h2>' +
      '<p class="muted" id="op-refresh-info">Cada 15 s · <button type="button" id="op-refresh-now" class="ghost" data-help="Consulta de nuevo el precio y saldo disponibles.">Actualizar</button></p></div>' +
      '<div class="row"><a class="ghost" href="#/portfolio" data-help="Consulta tus saldos y activos ficticios; no son patrimonio real.">Ver saldo ficticio</a>' +
      '<a class="cta operativa-perp" href="#/perpetuos">Perpetuos GRVT · PAPER manual</a></div></div>' + modeTag() +
      '<div class="pairbar">' +
      '<label class="field">Moneda digital <select id="t-asset">' + pairOptions(SUPPORTED_ASSETS, "BTC") + "</select></label>" +
      '<label class="field">Moneda de pago <select id="t-quote">' + pairOptions(SUPPORTED_QUOTES, "EUR") + "</select></label>" +
      marketRangePicker("t-range") +
      '<button type="button" id="t-reload" class="ghost">Actualizar precio</button></div>' +
      '<div id="t-market" aria-live="off">' + stateBox("loading", "Cargando precio del mercado…") + "</div>" +
      '<div id="t-chart"></div>' +
      '<div id="t-cash" aria-live="off">' + stateBox("loading", "Cargando tu saldo…") + "</div>" +
      '' +
      '<div class="tabs" role="tablist" aria-label="Comprar o vender">' +
      '<button type="button" id="tab-buy" class="active" role="tab" aria-selected="true" aria-controls="panel-buy" tabindex="0">Comprar</button>' +
      '<button type="button" id="tab-sell" role="tab" aria-selected="false" aria-controls="panel-sell" tabindex="-1">Vender</button></div>' +
      '<div id="panel-buy" role="tabpanel" aria-labelledby="tab-buy" tabindex="0">' +
      '<form id="f-buy"><div class="row">' +
      '<p class="avail" id="buy-avail" aria-live="off"></p>' +
      '<label class="field">Importe <span class="ccy" data-ccy>EUR</span> <input name="order_amount" value="100" inputmode="decimal" required></label>' +
      '<p class="marketprice" data-help="Referencia para el estimado. La compra PAPER usa una cotización pública nueva al enviarse; puede diferir.">Precio ref.: <strong id="buy-price">—</strong></p>' +
      '<p class="muted" id="buy-est" aria-live="off"></p>' +
      '<details class="adv"><summary>Opciones avanzadas</summary>' +
      '<label class="field">Comisión estimada <input name="estimated_fee" value="0" inputmode="decimal"></label></details>' +
      '<p class="total" id="buy-total" aria-live="off"></p>' +
      '<button type="submit" id="b-buy">Comprar</button></div></form><div id="r-buy"></div></div>' +
      '<div id="panel-sell" role="tabpanel" aria-labelledby="tab-sell" tabindex="0" hidden>' +
      '<form id="f-sell"><div class="row">' +
      '<p class="avail" id="sell-avail" aria-live="off"></p>' +
      '<label class="field">Cantidad <span class="ccy-a" data-asset>BTC</span> <input name="quantity" value="0.001" inputmode="decimal" required></label>' +
      '<p class="marketprice" data-help="Referencia para el estimado. La venta PAPER usa una cotización pública nueva al enviarse; puede diferir.">Precio ref.: <strong id="sell-price">—</strong></p>' +
      '<p class="muted" id="sell-est" aria-live="off"></p>' +
      '<details class="adv"><summary>Opciones avanzadas</summary>' +
      '<label class="field">Comisión <input name="fee" value="0" inputmode="decimal"></label></details>' +
      '<p class="total" id="sell-total" aria-live="off"></p>' +
      '<button type="submit" id="b-sell">Vender</button></div></form><div id="r-sell"></div></div>' +
      '<details class="start"><summary>Añadir saldo de simulación</summary>' +
      '<form id="f-scenario"><div class="row">' +
      '<label class="field">Moneda de pago <select name="quote_currency">' + pairOptions(SUPPORTED_QUOTES, "EUR") + "</select></label>" +
      '<label class="field">Saldo inicial <input name="initial_balance" value="1000" inputmode="decimal" required></label>' +
      '<label class="field">Máximo por orden <input name="max_order_amount" value="500" inputmode="decimal" required></label>' +
      '<label class="field">Comisión máxima % <input name="max_fee_percentage" value="2" inputmode="decimal" required></label>' +
      '<button type="submit">Crear saldo</button></div></form><div id="r-scenario"></div></details>' +
      "</section>";

    var assetSel = document.getElementById("t-asset");
    var quoteSel = document.getElementById("t-quote");
    var operativaChosenDates = bindMarketRange("t-range", "operativa", refreshMarket);
    var buyForm = document.getElementById("f-buy");
    var sellForm = document.getElementById("f-sell");
    var lastMarket = null;
    var lastChange = null;
    var lastCash = null;
    var lastQty = null;
    var marketSeq = 0;
    var lastMarketKey = null;
    var marketReceivedMs = null;

    function selAsset() { return upQuote(assetSel.value) || "BTC"; }
    function selQuote() { return upQuote(quoteSel.value) || "EUR"; }
    function marketPriceUsable(side) {
      var age = lastMarket && Number(lastMarket.age_seconds);
      var reference = lastMarket && (side === "sell" ? lastMarket.sell_price : lastMarket.buy_price);
      return !!lastMarket && lastMarketKey === selAsset() + "/" + selQuote() &&
        lastMarket.available === true && lastMarket.provider === "kraken" &&
        lastMarket.stale === false &&
        reference !== null && reference !== undefined &&
        Number.isFinite(Number(reference)) && Number(reference) > 0 &&
        lastMarket.age_seconds !== null && lastMarket.age_seconds !== undefined &&
        Number.isFinite(age) && age >= 0 &&
        marketReceivedMs !== null && age + (performance.now() - marketReceivedMs) / 1000 <= 60;
    }
    function clearMarketPrice() {
      lastMarket = null;
      lastMarketKey = null;
      marketReceivedMs = null;
      var bp = document.getElementById("buy-price");
      var sp = document.getElementById("sell-price");
      if (bp) bp.textContent = "—";
      if (sp) sp.textContent = "—";
      updateEstimates();
    }

    function paintCcy() {
      var q = selQuote(), a = selAsset();
      var els = document.querySelectorAll("[data-ccy]");
      for (var i = 0; i < els.length; i++) els[i].textContent = q;
      var elsA = document.querySelectorAll("[data-asset]");
      for (var j = 0; j < elsA.length; j++) elsA[j].textContent = a;
    }

    function refreshMarket(forceHistory) {
      var seq = ++marketSeq;
      var asset = selAsset(), quote = selQuote();
      clearMarketPrice();
      var range = document.getElementById("t-range").value;
      var dates = operativaChosenDates();
      if (dates === false) return Promise.resolve(null);
      var box = document.getElementById("t-market");
      var chartBox = document.getElementById("t-chart");
      releaseMiniChart("t-chart-cv");
      if (box) box.innerHTML = stateBox("loading", "Cargando precio de " + esc(asset) + "/" + esc(quote) + "…");
      var priceP = api("/paper/market/preview?asset=" + encodeURIComponent(asset) +
        "&quote_currency=" + encodeURIComponent(quote) + "&provider=kraken");
      var histP = loadMarketChart(asset, quote, range, forceHistory === true, dates);
      return Promise.all([priceP, histP]).then(function (arr) {
        var res = arr[0], hres = arr[1];
        if (!document.getElementById("t-market") || seq !== marketSeq) return null;
        if (!res.ok) {
          var offlineHistory = hres.ok && hres.data ? hres.data : {};
          var offlinePoints = Array.isArray(offlineHistory.points) ? offlineHistory.points : [];
          box.innerHTML = stateBox("error", "<strong>Precio no disponible ahora mismo.</strong> No se puede operar; el histórico se muestra por separado.");
          if (chartBox) {
            chartBox.innerHTML = chartBlock("t-chart-cv", offlinePoints, range,
              offlineHistory.source, true, offlineHistory.note || "Sin cotización actual.");
            paintChart("t-chart-cv", offlinePoints, asset + "/" + quote + "|" + range + "|" +
              (dates ? dates.from + "|" + dates.to : ""));
          }
          showToast("error", "Precio de " + asset + "/" + quote + " no disponible. Pulsa Actualizar precio.");
          return null;
        }
        var m = res.data || {};
        lastMarket = m;
        lastMarketKey = asset + "/" + quote;
        marketReceivedMs = performance.now();
        miniMarketCtx = asset + "/" + quote; /* M4: el mini pintará sus marcas PAPER */
        var pts = (hres.ok && hres.data && Array.isArray(hres.data.points)) ? hres.data.points : [];
        if (pts.length) marketLastCandleTime["t-range"] = Number(pts[pts.length - 1].t);
        var historySource = hres.ok && hres.data ? hres.data.source : null;
        lastChange = range === "24h" && !m.stale && hres.ok && hres.data && !hres.data.stale &&
          marketSourcesMatch(m.provider, historySource) ? marketWindowChange(pts, "24h") : null;
        box.innerHTML = priceTicker(asset, quote, m, lastChange);
        var bp = document.getElementById("buy-price");
        var sp = document.getElementById("sell-price");
        if (bp) bp.textContent = marketPriceUsable("buy") ? (m.buy_approximate ? "≈ " : "") + fmtMoney(m.buy_price, quote) : "—";
        if (sp) sp.textContent = marketPriceUsable("sell") ? (m.sell_approximate ? "≈ " : "") + fmtMoney(m.sell_price, quote) : "—";
        if (chartBox) {
          chartBox.innerHTML = chartBlock("t-chart-cv", pts, range, historySource,
            !hres.ok || !hres.data || hres.data.stale,
            withMarketSourceNote(hres.ok && hres.data ? hres.data.note : null,
              m.provider, historySource));
          paintChart("t-chart-cv", pts, asset + "/" + quote + "|" + range + "|" +
            (dates ? dates.from + "|" + dates.to : ""));
        }
        updateEstimates();
        return m;
      }).catch(function () {
        if (!document.getElementById("t-market") || seq !== marketSeq) return null;
        box.innerHTML = networkBox("mkt");
        armRetry(box, "mkt", refreshMarket);
        showToast("error", "No hemos podido conectar. ¿Está el servidor arrancado?");
        return null;
      });
    }

    function refreshCash() {
      var asset = selAsset(), quote = selQuote();
      var box = document.getElementById("t-cash");
      return api("/paper/portfolio?quote_currency=" + encodeURIComponent(quote)).then(function (res) {
        if (!document.getElementById("t-cash")) return;
        if (res.status === 404) {
          lastCash = null; lastQty = null;
          box.innerHTML = emptyState("Aún no tienes saldo en " + quote,
            "<li>Añade saldo PAPER abajo</li><li>Elige el par y confirma tu compra</li>", "");
          updateEstimates();
          return;
        }
        if (!res.ok) {
          box.innerHTML = httpErrorBox(res, { quote: quote });
          armAnyRetry(box, function () { refreshMarket(); refreshCash(); });
          return;
        }
        var p = res.data || {};
        lastCash = p.cash_balance;
        lastQty = null;
        (p.positions || []).forEach(function (pos) {
          if (pos.asset === asset) lastQty = pos.quantity;
        });
        box.innerHTML = '<dl class="kv">' +
          "<dt>Tu saldo disponible</dt><dd><strong>" + esc(fmtMoney(p.cash_balance, p.quote_currency)) + "</strong></dd>" +
          "<dt>Patrimonio total (servidor)</dt><dd>" + esc(fmtMoney(p.equity, p.quote_currency)) + "</dd></dl>";
        updateEstimates();
      }).catch(function () {
        if (!document.getElementById("t-cash")) return;
        box.innerHTML = networkBox("cash");
        armRetry(box, "cash", refreshCash);
      });
    }

    /* Solo presentación: el servidor valida la orden final con Guard. */
    function updateEstimates() {
      var quote = selQuote(), asset = selAsset();
      var decimal = window.OrbisDecimal;
      var buyAvail = document.getElementById("buy-avail");
      var sellAvail = document.getElementById("sell-avail");
      if (buyAvail) {
        buyAvail.textContent = (lastCash === null || lastCash === undefined)
          ? "Disponible: sin saldo todavía"
          : "Disponible " + fmtMoney(lastCash, quote) + " para comprar";
      }
      if (sellAvail) {
        sellAvail.textContent = (lastQty === null || lastQty === undefined)
          ? "Disponible: aún no tienes " + asset
          : "Disponible " + fmtQty(lastQty, asset) + " para vender";
      }
      var buyEstimate = decimal && marketPriceUsable("buy") ? decimal.estimateBuy(
        buyForm.order_amount.value, buyForm.estimated_fee.value, String(lastMarket.buy_price)) : null;
      var buyEst = document.getElementById("buy-est");
      var buyTotal = document.getElementById("buy-total");
      if (buyEstimate) {
        buyEst.textContent = "Recibirás aprox. " + fmtQty(buyEstimate.quantity, asset) +
          " (comisión " + fmtMoney(buyEstimate.fee, quote) + "; solo informativo).";
        buyTotal.textContent = "TOTAL: " + fmtMoney(buyEstimate.total, quote);
      } else {
        buyEst.textContent = "Indica importe y comisión válidos para estimar.";
        buyTotal.textContent = "";
      }
      var sellEstimate = decimal && marketPriceUsable("sell") ? decimal.estimateSell(
        sellForm.quantity.value, sellForm.fee.value, String(lastMarket.sell_price)) : null;
      var sellEst = document.getElementById("sell-est");
      var sellTotal = document.getElementById("sell-total");
      if (sellEstimate) {
        sellEst.textContent = "Recibirás aprox. " + fmtMoney(sellEstimate.net, quote) +
          " (comisión " + fmtMoney(sellEstimate.fee, quote) + "; solo informativo).";
        sellTotal.textContent = "TOTAL: " + fmtMoney(sellEstimate.gross, quote);
      } else {
        sellEst.textContent = "Indica cantidad y comisión válidas para estimar.";
        sellTotal.textContent = "";
      }
    }

    assetSel.addEventListener("change", function () { paintCcy(); refreshMarket(); refreshCash(); });
    quoteSel.addEventListener("change", function () { paintCcy(); refreshMarket(); refreshCash(); });
    document.getElementById("t-reload").addEventListener("click", function () { refreshMarket(true); refreshCash(); });
    buyForm.order_amount.addEventListener("input", updateEstimates);
    buyForm.estimated_fee.addEventListener("input", updateEstimates);
    sellForm.quantity.addEventListener("input", updateEstimates);
    sellForm.fee.addEventListener("input", updateEstimates);

    /* Tabs accesibles con roving tabindex y flechas. */
    var tabBuy = document.getElementById("tab-buy");
    var tabSell = document.getElementById("tab-sell");
    function showTab(which) {
      var buy = which === "buy";
      document.getElementById("panel-buy").hidden = !buy;
      document.getElementById("panel-sell").hidden = buy;
      tabBuy.classList.toggle("active", buy);
      tabSell.classList.toggle("active", !buy);
      tabBuy.setAttribute("aria-selected", buy ? "true" : "false");
      tabSell.setAttribute("aria-selected", !buy ? "true" : "false");
      tabBuy.tabIndex = buy ? 0 : -1;
      tabSell.tabIndex = !buy ? 0 : -1;
    }
    function focusTab(which) {
      showTab(which);
      (which === "buy" ? tabBuy : tabSell).focus();
    }
    tabBuy.addEventListener("click", function () { showTab("buy"); });
    tabSell.addEventListener("click", function () { showTab("sell"); });
    [tabBuy, tabSell].forEach(function (tab) {
      tab.addEventListener("keydown", function (ev) {
        if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
          /* Dos pestañas: cualquier flecha lateral mueve a la otra. */
          ev.preventDefault();
          focusTab(tab === tabBuy ? "sell" : "buy");
        } else if (ev.key === "Home") { ev.preventDefault(); focusTab("buy"); }
        else if (ev.key === "End") { ev.preventDefault(); focusTab("sell"); }
      });
    });

    document.getElementById("f-scenario").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var f = ev.target;
      var out = document.getElementById("r-scenario");
      out.innerHTML = stateBox("loading", "Creando tu saldo…");
      api("/paper/scenarios", { method: "POST", body: JSON.stringify({
        quote_currency: upQuote(f.quote_currency.value),
        initial_balance: String(f.initial_balance.value).trim(),
        max_order_amount: String(f.max_order_amount.value).trim(),
        max_fee_percentage: String(f.max_fee_percentage.value).trim()
      })}).then(function (res) {
        if (!document.getElementById("r-scenario")) return;
        if (res.status === 201 || (res.ok && res.data && res.data.quote_currency)) {
          out.innerHTML = stateBox("ok", "<strong>Saldo creado.</strong> Ya puedes comprar y vender.");
          showToast("ok", "Saldo creado. Ya puedes comprar y vender.");
        } else {
          out.innerHTML = httpErrorBox(res, { quote: upQuote(f.quote_currency.value) });
          armAnyRetry(out, function () { f.requestSubmit(); });
          showToast("error", "No se pudo crear el saldo. Revisa los datos.");
        }
        refreshCash();
      }).catch(function () {
        if (!document.getElementById("r-scenario")) return;
        out.innerHTML = networkBox("scn");
        armRetry(out, "scn", function () { f.requestSubmit(); });
        showToast("error", "No hemos podido conectar. ¿Está el servidor arrancado?");
      });
    });

    function bindTrade(formId, btnId, outId, url, verb, getBody, ctxOf) {
      var pendingRequest = null;
      document.getElementById(formId).addEventListener("submit", function (ev) {
        ev.preventDefault();
        var btn = document.getElementById(btnId);
        var out = document.getElementById(outId);
        if (btn.disabled) return; /* anti doble-clic: 1 envío = 1 operación */
        var side = url === "/paper/sell" ? "sell" : "buy";
        if (!marketPriceUsable(side)) {
          var ageAtSend = lastMarket && Number(lastMarket.age_seconds);
          var expired = lastMarket && lastMarket.stale === false &&
            marketReceivedMs !== null && Number.isFinite(ageAtSend) &&
            ageAtSend + (performance.now() - marketReceivedMs) / 1000 > 60;
          if (expired) {
            clearMarketPrice();
            var marketBox = document.getElementById("t-market");
            if (marketBox) marketBox.innerHTML = stateBox("empty", "La cotización ha caducado. Pulsa «Actualizar precio».");
          }
          out.innerHTML = stateBox("empty", "Necesitas una cotización actual de este par. Pulsa «Actualizar precio».");
          showToast("blocked", "Actualiza el precio de este par antes de operar.");
          return;
        }
        /* M3 (menor heredado de M4): chequeo explícito de modo PAPER antes
         * de enviar (Estudio ya lo hacía en submitTradeForm; Operativa
         * dejaba el clic llegar al helper en LIVE). */
        var mkAtSend = selAsset() + "/" + selQuote();
        if (orbisMode() !== "PAPER") {
          out.innerHTML = stateBox("empty", "La operativa simulada está en PAPER. Vuelve a PAPER para operar.");
          showToast("blocked", "Vuelve a PAPER para operar.");
          return;
        }
        btn.disabled = true;
        out.innerHTML = stateBox("loading", esc(verb) + " en curso…");
        /* El servidor fija el precio del mercado. La UI conserva el mismo
         * request_id si hubo un error de red al responder. */
        var extra = getBody();
        extra.asset = selAsset();
        extra.quote_currency = selQuote();
        extra.provider = lastMarket.provider; /* Mismo proveedor de la referencia validada. */
        var requestKey = JSON.stringify([side, extra.asset, extra.quote_currency,
          extra.provider, extra.order_amount, extra.quantity, extra.estimated_fee, extra.fee]);
        if (!pendingRequest || pendingRequest.key !== requestKey) {
          pendingRequest = { key: requestKey, id: newRequestId() };
        }
        extra.request_id = pendingRequest.id;
        postMarketPaperTrade(side, extra).then(function (res) {
          if (!document.getElementById(outId)) return;
          if (res.ok || res.status === 409 || res.status === 422) pendingRequest = null;
          /* M3 (menor heredado de M4): guarda anti-carrera de mercado como
           * la de submitTradeForm en Estudio — si el usuario cambió de par
           * mientras volaba, no se pinta un resultado ajeno al visible. */
          if (mkAtSend !== selAsset() + "/" + selQuote()) {
            btn.disabled = false;
            out.innerHTML = stateBox("empty", "Has cambiado de mercado mientras se enviaba. El resultado no corresponde al mercado visible.");
            return;
          }
          out.innerHTML = tradeResultBox(verb, res, ctxOf());
          armAnyRetry(out, function () {
            var frm = document.getElementById(formId);
            if (frm && frm.requestSubmit) frm.requestSubmit();
          });
          if (!res.ok) {
            showToast("error", verb + " no ha salido bien. Revisa el aviso.");
          } else if (res.data && res.data.executed) {
            showToast("ok", verb + " lista.");
          } else {
            var ph = guardPhrase(res.data && res.data.reason, ctxOf());
            showToast("blocked", ph || (verb + " no se ha podido hacer."));
          }
          btn.disabled = false;
          refreshCash();
        }).catch(function () {
          if (!document.getElementById(outId)) return;
          out.innerHTML = networkBox("tr");
          armRetry(out, "tr", function () { btn.disabled = false; ev.target.requestSubmit(); });
          showToast("error", "No hemos podido conectar. ¿Está el servidor arrancado?");
          btn.disabled = false;
        });
      });
    }
    bindTrade("f-buy", "b-buy", "r-buy", "/paper/buy", "Compra", function () {
      return {
        order_amount: String(buyForm.order_amount.value).trim(),
        estimated_fee: String(buyForm.estimated_fee.value || "0").trim()
      };
    }, function () {
      return { amount: fmtMoney(buyForm.order_amount.value, selQuote()), cash: fmtMoney(lastCash, selQuote()), fee: fmtMoney(buyForm.estimated_fee.value, selQuote()), max: fmtMoney("500", selQuote()), maxFee: "configurado", asset: selAsset() };
    });
    bindTrade("f-sell", "b-sell", "r-sell", "/paper/sell", "Venta", function () {
      return {
        quantity: String(sellForm.quantity.value).trim(),
        fee: String(sellForm.fee.value || "0").trim()
      };
    }, function () {
      return { qty: fmtQty(sellForm.quantity.value, selAsset()), avail: fmtQty(lastQty, selAsset()), asset: selAsset(), fee: fmtMoney(sellForm.fee.value, selQuote()) };
    });

    paintCcy();
    refreshMarket();
    refreshCash();
    updateEstimates();

    /* Timer Operativa: cada 15s */
    function startOperativaTimer() {
      if (operativaTimer) { clearInterval(operativaTimer); operativaTimer = null; }
      operativaTimer = setInterval(function () {
        if (document.hidden) return;
        if (!document.getElementById("t-market")) { clearInterval(operativaTimer); operativaTimer = null; return; }
        refreshMarket();
        refreshCash();
      }, OPERATIVA_REFRESH_MS);
    }
    document.getElementById("op-refresh-now").addEventListener("click", function () {
      refreshMarket(true);
      refreshCash();
    });
    startOperativaTimer();
  }

  /* ------------------------------- PORTFOLIO ------------------------------- */
  function renderPortfolio() {
    stopHomeTimer();
    var paperMarketSeq = 0;
    var portfolioRequestSeq = 0;
    view.innerHTML =
      '<section class="card portfolio"><div class="pagina-cabecera"><h2 data-help="Saldos y activos ficticios de la operativa simulada; no forman parte del patrimonio registrado.">Saldo ficticio</h2>' +
      '<a class="ghost" href="#/operativa">Volver a operar</a></div>' + modeTag() +
      '<p class="muted" id="pf-refresh-info">Cada 24 h · <span id="pf-last-update">—</span> · <button type="button" id="pf-refresh-now" class="ghost" data-help="Actualiza ahora la valoración PAPER con precios de referencia.">Actualizar</button></p>' +
      '<div class="row">' +
      '<label class="field">Moneda de pago <select id="p-quote">' + pairOptions(SUPPORTED_QUOTES, "EUR") + "</select></label>" +
      '<label class="field">Moneda digital <select id="p-asset">' + pairOptions(SUPPORTED_ASSETS, "BTC") + "</select></label>" +
      '<label class="field">Periodo <select id="p-tf">' +
      '<option value="live">Tiempo real</option><option value="1h">1 hora</option>' +
      '<option value="24h" selected>24 horas</option><option value="7d">7 días</option>' +
      '<option value="1y">1 año</option><option value="5y">5 años</option>' +
      '<option value="10y">10 años</option><option value="all">Todo disponible</option></select></label>' +
      '<button id="p-load" type="button">Cargar</button></div>' +
      '<div id="p-hero" aria-live="off">' + stateBox("loading", "Cargando tu cartera…") + "</div>" +
      '<h3>Tus activos (ordenados por valor)</h3><div id="p-body" aria-live="off">' +
      stateBox("empty", "Sin datos: pulsa Cargar.") + "</div>" +
      '<div id="p-market" class="pf-chart"></div></section>';

    function marketBox(forceHistory) {
      var seq = ++paperMarketSeq;
      var asset = upQuote(document.getElementById("p-asset").value) || "BTC";
      var quote = upQuote(document.getElementById("p-quote").value) || "EUR";
      miniMarketCtx = asset + "/" + quote; /* M4: el mini pintará sus marcas PAPER */
      var tf = document.getElementById("p-tf").value;
      var box = document.getElementById("p-market");
      if (!box) return;
      releaseMiniChart("p-chart");
      box.innerHTML = stateBox("loading", "Cargando precio de " + esc(asset) + "/" + esc(quote) + "…");
      var priceP = api("/market/price?asset=" + encodeURIComponent(asset) +
        "&quote_currency=" + encodeURIComponent(quote));
      var histP = tf === "live" ? api("/market/history?asset=" + encodeURIComponent(asset) +
        "&quote_currency=" + encodeURIComponent(quote) + "&timeframe=live") : loadMarketChart(asset, quote, tf, forceHistory === true);
      return Promise.all([priceP, histP]).then(function (arr) {
        if (!box.isConnected || seq !== paperMarketSeq) return;
        var res = arr[0], hres = arr[1];
        if (!res.ok) {
          var offlineHistory = hres.ok && hres.data ? hres.data : {};
          var offlinePoints = Array.isArray(offlineHistory.points) ? offlineHistory.points : [];
          box.innerHTML = stateBox("error", "<strong>Precio no disponible ahora mismo.</strong> La cartera se valora con el último precio conocido.") +
            (tf === "live" ? "" : '<h3 class="pf-graph-title">Gráfica secundaria</h3>' +
              '<div id="p-chartwrap" class="pf-chartwrap">' +
              chartBlock("p-chart", offlinePoints, tf, offlineHistory.source, true,
                offlineHistory.note || "Sin cotización actual.") + "</div>");
          if (tf !== "live") paintChart("p-chart", offlinePoints, asset + "/" + quote + "|" + tf);
          return;
        }
        var m = res.data || {};
        var h = (hres.ok && hres.data) ? hres.data : { points: [], source: "none", note: null };
        var pts = Array.isArray(h.points) ? h.points : [];
        var chg = tf === "24h" && !m.stale && !h.stale && marketSourcesMatch(m.source, h.source)
          ? marketWindowChange(pts, "24h") : null;
        var periodLabel = tf === "7d" ? "7 días" : tf === "1h" ? "1 h" : tf === "live" ? "Tiempo real" : tf === "24h" ? "24h" : tf;
        var html = priceTicker(asset, quote, m, chg, periodLabel);
        if (tf === "live") {
          html += stateBox("empty", "Tiempo real no tiene histórico: muestra el precio actual sin inventar serie. " +
            (h.note ? esc(h.note) : ""));
        } else {
          html = '<h3 class="pf-graph-title">Gráfica secundaria</h3>' + html +
            '<div id="p-chartwrap" class="pf-chartwrap">' + chartBlock("p-chart", pts, tf, h.source, h.stale,
              withMarketSourceNote(h.note, m.source, h.source)) + "</div>";
        }
        box.innerHTML = html;
        paintChart("p-chart", pts, asset + "/" + quote + "|" + tf);
      }).catch(function () {
        if (!box.isConnected || seq !== paperMarketSeq) return;
        box.innerHTML = networkBox("pm");
        armRetry(box, "pm", marketBox);
      });
    }

    function portfolioRequestCurrent(seq, quote, body) {
      var selector = document.getElementById("p-quote");
      return seq === portfolioRequestSeq && body.isConnected &&
        document.getElementById("p-body") === body && selector &&
        upQuote(selector.value) === quote;
    }

    function loadPortfolio(forceHistory) {
      var seq = ++portfolioRequestSeq;
      var quote = upQuote(document.getElementById("p-quote").value);
      if (!quote) {
        document.getElementById("p-body").innerHTML =
          stateBox("error", "<strong>Elige la moneda de pago.</strong>");
        return;
      }
      var hero = document.getElementById("p-hero");
      var body = document.getElementById("p-body");
      if (hero) hero.innerHTML = stateBox("loading", "Cargando tu cartera…");
      body.innerHTML = stateBox("loading", "Cargando tus activos…");
      updatePortfolioLastUpdate("loading");
      marketBox(forceHistory === true);
      api("/paper/portfolio?quote_currency=" + encodeURIComponent(quote)).then(function (res) {
        if (!portfolioRequestCurrent(seq, quote, body)) return;
        if (res.status === 404) {
          updatePortfolioLastUpdate("empty");
          if (hero) hero.innerHTML = stateBox("empty", "Todavía no tienes saldo en " + esc(quote) + ".");
          body.innerHTML = emptyState("Todavía no tienes saldo en " + quote,
            "<li>Añade saldo PAPER</li><li>Haz tu primera compra</li>",
            '<a class="cta" href="#/operativa">Añadir saldo PAPER</a>');
          return;
        }
        if (!res.ok) {
          updatePortfolioLastUpdate("error");
          if (hero) hero.innerHTML = "";
          body.innerHTML = httpErrorBox(res, { quote: quote });
          armAnyRetry(body, loadPortfolio);
          return;
        }
        var p = res.data || {};
        updatePortfolioLastUpdate("ok");
        var positions = Array.isArray(p.positions) ? p.positions : [];
        /* El valor y el resultado siguen saliendo del servidor; el rótulo
         * diferencia referencias recientes de precios manuales o antiguos. */
        var pnlSign = window.OrbisDecimal && window.OrbisDecimal.compareStrings(String(p.total_pnl), "0");
        var pnlCls = pnlSign === null || pnlSign === undefined ? "muted" : (pnlSign >= 0 ? "up" : "down");
        var estimated = portfolioEstimate(p);
        var staleCount = positions.filter(function (pos) { return pos.price_stale; }).length;
        if (hero) {
          hero.innerHTML = '<div class="pf-hero"><p class="pf-label">' +
            (estimated ? "VALOR PAPER ESTIMADO" : positions.length ? "VALOR PAPER DE REFERENCIA" : "SALDO PAPER") + '</p>' +
            '<p class="pf-value num"><strong>' + esc(fmtMoney(p.equity, p.quote_currency)) + "</strong></p>" +
            '<p class="pf-pnl ' + pnlCls + '"><span>' + (estimated ? "RESULTADO PAPER ESTIMADO " : "RESULTADO PAPER ") + '</span><strong>' +
            esc(fmtMoney(p.total_pnl, p.quote_currency)) + "</strong> · " + esc(fmtPctShown(p.return_percentage)) + "</p>" +
            (estimated ? '<p class="pf-valuation-note" role="status">' + staleCount +
              (staleCount === 1 ? " posición sin cotización reciente." : " posiciones sin cotización reciente.") + '</p>' : "") + '</div>';
        }
        /* Resumen debajo: Disponible / Invertido / PnL (valores del servidor). */
        var html = '<div class="stats">' +
          statCard("Disponible", esc(fmtMoney(p.cash_balance, p.quote_currency)), "Lo que puedes gastar") +
          statCard("Invertido", esc(fmtMoney(p.positions_value, p.quote_currency)), estimated ? "Valor estimado" : "Valor de referencia") +
          statCard(estimated ? "Resultado PAPER estimado" : "Resultado PAPER", esc(fmtMoney(p.total_pnl, p.quote_currency)) + " · " + esc(fmtPctShown(p.return_percentage)), "Cobrada + sin cobrar") +
          "</div>";
        if (!positions.length) {
          html += emptyState("Sin monedas compradas en " + esc(p.quote_currency || quote),
            "<li>Ve a Operativa</li><li>Haz tu primera compra</li>",
            '<a class="cta" href="#/operativa">Ir a Operativa</a>');
        } else {
          /* Activos comprados ordenados por valor desc (solo lectura del servidor). */
          var ordered = positions.slice().sort(function (a, b) {
            var order = window.OrbisDecimal && window.OrbisDecimal.compareStrings(String(b.value), String(a.value));
            return order === null || order === undefined ? 0 : order;
          });
          /* Movimiento de mercado, no rentabilidad de la posición. Sólo BTC
           * tiene una serie comparable; los demás activos muestran —. */
          html += '<div class="tablewrap"><table class="resp"><caption class="sr-only">Posiciones de la cartera</caption><thead><tr><th>Activo</th><th>Cantidad</th><th>Precio medio</th><th>Precio ref.</th>' +
            '<th>Valor</th><th data-help="Variación de BTC en el histórico público; no es la rentabilidad de tu posición PAPER.">Mercado 24h</th><th>P/L</th><th>Peso</th></tr></thead><tbody>';
          ordered.forEach(function (pos) {
            html += "<tr><td data-label=\"Activo\">" + esc(pos.asset) + "/" + esc(pos.quote_currency) + "</td>" +
              '<td data-label="Cantidad" class="num">' + esc(fmtQty(pos.quantity, pos.asset)) + "</td>" +
              '<td data-label="Precio medio" class="num">' + esc(fmtMoney(pos.average_cost, pos.quote_currency)) + "</td>" +
              '<td data-label="Precio ref." class="num" data-help="' + esc(priceReferenceHelp(pos)) + '">' + esc(fmtMoney(pos.current_price, pos.quote_currency)) + (pos.price_stale ? " *" : "") + "</td>" +
              '<td data-label="Valor" class="num">' + esc(fmtMoney(pos.value, pos.quote_currency)) + "</td>" +
              '<td data-label="Mercado 24h" class="num" data-chg="' + esc(pos.asset) + '">' +
              (pos.asset === "BTC" ? '<span class="muted">…</span>' : "—") + "</td>" +
              '<td data-label="P/L" class="num">' + esc(fmtMoney(pos.unrealized_pnl, pos.quote_currency)) + "</td>" +
              '<td data-label="Peso" class="num">' + esc(fmtShare(pos.weight_percentage)) + "</td></tr>";
          });
          html += "</tbody></table></div>";
          /* Sólo una serie pública reciente justifica un cambio de mercado.
           * La valoración y el P/L siguen viniendo del libro PAPER. */
          api("/market/history?asset=BTC&quote_currency=" + encodeURIComponent(quote) + "&timeframe=24h").then(function (hr) {
            if (!portfolioRequestCurrent(seq, quote, body)) return;
            var cell = body.querySelector('[data-chg="BTC"]');
            if (!cell) return;
            var historyOk = hr.ok && hr.data && hr.data.stale === false && hr.data.source;
            var hpts = (historyOk && Array.isArray(hr.data.points)) ? hr.data.points : [];
            var c = marketWindowChange(hpts, "24h");
            cell.setAttribute("data-help", c === null ? "Sin histórico BTC reciente y comparable." :
              "Cambio BTC de " + sourceLabel(hr.data.source) + "; no es P/L de esta posición.");
            cell.innerHTML = (c === null) ? "—" : '<span class="' + (c >= 0 ? "up" : "down") + '">' + esc(fmtPctShown(c)) + "</span>";
          }).catch(function () {
            if (!portfolioRequestCurrent(seq, quote, body)) return;
            var cell = body.querySelector('[data-chg="BTC"]');
            if (cell) cell.textContent = "—";
          });
        }
        /* Composición automática: efectivo frente a cada activo (pesos del servidor). */
        var cashW = Number(p.cash_weight);
        html += '<h3>Distribución</h3><div class="mixbar" role="img" aria-label="Distribución de la cartera">';
        if (isFinite(cashW) && cashW > 0) {
          html += '<span class="mix cash" style="flex:' + cashW.toFixed(2) + '" title="Efectivo"></span>';
        }
        positions.forEach(function (pos) {
          var w = Number(pos.weight_percentage);
          if (isFinite(w) && w > 0) {
            html += '<span class="mix coin" style="flex:' + w.toFixed(2) + '" title="' + esc(pos.asset) + '"></span>';
          }
        });
        html += "</div><ul class=\"mixlegend\">";
        html += "<li>Efectivo " + esc(fmtShare(p.cash_weight)) + "</li>";
        positions.forEach(function (pos) {
          html += "<li>" + esc(pos.asset) + " " + esc(fmtShare(pos.weight_percentage)) + "</li>";
        });
        html += "</ul>";
        body.innerHTML = html;
        paintPortfolioDca(p, positions, quote, seq, body);
      }).catch(function () {
        if (!portfolioRequestCurrent(seq, quote, body)) return;
        updatePortfolioLastUpdate("error");
        body.innerHTML = networkBox("pf");
        armRetry(body, "pf", loadPortfolio);
      });
    }

    /* DCA + valoración + evolución: solo valores del servidor, sin recalcular dinero. */
    function paintPortfolioDca(p, positions, quote, seq, body) {
      if (!portfolioRequestCurrent(seq, quote, body)) return;
      var extra = '<h3>DCA (plan de aportaciones)</h3>';
      if (!positions.length) {
        extra += '<p class="muted">Sin aportaciones todavía. Crea tu plan en <a href="#/estrategias">Estrategias</a>.</p>';
      } else {
        extra += '<div class="tablewrap"><table class="resp"><caption class="sr-only">DCA por activo</caption><thead><tr><th>Activo</th><th>Aportado</th><th>Acumulado</th><th>Medio</th><th>Precio ref.</th><th>Resultado</th><th>Nº ops</th></tr></thead><tbody>';
        positions.forEach(function (pos) {
          extra += "<tr><td data-label=\"Activo\">" + esc(pos.asset) + "</td>" +
            '<td data-label="Aportado" class="num">' + esc(fmtMoney(pos.invested, pos.quote_currency)) + "</td>" +
            '<td data-label="Acumulado" class="num">' + esc(fmtQty(pos.quantity, pos.asset)) + "</td>" +
            '<td data-label="Medio" class="num">' + esc(fmtMoney(pos.average_cost, pos.quote_currency)) + "</td>" +
            '<td data-label="Precio ref." class="num" data-help="' + esc(priceReferenceHelp(pos)) + '">' + esc(fmtMoney(pos.current_price, pos.quote_currency)) + (pos.price_stale ? " *" : "") + "</td>" +
            '<td data-label="Resultado" class="num">' + esc(fmtMoney(pos.unrealized_pnl, pos.quote_currency)) +
            " · " + esc(fmtMoney(pos.realized_pnl, pos.quote_currency)) + " cobrada</td>" +
            '<td data-label="Nº ops" class="num" data-dcacount="' + esc(pos.asset) + '">…</td></tr>';
        });
        extra += "</tbody></table></div>";
      }
      extra += '<p class="muted">Valoración PAPER según la fuente indicada en cada precio de referencia.</p>';
      extra += '<div id="p-evo"><p class="muted">Evolución: cargando histórico…</p></div>';
      extra += '<h3>Distribución secundaria</h3><p class="muted">Efectivo ' + esc(fmtShare(p.cash_weight)) +
        (positions.map(function (pos) { return " · " + pos.asset + " " + fmtShare(pos.weight_percentage); }).join("")) + ".</p>";
      var wrap = document.createElement("div");
      wrap.innerHTML = extra;
      body.appendChild(wrap);
      positions.forEach(function (pos) {
        api("/paper/operations?asset=" + encodeURIComponent(pos.asset) + "&quote=" + encodeURIComponent(pos.quote_currency)).then(function (r) {
          if (!portfolioRequestCurrent(seq, quote, body)) return;
          var cell = body.querySelector('[data-dcacount="' + pos.asset + '"]');
          if (!cell) return;
          cell.textContent = (r.ok && r.data) ? String(r.data.length) : "—";
        }).catch(function () {});
      });
      api("/market/history?asset=" + encodeURIComponent("BTC") +
        "&quote_currency=" + encodeURIComponent(quote) + "&timeframe=7d").then(function (hr) {
        if (!portfolioRequestCurrent(seq, quote, body)) return;
        var evo = body.querySelector("#p-evo");
        if (!evo) return;
        var hpts = (hr.ok && hr.data && Array.isArray(hr.data.points)) ? hr.data.points : [];
        if (hpts.length >= 2 && hr.data && hr.data.stale === false && hr.data.source) {
          var c = marketWindowChange(hpts, "7d");
          evo.innerHTML = c === null ? '<p class="muted">BTC mercado 7d: cobertura histórica insuficiente.</p>' :
            '<p data-help="Variación de BTC entre los extremos del histórico público; no es evolución ni P/L de tu cartera PAPER.">BTC mercado 7d · ' +
            esc(sourceLabel(hr.data.source)) + ': <strong class="' + (c >= 0 ? "up" : "down") + '">' +
            esc(fmtPctShown(c)) + "</strong></p>";
        } else {
          evo.innerHTML = '<p class="muted">BTC mercado 7d: histórico reciente no disponible.</p>';
        }
      }).catch(function () {
        if (!portfolioRequestCurrent(seq, quote, body)) return;
        var evo = body.querySelector("#p-evo");
        if (evo) evo.innerHTML = '<p class="muted">BTC mercado 7d: sin conexión.</p>';
      });
    }

    document.getElementById("p-tf").value = chartRangePref("paper", "24h");
    document.getElementById("p-load").addEventListener("click", function () { loadPortfolio(true); });
    document.getElementById("p-tf").addEventListener("change", function () {
      saveChartRangePref("paper", this.value); marketBox();
    });
    document.getElementById("p-quote").addEventListener("change", loadPortfolio);
    document.getElementById("p-asset").addEventListener("change", marketBox);
    loadPortfolio();

    /* La fecha indica una respuesta válida, no el momento de iniciar la petición. */
    function updatePortfolioLastUpdate(state) {
      var el = document.getElementById("pf-last-update");
      if (!el) return;
      if (state === "loading") el.textContent = "Consultando…";
      else if (state === "ok") el.textContent = "Última actualización: " +
        new Date().toLocaleString("es-ES", { dateStyle: "short", timeStyle: "medium" });
      else el.textContent = "No actualizado";
    }
    function startPortfolioTimer() {
      if (portfolioTimer) { clearInterval(portfolioTimer); portfolioTimer = null; }
      portfolioTimer = setInterval(function () {
        if (document.hidden) return;
        if (!document.getElementById("p-body")) { clearInterval(portfolioTimer); portfolioTimer = null; return; }
        loadPortfolio();
      }, PORTFOLIO_REFRESH_MS);
    }
    document.getElementById("pf-refresh-now").addEventListener("click", function () {
      loadPortfolio(true);
    });
    startPortfolioTimer();
  }

  /* ------------------------------ ESTRATEGIAS ------------------------------ */
  function renderEstrategias() {
    stopHomeTimer();
    view.innerHTML =
      '<section class="card"><h2 data-help="Planes de aportación como recordatorios; no se ejecutan automáticamente.">Estrategias</h2>' + modeTag() +
      '<p class="amber">Planes manuales: cada compra se confirma en Operativa.</p>' +
      '<div id="s-list">' + stateBox("loading", "Cargando…") + "</div>" +
      '<h3>Crear</h3><form id="f-strat"><div class="row">' +
      '<label class="field">Nombre <input name="name" required></label>' +
      '<label class="field">Tipo <select name="strategy_type"><option value="DCA">DCA</option><option value="MANUAL">Manual</option></select></label>' +
      '<label class="field">Moneda digital <select name="asset">' + pairOptions(SUPPORTED_ASSETS, "BTC") + "</select></label>" +
      '<label class="field">Moneda de pago <select name="quote">' + pairOptions(SUPPORTED_QUOTES, "EUR") + "</select></label>" +
      '<label class="field">Importe <input name="amount" value="50" inputmode="decimal" required></label>' +
      '<label class="field">Periodicidad <select name="frequency"><option value="diaria">Diaria</option><option value="semanal" selected>Semanal</option><option value="mensual">Mensual</option></select></label>' +
      '<button type="submit">Guardar</button></div></form><div id="r-strat"></div></section>';

    function cfgText(cfg) {
      cfg = cfg || {};
      var pair = (cfg.asset || cfg.base || "—") + "/" + (cfg.quote_currency || cfg.quote || "—");
      var amt = cfg.amount || cfg.order_amount || "—";
      var freq = cfg.frequency || cfg.periodicity || "—";
      return { pair: pair, amt: amt, freq: freq };
    }

    function refresh() {
      var box = document.getElementById("s-list");
      if (!box) return;
      api("/strategies").then(function (res) {
        if (!document.getElementById("s-list")) return;
        if (!res.ok) { box.innerHTML = httpErrorBox(res, {}); armAnyRetry(box, typeof load === "function" ? load : refresh); return; }
        var items = res.data || [];
        if (!items.length) {
          box.innerHTML = emptyState("Sin estrategias guardadas",
            "<li>Crea tu primer plan abajo</li><li>Confirma cada compra en Operativa</li>", "");
          return;
        }
        var html = '<div class="cards">';
        items.forEach(function (s) {
          var c = cfgText(s.config);
          html += '<article class="scard" data-id="' + esc(s.id) + '">' +
            '<h3>' + esc(s.name) + " " + statusBadge(!!s.enabled) + "</h3>" +
            '<dl class="kv"><dt>Par</dt><dd>' + esc(c.pair) + "</dd>" +
            "<dt>Importe</dt><dd>" + esc(String(c.amt)) + "</dd>" +
            "<dt>Periodicidad</dt><dd>" + esc(String(c.freq)) + "</dd>" +
            '<dt>Próxima</dt><dd>— <span class="muted">(Orbis no programa ejecuciones)</span></dd>' +
            '<dt>Ejecuciones</dt><dd data-exec="' + esc(s.id) + '">…</dd></dl>' +
            '<div class="cardbtns">' +
            '<button type="button" class="ghost" data-toggle="' + esc(s.id) + '" data-enabled="' + (s.enabled ? "1" : "0") + '">' + (s.enabled ? "Pausar" : "Reanudar") + "</button>" +
            '<button type="button" class="ghost" data-edit="' + esc(s.id) + '">Editar</button>' +
            '<button type="button" class="ghost danger" data-del="' + esc(s.id) + '">Borrar</button></div>' +
            '<div data-editform="' + esc(s.id) + '" hidden></div>' +
            detailBlock("Ver detalle", "Estrategia nº " + s.id + " · " + JSON.stringify(s.config || {})) +
            "</article>";
        });
        box.innerHTML = html + "</div>";
        /* Ejecuciones e invertido solo si hay filas para esa estrategia. */
        items.forEach(function (s) {
          var cell = box.querySelector('[data-exec="' + s.id + '"]');
          if (!cell) return;
          api("/paper/operations?strategy_id=" + encodeURIComponent(s.id)).then(function (r) {
            if (!cell.isConnected) return;
            if (!r.ok || !r.data || !r.data.length) {
              cell.textContent = "Sin ejecuciones";
              return;
            }
            var n = r.data.length;
            var total = window.OrbisDecimal && window.OrbisDecimal.sumStrings(r.data.map(function (o) {
              return String(o.amount_spent);
            }));
            cell.textContent = n + (n === 1 ? " ejecución" : " ejecuciones") +
              (total === null || total === undefined ? "" : " · " + fmtNum(total));
          }).catch(function () { if (cell.isConnected) cell.textContent = "Sin ejecuciones"; });
        });
        box.querySelectorAll("[data-del]").forEach(function (b) {
          b.addEventListener("click", function () {
            if (!window.confirm("¿Borrar esta estrategia? No se puede deshacer.")) return;
            api("/strategies/" + encodeURIComponent(b.getAttribute("data-del")), { method: "DELETE" }).then(function (r) {
              if (r.ok) { showToast("ok", "Estrategia borrada."); refresh(); }
              else { showToast("error", "No se pudo borrar. Vuelve a intentarlo."); }
            }).catch(function () { showToast("error", "No hemos podido conectar. ¿Está el servidor arrancado?"); });
          });
        });
        box.querySelectorAll("[data-toggle]").forEach(function (b) {
          b.addEventListener("click", function () {
            var id = b.getAttribute("data-toggle");
            var item = items.filter(function (x) { return String(x.id) === String(id); })[0];
            if (!item) return;
            var payload = {
              name: item.name, strategy_type: item.strategy_type,
              enabled: !(b.getAttribute("data-enabled") === "1"), config: item.config || {}
            };
            api("/strategies/" + encodeURIComponent(id), { method: "PUT", body: JSON.stringify(payload) }).then(function (r) {
              if (r.ok) { showToast("ok", payload.enabled ? "Estrategia reanudada." : "Estrategia pausada."); refresh(); }
              else { showToast("error", "No se pudo guardar. Vuelve a intentarlo."); }
            }).catch(function () { showToast("error", "No hemos podido conectar. ¿Está el servidor arrancado?"); });
          });
        });
        box.querySelectorAll("[data-edit]").forEach(function (b) {
          b.addEventListener("click", function () {
            var id = b.getAttribute("data-edit");
            var item = items.filter(function (x) { return String(x.id) === String(id); })[0];
            if (!item) return;
            var holder = box.querySelector('[data-editform="' + id + '"]');
            var c0 = cfgText(item.config);
            holder.hidden = !holder.hidden;
            if (holder.hidden) return;
            holder.innerHTML = '<form data-save="' + esc(id) + '"><div class="row">' +
              '<label class="field">Nombre <input name="name" value="' + esc(item.name) + '" required></label>' +
              '<label class="field">Importe <input name="amount" value="' + esc(String(c0.amt === "—" ? "" : c0.amt)) + '" inputmode="decimal"></label>' +
              '<label class="field">Periodicidad <select name="frequency">' +
              ["diaria", "semanal", "mensual"].map(function (f) {
                return '<option value="' + f + '"' + (String(c0.freq) === f ? " selected" : "") + ">" + f + "</option>";
              }).join("") + "</select></label>" +
              '<button type="submit">Guardar cambios</button></div></form>';
            holder.querySelector('[data-save="' + id + '"]').addEventListener("submit", function (ev) {
              ev.preventDefault();
              var f = ev.target;
              var cfg = Object.assign({}, item.config || {});
              cfg.amount = String(f.amount.value).trim();
              cfg.frequency = f.frequency.value;
              api("/strategies/" + encodeURIComponent(id), { method: "PUT", body: JSON.stringify({
                name: f.name.value.trim(), strategy_type: item.strategy_type, enabled: item.enabled, config: cfg
              })}).then(function (r) {
                if (r.ok) { showToast("ok", "Estrategia actualizada."); refresh(); }
                else { showToast("error", "No se pudo guardar. Vuelve a intentarlo."); }
              }).catch(function () { showToast("error", "No hemos podido conectar. ¿Está el servidor arrancado?"); });
            });
          });
        });
      }).catch(function () {
        if (!document.getElementById("s-list")) return;
        box.innerHTML = networkBox("st");
        armRetry(box, "st", refresh);
      });
    }
    document.getElementById("f-strat").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var f = ev.target;
      var out = document.getElementById("r-strat");
      var payload = {
        name: f.name.value.trim(),
        strategy_type: f.strategy_type.value,
        enabled: true,
        config: {
          asset: upQuote(f.asset.value),
          quote_currency: upQuote(f.quote.value),
          amount: String(f.amount.value).trim(),
          frequency: f.frequency.value
        }
      };
      api("/strategies", { method: "POST", body: JSON.stringify(payload) }).then(function (res) {
        if (!document.getElementById("r-strat")) return;
        if (res.ok) {
          out.innerHTML = stateBox("ok", "<strong>Estrategia guardada.</strong> Recuerda: tú confirmas cada compra en Operativa.");
          showToast("ok", "Estrategia guardada.");
          f.reset();
        } else {
          out.innerHTML = httpErrorBox(res, {});
          armAnyRetry(out, function () { f.requestSubmit(); });
          showToast("error", "No se pudo guardar. Vuelve a intentarlo.");
        }
        refresh();
      }).catch(function () {
        if (!document.getElementById("r-strat")) return;
        out.innerHTML = networkBox("sc");
        armRetry(out, "sc", function () { f.requestSubmit(); });
        showToast("error", "No hemos podido conectar. ¿Está el servidor arrancado?");
      });
    });
    refresh();
  }

  /* --------------------------------- LEDGER -------------------------------- */
  function renderLedger() {
    stopHomeTimer();
    view.innerHTML =
      '<section class="card"><h2>Movimientos</h2>' + modeTag() +
      '<div id="l-sum" aria-live="off">' + stateBox("loading", "Cargando resumen…") + "</div>" +
      '<form id="f-filter"><div class="row compact">' +
      '<label class="field">Moneda digital <select name="asset"><option value="">Todas</option>' +
      pairOptions(SUPPORTED_ASSETS, "") + "</select></label>" +
      '<label class="field">Moneda de pago <select name="quote"><option value="">Todas</option>' +
      pairOptions(SUPPORTED_QUOTES, "") + "</select></label>" +
      '<label class="field">Tipo <select name="side"><option value="">Todos</option>' +
      '<option value="BUY">Compra</option><option value="SELL">Venta</option></select></label>' +
      '<label class="field">Día <input type="date" name="date"></label>' +
      '<button type="submit">Filtrar</button><button type="button" id="l-clear" class="ghost">Limpiar</button></div></form>' +
      '<div id="l-list">' + stateBox("loading", "Cargando…") + "</div></section>";

    function filters() {
      var f = document.getElementById("f-filter");
      var q = [];
      if (f.asset.value) q.push("asset=" + encodeURIComponent(f.asset.value));
      if (f.quote.value) q.push("quote=" + encodeURIComponent(upQuote(f.quote.value)));
      if (f.side.value) q.push("side=" + encodeURIComponent(f.side.value));
      if (f.date.value) q.push("date=" + encodeURIComponent(f.date.value));
      return q.length ? "?" + q.join("&") : "";
    }

    function load() {
      var qs = filters();
      var box = document.getElementById("l-list");
      var sum = document.getElementById("l-sum");
      if (!box) return;
      box.innerHTML = stateBox("loading", "Cargando…");
      api("/paper/operations/summary" + qs).then(function (res) {
        if (!document.getElementById("l-sum")) return;
        if (res.ok && res.data) {
          var s = res.data;
          sum.innerHTML = '<dl class="kv">' +
            "<dt>Movimientos</dt><dd><strong>" + esc(String(s.operations_count)) + "</strong> (" +
            esc(String(s.buys_count)) + " compras · " + esc(String(s.sells_count)) + " ventas)</dd>" +
            "<dt>Volumen</dt><dd>" + esc(fmtNum(s.volume)) + "</dd>" +
            "<dt>Comisiones</dt><dd>" + esc(fmtNum(s.total_fees)) + "</dd></dl>";
        } else {
          sum.innerHTML = "";
        }
      }).catch(function () { /* el resumen es secundario */ });
      api("/paper/operations" + (qs ? qs + "&pnl=true" : "?pnl=true")).then(function (res) {
        if (!document.getElementById("l-list")) return;
        if (!res.ok) { box.innerHTML = httpErrorBox(res, {}); armAnyRetry(box, typeof load === "function" ? load : refresh); return; }
        var items = res.data || [];
        if (!items.length) {
          box.innerHTML = emptyState("Sin movimientos para esos filtros",
            "<li>Prueba con otros filtros</li><li>O haz tu primera compra en Operativa</li>",
            '<a class="cta" href="#/operativa">Ir a Operativa</a>');
          return;
        }
        var html = '<div class="tablewrap"><table class="resp"><caption class="sr-only">Movimientos PAPER</caption><thead><tr><th>Fecha</th><th>Tipo</th><th>Par</th>' +
          "<th>Importe</th><th>Cantidad</th><th>Precio</th><th>Comisión</th><th>Proveedor</th><th>Origen</th><th>Estado</th><th>Resultado</th><th>%</th></tr></thead><tbody>";
        items.forEach(function (o) {
          var isBuy = o.operation_type === "BUY";
          var fee = window.OrbisDecimal && window.OrbisDecimal.sumStrings([
            o.trading_fee == null ? "0" : String(o.trading_fee),
            o.withdrawal_fee == null ? "0" : String(o.withdrawal_fee),
            o.network_fee == null ? "0" : String(o.network_fee),
          ]);
          /* Resultado/%: solo lectura del PnL que calcula el servidor
           * (?pnl=true); la compra no realiza resultado. Sin campos, "—". */
          var pnlTxt = "—", pnlPct = "—";
          if (!isBuy && o.realized_pnl !== null && o.realized_pnl !== undefined) {
            pnlTxt = fmtMoney(o.realized_pnl, o.quote_currency);
            pnlPct = fmtPct(o.pnl_pct);
          }
          html += "<tr><td data-label=\"Fecha\" class=\"num\">" + esc(fmtDateTime(o.timestamp)) + "</td>" +
            '<td data-label="Tipo"><span class="pillop ' + (isBuy ? "buy" : "sell") + '">' + (isBuy ? "Compra" : "Venta") + "</span></td>" +
            "<td data-label=\"Par\">" + esc(o.asset) + "/" + esc(o.quote_currency) + "</td>" +
            '<td data-label="Importe" class="num">' + esc(fmtMoney(o.amount_spent, o.quote_currency)) + "</td>" +
            '<td data-label="Cantidad" class="num">' + esc(fmtQty(o.asset_received, o.asset)) + "</td>" +
            '<td data-label="Precio" class="num">' + esc(fmtMoney(o.price, o.quote_currency)) + "</td>" +
            '<td data-label="Comisión" class="num">' + esc(fmtMoney(fee, o.quote_currency)) + "</td>" +
            '<td data-label="Proveedor">' + esc((o.exchange === null || o.exchange === undefined || o.exchange === "") ? "—" : o.exchange) + "</td>" +
            "<td data-label=\"Origen\">" + esc(origenLabel(o.source)) + "</td>" +
            "<td data-label=\"Estado\">" + esc(estadoLabel(o.status)) + "</td>" +
            '<td data-label="Resultado" class="num">' + esc(pnlTxt) + "</td>" +
            '<td data-label="%" class="num">' + esc(pnlPct) + "</td></tr>";
        });
        box.innerHTML = html + "</tbody></table></div>" +
          '<p class="muted">Resultado calculado sobre todo el historial; puede diferir del coste medio de la sesión activa.</p>';
      }).catch(function () {
        if (!document.getElementById("l-list")) return;
        box.innerHTML = networkBox("ld");
        armRetry(box, "ld", load);
      });
    }
    function origenLabel(s) {
      s = String(s || "").toUpperCase();
      if (s === "MANUAL") return "Manual";
      if (s === "STRATEGY") return "Estrategia";
      return "—";
    }
    function estadoLabel(s) {
      s = String(s || "").toUpperCase();
      if (s === "EXECUTED") return "Hecha";
      return "—";
    }
    document.getElementById("f-filter").addEventListener("submit", function (ev) {
      ev.preventDefault();
      load();
    });
    document.getElementById("l-clear").addEventListener("click", function () {
      var f = document.getElementById("f-filter");
      f.reset();
      load();
    });
    load();
  }

  /* ------------------------- CONFIGURACIÓN DEL SERVICIO ------------------------- */
  /* Apartado #/config con preferencias generales. En Estudio, los controles
   * básicos están siempre visibles, las herramientas tienen una preferencia
   * compacta y cada dibujo se configura únicamente al seleccionarlo. */
  var CFG_THEME_KEY = "orbis.theme";
  function cfgThemeGet() {
    try {
      var t = (localStorage.getItem(CFG_THEME_KEY) || "oscuro").toLowerCase();
      if (t === "claro" || t === "oscuro") return t;
    } catch (e) {}
    return "oscuro";
  }
  function cfgThemeSet(t) {
    t = String(t || "").toLowerCase() === "claro" ? "claro" : "oscuro";
    try { localStorage.setItem(CFG_THEME_KEY, t); } catch (e) {}
    try { document.body.setAttribute("data-theme", t); } catch (e2) {}
    return t;
  }
  try { cfgThemeSet(cfgThemeGet()); } catch (e) {}
  var CFG_ALERT_SOUND_KEY = "orbis.alert.sound";
  function cfgAlertSoundGet() {
    try {
      var v = localStorage.getItem(CFG_ALERT_SOUND_KEY);
      if (v === "false") return false;
    } catch (e) {}
    return true;
  }
  function cfgAlertSoundSet(v) {
    v = !!v;
    try { localStorage.setItem(CFG_ALERT_SOUND_KEY, v ? "true" : "false"); } catch (e) {}
    return v;
  }
  function renderConfig() {
    function plainStoreState(label) {
      return stateBox("blocked",label+" · sin cifrar.")+'<button type="button" class="ghost" data-open-storage-protection data-help="Abre el formulario para elegir almacenes y contraseña; todavía no inicia la protección.">Proteger</button>';
    }
    stopHomeTimer();
    var theme = cfgThemeGet();
    var alertSound = cfgAlertSoundGet();
    view.innerHTML =
      '<section class="card" aria-label="Configuración del servicio"><h2 data-help="Tema y sonido. Mercado, fuente e intervalo se configuran en Estudio.">Preferencias</h2>' + modeTag() +
      '<div class="cfggrid">' +
      '<label class="field">Tema <select id="cfg-theme" aria-label="Tema">' +
      '<option value="oscuro"' + (theme === "oscuro" ? " selected" : "") + '>Oscuro</option>' +
      '<option value="claro"' + (theme === "claro" ? " selected" : "") + '>Claro</option></select></label>' +
      '<label class="field">Sonido de alertas <select id="cfg-alertsound" aria-label="Sonido de alertas">' +
      '<option value="true"' + (alertSound ? " selected" : "") + '>Activado</option>' +
      '<option value="false"' + (!alertSound ? " selected" : "") + '>Desactivado</option></select></label>' +
      "</div>" +
      '<div class="row"><button type="button" id="cfg-testalert" class="ghost" data-help="Reproduce una alerta de prueba; no cambia tus datos.">Probar sonido</button></div>' +
      '<h3 data-help="Preferencias del navegador y ajustes de interfaz guardados localmente.">Guardado</h3><div id="cfg-persist">' + stateBox("loading", "Comprobando el guardado…") + "</div>" +
      '<section id="cfg-privacidad" class="cfg-privacy"><h3>Privacidad y seguridad</h3>' +
      '<div id="cfg-privacy-summary" aria-live="polite" data-help="Revisa sólo las bases activas de ORBIS; no certifica copias antiguas, bloques borrados ni la seguridad del sistema operativo.">' + stateBox("loading", "Comprobando almacenes locales…") + '</div>' +
      '<details><summary>Datos locales y cookies</summary><p>REAL usa SQLCipher. LAB, DCA y los almacenes PAPER admiten cifrado independiente. Preferencias, watchlist y alertas de mercado viven en localStorage. La interfaz no crea cookies propias ni envía telemetría patrimonial.</p></details>' +
      '<details id="cfg-storage-protection"><summary data-help="Protege los almacenes legados con contraseña elegida por ti y copias verificadas, después de pausar ORBIS.">Proteger legado</summary><div id="cfg-storage-protection-body"></div></details>' +
      '<details id="cfg-legacy-panel"><summary>PAPER manual</summary><div id="cfg-legacy-body"></div></details>' +
      '<details><summary>LAB activo</summary><div id="cfg-lab-status" role="status">' + stateBox("loading", "Comprobando LAB…") + '</div>' +
      '<form id="cfg-lab-create-form" class="row" hidden><label class="field" data-help="Crea el laboratorio cifrado. Conserva esta contraseña: sin ella no podrás recuperar históricos ni estrategias.">Nueva contraseña LAB<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>' +
      localSessionField() +
      '<button type="submit" class="ghost">Crear LAB cifrado</button></form>' +
      '<form id="cfg-lab-unlock-form" class="row" hidden><label class="field" data-help="Desbloquea LAB durante el plazo elegido; la clave sólo vive en memoria. DCA necesita LAB disponible para verificar cada compra.">Contraseña LAB<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="off" required></label>' +
      localSessionField() +
      '<button type="submit" class="ghost">Desbloquear</button></form>' +
      '<button type="button" id="cfg-lab-lock" class="ghost" data-help="Olvida la contraseña LAB del proceso; las operaciones ya iniciadas pueden terminar.">Bloquear LAB</button></details>' +
      '<details><summary>DCA y automatizaciones</summary><div id="cfg-automation-status" role="status">' + stateBox("loading", "Comprobando DCA…") + '</div>' +
      '<form id="cfg-automation-create-form" class="row" hidden><label class="field" data-help="Crea la base DCA cifrada. Conserva esta contraseña: sin ella no podrás recuperar las reglas.">Nueva contraseña DCA<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>' +
      localSessionField() +
      '<button type="submit" class="ghost">Crear DCA cifrado</button></form>' +
      '<form id="cfg-automation-unlock-form" class="row" hidden><label class="field" data-help="Clave temporal para reglas y sesiones DCA. Elige un plazo suficiente también en LAB: sus datos de origen se verifican antes de cada compra.">Contraseña DCA<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="off" required></label>' +
      localSessionField() +
      '<button type="submit" class="ghost">Desbloquear</button></form>' +
      '<button type="button" id="cfg-automation-lock" class="ghost" data-help="Olvida la clave DCA del proceso local.">Bloquear DCA</button></details>' +
      '<details><summary>Spot PAPER</summary><div id="cfg-spot-status" role="status">' + stateBox("loading", "Comprobando Spot PAPER…") + '</div>' +
      '<form id="cfg-spot-create-form" class="row" hidden><label class="field" data-help="Crea Spot PAPER cifrado. Conserva esta contraseña para recuperar sus sesiones.">Nueva contraseña Spot PAPER<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>' +
      localSessionField() +
      '<button type="submit" class="ghost">Crear Spot PAPER cifrado</button></form>' +
      '<form id="cfg-spot-unlock-form" class="row" hidden><label class="field" data-help="Desbloquea sesiones y señales Spot PAPER durante el plazo elegido; la clave sólo vive en memoria.">Contraseña Spot PAPER<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="off" required></label>' +
      localSessionField() +
      '<button type="submit" class="ghost">Desbloquear</button></form>' +
      '<button type="button" id="cfg-spot-lock" class="ghost" data-help="Olvida la clave Spot PAPER; una observación ya iniciada puede terminar.">Bloquear Spot PAPER</button></details>' +
      '<details><summary>GRVT PAPER</summary><div id="cfg-perp-status" role="status">' + stateBox("loading", "Comprobando GRVT PAPER…") + '</div>' +
      '<form id="cfg-perp-create-form" class="row" hidden><label class="field" data-help="Crea GRVT PAPER cifrado. Conserva esta contraseña para recuperar cuentas y posiciones.">Nueva contraseña GRVT PAPER<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>' +
      localSessionField() +
      '<button type="submit" class="ghost">Crear GRVT PAPER cifrado</button></form>' +
      '<form id="cfg-perp-unlock-form" class="row" hidden><label class="field" data-help="Desbloquea posiciones y señales ficticias. La clave sólo vive en memoria hasta el plazo elegido, el bloqueo manual o el cierre de ORBIS.">Contraseña GRVT PAPER<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="off" required></label>' +
      localSessionField() +
      '<button type="submit" class="ghost">Desbloquear</button></form>' +
      '<button type="button" id="cfg-perp-lock" class="ghost" data-help="Olvida la clave GRVT PAPER; una operación ya iniciada puede terminar.">Bloquear GRVT PAPER</button></details>' +
      '<details><summary>Bóveda y copias</summary><p>En modo Sesión, REAL se bloquea tras 15 minutos de inactividad. Operativo permite proteger la vista conservando clave y credenciales en memoria hasta caducar la política revisada (máximo 24 horas). Continuo exige otra confirmación y una fecha final, hasta 30 días y sin superar los límites guardados: renueva evidencia, no duración ni capital. No se reanuda al cerrar ORBIS. Bloquear o Detener y bloquear revoca todos los permisos. Las copias gestionadas son cifradas y verificadas; no hay recuperación automática de contraseña.</p>' +
      '<p>El cifrado protege datos en reposo, no frente a un sistema comprometido o una sesión ya desbloqueada. <a href="#/real">Abrir cartera real</a>.</p></details>' +
      '<details><summary>Copias LAB</summary><p>Las copias nuevas se cifran con esta contraseña. El estado de LAB activo aparece arriba; las copias antiguas se señalan aparte.</p>' +
      '<form id="cfg-lab-backup-form" class="row"><label class="field" data-help="Contraseña independiente de la bóveda REAL. Sin ella no podrás restaurar esta copia; no se guarda en Orbis.">Contraseña de copia<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>' +
      '<button type="submit" id="cfg-lab-backup" class="ghost" data-help="Crea una instantánea consistente, comprueba datasets y backtests, cifra con SQLCipher y verifica la copia reabriéndola.">Crear copia cifrada</button></form>' +
      '<div id="cfg-lab-backups" role="status">' + stateBox("loading", "Buscando copias…") + '</div>' +
      '<p class="muted">La restauración no está disponible en la app: requiere cerrar Orbis para evitar que PAPER use una base sustituida.</p></details>' +
      '<details><summary>Conexiones privadas a exchanges</summary><p>MEXC View consulta saldos y fills; GRVT consulta saldos y posiciones. Operar en MEXC requiere otra verificación Trade y autorización explícita; retirar requiere su permiso separado. Claves y cookies sólo viven en memoria. Operativo y Continuo conservan sus credenciales con la vista protegida hasta su fecha final; Bloquear, caducar ese permiso o cerrar el proceso las revoca. Las renovaciones de saldo no abren permisos de retirada ni cambian sus límites.</p>' +
      '<p>Los fills MEXC consultados son observaciones hasta verificarse y asentarse explícitamente. Para sólo observar, usa claves View. GRVT no acredita permisos efectivos en el login. El exchange recibe tu IP y la clave al autenticar.</p></details>' +
      '<details><summary>Wallets EVM</summary><p>La conexión es voluntaria y de solo lectura: las direcciones completas existen temporalmente en esta pestaña y se envían a la extensión/RPC para consultar saldos. Al consultar un ERC-20, el RPC recibe también el contrato y la cuenta codificada en la llamada. Orbis no guarda esos datos en SQLite, localStorage ni sus registros. El RPC puede conocer tu IP.</p>' +
      '<p>Escribe el contrato exacto de la red: el símbolo no identifica por sí solo un token, y el saldo observado no se incorpora a la cartera. Si el contrato no expone decimales, Orbis muestra unidades mínimas.</p>' +
      '<p>Conectar o consultar no autoriza gasto. Verificar control EOA y enviar USDT/BEP-20 a MEXC son pasos separados con firma o confirmación expresa en la wallet. Desconectar limpia el estado local, pero no revoca permisos concedidos en MetaMask o Trust Wallet: revócalos también en la extensión si lo deseas.</p>' +
      '<p>Al comprobar un recibo DEX, el RPC BNB configurado en ORBIS puede ver el hash de la transacción y la dirección pública consultada. La bóveda conserva los importes y referencias protegidas.</p></details>' +
      '<details><summary>Proveedores externos</summary><p>Las cotizaciones e históricos públicos requieren conexión con proveedores de mercado, que pueden ver la IP del equipo que consulta. La consulta voluntaria de wallets se explica en el apartado anterior.</p></details>' +
      '</section></section>';
    var privacyFormats = {legacy:null, lab:null, automation:null, spot:null, perp:null};
    var privacyNames = {legacy:"PAPER manual", lab:"LAB", automation:"DCA", spot:"Spot PAPER", perp:"GRVT PAPER"};
    function paintPrivacySummary() {
      var box = document.getElementById("cfg-privacy-summary");
      if (!box) return;
      var names = Object.keys(privacyFormats);
      var plain = names.filter(function (name) { return privacyFormats[name] === "plain"; });
      var unknown = names.filter(function (name) {
        return privacyFormats[name] === "unknown" || privacyFormats[name] === "empty";
      });
      var pending = names.filter(function (name) { return privacyFormats[name] === null; });
      if (plain.length) {
        box.innerHTML = stateBox("blocked", plain.length + " almacén(es) sin cifrar: " +
          plain.map(function (name) { return privacyNames[name]; }).join(" · ") +
          "." + (unknown.length ? " Estado no verificable: " +
          unknown.map(function (name) { return privacyNames[name]; }).join(" · ") + "." : "") +
          (pending.length ? " Aún se comprueban otros almacenes." : ""));
      } else if (unknown.length) {
        box.innerHTML = stateBox("error", "Estado de cifrado no verificable: " +
          unknown.map(function (name) { return privacyNames[name]; }).join(" · ") + ".");
      } else if (pending.length) {
        box.innerHTML = stateBox("loading", "Comprobando almacenes locales…");
      } else {
        box.innerHTML = stateBox("empty", "Sin SQLite en claro en los almacenes activos. Desbloquea los cifrados para verificarlos; revisa las copias aparte.");
      }
    }
    function recordPrivacyFormat(name, format) {
      privacyFormats[name] = ["plain", "missing", "empty", "encrypted_or_invalid"].indexOf(format) >= 0
        ? format : "unknown";
      paintPrivacySummary();
    }
    paintPrivacySummary();
    if (window.OrbisStorageProtection) {
      var storagePanel = document.getElementById("cfg-storage-protection");
      var protectionWidget = window.OrbisStorageProtection.mount(document.getElementById("cfg-storage-protection-body"), {
        onComplete: function () { var open=storagePanel.open; renderConfig(); var replacement=document.getElementById("cfg-storage-protection"); if(replacement) replacement.open=open; }
      });
      storagePanel.addEventListener("toggle",function () { if(storagePanel.open) protectionWidget.load(); });
      document.getElementById("cfg-privacidad").addEventListener("click",function(event){
        if(event.target.closest("[data-open-storage-protection]")) {
          storagePanel.open=true;
          storagePanel.scrollIntoView({block:"start",behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth"});
        }
      });
    }
    document.getElementById("cfg-theme").addEventListener("change", function (ev) {
      var t = cfgThemeSet(ev.target.value);
      showToast("ok", "Tema: " + t + ".");
    });
    document.getElementById("cfg-alertsound").addEventListener("change", function (ev) {
      var v = cfgAlertSoundSet(ev.target.value === "true");
      showToast("ok", "Sonido de alertas " + (v ? "activado" : "desactivado") + ".");
      window.OrbisMarketAlerts.setSound(v);
    });
    document.getElementById("cfg-testalert").addEventListener("click", function () {
      window.OrbisMarketAlerts.playBell().then(function () {
        showToast("ok", "Sonido de prueba reproducido.");
      }).catch(function () { showToast("blocked", "Audio no disponible."); });
    });
    if (window.OrbisManualPaper) {
      var manualPanel=document.getElementById("cfg-legacy-panel");
      var manualWidget=window.OrbisManualPaper.mount(document.getElementById("cfg-legacy-body"), {
        onStatus:function (state) { recordPrivacyFormat("legacy",state.format); },
        onComplete:function () { var open=manualPanel.open;renderConfig();var replacement=document.getElementById("cfg-legacy-panel");if(replacement)replacement.open=open; }
      });
      manualPanel.addEventListener("toggle",function () { if(manualPanel.open)manualWidget.load(); });
    }
    function paintLabStatus() {
      var box = document.getElementById("cfg-lab-status");
      var create = document.getElementById("cfg-lab-create-form");
      var form = document.getElementById("cfg-lab-unlock-form");
      var lock = document.getElementById("cfg-lab-lock");
      if (!box || !create || !form || !lock) return;
      api("/lab/status").then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { recordPrivacyFormat("lab", "unknown"); box.innerHTML = stateBox("error", "No se pudo consultar LAB."); create.hidden = true; form.hidden = true; lock.hidden = true; return; }
        var state = r.data || {};
        recordPrivacyFormat("lab", state.format);
        var encrypted = state.format === "encrypted_or_invalid";
        create.hidden = state.format !== "missing";
        form.hidden = !encrypted || !!state.unlocked;
        lock.hidden = !encrypted || !state.unlocked;
        if (state.unlocked) {
          box.innerHTML = stateBox("ok", "LAB cifrado · desbloqueado · " + localTimeRemaining(state.remaining_seconds) + ".");
        } else if (encrypted) {
          box.innerHTML = stateBox("blocked", "LAB cifrado · bloqueado.");
        } else if (state.format === "plain") {
          box.innerHTML = plainStoreState("LAB activo");
        } else {
          box.innerHTML = stateBox("empty", state.format === "empty" ? "LAB tiene un archivo vacío; revísalo antes de inicializar." : "LAB aún no está inicializado. Crea una base cifrada para guardar históricos y estrategias.");
        }
      }).catch(function () {
        recordPrivacyFormat("lab", "unknown");
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio LAB no disponible.");
        create.hidden = true;
        form.hidden = true;
        lock.hidden = true;
      });
    }
    document.getElementById("cfg-lab-create-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = this;
      var button = form.querySelector("button");
      var box = document.getElementById("cfg-lab-status");
      var payload = JSON.stringify({ passphrase: form.elements.passphrase.value,
        session_seconds: Number(form.elements.session_seconds.value) });
      form.reset();
      button.disabled = true;
      box.innerHTML = stateBox("loading", "Creando LAB cifrado…");
      api("/lab/create", { method: "POST", body: payload }).then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { box.innerHTML = stateBox("error", esc((r.data && r.data.detail) || "No se pudo crear LAB.")); return; }
        showToast("ok", "LAB cifrado creado.");
        paintLabStatus();
      }).catch(function () {
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio LAB no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    document.getElementById("cfg-lab-unlock-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = this;
      var button = form.querySelector("button");
      var box = document.getElementById("cfg-lab-status");
      var payload = JSON.stringify({ passphrase: form.elements.passphrase.value,
        session_seconds: Number(form.elements.session_seconds.value) });
      form.reset();
      button.disabled = true;
      box.innerHTML = stateBox("loading", "Desbloqueando LAB…");
      api("/lab/unlock", { method: "POST", body: payload }).then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { box.innerHTML = stateBox("error", esc((r.data && r.data.detail) || "No se pudo desbloquear LAB.")); return; }
        showToast("ok", "LAB desbloqueado.");
        paintLabStatus();
      }).catch(function () {
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio LAB no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    document.getElementById("cfg-lab-lock").addEventListener("click", function () {
      var button = this;
      button.disabled = true;
      api("/lab/lock", { method: "POST" }).then(function (r) {
        if (!r.ok) { showToast("error", "No se pudo bloquear LAB."); return; }
        showToast("ok", "LAB bloqueado.");
        paintLabStatus();
      }).catch(function () { showToast("error", "Servicio LAB no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    paintLabStatus();
    function paintAutomationStatus() {
      var box = document.getElementById("cfg-automation-status");
      var create = document.getElementById("cfg-automation-create-form");
      var form = document.getElementById("cfg-automation-unlock-form");
      var lock = document.getElementById("cfg-automation-lock");
      if (!box || !create || !form || !lock) return;
      api("/automation/dca/status").then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { recordPrivacyFormat("automation", "unknown"); box.innerHTML = stateBox("error", "No se pudo consultar DCA."); create.hidden = true; form.hidden = true; lock.hidden = true; return; }
        var state = r.data || {};
        recordPrivacyFormat("automation", state.format);
        var encrypted = state.format === "encrypted_or_invalid";
        create.hidden = state.format !== "missing";
        form.hidden = !encrypted || !!state.unlocked;
        lock.hidden = !encrypted || !state.unlocked;
        if (state.unlocked) {
          box.innerHTML = stateBox("ok", "DCA cifrado · desbloqueado · " + localTimeRemaining(state.remaining_seconds) + ".");
        } else if (encrypted) {
          box.innerHTML = stateBox("blocked", "DCA cifrado · bloqueado.");
        } else if (state.format === "plain") {
          box.innerHTML = plainStoreState("DCA activo");
        } else {
          box.innerHTML = stateBox("empty", state.format === "empty" ? "DCA tiene un archivo vacío; revísalo antes de inicializar." : "DCA aún no está inicializado. Crea una base cifrada para guardar planes.");
        }
      }).catch(function () {
        recordPrivacyFormat("automation", "unknown");
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio DCA no disponible.");
        create.hidden = true;
        form.hidden = true;
        lock.hidden = true;
      });
    }
    document.getElementById("cfg-automation-create-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = this;
      var button = form.querySelector("button");
      var box = document.getElementById("cfg-automation-status");
      var payload = JSON.stringify({ passphrase: form.elements.passphrase.value,
        session_seconds: Number(form.elements.session_seconds.value) });
      form.reset();
      button.disabled = true;
      box.innerHTML = stateBox("loading", "Creando DCA cifrado…");
      api("/automation/dca/create", { method: "POST", body: payload }).then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { box.innerHTML = stateBox("error", esc((r.data && r.data.detail) || "No se pudo crear DCA.")); return; }
        showToast("ok", "DCA cifrado creado.");
        paintAutomationStatus();
      }).catch(function () {
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio DCA no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    document.getElementById("cfg-automation-unlock-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = this;
      var button = form.querySelector("button");
      var box = document.getElementById("cfg-automation-status");
      var payload = JSON.stringify({ passphrase: form.elements.passphrase.value,
        session_seconds: Number(form.elements.session_seconds.value) });
      form.reset();
      button.disabled = true;
      box.innerHTML = stateBox("loading", "Desbloqueando DCA…");
      api("/automation/dca/unlock", { method: "POST", body: payload }).then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { box.innerHTML = stateBox("error", esc((r.data && r.data.detail) || "No se pudo desbloquear DCA.")); return; }
        showToast("ok", "DCA desbloqueado.");
        paintAutomationStatus();
      }).catch(function () {
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio DCA no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    document.getElementById("cfg-automation-lock").addEventListener("click", function () {
      var button = this;
      button.disabled = true;
      api("/automation/dca/lock", { method: "POST" }).then(function (r) {
        if (!r.ok) { showToast("error", "No se pudo bloquear DCA."); return; }
        showToast("ok", "DCA bloqueado.");
        paintAutomationStatus();
      }).catch(function () { showToast("error", "Servicio DCA no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    paintAutomationStatus();
    function paintSpotStatus() {
      var box = document.getElementById("cfg-spot-status");
      var create = document.getElementById("cfg-spot-create-form");
      var form = document.getElementById("cfg-spot-unlock-form");
      var lock = document.getElementById("cfg-spot-lock");
      if (!box || !create || !form || !lock) return;
      api("/paper/spot/strategies/status").then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { recordPrivacyFormat("spot", "unknown"); box.innerHTML = stateBox("error", "No se pudo consultar Spot PAPER."); create.hidden = true; form.hidden = true; lock.hidden = true; return; }
        var state = r.data || {};
        recordPrivacyFormat("spot", state.format);
        var encrypted = state.format === "encrypted_or_invalid";
        create.hidden = state.format !== "missing";
        form.hidden = !encrypted || !!state.unlocked;
        lock.hidden = !encrypted || !state.unlocked;
        if (state.unlocked) box.innerHTML = stateBox("ok", "Spot PAPER cifrado · " + localTimeRemaining(state.remaining_seconds) + ".");
        else if (encrypted) box.innerHTML = stateBox("blocked", "Spot PAPER cifrado · bloqueado.");
        else if (state.format === "plain") box.innerHTML = plainStoreState("Spot PAPER");
        else box.innerHTML = stateBox("empty", state.format === "empty" ? "Spot PAPER tiene un archivo vacío; revísalo antes de inicializar." : "Spot PAPER aún no está inicializado. Crea una base cifrada para activar sesiones.");
      }).catch(function () {
        recordPrivacyFormat("spot", "unknown");
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio Spot PAPER no disponible.");
        create.hidden = true;
        form.hidden = true;
        lock.hidden = true;
      });
    }
    document.getElementById("cfg-spot-create-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = this;
      var button = form.querySelector("button");
      var box = document.getElementById("cfg-spot-status");
      var payload = JSON.stringify({ passphrase: form.elements.passphrase.value,
        session_seconds: Number(form.elements.session_seconds.value) });
      form.reset();
      button.disabled = true;
      box.innerHTML = stateBox("loading", "Creando Spot PAPER cifrado…");
      api("/paper/spot/strategies/create", { method: "POST", body: payload }).then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { box.innerHTML = stateBox("error", esc((r.data && r.data.detail) || "No se pudo crear Spot PAPER.")); return; }
        showToast("ok", "Spot PAPER cifrado creado.");
        paintSpotStatus();
      }).catch(function () {
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio Spot PAPER no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    document.getElementById("cfg-spot-unlock-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = this;
      var button = form.querySelector("button");
      var box = document.getElementById("cfg-spot-status");
      var payload = JSON.stringify({ passphrase: form.elements.passphrase.value,
        session_seconds: Number(form.elements.session_seconds.value) });
      form.reset();
      button.disabled = true;
      box.innerHTML = stateBox("loading", "Desbloqueando Spot PAPER…");
      api("/paper/spot/strategies/unlock", { method: "POST", body: payload }).then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { box.innerHTML = stateBox("error", esc((r.data && r.data.detail) || "No se pudo desbloquear Spot PAPER.")); return; }
        showToast("ok", "Spot PAPER desbloqueado.");
        paintSpotStatus();
      }).catch(function () {
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio Spot PAPER no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    document.getElementById("cfg-spot-lock").addEventListener("click", function () {
      var button = this;
      button.disabled = true;
      api("/paper/spot/strategies/lock", { method: "POST" }).then(function (r) {
        if (!r.ok) { showToast("error", "No se pudo bloquear Spot PAPER."); return; }
        showToast("ok", "Spot PAPER bloqueado.");
        paintSpotStatus();
      }).catch(function () { showToast("error", "Servicio Spot PAPER no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    paintSpotStatus();
    function localSessionField() {
      return '<label class="field" data-help="Caducidad fija para este almacén; consultar no renueva el plazo. Al bloquear o caducar se interrumpen las lecturas y puede quedar exposición ficticia pendiente. Cerrar ORBIS elimina la clave. DCA necesita también LAB desbloqueado al comprar; no se conceden permisos REAL.">Plazo<select name="session_seconds">' +
        '<option value="900">15 minutos</option><option value="3600">1 hora</option>' +
        '<option value="14400">4 horas</option><option value="86400">24 horas</option>' +
        '<option value="604800">7 días</option><option value="2592000">30 días</option></select></label>';
    }
    function localTimeRemaining(seconds) {
      var minutes = Math.max(0, Math.ceil(Number(seconds || 0) / 60));
      if (minutes < 60) return minutes + " min restantes";
      if (minutes < 1440) return Math.floor(minutes / 60) + " h " + (minutes % 60) + " min restantes";
      return Math.floor(minutes / 1440) + " d " + Math.floor((minutes % 1440) / 60) + " h restantes";
    }
    function paintPerpStatus() {
      var box = document.getElementById("cfg-perp-status");
      var create = document.getElementById("cfg-perp-create-form");
      var form = document.getElementById("cfg-perp-unlock-form");
      var lock = document.getElementById("cfg-perp-lock");
      if (!box || !create || !form || !lock) return;
      api("/paper/perp/status").then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { recordPrivacyFormat("perp", "unknown"); box.innerHTML = stateBox("error", "No se pudo consultar GRVT PAPER."); create.hidden = true; form.hidden = true; lock.hidden = true; return; }
        var state = r.data || {};
        recordPrivacyFormat("perp", state.format);
        var encrypted = state.format === "encrypted_or_invalid";
        create.hidden = state.format !== "missing";
        form.hidden = !encrypted || !!state.unlocked;
        lock.hidden = !encrypted || !state.unlocked;
        if (state.unlocked) box.innerHTML = stateBox("ok", "GRVT PAPER cifrado · " + localTimeRemaining(state.remaining_seconds) + ".");
        else if (encrypted) box.innerHTML = stateBox("blocked", "GRVT PAPER cifrado · bloqueado.");
        else if (state.format === "plain") box.innerHTML = plainStoreState("GRVT PAPER");
        else box.innerHTML = stateBox("empty", state.format === "empty" ? "GRVT PAPER tiene un archivo vacío; revísalo antes de inicializar." : "GRVT PAPER aún no está inicializado. Crea una base cifrada para guardar cuentas ficticias.");
      }).catch(function () {
        recordPrivacyFormat("perp", "unknown");
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio GRVT PAPER no disponible.");
        create.hidden = true;
        form.hidden = true;
        lock.hidden = true;
      });
    }
    document.getElementById("cfg-perp-create-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = this;
      var button = form.querySelector("button");
      var box = document.getElementById("cfg-perp-status");
      var payload = JSON.stringify({ passphrase: form.elements.passphrase.value,
        session_seconds: Number(form.elements.session_seconds.value) });
      form.reset();
      button.disabled = true;
      box.innerHTML = stateBox("loading", "Creando GRVT PAPER cifrado…");
      api("/paper/perp/create", { method: "POST", body: payload }).then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { box.innerHTML = stateBox("error", esc((r.data && r.data.detail) || "No se pudo crear GRVT PAPER.")); return; }
        showToast("ok", "GRVT PAPER cifrado creado.");
        paintPerpStatus();
      }).catch(function () {
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio GRVT PAPER no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    document.getElementById("cfg-perp-unlock-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = this;
      var button = form.querySelector("button");
      var box = document.getElementById("cfg-perp-status");
      var payload = JSON.stringify({ passphrase: form.elements.passphrase.value,
        session_seconds: Number(form.elements.session_seconds.value) });
      form.reset();
      button.disabled = true;
      box.innerHTML = stateBox("loading", "Desbloqueando GRVT PAPER…");
      api("/paper/perp/unlock", { method: "POST", body: payload }).then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { box.innerHTML = stateBox("error", esc((r.data && r.data.detail) || "No se pudo desbloquear GRVT PAPER.")); return; }
        showToast("ok", "GRVT PAPER desbloqueado.");
        paintPerpStatus();
      }).catch(function () {
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio GRVT PAPER no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    document.getElementById("cfg-perp-lock").addEventListener("click", function () {
      var button = this;
      button.disabled = true;
      api("/paper/perp/lock", { method: "POST" }).then(function (r) {
        if (!r.ok) { showToast("error", "No se pudo bloquear GRVT PAPER."); return; }
        showToast("ok", "GRVT PAPER bloqueado.");
        paintPerpStatus();
      }).catch(function () { showToast("error", "Servicio GRVT PAPER no disponible.");
      }).finally(function () { button.disabled = false; });
    });
    paintPerpStatus();
    function paintLabBackups() {
      var box = document.getElementById("cfg-lab-backups");
      if (!box) return;
      api("/lab/backups").then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) { box.innerHTML = stateBox("error", "No se pudieron consultar las copias LAB."); return; }
        var rows = Array.isArray(r.data) ? r.data : [];
        var legacy = rows.filter(function (item) { return !item.encrypted; }).length;
        box.innerHTML = rows.length
          ? '<p class="muted">Última copia: <strong>' + esc(rows[0].name) + '</strong> · ' + esc(String(rows[0].size_bytes)) + ' bytes · ' +
            (rows[0].encrypted ? 'cifrada' : 'SIN CIFRAR') + '. Total: ' + rows.length +
            (legacy ? ' · ' + legacy + ' sin cifrar' : '') + '.</p>'
          : '<p class="muted">Aún no hay copias LAB.</p>';
      }).catch(function () {
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio LAB no disponible.");
      });
    }
    document.getElementById("cfg-lab-backup-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = this;
      var button = form.querySelector("button");
      var box = document.getElementById("cfg-lab-backups");
      var payload = JSON.stringify({ passphrase: form.elements.passphrase.value });
      form.reset();
      button.disabled = true;
      box.innerHTML = stateBox("loading", "Cifrando y verificando la copia…");
      api("/lab/backup/encrypted", { method: "POST", body: payload }).then(function (r) {
        if (!box.isConnected) return;
        if (!r.ok) {
          box.innerHTML = stateBox("error", esc((r.data && r.data.detail) || "No se creó la copia LAB."));
          return;
        }
        box.innerHTML = stateBox("ok", "Copia cifrada verificada: <strong>" + esc(r.data.name) + "</strong>.");
        showToast("ok", "Copia LAB cifrada verificada.");
      }).catch(function () {
        if (box.isConnected) box.innerHTML = stateBox("error", "Servicio LAB no disponible; no se confirmó la copia.");
      }).finally(function () { button.disabled = false; });
    });
    paintLabBackups();
    (function paintPersist() {
      var box = document.getElementById("cfg-persist");
      if (!box) return;
      var keys = [CFG_THEME_KEY, CFG_ALERT_SOUND_KEY];
       var guardadas = 0;
       keys.forEach(function (clave) {
         try { if (localStorage.getItem(clave) !== null) guardadas += 1; } catch (e) {}
       });
       api("/ux/preferences").then(function (r) {
         if (!document.getElementById("cfg-persist")) return;
         box.innerHTML = '<dl class="kv"><dt>En este navegador</dt><dd><strong>' +
           (guardadas ? "Preferencias guardadas" : "Valores iniciales") + "</strong></dd>" +
           "<dt>Servicio local</dt><dd><strong>" + (r.ok ? "Disponible" : "No disponible") + "</strong></dd></dl>";
       }).catch(function () {
         if (!document.getElementById("cfg-persist")) return;
         box.innerHTML = '<p class="muted">Preferencias del navegador disponibles; servicio local sin conexión.</p>';
       });
    })();
  }

  /* Puente Estudio (implementado en /ui/estudio.js, mismo origen). */
  try {
    window.OrbisUI = {
      api: api, fmtMoney: fmtMoney, fmtQty: fmtQty, fmtNum: fmtNum,
      fmtDecimal: fmtExactDecimal,
      fmtPct: fmtPct, fmtPctShown: fmtPctShown, fmtShare: fmtShare,
      pricePrecision: pricePrecision, fmtPriceFor: fmtPriceFor,
      fmtAge: fmtAge, fmtDateTime: fmtDateTime, esc: esc,
      stateBox: stateBox, detailBlock: detailBlock, showToast: showToast,
      networkBox: networkBox, httpErrorBox: httpErrorBox, change24h: change24h,
      orbisMode: orbisMode, orbisProvider: orbisProvider,
      setOrbisProvider: setOrbisProvider, modeTag: modeTag,
      newRequestId: newRequestId, sourceLabel: sourceLabel,
      postPaperTrade: postPaperTrade, guardPhrase: guardPhrase,
      postMarketPaperTrade: postMarketPaperTrade,
      tradeResultBox: tradeResultBox,
      pairOptions: pairOptions, armRetry: armRetry, armAnyRetry: armAnyRetry,
      nextRetryId: nextRetryId
    };
  } catch (e) {}
  function renderEstudio(params) {
    stopHomeTimer();
    paintMode();
    var links = document.querySelectorAll("nav a");
    for (var i = 0; i < links.length; i++) links[i].removeAttribute("aria-current");
    var cur = document.querySelector('nav a[href="#/estudio"]');
    if (cur) cur.setAttribute("aria-current", "page");
    if (window.OrbisEstudio && window.OrbisEstudio.render) {
      window.OrbisEstudio.render(view, window.OrbisUI, params);
      return;
    }
    view.innerHTML = '<section class="card"><h2>Estudio</h2>' + modeTag() +
      stateBox("empty", "Estudio no disponible: falta /ui/estudio.js. Sin datos inventados.") + "</section>";
  }

  /* ------------------------- MODO GLOBAL + EXCHANGE ------------------------- */
  /* Operativa siempre PAPER; la cartera REAL es un registro manual separado.
   * Una preferencia antigua orbis.mode=LIVE no cambia esta garantía.
   * Exchange default persistente
   * (localStorage orbis.provider.default + env ORBIS_MARKET_PROVIDER vía
   * /market/providers) + AUTO sin mezclar silencioso (resolved visible). */
  var ORBIS_PROVIDERS = ["kraken", "binance", "coinbase", "okx", "bybit", "mexc", "kucoin", "gate", "bitget", "grvt", "bitstamp"];
  function orbisMode() {
    return "PAPER";
  }
  function orbisProvider() {
    try {
      var p = (localStorage.getItem("orbis.provider.default") || "").toLowerCase();
      if (p === "auto" || ORBIS_PROVIDERS.indexOf(p) >= 0) return p;
    } catch (e) {}
    return "auto";
  }
  function setOrbisProvider(p) {
    if (p !== "auto" && ORBIS_PROVIDERS.indexOf(p) < 0) return;
    var previous = orbisProvider();
    try { localStorage.setItem("orbis.provider.default", p); } catch (e) {}
    paintMode();
    if (previous !== p) {
      window.dispatchEvent(new CustomEvent("orbis:provider-change", { detail: { provider: p } }));
    }
  }
  function paintMode() {
    var line = document.getElementById("modeline");
    var prov = orbisProvider();
    var viewName = document.body.dataset.view || "inicio";
    var context = viewName === "laboratorio" ? "Laboratorio histórico" :
      viewName === "estudio" ? "Mercado público" :
      ["dca", "estrategias", "ledger"].indexOf(viewName) >= 0 ? "Simulación PAPER" :
      viewName === "real" ? "Patrimonio registrado" :
      ["portfolio", "operativa", "spot-paper", "perpetuos"].indexOf(viewName) >= 0 ? "Saldo PAPER ficticio" : "";
    var sel = document.getElementById("provider-sel");
    if (sel && sel.value !== prov) sel.value = prov;
    /* El contexto permanece accesible, pero no duplica la navegación visual. */
    if (line) line.textContent = (context ? context + " · " : "") +
      "Fuente de gráficos " + prov.toUpperCase();
  }
  function armGlobalMode() {
    paintMode();
    var sel = document.getElementById("provider-sel");
    if (sel && !sel._armed) {
      sel._armed = true;
      try {
        var saved = localStorage.getItem("orbis.provider.default");
        if (!saved) {
          api("/market/providers").then(function (res) {
            if (res.ok && res.data && res.data.default) {
              var cur = orbisProvider();
              if (cur === "auto") { sel.value = "auto"; }
              paintMode();
            }
          }).catch(function () {});
        }
      } catch (e) {}
      sel.addEventListener("change", function () {
        setOrbisProvider(sel.value);
        paintMode();
        var refresh = document.getElementById("home-market-refresh") ||
          document.getElementById("t-reload") || document.getElementById("p-load");
        if (refresh) refresh.click();
        showToast("ok", "Fuente de gráficos: " + sel.value.toUpperCase() + ".");
      });
    }
  }
  function modeTag() {
    /* El contexto se indica sin controles de modo en la cabecera. Mantener
     * esta función preserva el contrato de las vistas sin repetir información. */
    return "";
  }

  /* -------------------------------- ROUTER ------------------------------- */
  var routes = {
    "#/inicio": renderInicio,
    "#/portfolio": renderPortfolio,
    "#/real": function () { window.OrbisReal.render(view); },
    "#/laboratorio": function () { window.OrbisLab.render(view); },
    "#/spot-paper": function (params) { window.OrbisSpotPaper.render(view, params); },
    "#/dca": function () { window.OrbisDca.render(view); },
    "#/perpetuos": function (params) { window.OrbisPerp.render(view, window.OrbisUI, params); },
    "#/operativa": renderOperativa,
    "#/estudio": renderEstudio,
    "#/estrategias": renderEstrategias,
    "#/ledger": renderLedger,
    "#/alertas": function () { window.OrbisMarketAlerts.render(view); },
    "#/config": renderConfig
  };
  function stopAllTimers() {
    stopHomeTimer();
    if (mktTimer) { clearInterval(mktTimer); mktTimer = null; }
    if (operativaTimer) { clearInterval(operativaTimer); operativaTimer = null; }
    if (portfolioTimer) { clearInterval(portfolioTimer); portfolioTimer = null; }
    if (estudioTimer) { clearInterval(estudioTimer); estudioTimer = null; }
  }
  function route() {
    window.OrbisMarketAlerts.cleanupView();
    stopAllTimers();
    releaseMiniCharts();
    if (window.OrbisLab && typeof window.OrbisLab.cleanup === "function") {
      try { window.OrbisLab.cleanup(); } catch (e) {}
    }
    if (window.OrbisDca && typeof window.OrbisDca.cleanup === "function") {
      try { window.OrbisDca.cleanup(); } catch (e) {}
    }
    if (window.OrbisPerp && typeof window.OrbisPerp.cleanup === "function") {
      try { window.OrbisPerp.cleanup(); } catch (e) {}
    }
    if (window.OrbisSpotPaper && typeof window.OrbisSpotPaper.cleanup === "function") {
      try { window.OrbisSpotPaper.cleanup(); } catch (e) {}
    }
    /* Cleanup Estudio if leaving that view */
    if (window.OrbisEstudio && typeof window.OrbisEstudio.cleanup === "function") {
      try { window.OrbisEstudio.cleanup(); } catch (e) {}
    }
    var requested = location.hash || "#/inicio";
    var queryAt = requested.indexOf("?");
    var h = queryAt < 0 ? requested : requested.slice(0, queryAt);
    var params = new URLSearchParams(queryAt < 0 ? "" : requested.slice(queryAt + 1));
    if (!routes[h]) {
      h = "#/inicio";
      history.replaceState(null, "", location.pathname + location.search + h);
    }
    document.body.dataset.view = h.slice(2);
    paintMode();
    var simulationRoutes = ["#/operativa", "#/portfolio", "#/spot-paper", "#/perpetuos", "#/dca", "#/estrategias", "#/ledger"];
    var inSimulation = simulationRoutes.indexOf(h) >= 0;
    var simNav = document.getElementById("sim-nav");
    if (simNav) simNav.hidden = !inSimulation;
    var links = document.querySelectorAll(".navegacion-principal a, .navegacion-simulacion a");
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute("href") === h ||
          (inSimulation && links[i].closest(".navegacion-principal") && links[i].getAttribute("href") === "#/operativa")) links[i].setAttribute("aria-current", "page");
      else links[i].removeAttribute("aria-current");
    }
    /* Suelta el foco del enlace tras navegar con ratón para que no quede
     * el outline de :focus-visible clavado; el teclado sigue mostrando
     * :focus-visible al tabular porque el estilo permanece en CSS. */
    if (document.activeElement && document.activeElement.blur) {
      try { document.activeElement.blur(); } catch (e) { /* sin foco que quitar */ }
    }
    routes[h](params);
  }
  window.addEventListener("hashchange", route);
  armGlobalMode();
  checkRuntimeStatus();
  setInterval(checkRuntimeStatus, 60000);
  window.OrbisMarketAlerts.start(window.OrbisUI);
  route();
})();
